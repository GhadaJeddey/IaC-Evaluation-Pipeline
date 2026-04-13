"""
validators/terraform_plan.py

Runs terraform init + terraform plan against LocalStack.

"""

import os
import shutil
import socket
import subprocess
import logging
from pathlib import Path
from dataclasses import dataclass, field

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
    
