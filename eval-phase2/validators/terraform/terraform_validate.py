"""
Runs `terraform validate` against the LLM-generated Terraform.
Writes to tf_workspace/main.tf, expects tf_workspace to be pre-initialised.
"""
import json
import logging
import shutil
import subprocess
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass
class ValidatorResult:
    name:    str
    passed:  bool
    score:   float          # 0.0 – 1.0
    details: str


def validate(terraform_code: str, workspace: Path) -> ValidatorResult:
    if not terraform_code or not terraform_code.strip():
        return ValidatorResult("terraform_validate", False, 0.0, "No Terraform output to validate")

    if not shutil.which("terraform"):
        return ValidatorResult("terraform_validate", False, 0.0, "terraform binary not found — skipped")

    main_tf = workspace / "main.tf"
    main_tf.write_text(terraform_code)

    result = subprocess.run(
        ["terraform", "validate", "-json"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=60,
    )

    # terraform validate -json returns 0 on success
    passed  = result.returncode == 0
    details = result.stdout.strip() or result.stderr.strip()

    return ValidatorResult(
        name    = "terraform_validate",
        passed  = passed,
        score   = 1.0 if passed else 0.0,
        details = details[:500],
    )


# ============================================================================
# Batch scenario tester
# ============================================================================

# Minimal provider block so `terraform init` + `terraform validate` work
# without any real cloud credentials.
_PROVIDER_BLOCK = """\
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
}
"""


@dataclass
class ScenarioResult:
    scenario_id: str
    tier:        str
    passed:      bool
    score:       float
    skipped:     bool
    details:     str


# Persistent workspace shared across all validate runs.
# Kept on disk so terraform init runs only once ever (not per test run).
_PERSISTENT_WORKSPACE = Path(__file__).parent.parent / ".tf_workspace"


def _ensure_workspace_ready(workspace: Path) -> tuple[bool, str]:
    """
    Ensure the workspace has provider.tf and a valid .terraform directory.
    Runs `terraform init` only when .terraform is absent (first run only).
    Returns (ready, message).
    """
    import os
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "provider.tf").write_text(_PROVIDER_BLOCK, encoding="utf-8")

    dot_terraform = workspace / ".terraform"
    if dot_terraform.exists():
        logger.debug("Workspace already initialised — skipping terraform init")
        return True, "already initialised"

    logger.info(f"Running terraform init in {workspace} (one-time setup) …")
    env = os.environ.copy()
    env.update({
        "AWS_ACCESS_KEY_ID":     "mock",
        "AWS_SECRET_ACCESS_KEY": "mock",
        "AWS_DEFAULT_REGION":    "us-east-1",
        "CHECKPOINT_DISABLE":    "1",
        "TF_INPUT":              "false",
    })

    try:
        result = subprocess.run(
            ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=300,   # generous — only runs once
            env=env,
        )
        out = (result.stdout + result.stderr).strip()
        return result.returncode == 0, out
    except subprocess.TimeoutExpired:
        return False, "terraform init timed out after 300s"
    except Exception as exc:
        return False, str(exc)


def test_saved_scenarios(
    scenarios_file: Optional[Path] = None,
    workspace: Optional[Path] = None,
) -> list[ScenarioResult]:
    """
    Load scenarios from test_scenarios.json and run terraform validate on each.

    Each scenario declares expected_validate: "pass" | "fail" so the runner
    can report whether the validator's actual result matches the expectation.

    Args:
        scenarios_file: Path to test_scenarios.json.
                        Defaults to <this file's directory>/test_scenarios.json.
        workspace:      Pre-initialized terraform workspace.
                        Defaults to <repo>/.tf_workspace/ (persisted on disk).

    Returns:
        List of ScenarioResult. Prints a summary table with pass/fail/mismatch.
    """
    if scenarios_file is None:
        scenarios_file = Path(__file__).parent / "test_scenarios.json"

    if workspace is None:
        workspace = _PERSISTENT_WORKSPACE

    if not shutil.which("terraform"):
        logger.warning("terraform binary not found — test_saved_scenarios skipped")
        return []

    data = json.loads(scenarios_file.read_text(encoding="utf-8"))
    scenarios = data.get("scenarios", {})

    print("Preparing terraform workspace … ", end="", flush=True)
    ready, msg = _ensure_workspace_ready(workspace)
    if not ready:
        print("FAILED")
        logger.error(f"Workspace setup failed: {msg}")
        return []
    print("ready\n")

    results:     list[ScenarioResult] = []
    output_rows: list[dict]           = []

    for scenario_id, scenario in scenarios.items():
        tf_code  = scenario.get("terraform_code", "")
        expected = scenario.get("expected_validate", "pass")

        result = validate(tf_code, workspace)

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
            "scenario_id":      scenario_id,
            "category":         scenario.get("category", ""),
            "description":      scenario.get("description", ""),
            "expected_result":  expected,
            "actual_result":    actual,
            "conformant":       matched,
            "error":            result.details if not matched else None,
        })

        icon = "OK" if matched else "MISMATCH"
        print(f"  [{icon:<8}] {scenario_id:<40} expected={expected:<4} got={actual}")

    # Summary
    ok       = sum(1 for r in results if r.passed)
    mismatch = len(results) - ok

    print(f"\n{'='*62}")
    print(f"  terraform validate — saved scenario results")
    print(f"{'='*62}")
    print(f"  Total: {len(results)}  OK: {ok}  MISMATCH: {mismatch}")
    if mismatch:
        print(f"\n  Mismatches (validator behaved unexpectedly):")
        for r in results:
            if not r.passed:
                print(f"    {r.scenario_id}  [{r.tier}]  {r.details[:80]}")
    print(f"{'='*62}\n")

    # Persist results to JSON
    output_file = Path(__file__).parent / "output_validate_test_scenarios.json"
    output_payload = {
        "summary": {
            "total":    len(results),
            "ok":       ok,
            "mismatch": mismatch,
        },
        "results": output_rows,
    }
    output_file.write_text(
        json.dumps(output_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  Results saved → {output_file}\n")

    return results


if __name__ == "__main__":
    test_saved_scenarios()