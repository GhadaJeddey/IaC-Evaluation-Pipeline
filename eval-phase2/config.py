
"""
trust_paragraph:  an exeternl LLM judge ( claude sonnet 4.6 ] evaluates the model's output 

"""


from pathlib import Path

BASE_DIR = Path(__file__).parent

SCENARIOS_DIR  = BASE_DIR / "scenarios"
OUTPUTS_DIR    = BASE_DIR / "outputs"
RESULTS_DIR    = BASE_DIR / "results"
TF_WORKSPACE   = BASE_DIR / "tf_workspace"
POLICIES_DIR   = BASE_DIR / "policies"

# ---------------------------------------------------------------------------
# Models under evaluation
# ---------------------------------------------------------------------------
MODELS = {
    "qwen3-coder-32b": {
        "provider":          "groq",
        "model_id":          "qwen/qwen3-32b",
        "rpm_limit":         60,
        "rpd_limit":         1000,
        "interval_seconds":  10,
    },
    "llama3.3-70b": {
        "provider":          "groq",
        "model_id":          "llama-3.3-70b-versatile",
        "rpm_limit":         30,
        "rpd_limit":         1000,
        "interval_seconds":  10,
    },
    "gemini-2.5-flash": {
        "provider":          "google",
        "model_id":          "gemini-2.5-flash-preview-04-17",
        "rpm_limit":         10,
        "rpd_limit":         250,
        "interval_seconds":  7,
    },
    "codestral-22b": {
        "provider":          "mistral",
        "model_id":          "codestral-latest",
        "rpm_limit":         60,
        "rpd_limit":         None,
        "interval_seconds":  2,
    },
}

# ---------------------------------------------------------------------------
# Scoring weights — keys must match ValidatorResult.name values in scorer.py
# Weights per mode; must sum to 1.0
# ---------------------------------------------------------------------------
WEIGHTS = {
    # Mode 1: LLM validates Agent 2, outputs NL only — no Terraform generated
    1: {
        "behavior_correct":  0.35,
        "nl_quality":        0.40,
        "trust_paragraph":   0.25,
    },
    # Mode 2: LLM overrides Agent 2, generates Terraform
    2: {
        "behavior_correct":   0.10,
        "terraform_validate": 0.15,
        "terraform_plan":     0.15,
        "checkov":            0.20,
        "opa":                0.15,
        "nl_quality":         0.15,
        "trust_paragraph":    0.10,
    },
    # Mode 3: Crash RCA — diagnosis + optional Terraform
    3: {
        "diagnosis_correct":  0.35,
        "nl_quality":         0.25,
        "trust_paragraph":    0.10,
        "terraform_validate": 0.10,
        "terraform_plan":     0.10,
        "checkov":            0.10,
    },
}

# ---------------------------------------------------------------------------
# NL judge — external model used to score explanations (not under evaluation)
# ---------------------------------------------------------------------------
JUDGE_MODEL = "claude-sonnet-4-6"   # Anthropic SDK

# ---------------------------------------------------------------------------
# Terraform workspace provider stub (written once, used by all validators)
# ---------------------------------------------------------------------------
TF_PROVIDER_STUB = """\
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
  backend "local" {
    path = "/tmp/tfeval.tfstate"
  }
}

provider "aws" {
  region                      = "us-east-1"
  access_key                  = "mock_access_key"
  secret_key                  = "mock_secret_key"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
}
"""

TIER_FILES = ["tier_a", "tier_b", "tier_c", "tier_d"]
