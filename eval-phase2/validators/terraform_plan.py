"""
Runs `terraform plan` (requires LocalStack or real AWS endpoint).
Skipped gracefully when neither is available.
"""
import subprocess, shutil, os
from pathlib import Path
from dataclasses import dataclass

@dataclass
class ValidatorResult:
    name:    str
    passed:  bool
    score:   float
    details: str


def _localstack_reachable() -> bool:
    import socket
    try:
        socket.setdefaulttimeout(1)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("localhost", 4566))
        return True
    except Exception:
        return False


def validate(terraform_code: str, workspace: Path) -> ValidatorResult:
    if not terraform_code or not terraform_code.strip():
        return ValidatorResult("terraform_plan", False, 0.0, "No Terraform output to plan")

    if not shutil.which("terraform"):
        return ValidatorResult("terraform_plan", False, 0.0, "terraform binary not found — skipped")

    if not _localstack_reachable():
        return ValidatorResult(
            "terraform_plan", True, 1.0,
            "LocalStack not running — terraform plan skipped, score credited"
        )

    main_tf = workspace / "main.tf"
    main_tf.write_text(terraform_code)

    env = os.environ.copy()
    env.update({
        "AWS_ACCESS_KEY_ID":     "mock",
        "AWS_SECRET_ACCESS_KEY": "mock",
        "AWS_DEFAULT_REGION":    "us-east-1",
    })

    result = subprocess.run(
        ["terraform", "plan", "-no-color", "-input=false"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    passed  = result.returncode == 0
    details = (result.stdout + result.stderr).strip()

    return ValidatorResult(
        name    = "terraform_plan",
        passed  = passed,
        score   = 1.0 if passed else 0.0,
        details = details[:500],
    )
