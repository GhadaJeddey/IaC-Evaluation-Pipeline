"""
validators/terraform_plan.py

Runs terraform init + terraform plan against LocalStack.

"""

import os
import json
import shutil
import socket
import subprocess
import logging
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ============================================================================
# LocalStack provider block — written alongside main.tf before every plan
# ============================================================================

_LOCALSTACK_PROVIDER = """\
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  access_key                  = "mock"
  secret_key                  = "mock"
  region                      = "us-east-1"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  endpoints {
    ec2            = "http://localhost:4566"
    s3             = "http://localhost:4566"
    iam            = "http://localhost:4566"
    sts            = "http://localhost:4566"
    cloudwatch     = "http://localhost:4566"
    logs           = "http://localhost:4566"
    dynamodb       = "http://localhost:4566"
    ebs            = "http://localhost:4566"
  }
}
"""

# ============================================================================
# Result dataclass
# ============================================================================

@dataclass
class ValidatorResult:
    name:    str
    passed:  bool
    score:   float          # 0.0–1.0, or None when skipped
    details: str
    skipped: bool = False   # Fix 1: scorer.py excludes skipped from denominator


# ============================================================================
# Helpers
# ============================================================================

def _localstack_reachable() -> bool:

    try:
        with socket.create_connection(("localhost", 4566), timeout=1):
            return True
    except Exception:
        return False


