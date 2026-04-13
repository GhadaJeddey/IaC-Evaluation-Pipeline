"""
Runs Checkov static security scan on the LLM-generated Terraform.
Score = passing_checks / total_checks (partial credit).
"""
import subprocess, shutil, json
from pathlib import Path
from dataclasses import dataclass

@dataclass
class ValidatorResult:
    name:    str
    passed:  bool
    score:   float
    details: str


# Checks to suppress — known false positives on eval-generated EC2 Terraform
_SKIP_CHECKS = [
    "CKV_AWS_8",    # IMDSv2 — demo instances use default
    "CKV_AWS_126",  # detailed monitoring — not relevant for eval
    "CKV_AWS_135",  # gp3 EBS — eval templates use gp2 deliberately
    "CKV_AWS_18",   # S3 access logging
    "CKV_AWS_144",  # S3 cross-region replication
    "CKV2_AWS_62",  # S3 event notifications
]


def validate(terraform_code: str, workspace: Path) -> ValidatorResult:
    if not terraform_code or not terraform_code.strip():
        return ValidatorResult("checkov", False, 0.0, "No Terraform output to scan")

    if not shutil.which("checkov"):
        return ValidatorResult("checkov", False, 0.0, "checkov binary not found — skipped")

    main_tf = workspace / "main.tf"
    main_tf.write_text(terraform_code)

    skip_arg = ",".join(_SKIP_CHECKS)
    result = subprocess.run(
        [
            "checkov", "--directory", str(workspace),
            "--framework", "terraform",
            "--output", "json",
            "--skip-check", skip_arg,
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )

    raw = (result.stdout or "").strip()
    if not raw:
        return ValidatorResult("checkov", False, 0.0, f"checkov produced no output: {result.stderr[:200]}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ValidatorResult("checkov", False, 0.0, f"checkov output not JSON: {raw[:200]}")

    summary = data.get("summary", {})
    passed_n  = summary.get("passed", 0)
    failed_n  = summary.get("failed", 0)
    total     = passed_n + failed_n

    if total == 0:
        # No checks applicable (e.g. only resource deletions)
        return ValidatorResult("checkov", True, 1.0, "No applicable checks")

    score   = passed_n / total
    passed  = failed_n == 0
    details = f"{passed_n}/{total} checks passed"

    if failed_n > 0:
        failed_ids = [
            c.get("check_id", "?")
            for c in data.get("results", {}).get("failed_checks", [])[:5]
        ]
        details += f" — failed: {failed_ids}"

    return ValidatorResult(name="checkov", passed=passed, score=score, details=details)
