"""
Runs `terraform validate` against the LLM-generated Terraform.
Writes to tf_workspace/main.tf, expects tf_workspace to be pre-initialised.
"""
import subprocess, shutil
from pathlib import Path
from dataclasses import dataclass

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
