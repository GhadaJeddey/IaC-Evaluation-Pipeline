"""
test_terraform_plan.py

Manual test suite for validators/terraform_plan.py
Tests: empty input, missing terraform binary, LocalStack skip, valid plan, invalid HCL.
Run: python test_terraform_plan.py
"""

import sys
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from validators.terraform_plan import validate, ValidatorResult, _localstack_reachable

# ---------------------------------------------------------------------------
# Sample Terraform snippets
# ---------------------------------------------------------------------------

VALID_EC2 = """\
resource "aws_instance" "web" {
  ami           = "ami-0c55b159cbfafe1f0"
  instance_type = "t3.micro"

  tags = {
    Name = "test-instance"
  }
}
"""

INVALID_HCL = """\
resource "aws_instance" "bad" {
  ami = "ami-123"
  instance_type = "t3.micro"
  INVALID_SYNTAX >>>
}
"""

VALID_S3 = """\
resource "aws_s3_bucket" "my_bucket" {
  bucket = "my-test-bucket-xyz-123"

  tags = {
    Environment = "test"
  }
}
"""


class TestPreFlightChecks(unittest.TestCase):
    """Tests that don't require LocalStack or terraform."""

    def test_empty_string_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = validate("", Path(tmp))
        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.skipped)
        print(f"  [PASS] empty input → {result.details}")

    def test_whitespace_only_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = validate("   \n\t  ", Path(tmp))
        self.assertFalse(result.passed)
        self.assertFalse(result.skipped)
        print(f"  [PASS] whitespace only → {result.details}")

    def test_no_terraform_binary_skipped(self):
        with patch("shutil.which", return_value=None):
            with tempfile.TemporaryDirectory() as tmp:
                result = validate(VALID_EC2, Path(tmp))
        self.assertTrue(result.skipped)
        self.assertFalse(result.passed)
        print(f"  [PASS] no terraform binary → skipped, {result.details}")

    def test_localstack_unreachable_skipped(self):
        """When LocalStack is down, validator must skip (not fail)."""
        with patch("validators.terraform_plan._localstack_reachable", return_value=False):
            with tempfile.TemporaryDirectory() as tmp:
                result = validate(VALID_EC2, Path(tmp))
        self.assertTrue(result.skipped)
        self.assertFalse(result.passed)
        print(f"  [PASS] localstack unreachable → skipped, {result.details}")

    def test_result_dataclass_fields(self):
        r = ValidatorResult("terraform_plan", True, 1.0, "ok")
        self.assertEqual(r.name, "terraform_plan")
        self.assertFalse(r.skipped)   # default
        print(f"  [PASS] ValidatorResult defaults correct")


class TestWithLocalStack(unittest.TestCase):
    """Integration tests — require LocalStack on localhost:4566."""

    @classmethod
    def setUpClass(cls):
        cls.localstack_available = _localstack_reachable()
        if not cls.localstack_available:
            print("\n  [SKIP] LocalStack not reachable — skipping integration tests")

    def setUp(self):
        if not self.localstack_available:
            self.skipTest("LocalStack not running")
        self.workspace = Path(tempfile.mkdtemp(prefix="tf_test_"))

    def tearDown(self):
        if hasattr(self, "workspace") and self.workspace.exists():
            shutil.rmtree(self.workspace)

    def test_valid_ec2_passes(self):
        result = validate(VALID_EC2, self.workspace)
        self.assertTrue(result.passed, f"Expected plan to pass. Details: {result.details}")
        self.assertEqual(result.score, 1.0)
        self.assertFalse(result.skipped)
        print(f"  [PASS] valid EC2 plan passed. Details: {result.details[:120]}")

    def test_valid_s3_passes(self):
        result = validate(VALID_S3, self.workspace)
        self.assertTrue(result.passed, f"Expected plan to pass. Details: {result.details}")
        self.assertEqual(result.score, 1.0)
        print(f"  [PASS] valid S3 plan passed.")

    def test_invalid_hcl_fails(self):
        result = validate(INVALID_HCL, self.workspace)
        self.assertFalse(result.passed)
        self.assertEqual(result.score, 0.0)
        self.assertFalse(result.skipped)
        print(f"  [PASS] invalid HCL failed correctly. Details: {result.details[:120]}")

    def test_workspace_cleaned_between_runs(self):
        """Running twice in same workspace dir should work (no stale state)."""
        r1 = validate(VALID_EC2, self.workspace)
        r2 = validate(VALID_EC2, self.workspace)
        self.assertTrue(r1.passed)
        self.assertTrue(r2.passed)
        print(f"  [PASS] workspace cleaned between runs — both plans passed")


if __name__ == "__main__":
    print("=" * 60)
    print("terraform_plan.py — validator test suite")
    print("=" * 60)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()

    # Always run pre-flight tests
    suite.addTests(loader.loadTestsFromTestCase(TestPreFlightChecks))

    # Integration tests (will self-skip if LocalStack is down)
    suite.addTests(loader.loadTestsFromTestCase(TestWithLocalStack))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
