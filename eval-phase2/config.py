
"""
Evaluation configuration — models, paths, and per-weight-key scoring weights.

Weight keys (used by scorer._weight_key):
  Two orthogonal axes — multi-instance and terraform-generated — produce 4 combinations,
  plus a dedicated key for Mode 3 (crash RCA).

  "nl"       — single resource,  OPTIMAL   (no terraform)   e.g. Tier A/C OPTIMAL
  "tf"       — single resource,  LLM_GEN   (terraform out)  e.g. Tier A/C SUBOPTIMAL/INCORRECT
  "multi_nl" — multi-instance,   OPTIMAL   (no terraform)   e.g. Tier B all-CLEAN
  "multi_tf" — multi-instance,   LLM_GEN   (terraform out)  e.g. Tier B with overrides
  "3"        — Tier D crash RCA  (terraform optional)
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
        "max_tokens":        3500,   # Groq TPM limit is 6000 (input+output); largest prompts ~2500 tokens
    },
    "llama3.3-70b": {
        "provider":          "groq",
        "model_id":          "llama-3.3-70b-versatile",
        "rpm_limit":         30,
        "rpd_limit":         1000,
        "interval_seconds":  20,   # raised from 10 — 12k TPM limit; Terraform responses are large
    },
    "gemini-2.5-flash": {
        "provider":          "google",
        "model_id":          "gemini-2.5-flash",
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
# Scoring weights — keys must match ValidatorResult.name values.
# Keys are resolved by scorer._weight_key(), not by terraform_mode directly.
# Each set must sum to 1.0.
# ---------------------------------------------------------------------------
WEIGHTS = {
    # Single resource, OPTIMAL verdict — NL explanation only, no Terraform
    # Covers: Tier A OPTIMAL, Tier C OPTIMAL
    "nl": {
        "behavior_correct": 0.45,
        "nl_quality":       0.55,
    },
    # Single resource, SUBOPTIMAL / INCORRECT — LLM generated Terraform
    # Covers: Tier A override, Tier C override
    "tf": {
        "behavior_correct":   0.15,
        "nl_quality":         0.20,
        "terraform_validate": 0.15,
        "terraform_plan":     0.20,
        "checkov":            0.20,
        "opa":                0.10,
    },
    # Multi-instance, OPTIMAL verdict — NL + execution order, no Terraform
    # Covers: Tier B all-CLEAN / all-AGENT_VALIDATED
    "multi_nl": {
        "behavior_correct": 0.30,
        "nl_quality":       0.45,
        "execution_order":  0.25,
    },
    # Multi-instance, LLM generated Terraform for at least one instance
    # Covers: Tier B with SUBOPTIMAL / INCORRECT overrides
    "multi_tf": {
        "behavior_correct":   0.15,
        "nl_quality":         0.25,
        "execution_order":    0.15,
        "terraform_validate": 0.15,
        "terraform_plan":     0.15,
        "checkov":            0.10,
        "opa":                0.05,
    },
    # Tier D — crash RCA; Terraform is optional (only scored when emitted)
    "3": {
        "diagnosis_correct":  0.40,
        "nl_quality":         0.35,
        "terraform_validate": 0.10,
        "terraform_plan":     0.10,
        "checkov":            0.05,
    },
    # Tier C multi-finding, all OPTIMAL (no terraform produced)
    "c_multi_nl": {
        "behavior_correct": 0.40,
        "nl_quality":       0.60,
    },
    # Tier C multi-finding, at least one LLM_GENERATED terraform block
    "c_multi_tf": {
        "behavior_correct":   0.15,
        "nl_quality":         0.25,
        "terraform_validate": 0.15,
        "terraform_plan":     0.20,
        "checkov":            0.15,
        "opa":                0.10,
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
