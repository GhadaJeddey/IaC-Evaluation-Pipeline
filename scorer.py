"""
Reads all outputs/, applies per-weight-key weights, writes results/leaderboard.json.

Usage:
  python scorer.py
  python scorer.py --tiers tier_a tier_d
"""
import argparse, json
from pathlib import Path
from collections import defaultdict

import config

def _weight_key(data: dict) -> str:
    """
    Resolve which WEIGHTS entry to use for a scored output record.

    Two orthogonal axes:
      multi  — LLM response has per-instance "instances" dict  → adds execution_order
      tf     — LLM emitted terraform_action = LLM_GENERATED    → adds terraform validators

    Combinations:
      multi=False, tf=False  → "nl"
      multi=False, tf=True   → "tf"
      multi=True,  tf=False  → "multi_nl"
      multi=True,  tf=True   → "multi_tf"
      mode 3 (crash RCA)     → "3"  (always, regardless of terraform)
    """
    if data.get("terraform_mode") == 3:
        return "3"

    llm_resp = data.get("llm_response", {})

    # Tier C multi-finding
    if "findings" in llm_resp:
        is_tf = any(
            v.get("terraform_action") == "LLM_GENERATED"
            for v in llm_resp["findings"].values()
            if isinstance(v, dict)
        )
        return "c_multi_tf" if is_tf else "c_multi_nl"

    # Tier B multi-instance — check per-instance terraform_action
    if "instances" in llm_resp:
        is_tf = any(
            v.get("terraform_action") == "LLM_GENERATED"
            for v in llm_resp["instances"].values()
            if isinstance(v, dict)
        )
        return "multi_tf" if is_tf else "multi_nl"

    # Single resource
    is_tf = llm_resp.get("terraform_action") == "LLM_GENERATED"
    return "tf" if is_tf else "nl"

def _compute_score(validators: dict, weight_key: str) -> tuple[float, dict]:
    """
    Apply WEIGHTS[weight_key] to validator results.
    Returns (total_score_0_to_100, breakdown_dict).
    Missing validators default to score 0.0 (no KeyError).
    """
    weights   = config.WEIGHTS.get(weight_key, config.WEIGHTS["nl"])
    breakdown = {}
    total     = 0.0

    for key, weight in weights.items():
        result   = validators.get(key, {})
        raw      = result.get("score", 0.0) if isinstance(result, dict) else 0.0
        weighted = raw * weight
        breakdown[key] = {
            "raw":      round(raw, 3),
            "weight":   weight,
            "weighted": round(weighted, 3),
        }
        total += weighted

    return round(total * 100, 2), breakdown


