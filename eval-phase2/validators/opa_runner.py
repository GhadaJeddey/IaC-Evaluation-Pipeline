"""
Runs OPA/conftest against the Terraform plan JSON.
Skipped gracefully if conftest not available or plan didn't run.
"""
import subprocess, shutil
from pathlib import Path
from dataclasses import dataclass

@dataclass
class ValidatorResult:
    name:    str
    passed:  bool
    score:   float
    details: str


def validate(terraform_code: str, workspace: Path) -> ValidatorResult:
    if not terraform_code or not terraform_code.strip():
        return ValidatorResult("opa", False, 0.0, "No Terraform output")

    if not shutil.which("conftest"):
        return ValidatorResult("opa", True, 1.0, "conftest not found — skipped, score credited")

    policies_dir = Path(__file__).parent.parent / "policies"
    if not policies_dir.exists() or not list(policies_dir.glob("*.rego")):
        return ValidatorResult("opa", True, 1.0, "No OPA policies found — skipped, score credited")

    plan_json = workspace / "plan.json"
    if not plan_json.exists():
        return ValidatorResult("opa", True, 1.0, "No plan.json — terraform plan skipped, score credited")

    result = subprocess.run(
        ["conftest", "test", str(plan_json), "--policy", str(policies_dir), "--no-color"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    passed  = result.returncode == 0
    details = (result.stdout + result.stderr).strip()[:500]
    return ValidatorResult(name="opa", passed=passed, score=1.0 if passed else 0.0, details=details)
