"""
Orchestrator — runs every model against every scenario tier.

Usage:
  python pipeline.py                         # all models, all tiers
  python pipeline.py --models qwen3-coder-32b
  python pipeline.py --tiers tier_a tier_d
  python pipeline.py --scenario A1           # single scenario
"""
import argparse, json, re, sys, time
from pathlib import Path

import config
from prompts.prompt_builder import build_prompt
from runners import groq_runner, google_runner, mistral_runner, ollama_runner
from validators import (
    terraform_validate, terraform_plan,
    checkov_runner, opa_runner,
    execution_order, nl_quality,
)

# ---------------------------------------------------------------------------
RUNNER_MAP = {
    "groq":    groq_runner,
    "google":  google_runner,
    "mistral": mistral_runner,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> dict:
    """Extract JSON from raw LLM text (strips markdown fences if present)."""
    if not raw:
        return {}
    # Strip ```json ... ``` fences
    match = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    text = match.group(1) if match else raw
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        return {"_raw": raw, "_parse_error": True}


def _ensure_workspace() -> Path:
    """ creates the tf_workspace directory if it doesn't exist - the mock AWS provider """
    ws = config.TF_WORKSPACE
    ws.mkdir(exist_ok=True)
    provider_tf = ws / "provider.tf"
    if not provider_tf.exists():
        provider_tf.write_text(config.TF_PROVIDER_STUB)
        print(f"[workspace] provider.tf written — run `terraform init` in {ws} before validators")
    return ws


def _load_scenarios(tiers: list[str], scenario_filter: str | None) -> list[dict]:
    scenarios = []
    for tier in tiers:
        path = config.SCENARIOS_DIR / f"{tier}.json"
        if not path.exists():
            print(f"[warn] {path} not found — skipping")
            continue
        data = json.loads(path.read_text())
        for sid, s in data[tier]["scenarios"].items():
            if scenario_filter and sid != scenario_filter:
                continue
            s["_scenario_id"] = sid
            s["_tier"]        = tier
            scenarios.append(s)
    return scenarios


def _run_validators(scenario: dict, llm_response: dict, workspace: Path) -> dict:
    mode = scenario.get("terraform_mode", 1)
    tf   = llm_response.get("terraform_output", "")

    results = {}

    # Code pipeline — only when Terraform was produced
    if mode in (2, 3) and tf:
        results["terraform_validate"] = vars(terraform_validate.validate(tf, workspace))
        results["terraform_plan"]     = vars(terraform_plan.validate(tf, workspace))
        results["checkov"]            = vars(checkov_runner.validate(tf, workspace))
        results["opa"]                = vars(opa_runner.validate(tf, workspace))
    
    elif mode in (2, 3):
        # Mode 2/3 but LLM produced no Terraform — zero score on all code checks
        for k in ("terraform_validate", "terraform_plan", "checkov", "opa"):
            results[k] = {"name": k, "passed": False, "score": 0.0, "details": "No Terraform produced"}

    # Execution order — only for bundle scenarios (Tier B)
    if scenario.get("_tier") == "tier_b":
        results["execution_order"] = vars(execution_order.validate(scenario, llm_response))

    # NL quality — every mode
    results["nl_quality"] = vars(nl_quality.validate(scenario, llm_response))

    # Behavior correctness — did LLM pick the right behavior?
    expected = scenario.get("expected_llm_behavior", "validate")
    actual   = llm_response.get("behavior", "")
    correct  = actual == expected
    results["behavior_correct"] = {
        "name": "behavior_correct", "passed": correct,
        "score": 1.0 if correct else 0.0,
        "details": f"expected={expected} actual={actual}",
    }

    # Mode 3 diagnosis correctness — NL judge already covers this,
    # but we add a binary flag for the scorer
    if mode == 3:
        diagnosis_score = results["nl_quality"].get("breakdown", {}).get("diagnosis_accuracy", 0)
        correct_diag = diagnosis_score >= 3   # 3/5 threshold
        results["diagnosis_correct"] = {
            "name": "diagnosis_correct", "passed": correct_diag,
            "score": diagnosis_score / 5.0,
            "details": f"diagnosis_accuracy={diagnosis_score}/5",
        }

    return results


def _save_output(model_name: str, scenario_id: str, data: dict) -> None:
    out_dir = config.OUTPUTS_DIR / model_name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{scenario_id}.json").write_text(json.dumps(data, indent=2))

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(models: list[str], tiers: list[str], scenario_filter: str | None = None) -> None:
    workspace = _ensure_workspace()
    scenarios = _load_scenarios(tiers, scenario_filter)

    if not scenarios:
        print("No scenarios matched — check tier names or scenario ID")
        return

    print(f"\nRunning eval: {len(models)} models × {len(scenarios)} scenarios\n")

    for model_name in models:
        model_cfg = config.MODELS[model_name]
        provider  = model_cfg["provider"]
        runner    = RUNNER_MAP.get(provider)
        if runner is None:
            print(f"[skip] no runner for provider={provider}")
            continue

        print(f"{'='*60}")
        print(f"Model: {model_name}  ({provider})")
        print(f"{'='*60}")

        for scenario in scenarios:
            sid  = scenario["_scenario_id"]
            mode = scenario.get("terraform_mode")

            # Skip if output already exists (resume support)
            out_path = config.OUTPUTS_DIR / model_name / f"{sid}.json"
            if out_path.exists():
                print(f"  [{sid}] already done — skipping")
                continue

            print(f"  [{sid}] mode={mode}  behavior={scenario.get('expected_llm_behavior')} ...", end=" ", flush=True)

            system, user = build_prompt(scenario)

            raw = runner.call(
                model_cfg["model_id"], system, user,
                interval=model_cfg.get("interval_seconds", 5),
            )

            llm_response = _parse_response(raw)
            validators   = _run_validators(scenario, llm_response, workspace)

            output = {
                "scenario_id":       sid,
                "tier":              scenario["_tier"],
                "terraform_mode":    mode,
                "expected_behavior": scenario.get("expected_llm_behavior"),
                "model":             model_name,
                "llm_response":      llm_response,
                "validators":        validators,
                "raw_output":        raw,
            }

            _save_output(model_name, sid, output)
            behavior_ok = validators.get("behavior_correct", {}).get("passed", False)
            print(f"{'OK' if behavior_ok else 'WRONG_BEHAVIOR'}  saved")

    print("\nDone. Run scorer.py to generate leaderboard.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--models",   nargs="+", default=list(config.MODELS.keys()))
    parser.add_argument("--tiers",    nargs="+", default=config.TIER_FILES)
    parser.add_argument("--scenario", default=None, help="Run a single scenario by ID, e.g. A1")
    args = parser.parse_args()

    invalid = [m for m in args.models if m not in config.MODELS]
    if invalid:
        print(f"Unknown models: {invalid}\nAvailable: {list(config.MODELS.keys())}")
        sys.exit(1)

    run(args.models, args.tiers, args.scenario)