def _clean_workspace(workspace: Path) -> None:

    for tf_file in workspace.glob("*.tf"):
        tf_file.unlink()
    for stale in [".terraform", "terraform.tfstate", "terraform.tfstate.backup", ".terraform.lock.hcl"]:
        target = workspace / stale
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def _run_cmd(cmd: list, cwd: Path, env: dict, timeout: int) -> tuple[int, str]:
    """Run a subprocess and return (returncode, combined stdout+stderr)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except Exception as exc:
        return -1, f"Command failed to run: {exc}"


# ============================================================================
# Main validator
# ============================================================================

def validate(terraform_code: str, workspace: Path) -> ValidatorResult:
    """
    Runs terraform init + terraform plan on terraform_code.
    Requires LocalStack to be running on localhost:4566.

    Returns ValidatorResult with:
      passed  — True if plan exits 0
      score   — 1.0 if passed, 0.0 if failed
      skipped — True if LocalStack unreachable or terraform not installed
                 (scorer.py should exclude skipped validators from denominator)
      details — truncated plan output or error message
    """

    # ------------------------------------------------------------------ #
    # Pre-flight checks
    # ------------------------------------------------------------------ #

    if not terraform_code or not terraform_code.strip():
        return ValidatorResult("terraform_plan", False, 0.0, "No Terraform to plan")

    if not shutil.which("terraform"):
        return ValidatorResult(
            "terraform_plan", False, 0.0,
            "terraform binary not found — install from https://developer.hashicorp.com/terraform/install",
            skipped=True,
        )

    # scorer.py will exclude this validator from the denominator
    if not _localstack_reachable():
        logger.info("LocalStack not reachable on localhost:4566 — terraform plan skipped")
        return ValidatorResult(
            "terraform_plan", False, 0.0,
            "LocalStack not running — terraform plan skipped",
            skipped=True,
        )

    # ------------------------------------------------------------------ #
    # Prepare workspace
    # ------------------------------------------------------------------ #

    workspace.mkdir(parents=True, exist_ok=True)
    _clean_workspace(workspace)

    (workspace / "provider.tf").write_text(_LOCALSTACK_PROVIDER, encoding="utf-8")
    (workspace / "main.tf").write_text(terraform_code, encoding="utf-8")

    env = os.environ.copy()
    env.update({
        "AWS_ACCESS_KEY_ID":     "mock",
        "AWS_SECRET_ACCESS_KEY": "mock",
        "AWS_DEFAULT_REGION":    "us-east-1",
        # Suppress Terraform update checks and telemetry
        "CHECKPOINT_DISABLE":    "1",
        "TF_INPUT":              "false",
        # Cache downloaded providers so subsequent inits are instant
        "TF_PLUGIN_CACHE_DIR":   str(Path.home() / ".terraform.d" / "plugin-cache"),
    })

    # ------------------------------------------------------------------ #
    # Fix 2 + 6: terraform init with -backend=false -input=false
    # -backend=false  — use local state only, no remote backend config needed
    # -input=false    — never prompt for input
    # -no-color       — clean output for logging
    # ------------------------------------------------------------------ #

    logger.debug(f"Running terraform init in {workspace}")
    init_rc, init_out = _run_cmd(
        ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
        cwd=workspace,
        env=env,
        timeout=120,
    )

    if init_rc != 0:
        return ValidatorResult(
            "terraform_plan", False, 0.0,
            f"terraform init failed (rc={init_rc}): {init_out[:400]}",
        )

    # ------------------------------------------------------------------ #
    # terraform plan
    # ------------------------------------------------------------------ #

    logger.debug(f"Running terraform plan in {workspace}")
    plan_rc, plan_out = _run_cmd(
        ["terraform", "plan", "-no-color", "-input=false"],
        cwd=workspace,
        env=env,
        timeout=120,
    )

    passed  = plan_rc == 0
    details = plan_out[:600] if plan_out else "(no output)"

    # Truncate and annotate
    if not passed:
        # Surface the most useful part — errors are usually at the end
        lines = plan_out.splitlines()
        error_lines = [l for l in lines if "Error" in l or "error" in l]
        if error_lines:
            details = "\n".join(error_lines[:10])

    return ValidatorResult(
        name    = "terraform_plan",
        passed  = passed,
        score   = 1.0 if passed else 0.0,
        details = details,
        skipped = False,
    )


# ============================================================================
# Batch scenario tester
# ============================================================================

@dataclass
class ScenarioResult:
    scenario_id: str
    tier:        str
    passed:      bool
    score:       float
    skipped:     bool
    details:     str


def test_saved_scenarios(
    scenarios_file: Optional[Path] = None,
) -> list[ScenarioResult]:
    """
    Load scenarios from test_scenarios.json and run terraform plan on each.

    Each scenario declares expected_plan: "pass" | "fail" | "skip" so the
    runner can report whether the validator's actual result matches the
    expectation.

    Requires LocalStack on localhost:4566. Scenarios with expected_plan="skip"
    are noted but not run. When LocalStack is unreachable every scenario is
    marked skipped.

    Args:
        scenarios_file: Path to test_scenarios.json.
                        Defaults to <this file's directory>/test_scenarios.json.

    Returns:
        List of ScenarioResult. Prints a summary table with pass/fail/mismatch.
    """
    if scenarios_file is None:
        scenarios_file = Path(__file__).parent / "test_scenarios.json"

    data = json.loads(scenarios_file.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios", {})

    if not shutil.which("terraform"):
        logger.warning("terraform binary not found — test_saved_scenarios skipped")
        return []

    if not _localstack_reachable():
        logger.info("LocalStack not reachable — all plan scenarios will be skipped")
        print("LocalStack not running on localhost:4566 — plan scenarios skipped\n")
        skipped_results = [
            ScenarioResult(
                scenario_id = sid,
                tier        = s.get("category", ""),
                passed      = False,
                score       = 0.0,
                skipped     = True,
                details     = "LocalStack not running",
            )
            for sid, s in scenarios.items()
            if s.get("expected_plan") != "skip"
        ]
        skipped_rows = [
            {
                "scenario_id":     sid,
                "category":        s.get("category", ""),
                "description":     s.get("description", ""),
                "expected_result": s.get("expected_plan", "pass"),
                "actual_result":   "skip",
                "conformant":      None,
                "error":           "LocalStack not running on localhost:4566",
            }
            for sid, s in scenarios.items()
        ]
        output_file = Path(__file__).parent / "output_plan_test_scenarios.json"
        output_file.write_text(
            json.dumps(
                {
                    "summary": {
                        "total":    len(skipped_rows),
                        "ok":       0,
                        "mismatch": 0,
                        "skipped":  len(skipped_results),
                    },
                    "results": skipped_rows,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        print(f"  Results saved → {output_file}\n")
        return skipped_results

    results:     list[ScenarioResult] = []
    output_rows: list[dict]           = []

    for scenario_id, scenario in scenarios.items():
        tf_code  = scenario.get("terraform_code", "")
        expected = scenario.get("expected_plan", "pass")

        # Scenarios explicitly marked skip — record and move on
        if expected == "skip":
            print(f"  [NOTED   ] {scenario_id:<40} expected=skip (not run)")
            output_rows.append({
                "scenario_id":     scenario_id,
                "category":        scenario.get("category", ""),
                "description":     scenario.get("description", ""),
                "expected_result": "skip",
                "actual_result":   "skip",
                "conformant":      True,
                "error":           None,
            })
            continue

        workspace = Path(tempfile.mkdtemp(prefix=f"tf_plan_{scenario_id}_"))
        try:
            result = validate(tf_code, workspace)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

        if result.skipped:
            results.append(ScenarioResult(
                scenario_id = scenario_id,
                tier        = scenario.get("category", ""),
                passed      = False,
                score       = 0.0,
                skipped     = True,
                details     = result.details,
            ))
            output_rows.append({
                "scenario_id":     scenario_id,
                "category":        scenario.get("category", ""),
                "description":     scenario.get("description", ""),
                "expected_result": expected,
                "actual_result":   "skip",
                "conformant":      None,
                "error":           result.details,
            })
            print(f"  [SKIP    ] {scenario_id}")
            continue

        actual  = "pass" if result.passed else "fail"
        matched = actual == expected

        results.append(ScenarioResult(
            scenario_id = scenario_id,
            tier        = scenario.get("category", ""),
            passed      = matched,
            score       = 1.0 if matched else 0.0,
            skipped     = False,
            details     = result.details,
        ))

        output_rows.append({
            "scenario_id":     scenario_id,
            "category":        scenario.get("category", ""),
            "description":     scenario.get("description", ""),
            "expected_result": expected,
            "actual_result":   actual,
            "conformant":      matched,
            "error":           result.details if not matched else None,
        })

        icon = "OK" if matched else "MISMATCH"
        print(f"  [{icon:<8}] {scenario_id:<40} expected={expected:<4} got={actual}")

    # Summary
    ok       = sum(1 for r in results if r.passed)
    skipped  = sum(1 for r in results if r.skipped)
    mismatch = len(results) - ok - skipped

    print(f"\n{'='*62}")
    print(f"  terraform plan — saved scenario results")
    print(f"{'='*62}")
    print(f"  Total: {len(results)}  OK: {ok}  MISMATCH: {mismatch}  SKIP: {skipped}")
    if mismatch:
        print(f"\n  Mismatches (validator behaved unexpectedly):")
        for r in results:
            if not r.passed and not r.skipped:
                print(f"    {r.scenario_id}  [{r.tier}]  {r.details[:80]}")
    print(f"{'='*62}\n")

    # Persist results to JSON
    output_file = Path(__file__).parent / "output_plan_test_scenarios.json"
    output_payload = {
        "summary": {
            "total":    len(output_rows),
            "ok":       ok,
            "mismatch": mismatch,
            "skipped":  skipped,
        },
        "results": output_rows,
    }
    output_file.write_text(
        json.dumps(output_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  Results saved → {output_file}\n")

    return results


def test_scenarios(
    scenarios_dir: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
    tier_filter: Optional[list[str]] = None,
) -> list[ScenarioResult]:
    """
    Load every scenario from scenarios_dir (default: <repo>/scenarios/),
    extract its current_terraform, and run validate() on each one.

    Args:
        scenarios_dir:  Path to the directory containing tier_*.json files.
                        Defaults to <this file's parent parent>/scenarios/.
        workspace_root: Root dir under which per-scenario workspaces are
                        created. Defaults to a fresh temp directory.
        tier_filter:    Optional list of tier keys to run, e.g. ["tier_a", "tier_b"].
                        When None all tiers are tested.

    Returns:
        List of ScenarioResult — one per scenario across all matching tiers.
        Prints a summary table to stdout.
    """
    if scenarios_dir is None:
        scenarios_dir = Path(__file__).parent.parent / "scenarios"

    use_temp = workspace_root is None
    if use_temp:
        workspace_root = Path(tempfile.mkdtemp(prefix="tf_plan_scenarios_"))

    results: list[ScenarioResult] = []

    tier_files = sorted(scenarios_dir.glob("tier_*.json"))
    if not tier_files:
        logger.warning(f"No tier_*.json files found in {scenarios_dir}")
        return results

    for tier_file in tier_files:
        data = json.loads(tier_file.read_text(encoding="utf-8"))
        tier_key = next(iter(data))  # "tier_a" / "tier_b" / …

        if tier_filter and tier_key not in tier_filter:
            continue

        scenarios = data[tier_key].get("scenarios", {})

        for scenario_id, scenario in scenarios.items():
            tf_code = scenario.get("current_terraform", "")
            workspace = workspace_root / tier_key / scenario_id

            logger.info(f"[{tier_key}/{scenario_id}] running terraform plan …")
            result = validate(tf_code, workspace)

            results.append(ScenarioResult(
                scenario_id = scenario_id,
                tier        = tier_key,
                passed      = result.passed,
                score       = result.score,
                skipped     = result.skipped,
                details     = result.details,
            ))

    # ------------------------------------------------------------------ #
    # Summary table
    # ------------------------------------------------------------------ #
    passed  = sum(1 for r in results if r.passed)
    skipped = sum(1 for r in results if r.skipped)
    failed  = len(results) - passed - skipped

    print(f"\n{'='*62}")
    print(f"  terraform plan — scenario batch results")
    print(f"{'='*62}")
    print(f"  {'SCENARIO':<12} {'TIER':<10} {'RESULT':<10} DETAILS")
    print(f"  {'-'*58}")
    for r in results:
        status = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
        detail_short = r.details.replace("\n", " ")[:45]
        print(f"  {r.scenario_id:<12} {r.tier:<10} {status:<10} {detail_short}")
    print(f"  {'-'*58}")
    print(f"  Total: {len(results)}  PASS: {passed}  FAIL: {failed}  SKIP: {skipped}")
    print(f"{'='*62}\n")

    if use_temp:
        shutil.rmtree(workspace_root, ignore_errors=True)

    return results


if __name__ == "__main__":
    test_saved_scenarios()