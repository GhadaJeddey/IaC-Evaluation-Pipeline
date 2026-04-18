"""
validators/checkov_runner.py

Runs Checkov static security scan on LLM-generated Terraform.
Score = passing_checks / total_checks (partial credit).

"""

import json
import shutil
import subprocess
import logging
from pathlib import Path
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CHECKS_JSON = Path(__file__).parent / "checkov_checks.json"


def _load_check_ids() -> tuple[list[str], list[str]]:
    """
    Parse checkov_checks.json and return (enforce_ids, suppress_ids).
    enforce_ids → passed to --check   (only these checks run)
    suppress_ids → passed to --skip-check (belt-and-suspenders; redundant when
                   --check is used, but kept for explicitness)
    """
    try:
        data = json.loads(_CHECKS_JSON.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning(f"Could not load checkov_checks.json: {exc}")
        return [], []

    checks = data.get("checkov_checks", {})

    enforce_ids: list[str] = []
    for group in checks.get("enforce", {}).values():
        enforce_ids.extend(item["id"] for item in group)

    suppress_ids: list[str] = []
    for group in checks.get("suppress", {}).values():
        suppress_ids.extend(item["id"] for item in group)

    return enforce_ids, suppress_ids


_ENFORCE_CHECKS, _SKIP_CHECKS = _load_check_ids()


@dataclass
class ValidatorResult:
    name:    str
    passed:  bool
    score:   float
    details: str
    raw:     dict = field(default_factory=dict, repr=False)



def _clean_workspace(workspace: Path) -> None:
    """
    Remove any .tf files and .terraform directories left by previous runs.
    Prevents cross-scenario contamination when the same workspace is reused.
    """
    for tf_file in workspace.glob("*.tf"):
        tf_file.unlink()
    tf_dir = workspace / ".terraform"
    if tf_dir.exists():
        shutil.rmtree(tf_dir)
    lock_file = workspace / ".terraform.lock.hcl"
    if lock_file.exists():
        lock_file.unlink()


def _extract_summary(data) -> dict:
    """
    Handles two Checkov JSON output shapes:
      - dict  (single file scan): {"summary": {...}, "results": {...}}
      - list  (multi-file scan):  [{"summary": {...}, ...}, ...]

    Merges all summaries into one aggregate dict.
    Also returns a flat list of all failed check objects.
    """
    if isinstance(data, dict):
        return {
            "passed":        data.get("summary", {}).get("passed", 0),
            "failed":        data.get("summary", {}).get("failed", 0),
            "failed_checks": data.get("results", {}).get("failed_checks", []),
        }

    if isinstance(data, list):
        total_passed = 0
        total_failed = 0
        all_failed   = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            total_passed += entry.get("summary", {}).get("passed", 0)
            total_failed += entry.get("summary", {}).get("failed", 0)
            all_failed   += entry.get("results", {}).get("failed_checks", [])
        return {
            "passed":        total_passed,
            "failed":        total_failed,
            "failed_checks": all_failed,
        }

    return {"passed": 0, "failed": 0, "failed_checks": []}


def validate(terraform_code: str, workspace: Path) -> ValidatorResult:
    """
    Runs Checkov on terraform_code written to workspace/main.tf.

    Returns ValidatorResult with:
      passed  — True only if zero checks failed
      score   — passed_checks / total_checks  (partial credit, 0.0–1.0)
      details — human-readable summary of what passed/failed
      raw     — the full parsed Checkov JSON for storage in output files
    """
    if not terraform_code or not terraform_code.strip():
        return ValidatorResult("checkov", False, 0.0, "No Terraform to scan", {})

    if not shutil.which("checkov"):
        return ValidatorResult(
            "checkov", False, 0.0,
            "checkov binary not found — install with: pip install checkov",
            {}
        )

    workspace.mkdir(parents=True, exist_ok=True)
    _clean_workspace(workspace)

    main_tf = workspace / "main.tf"
    main_tf.write_text(terraform_code, encoding="utf-8")

    cmd = [
        "checkov",
        "--directory",  str(workspace),
        "--framework",  "terraform",
        "--output",     "json",
    ]
    
    if _ENFORCE_CHECKS:
        cmd += ["--check", ",".join(_ENFORCE_CHECKS)]
    if _SKIP_CHECKS:
        cmd += ["--skip-check", ",".join(_SKIP_CHECKS)]

    logger.debug(f"checkov cmd: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return ValidatorResult("checkov", False, 0.0, "checkov timed out after 120s", {})
    except Exception as exc:
        return ValidatorResult("checkov", False, 0.0, f"checkov failed to run: {exc}", {})

    # rc : return code 
    # rc=0  → all checks passed (stdout has JSON)
    # rc=1  → some checks failed (stdout still has JSON — this is normal)
    # rc=2  → execution error — Checkov itself crashed
    # rc=-1 → binary not executable or other OS error
    if result.returncode == 2:
        stderr_preview = result.stderr[:300] if result.stderr else "no stderr"
        return ValidatorResult(
            "checkov", False, 0.0,
            f"checkov execution error (rc=2): {stderr_preview}",
            {}
        )

    if result.returncode not in (0, 1):
        return ValidatorResult(
            "checkov", False, 0.0,
            f"checkov unexpected return code {result.returncode}: {result.stderr[:200]}",
            {}
        )

    raw_stdout = (result.stdout or "").strip()

    # Log stderr for debugging unrecognised resource types
    if result.stderr and result.stderr.strip():
        logger.debug(f"checkov stderr: {result.stderr[:400]}")

    if not raw_stdout:
        return ValidatorResult(
            "checkov", False, 0.0,
            f"checkov produced no stdout (rc={result.returncode}): {result.stderr[:200]}",
            {}
        )

    try:
        data = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return ValidatorResult(
            "checkov", False, 0.0,
            f"checkov output not valid JSON: {raw_stdout[:200]}",
            {}
        )

    summary      = _extract_summary(data)
    passed_n     = summary["passed"]
    failed_n     = summary["failed"]
    failed_checks = summary["failed_checks"]
    total        = passed_n + failed_n

    # No applicable checks — resource type not scannable by Checkov
    # (e.g. pure resource deletions, or resource types Checkov doesn't cover)
    if total == 0:
        return ValidatorResult(
            "checkov", True, 1.0,
            "No applicable Checkov checks for this resource type",
            data if isinstance(data, dict) else {}
        )

    score   = round(passed_n / total, 4)
    passed  = failed_n == 0

    details = f"{passed_n}/{total} checks passed"
    if failed_n > 0:
        failed_ids = [
            c.get("check_id", "?")
            for c in failed_checks[:5]
        ]
        failed_names = [
            c.get("check_type", c.get("resource", ""))
            for c in failed_checks[:3]
        ]
        details += f" — failed IDs: {failed_ids}"
        if any(failed_names):
            details += f" — resources: {failed_names}"

    return ValidatorResult(
        name    = "checkov",
        passed  = passed,
        score   = score,
        details = details,
        raw     = data if isinstance(data, dict) else {"entries": data},
    )


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

def _extract_failed_ids(raw: dict) -> list[str]:
    """
    Pull check_id strings out of a ValidatorResult.raw dict.

    Handles both output shapes the runner produces:
      single-file → {"results": {"failed_checks": [...]}}
      multi-file  → {"entries": [{"results": {"failed_checks": [...]}}, ...]}
    """
    if not raw:
        return []

    if "results" in raw:
        checks = raw["results"].get("failed_checks", [])
        return [c.get("check_id") for c in checks if c.get("check_id")]

    if "entries" in raw:
        ids: list[str] = []
        for entry in raw["entries"]:
            if isinstance(entry, dict) and "results" in entry:
                checks = entry["results"].get("failed_checks", [])
                ids.extend(c.get("check_id") for c in checks if c.get("check_id"))
        return ids

    return []


def run_test_scenarios(
    scenarios_file: Path | None = None,
    output_file:    Path | None = None,
    workspace:      Path | None = None,
) -> dict:
    """
    Run every scenario in checkov_test_scenarios.json through validate(),
    evaluate the result against its ground_truth, and write a full report
    to output_test_scenarios.json.

    Returns a summary dict: {"total": N, "passed": N, "failed": N, "errors": N}

    Parameters
    ----------
    scenarios_file : Path, optional
        Defaults to checkov_test_scenarios.json next to this file.
    output_file : Path, optional
        Defaults to output_test_scenarios.json next to this file.
    workspace : Path, optional
        Terraform workspace directory.  When omitted a fresh temp directory
        is created and deleted after the run so scenarios never contaminate
        each other.
    """
    import tempfile
    from datetime import datetime, timezone

    _here           = Path(__file__).parent
    scenarios_file  = scenarios_file or _here / "checkov_test_scenarios.json"
    output_file     = output_file    or _here / "output_test_scenarios.json"

    try:
        raw_json = json.loads(scenarios_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot load scenarios file {scenarios_file}: {exc}") from exc

    scenarios  = raw_json.get("scenarios", [])
    results    = []
    n_pass = n_fail = n_error = 0

    def _run_one(scenario: dict, ws: Path) -> dict:
        """Run a single scenario and return a result record."""
        sid      = scenario.get("id", "UNKNOWN")
        tf_code  = scenario.get("terraform_code", "")
        gt       = scenario.get("ground_truth", {})

        try:
            result = validate(tf_code, ws)
        except Exception as exc:
            return {
                "scenario_id":    sid,
                "category":       scenario.get("category"),
                "description":    scenario.get("description"),
                "explanation":    scenario.get("explanation"),
                "checkov_result": None,
                "ground_truth":   gt,
                "evaluation": {
                    "overall_pass":        False,
                    "passed_match":        False,
                    "score_match":         False,
                    "failed_checks_match": False,
                    "actual_failed_ids":   [],
                    "mismatches":          [f"runner raised exception: {exc}"],
                    "error":               str(exc),
                },
            }

        mismatches: list[str] = []

        # 1. passed flag
        exp_passed   = gt.get("expected_passed")
        passed_match = (result.passed == exp_passed)
        if not passed_match:
            mismatches.append(
                f"passed: got {result.passed!r}, expected {exp_passed!r}"
            )

        # 2. score — small float tolerance for rounding
        exp_score   = gt.get("expected_score")
        score_match = True
        if exp_score is not None:
            score_match = abs(result.score - exp_score) < 0.001
            if not score_match:
                mismatches.append(
                    f"score: got {result.score}, expected {exp_score}"
                )

        # 3. failed check IDs — sets must match exactly
        actual_failed  = _extract_failed_ids(result.raw)
        exp_failed_set = set(gt.get("expected_failed_check_ids", []))
        actual_set     = set(actual_failed)
        failed_match   = (actual_set == exp_failed_set)
        if not failed_match:
            extra   = sorted(actual_set - exp_failed_set)
            missing = sorted(exp_failed_set - actual_set)
            if extra:
                mismatches.append(f"unexpected failures: {extra}")
            if missing:
                mismatches.append(f"expected to fail but passed: {missing}")

        overall = passed_match and score_match and failed_match

        return {
            "scenario_id":  sid,
            "category":     scenario.get("category"),
            "description":  scenario.get("description"),
            "explanation":  scenario.get("explanation"),
            "checkov_result": {
                "passed":  result.passed,
                "score":   result.score,
                "details": result.details,
            },
            "ground_truth": gt,
            "evaluation": {
                "overall_pass":        overall,
                "passed_match":        passed_match,
                "score_match":         score_match,
                "failed_checks_match": failed_match,
                "actual_failed_ids":   actual_failed,
                "mismatches":          mismatches,
            },
        }

    with tempfile.TemporaryDirectory(prefix="checkov_test_") as _tmp:
        ws = workspace or Path(_tmp)

        for scenario in scenarios:
            record = _run_one(scenario, ws)
            results.append(record)

            if record["checkov_result"] is None:
                n_error += 1
            elif record["evaluation"]["overall_pass"]:
                n_pass += 1
            else:
                n_fail += 1

    summary = {
        "total":  len(scenarios),
        "passed": n_pass,
        "failed": n_fail,
        "errors": n_error,
    }

    output = {
        "run_timestamp":   datetime.now(timezone.utc).isoformat(),
        "scenarios_file":  str(scenarios_file),
        "enforce_checks":  _ENFORCE_CHECKS,
        "suppress_checks": _SKIP_CHECKS,
        "summary":         summary,
        "results":         results,
    }

    output_file.write_text(json.dumps(output, indent=2), encoding="utf-8")
    logger.info(
        "Checkov test run complete — %d/%d passed. Report: %s",
        n_pass, len(scenarios), output_file,
    )

    return summary


if __name__ == "__main__":
    
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    summary = run_test_scenarios()

    total  = summary["total"]
    passed = summary["passed"]
    failed = summary["failed"]
    errors = summary["errors"]

    print(f"\nResults: {passed}/{total} passed  |  {failed} failed  |  {errors} errors")
    sys.exit(0 if failed == 0 and errors == 0 else 1)
