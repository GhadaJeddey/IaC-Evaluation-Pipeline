"""
validators/opa_runner.py

Runs conftest (OPA) against the Terraform plan JSON.

"""

import json
import shutil
import subprocess
import tempfile
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================================
# Result dataclass
# ============================================================================

@dataclass
class ValidatorResult:
    name:    str
    passed:  bool
    score:   float
    details: str
    skipped: bool = False
    raw:     dict = field(default_factory=dict, repr=False)


# ============================================================================
# conftest output parser
# ============================================================================

def _parse_conftest_json(raw: str) -> tuple[int, int, list[str]]:
    """
    Parses conftest --output json output.

    Conftest JSON structure:
    [
      {
        "filename": "plan.json",
        "successes": 3,
        "failures": [
          {"msg": "Production instances must be encrypted", "metadata": {...}},
          ...
        ],
        "warnings": [...]
      }
    ]

    Returns (passed_count, failed_count, failed_messages[:5]).
    Falls back to (0, 1, [raw_text]) if parsing fails.
    """
    if not raw or not raw.strip():
        return 0, 1, ["conftest produced no output"]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0, 1, [f"conftest output not JSON: {raw[:200]}"]

    if not isinstance(data, list):
        return 0, 1, [f"unexpected conftest output shape: {type(data)}"]

    total_passed = 0
    total_failed = 0
    failed_msgs  = []

    for entry in data:
        if not isinstance(entry, dict):
            continue
        total_passed += entry.get("successes", 0)
        failures      = entry.get("failures", []) or []
        total_failed += len(failures)
        for f in failures:
            msg = f.get("msg", str(f))
            if msg and msg not in failed_msgs:
                failed_msgs.append(msg)

    return total_passed, total_failed, failed_msgs[:5]


# ============================================================================
# Main validator
# ============================================================================

