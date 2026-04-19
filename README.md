# LLM Evaluation Pipeline — AWS FinOps Agent

An automated evaluation framework that benchmarks LLMs on their ability to act as a third-party validation layer (Agent 3) in an AWS cost-optimisation pipeline. Models are asked to review decisions made by an automated analysis agent (Agent 2) and produce structured verdicts, cost reports, risk assessments, and corrective Terraform.

---

## What it evaluates

Each model receives a real-world AWS scenario and must:

1. Review an automated agent's cost-optimisation decision (STOP / DOWNSIZE / TERMINATE / etc.)
2. Return a structured verdict — **OPTIMAL**, **SUBOPTIMAL**, or **INCORRECT**
3. Generate corrective Terraform HCL when the agent's decision was wrong
4. Diagnose crash root causes from EC2 logs (Tier D)

Outputs are then scored by a suite of automated validators and an LLM-as-judge.

---

## Scenario tiers

| Tier | File | Description | Mode |
|------|------|-------------|------|
| A | `scenarios/tier_a.json` | Single EC2 instance — role detection + blast radius | 1/2 |
| B | `scenarios/tier_b.json` | Multi-instance app groups — dependency ordering | 1/2 |
| C | `scenarios/tier_c.json` | Non-EC2 resources (CloudWatch, EBS, EIP, S3, NAT, ALB, ECR, Lambda) — single and multi-finding | 1/2 |
| D | `scenarios/tier_d.json` | Crash RCA from StatusCheckFailed logs | 3 |

Scenario IDs follow the pattern `A1–A20`, `B1–B6`, `C1–C10 + CM1–CM3`, `D1–D5`.

---

## Models under evaluation

| Model | Provider | Model ID |
|-------|----------|----------|
| `qwen3-coder-32b` | Groq | `qwen/qwen3-32b` |
| `llama3.3-70b` | Groq | `llama-3.3-70b-versatile` |
| `gemini-2.5-flash` | Google | `gemini-2.5-flash` |
| `codestral-22b` | Mistral | `codestral-latest` |

Model configuration (rate limits, token caps, provider) lives in `config.py`.

---

## Project structure

```
.
├── pipeline.py              # Main orchestrator — runs models × scenarios
├── scorer.py                # Aggregates outputs into a leaderboard
├── config.py                # Models, paths, scoring weights
│
├── scenarios/
│   ├── tier_a.json          # Single-instance EC2 scenarios
│   ├── tier_b.json          # Multi-instance app group scenarios
│   ├── tier_c.json          # Non-EC2 resource scenarios
│   └── tier_d.json          # Crash RCA scenarios
│
├── prompts/
│   ├── prompt_builder.py    # Builds system + user prompts per scenario type
│   └── llm_judge_prompts.py # Prompts used by the LLM judge (nl_quality)
│
├── runners/
│   ├── base_runner.py       # Abstract base: rate limiting, retry, JSON extraction
│   ├── groq_runner.py       # Groq provider
│   ├── google_runner.py     # Google Gemini provider
│   └── mistral_runner.py    # Mistral provider
│
├── validators/
│   ├── nl_quality.py        # LLM-as-judge scorer (Claude, 6 dimensions)
│   ├── execution_order.py   # Tier B dependency ordering checker
│   ├── terraform/
│   │   ├── terraform_validate.py   # `terraform validate` runner
│   │   └── terraform_plan.py       # `terraform plan` runner
│   ├── checkov/
│   │   └── checkov_runner.py       # Checkov static security scanner
│   └── OPA/
│       └── opa_runner.py           # OPA policy evaluation
│
├── policies/                # OPA Rego policies (security + correctness)
├── outputs/                 # Per-model, per-scenario JSON results
│   └── <model-name>/
│       └── <scenario-id>.json
├── results/
│   └── leaderboard.json     # Final aggregated scores
└── tf_workspace/            # Temporary Terraform workspace used by validators
```

---

## Validators

Each model output is scored by up to 8 validators depending on the scenario type:

| Validator | What it checks | Applies to |
|-----------|---------------|------------|
| `behavior_correct` | Verdict matches expected (OPTIMAL / SUBOPTIMAL / INCORRECT) | All tiers |
| `nl_quality` | LLM-as-judge on 6 NL dimensions (accuracy, clarity, risk, cost, actionability, completeness) | All tiers |
| `terraform_validate` | `terraform validate` on generated HCL | When `terraform_action = LLM_GENERATED` |
| `terraform_plan` | `terraform plan` (mock provider) | When `terraform_action = LLM_GENERATED` |
| `checkov` | Static security scan on generated HCL | When `terraform_action = LLM_GENERATED` |
| `opa` | Custom OPA Rego policies on generated HCL | When `terraform_action = LLM_GENERATED` |
| `execution_order` | Instance dependency order is correct | Tier B only |
| `diagnosis_correct` | Root cause category matches expected | Tier D only |