def run(tiers: list[str]) -> None:
    outputs_dir = config.OUTPUTS_DIR
    results_dir = config.RESULTS_DIR
    results_dir.mkdir(exist_ok=True)

    # model → {sid → scored_result}
    model_results: dict[str, dict] = defaultdict(dict)
    all_scenario_ids: set[str] = set()

    for model_dir in sorted(outputs_dir.iterdir()):
        if not model_dir.is_dir():
            continue
        model_name = model_dir.name

        for out_file in sorted(model_dir.glob("*.json")):
            data = json.loads(out_file.read_text())
            sid  = data["scenario_id"]
            tier = data.get("tier", "")

            if tiers and not any(tier == t for t in tiers):
                continue

            wkey       = _weight_key(data)
            validators = data.get("validators", {})
            score, bkd = _compute_score(validators, wkey)

            # Verdict correctness — pipeline saves expected_verdict from llm_evaluation.
            # LLM emits "verdict" (Mode 1/2) or "root_cause_category" (Mode 3).
            llm_resp = data.get("llm_response", {})
            mode     = data.get("terraform_mode", 1)
            if mode == 3:
                actual_verdict = llm_resp.get("root_cause_category", "")
            elif "findings" in llm_resp:
                # Tier C multi-finding: collect per-resource verdicts
                actual_verdict = {
                    rid: f.get("verdict", "")
                    for rid, f in llm_resp["findings"].items()
                    if isinstance(f, dict)
                }
            elif "instances" in llm_resp:
                # Tier B multi-instance: collect per-instance verdicts
                actual_verdict = {
                    iid: inst.get("verdict", "")
                    for iid, inst in llm_resp["instances"].items()
                    if isinstance(inst, dict)
                }
            else:
                actual_verdict = llm_resp.get("verdict", "")

            model_results[model_name][sid] = {
                "scenario_id":    sid,
                "tier":           tier,
                "mode":           mode,
                "weight_key":     wkey,
                "expected_verdict": data.get("expected_verdict"),
                "actual_verdict":   actual_verdict,
                "score":          score,
                "breakdown":      bkd,
            }
            all_scenario_ids.add(sid)

    if not model_results:
        print("No outputs found. Run pipeline.py first.")
        return

    # Build leaderboard
    leaderboard = []
    for model_name, results in model_results.items():
        scores    = [r["score"] for r in results.values()]
        avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0

        # Per-weight-key averages
        by_wkey: dict[str, list] = defaultdict(list)
        for r in results.values():
            by_wkey[r["weight_key"]].append(r["score"])
        wkey_avgs = {
            wk: round(sum(v) / len(v), 2)
            for wk, v in sorted(by_wkey.items())
        }

        # Verdict correctness rate — covers all scenario types:
        #   scalar   (Tier A / D): direct string comparison
        #   dict     (Tier B multi-instance / Tier C multi-finding): ALL
        #            per-instance/finding verdicts must match expected
        def _verdict_correct(r: dict) -> bool | None:
            exp = r.get("expected_verdict")
            act = r.get("actual_verdict")
            if isinstance(exp, str) and isinstance(act, str):
                return act == exp
            if isinstance(exp, dict) and isinstance(act, dict):
                # exp: {id: {"expected_verdict": "OPTIMAL", ...}}
                # act: {id: "OPTIMAL"}
                for rid, exp_val in exp.items():
                    expected_v = exp_val.get("expected_verdict") if isinstance(exp_val, dict) else exp_val
                    if act.get(rid) != expected_v:
                        return False
                return True
            return None  # not scorable

        scorable = [r for r in results.values() if _verdict_correct(r) is not None]
        correct_verdicts = sum(1 for r in scorable if _verdict_correct(r))
        verdict_rate = (
            round(correct_verdicts / len(scorable) * 100, 1) if scorable else 0.0
        )

        leaderboard.append({
            "model":          model_name,
            "avg_score":      avg_score,
            "verdict_rate":   verdict_rate,
            "scenarios_run":  len(results),
            **{f"wkey_{k}": v for k, v in wkey_avgs.items()},
            "results":        results,
        })

    leaderboard.sort(key=lambda x: x["avg_score"], reverse=True)

    out = {"leaderboard": leaderboard, "total_scenarios": len(all_scenario_ids)}
    (results_dir / "leaderboard.json").write_text(json.dumps(out, indent=2))

    # Print legend
    print("""
┌─ SCORING LEGEND ──────────────────────────────────────────────────────────────┐
│  Avg       — weighted average score across ALL scenarios (0–100).             │
│              Each scenario is scored by summing validator scores × weights.    │
│              Higher is better.                                                 │
│                                                                                │
│  Verdict%  — % of scenarios where the model's verdict(s) exactly match the   │
│              expected. For single-verdict (Tier A/D): direct string match.    │
│              For multi-instance/multi-finding (Tier B/C): ALL per-instance or │
│              per-finding verdicts must match (scenario counted as correct only │
│              if every sub-verdict is right).                                   │
│                                                                                │
│  Per-column weight profiles (scenario type → validators used):                │
│  nl        — Tier A/B/C, no Terraform generated (nl_quality + behavior only)  │
│  tf        — Tier A, LLM generated Terraform (adds validate/plan/checkov/opa) │
│  multi_nl  — Tier B multi-instance, no Terraform generated                    │
│  multi_tf  — Tier B multi-instance, LLM generated Terraform                  │
│  c_multi_nl— Tier C multi-finding, no Terraform generated                     │
│  c_multi_tf— Tier C multi-finding, LLM generated Terraform                   │
│  3         — Tier D crash RCA (diagnosis_correct + nl_quality + behavior)     │
└───────────────────────────────────────────────────────────────────────────────┘""")

    # Print summary table
    wkeys = ["nl", "tf", "multi_nl", "multi_tf", "c_multi_nl", "c_multi_tf", "3"]
    header = f"{'Model':<25} {'Avg':>6} {'Verdict%':>9}"
    for wk in wkeys:
        header += f"  {wk:>8}"
    print(f"\n{header}")
    print("-" * (25 + 6 + 9 + 3 + len(wkeys) * 10))
    for entry in leaderboard:
        row = f"{entry['model']:<25} {entry['avg_score']:>6.1f} {entry['verdict_rate']:>8.1f}%"
        for wk in wkeys:
            val = entry.get(f"wkey_{wk}", "-")
            row += f"  {val:>8}" if isinstance(val, float) else f"  {'—':>8}"
        print(row)
    print(f"\nLeaderboard saved → {results_dir / 'leaderboard.json'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tiers", nargs="*", default=[], help="Filter by tier (empty = all)")
    args = parser.parse_args()
    run(args.tiers)