def validate(
    terraform_code: str,
    workspace:      Path,
    policies_dir:   Path | None = None,
) -> ValidatorResult:
    """
    Runs conftest against workspace/plan.json using Rego policies in policies_dir.

    IMPORTANT: plan.json must already exist in workspace before calling this.
    It is produced by the terraform_plan validator via:
        terraform plan -out=tfplan
        terraform show -json tfplan > plan.json

    Args:
        terraform_code : the LLM-generated HCL (used only for the empty-check guard)
        workspace      : directory containing plan.json
        policies_dir   : directory containing *.rego policy files
                         defaults to config.OPA_POLICIES_DIR

    Returns ValidatorResult with:
        passed  — True only if all OPA policies pass
        score   — passed_checks / total_checks  (partial credit)
        skipped — True if conftest not installed, no policies, or no plan.json
        details — human-readable summary of which policies passed/failed
        raw     — parsed conftest JSON for storage in output files
    """

    # ------------------------------------------------------------------ #
    # Resolve policies directory
    # ------------------------------------------------------------------ #

    if policies_dir is None:
        try:
            # Fix 6: read from config instead of hardcoding path
            import sys
            sys.path.insert(0, str(Path(__file__).parent.parent))
            from config import POLICIES_DIR
            policies_dir = POLICIES_DIR
        except ImportError:
            # Fallback if config not importable — use conventional location
            policies_dir = Path(__file__).parent / "opa_policies"

    # ------------------------------------------------------------------ #
    # Pre-flight checks
    # ------------------------------------------------------------------ #

    if not terraform_code or not terraform_code.strip():
        return ValidatorResult("opa", False, 0.0, "No Terraform to evaluate")

    if not shutil.which("conftest"):
        logger.info("conftest binary not found — OPA skipped")
        return ValidatorResult(
            "opa", False, 0.0,
            "conftest not found — install with: brew install conftest  OR  "
            "https://www.conftest.dev/install/",
            skipped=True,   # Fix 1: not credited
        )

    # Fix 6: use the correct directory name from config
    if not policies_dir.exists() or not list(policies_dir.glob("*.rego")):
        logger.info(f"No .rego policies found in {policies_dir} — OPA skipped")
        return ValidatorResult(
            "opa", False, 0.0,
            f"No OPA policies found in {policies_dir} — skipped",
            skipped=True,   # Fix 1: not credited
        )

    # Fix 3: plan.json must be produced by terraform_plan before calling this
    plan_json = workspace / "plan.json"
    if not plan_json.exists():
        logger.info(f"plan.json not found in {workspace} — OPA skipped")
        return ValidatorResult(
            "opa", False, 0.0,
            "plan.json not found — terraform plan must run first (requires LocalStack)",
            skipped=True,   # Fix 1: not credited
        )

    # ------------------------------------------------------------------ #
    # Run conftest
    # Fix 4: --output json for structured pass/fail parsing
    # ------------------------------------------------------------------ #

    cmd = [
        "conftest", "test",
        str(plan_json),
        "--policy",   str(policies_dir),
        "--output",   "json",       # Fix 4: structured output
        "--no-color",
        "--all-namespaces",
    ]

    logger.debug(f"conftest cmd: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return ValidatorResult("opa", False, 0.0, "conftest timed out after 60s")
    except Exception as exc:
        return ValidatorResult("opa", False, 0.0, f"conftest failed to run: {exc}")

    # conftest exit codes:
    # 0 = all policies passed
    # 1 = one or more policies failed (still produces valid JSON)
    # 2 = execution error (bad policy file, bad input)
    if result.returncode == 2:
        return ValidatorResult(
            "opa", False, 0.0,
            f"conftest execution error (rc=2): {result.stderr[:300]}",
        )

    raw_stdout = (result.stdout or "").strip()

    # Fix 2 + Fix 4: parse structured output for partial credit
    passed_n, failed_n, failed_msgs = _parse_conftest_json(raw_stdout)
    total = passed_n + failed_n

    if total == 0:
        # Conftest ran but no rules evaluated — policy files exist but
        # don't match the input structure. Treat as skip.
        return ValidatorResult(
            "opa", False, 0.0,
            "conftest ran but no rules matched plan.json — check policy selectors",
            skipped=True,
        )

    score  = round(passed_n / total, 4)
    passed = failed_n == 0

    details = f"{passed_n}/{total} OPA policies passed"
    if failed_msgs:
        details += " — failures: " + " | ".join(failed_msgs)

    # Store raw conftest output for output JSON files
    try:
        raw_data = json.loads(raw_stdout) if raw_stdout else {}
    except json.JSONDecodeError:
        raw_data = {"raw": raw_stdout}

    return ValidatorResult(
        name    = "opa",
        passed  = passed,
        score   = score,
        details = details,
        skipped = False,
        raw     = raw_data if isinstance(raw_data, dict) else {"entries": raw_data},
    )


# ============================================================================
# Saved scenario tester
# ============================================================================

def _run_conftest_on_plan(
    plan_json_path: Path,
    policies_dir:   Path,
) -> tuple[bool, str, list[str]]:
    """
    Run conftest against a single plan.json file.
    Returns (all_passed, raw_stdout, failed_messages).
    """
    cmd = [
        "conftest", "test",
        str(plan_json_path),
        "--policy",  str(policies_dir),
        "--output",  "json",
        "--no-color",
        "--all-namespaces",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False, "", ["conftest timed out"]
    except Exception as exc:
        return False, "", [f"conftest failed to run: {exc}"]
    
    if result.returncode == 2:
        return False, "", [f"conftest execution error: {result.stderr[:200]}"]

    raw = (result.stdout or "").strip()
    
    # returncode 0 = all passed, 1 = some failed — both produce valid JSON
    # empty output on rc=0 or rc=1 means namespace mismatch, not a clean pass
    if not raw:
        return False, "", ["conftest produced no output — check --all-namespaces"]
    
    _, failed_n, failed_msgs = _parse_conftest_json(raw)
    return failed_n == 0, raw, failed_msgs


def test_saved_scenarios(
    scenarios_file: Optional[Path] = None,
    policies_dir:   Optional[Path] = None,
    output_file:    Optional[Path] = None,
) -> list[dict]:
    """
    Load OPA test scenarios from opa_test_scenarios.json, run each plan_json
    through conftest, and compare the actual result against expected_result.

    Each scenario supplies a synthetic plan.json so no LocalStack or Terraform
    is required — conftest evaluates the JSON directly against the .rego files.

    Args:
        scenarios_file: Path to opa_test_scenarios.json.
                        Defaults to <this file's directory>/opa_test_scenarios.json.
        policies_dir:   Directory containing *.rego files.
                        Defaults to <this file's directory>/policies/.
        output_file:    Where to write the JSON report.
                        Defaults to <this file's directory>/output_opa_test_scenarios.json.

    Returns:
        List of result dicts. Prints a summary table and writes the output file.
    """
    if scenarios_file is None:
        scenarios_file = Path(__file__).parent / "opa_test_scenarios.json"

    if policies_dir is None:
        policies_dir = Path(__file__).parent / "policies"

    if output_file is None:
        output_file = Path(__file__).parent / "output_opa_test_scenarios.json"

    # ------------------------------------------------------------------ #
    # Pre-flight
    # ------------------------------------------------------------------ #
    if not shutil.which("conftest"):
        print("conftest not installed — cannot run OPA scenario tests")
        print("Install: https://www.conftest.dev/install/")
        return []

    if not policies_dir.exists() or not list(policies_dir.glob("*.rego")):
        print(f"No .rego files found in {policies_dir}")
        return []

    data      = json.loads(scenarios_file.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios", {})

    output_rows: list[dict] = []

    print(f"\n{'='*66}")
    print(f"  OPA policy test — {len(scenarios)} scenarios")
    print(f"{'='*66}")
    print(f"  {'ID':<42} {'EXP':<5} {'GOT':<5} STATUS")
    print(f"  {'-'*62}")

    with tempfile.TemporaryDirectory(prefix="opa_scenario_") as tmp:
        tmp_path = Path(tmp)

        for scenario_id, scenario in scenarios.items():
            expected     = scenario.get("expected_result", "pass")
            plan_json    = scenario.get("plan_json", {})
            description  = scenario.get("description", "")
            policy_name  = scenario.get("policy_under_test", "")

            # Write synthetic plan.json to temp dir
            plan_path = tmp_path / f"{scenario_id}_plan.json"
            plan_path.write_text(
                json.dumps(plan_json, indent=2), encoding="utf-8"
            )

            all_passed, _, failed_msgs = _run_conftest_on_plan(
                plan_path, policies_dir
            )

            actual    = "pass" if all_passed else "fail"
            conformant = actual == expected
            status    = "OK" if conformant else "MISMATCH"

            mismatch_description = None
            if not conformant:
                if expected == "pass" and actual == "fail":
                    mismatch_description = (
                        f"Policy fired unexpectedly. "
                        f"Failures: {' | '.join(failed_msgs)}"
                    )
                else:
                    mismatch_description = (
                        "Expected policy to deny but all rules passed — "
                        "the hallucination was not caught by the policy."
                    )

            output_rows.append({
                "scenario_id":          scenario_id,
                "policy_under_test":    policy_name,
                "description":          description,
                "expected_result":      expected,
                "actual_result":        actual,
                "conformant":           conformant,
                "failed_rules":         failed_msgs if not all_passed else [],
                "mismatch_description": mismatch_description,
            })

            print(f"  {scenario_id:<42} {expected:<5} {actual:<5} {status}")

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    ok       = sum(1 for r in output_rows if r["conformant"])
    mismatch = len(output_rows) - ok

    print(f"  {'-'*62}")
    print(f"  Total: {len(output_rows)}  OK: {ok}  MISMATCH: {mismatch}")

    if mismatch:
        print(f"\n  Mismatches:")
        for r in output_rows:
            if not r["conformant"]:
                print(f"    [{r['scenario_id']}]")
                print(f"      policy : {r['policy_under_test']}")
                print(f"      reason : {r['mismatch_description']}")

    print(f"{'='*66}\n")

    # ------------------------------------------------------------------ #
    # Persist
    # ------------------------------------------------------------------ #
    payload = {
        "summary": {
            "total":    len(output_rows),
            "ok":       ok,
            "mismatch": mismatch,
        },
        "results": output_rows,
    }
    output_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"  Results saved → {output_file}\n")

    return output_rows


if __name__ == "__main__":
    test_saved_scenarios()