The NL judge (`nl_quality`) runs Claude externally via `claude -p` and is not counted as a model under evaluation.

---

## Scoring

Scores are weighted differently depending on what the model produced:

| Weight profile | When used | Key validators |
|----------------|-----------|----------------|
| `nl` | Single resource, OPTIMAL (no Terraform) | behavior_correct (45%), nl_quality (55%) |
| `tf` | Single resource, LLM-generated Terraform | behavior_correct (15%), nl_quality (20%), terraform_plan (20%), checkov (20%), terraform_validate (15%), opa (10%) |
| `multi_nl` | Multi-instance, OPTIMAL | behavior_correct (30%), nl_quality (45%), execution_order (25%) |
| `multi_tf` | Multi-instance, LLM-generated Terraform | behavior_correct (15%), nl_quality (25%), execution_order (15%), terraform validators |
| `c_multi_nl` | Tier C multi-finding, OPTIMAL | behavior_correct (40%), nl_quality (60%) |
| `c_multi_tf` | Tier C multi-finding, LLM-generated Terraform | behavior_correct (15%), nl_quality (25%), terraform validators |
| `3` | Tier D crash RCA | diagnosis_correct (40%), nl_quality (35%), terraform_validate (10%), terraform_plan (10%), checkov (5%) |

Final scores are on a 0–100 scale. The leaderboard also reports **Verdict%** — the percentage of scenarios where all verdicts exactly matched expected.

---

## Setup

### Prerequisites

- Python 3.11+
- Terraform CLI (`terraform init` required once in `tf_workspace/`)
- [Checkov](https://www.checkov.io/) (`pip install checkov`)
- [OPA](https://www.openpolicyagent.org/) binary on `PATH`
- [Claude Code CLI](https://claude.ai/code) (`claude`) for the NL judge
- API keys for the providers you want to run

### Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Set API keys

```bash
export GROQ_API_KEY=...
export GOOGLE_API_KEY=...
export MISTRAL_API_KEY=...
```

### Initialize Terraform workspace

```bash
cd tf_workspace
terraform init
cd ..
```

---

## Usage

### Run full evaluation

```bash
python pipeline.py
```

### Filter by model or tier

```bash
python pipeline.py --models gemini-2.5-flash
python pipeline.py --tiers tier_a tier_d
python pipeline.py --models qwen3-coder-32b --scenario A1
```

### Re-run specific validators on existing outputs

Useful when you update a validator or policy without re-querying models:

```bash
python pipeline.py --reeval --validators behavior_correct nl_quality
python pipeline.py --reeval --validators terraform_validate checkov opa
python pipeline.py --reeval --models qwen3-coder-32b --scenario A1 --validators all
```

### Generate leaderboard

```bash
python scorer.py
python scorer.py --tiers tier_a tier_d   # filter to specific tiers
```

Results are written to `results/leaderboard.json` and printed as a summary table.

---

## Output format

Each scenario result is saved as `outputs/<model>/<scenario_id>.json`:

```json
{
  "scenario_id": "A1",
  "tier": "tier_a",
  "terraform_mode": 1,
  "expected_verdict": "OPTIMAL",
  "model": "gemini-2.5-flash",
  "llm_response": { ... },
  "validators": {
    "behavior_correct": { "name": "behavior_correct", "passed": true, "score": 1.0, "details": "..." },
    "nl_quality":       { "name": "nl_quality", "passed": true, "score": 0.82, "details": "...", "breakdown": { ... } },
    "terraform_validate": { ... }
  },
  "raw_output": "...",
  "latency_ms": 3421,
  "attempts": 1
}
```

---

## Current results

| Model | Avg Score | Verdict% | Scenarios |
|-------|-----------|----------|-----------|
| gemini-2.5-flash | 72.67 | 51.2% | 44 |
| qwen3-coder-32b | 72.64 | 74.4% | 44 |
| llama3.3-70b | 69.20 | 74.4% | 44 |
| codestral-22b | 68.49 | 76.7% | 44 |

Run `python scorer.py` for the full up-to-date table.
