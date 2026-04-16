"""
Orchestrator — runs every model against every scenario tier.

Usage:
  python pipeline.py                         # all models, all tiers
  python pipeline.py --models qwen3-coder-32b
  python pipeline.py --tiers tier_a tier_d
  python pipeline.py --scenario A1           # single scenario
"""

import argparse, json, os, sys
from pathlib import Path

import config
from prompts.prompt_builder import build_prompt
from runners import get_runner
from validators import execution_order, nl_quality
from validators.checkov import checkov_runner
from validators.OPA import opa_runner
from validators.terraform import terraform_plan, terraform_validate


# ---------------------------------------------------------------------------
# API keys — loaded once from environment
# ---------------------------------------------------------------------------

API_KEYS = {
    "groq":    os.environ.get("GROQ_API_KEY", ""),
    "google":  os.environ.get("GOOGLE_API_KEY", ""),
    "mistral": os.environ.get("MISTRAL_API_KEY", ""),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_workspace() -> Path:
    """Creates the tf_workspace directory and writes the mock provider stub."""
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
    mode     = scenario.get("terraform_mode", 1)
    tier     = scenario.get("_tier", "")
    llm_eval = scenario.get("llm_evaluation", {})

    # Resolve terraform content — differs by scenario shape
    tf        = llm_response.get("terraform_block") or ""
    tf_action = llm_response.get("terraform_action", "")

    # Multi-finding Tier C: concatenate all per-finding terraform blocks
    if "findings" in llm_response:
        generated_blocks = [
            f.get("terraform_block") or ""
            for f in llm_response["findings"].values()
            if isinstance(f, dict) and f.get("terraform_action") == "LLM_GENERATED"
            and f.get("terraform_block")
        ]
        if generated_blocks:
            tf        = "\n\n".join(generated_blocks)
            tf_action = "LLM_GENERATED"

    results = {}

    # ── Terraform validators — only when LLM actually generated a block ──────
    if tf_action == "LLM_GENERATED" and tf:
        results["terraform_validate"] = vars(terraform_validate.validate(tf, workspace))
        results["terraform_plan"]     = vars(terraform_plan.validate(tf, workspace))
        results["checkov"]            = vars(checkov_runner.validate(tf, workspace))
        results["opa"]                = vars(opa_runner.validate(tf, workspace))
    elif tf_action == "LLM_GENERATED" and not tf:
        # LLM claimed to generate but the block is absent — zero-score validators
        for k in ("terraform_validate", "terraform_plan", "checkov", "opa"):
            results[k] = {"name": k, "passed": False, "score": 0.0,
                          "details": "LLM_GENERATED declared but terraform_block is empty"}

    # ── Execution order — Tier B only ────────────────────────────────────────
    if tier == "tier_b":
        results["execution_order"] = vars(execution_order.validate(scenario, llm_response))

    # ── NL quality — every mode ──────────────────────────────────────────────
    results["nl_quality"] = vars(nl_quality.validate(scenario, llm_response))

    # ── Behavior / verdict correctness ───────────────────────────────────────
    if mode == 3:
        # Tier D crash RCA: compare root_cause_category
        expected_v = llm_eval.get("expected_root_cause_category", "")
        actual_v   = llm_response.get("root_cause_category", "")
        correct    = actual_v == expected_v
        results["behavior_correct"] = {
            "name": "behavior_correct", "passed": correct,
            "score": 1.0 if correct else 0.0,
            "details": f"expected={expected_v}  actual={actual_v}",
        }
        # diagnosis_correct — derived from the nl_quality judge dimension
        diag_score = results["nl_quality"].get("breakdown", {}).get("diagnosis_accuracy", 0)
        results["diagnosis_correct"] = {
            "name": "diagnosis_correct", "passed": diag_score >= 3,
            "score": diag_score / 5.0,
            "details": f"diagnosis_accuracy={diag_score}/5",
        }

    elif "findings" in scenario:
        # Tier C multi-finding: fraction of per-finding verdicts that match
        per_finding       = llm_eval.get("per_finding", {})
        finding_responses = llm_response.get("findings", {})
        if per_finding:
            correct_count = sum(
                1 for rid, exp in per_finding.items()
                if finding_responses.get(rid, {}).get("verdict") == exp.get("expected_verdict")
            )
            total = len(per_finding)
            results["behavior_correct"] = {
                "name": "behavior_correct", "passed": correct_count == total,
                "score": correct_count / total,
                "details": f"verdicts correct: {correct_count}/{total}",
            }
        else:
            results["behavior_correct"] = {
                "name": "behavior_correct", "passed": False,
                "score": 0.0, "details": "No per_finding llm_evaluation defined",
            }

    elif tier == "tier_b":
        # Tier B multi-instance: fraction of per-instance verdicts that match
        per_instance   = llm_eval.get("per_instance", {})
        inst_responses = llm_response.get("instances", {})
        if per_instance:
            correct_count = sum(
                1 for iid, exp in per_instance.items()
                if inst_responses.get(iid, {}).get("verdict") == exp.get("expected_verdict")
            )
            total = len(per_instance)
            results["behavior_correct"] = {
                "name": "behavior_correct", "passed": correct_count == total,
                "score": correct_count / total,
                "details": f"verdicts correct: {correct_count}/{total}",
            }
        else:
            results["behavior_correct"] = {
                "name": "behavior_correct", "passed": False,
                "score": 0.0, "details": "No per_instance llm_evaluation defined",
            }

    else:
        # Tier A/C single-resource: compare verdict directly
        expected_v = llm_eval.get("expected_verdict", "")
        actual_v   = llm_response.get("verdict", "")
        correct    = actual_v == expected_v
        results["behavior_correct"] = {
            "name": "behavior_correct", "passed": correct,
            "score": 1.0 if correct else 0.0,
            "details": f"expected={expected_v}  actual={actual_v}",
        }

    return results


def _expected_verdict_for(scenario: dict) -> object:
    """Returns the expected_verdict value to store in the output record."""
    llm_eval = scenario.get("llm_evaluation", {})
    mode     = scenario.get("terraform_mode", 1)
    if mode == 3:
        return llm_eval.get("expected_root_cause_category")
    if "findings" in scenario:
        return llm_eval.get("per_finding")   # dict keyed by resource_id
    return llm_eval.get("expected_verdict")


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

        try:
            runner = get_runner(model_cfg, API_KEYS)
        except ValueError as e:
            print(f"[skip] {e}")
            continue

        print(f"{'='*60}")
        print(f"Model: {model_name}  ({provider})")
        print(f"{'='*60}")

        for scenario in scenarios:
            sid  = scenario["_scenario_id"]
            tier = scenario["_tier"]
            mode = scenario.get("terraform_mode")

            # Skip if output already exists
            out_path = config.OUTPUTS_DIR / model_name / f"{sid}.json"
            if out_path.exists():
                print(f"  [{sid}] already done — skipping")
                continue

            # Determine a display label for the scenario type
            if "findings" in scenario:
                label = f"multi-finding ({len(scenario['findings'])})"
            elif scenario.get("flagged_resources"):
                label = f"multi-instance ({len(scenario['flagged_resources'])})" \
                        if len(scenario["flagged_resources"]) > 1 \
                        else scenario["flagged_resources"][0].get("agent2_decision", {}).get("action", "")
            else:
                label = scenario.get("agent2_decision", {}).get("action", "")

            print(f"  [{sid}] tier={tier}  mode={mode}  {label} ...", end=" ", flush=True)

            system, user = build_prompt(scenario)

            result       = runner.run(system, user)
            llm_response = result["parsed"] or {}
            raw          = result["raw_response"] or ""

            if result["parse_error"]:
                print(f"PARSE_ERROR ({result['parse_error'][:60]})")

            validators = _run_validators(scenario, llm_response, workspace)

            output = {
                "scenario_id":      sid,
                "tier":             tier,
                "terraform_mode":   mode,
                "expected_verdict": _expected_verdict_for(scenario),
                "model":            model_name,
                "llm_response":     llm_response,
                "validators":       validators,
                "raw_output":       raw,
                "latency_ms":       result.get("latency_ms"),
                "attempts":         result.get("attempts"),
            }

            _save_output(model_name, sid, output)
            verdict_ok = validators.get("behavior_correct", {}).get("passed", False)
            print(f"{'OK' if verdict_ok else 'WRONG_VERDICT'}  saved")

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
