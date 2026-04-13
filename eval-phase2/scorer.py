"""
Reads all outputs/, applies per-mode weights, writes results/leaderboard.json.

Usage:
  python scorer.py
  python scorer.py --tiers tier_a tier_d
"""
import argparse, json
from pathlib import Path
from collections import defaultdict

import config


def _compute_score(validators: dict, mode: int) -> tuple[float, dict]:
    """
    Apply WEIGHTS[mode] to validator results.
    Returns (total_score_0_to_100, breakdown_dict).
    """
    weights   = config.WEIGHTS.get(mode, config.WEIGHTS[1])
    breakdown = {}
    total     = 0.0

    for key, weight in weights.items():
        result = validators.get(key, {})
        raw    = result.get("score", 0.0) if isinstance(result, dict) else 0.0
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

            mode        = data.get("terraform_mode", 1)
            validators  = data.get("validators", {})
            score, bkd  = _compute_score(validators, mode)

            model_results[model_name][sid] = {
                "scenario_id":       sid,
                "tier":              tier,
                "mode":              mode,
                "expected_behavior": data.get("expected_behavior"),
                "actual_behavior":   data.get("llm_response", {}).get("behavior"),
                "score":             score,
                "breakdown":         bkd,
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

        # Per-mode averages
        by_mode: dict[int, list] = defaultdict(list)
        for r in results.values():
            by_mode[r["mode"]].append(r["score"])
        mode_avgs = {f"mode_{m}": round(sum(v)/len(v), 2) for m, v in sorted(by_mode.items())}

        # Behavior correctness rate
        behavior_results = [
            r for r in results.values()
            if r.get("expected_behavior") is not None
        ]
        correct_behavior = sum(
            1 for r in behavior_results
            if r.get("actual_behavior") == r.get("expected_behavior")
        )
        behavior_rate = round(correct_behavior / len(behavior_results) * 100, 1) if behavior_results else 0.0

        leaderboard.append({
            "model":          model_name,
            "avg_score":      avg_score,
            "behavior_rate":  behavior_rate,
            "scenarios_run":  len(results),
            **mode_avgs,
            "results":        results,
        })

    leaderboard.sort(key=lambda x: x["avg_score"], reverse=True)

    out = {"leaderboard": leaderboard, "total_scenarios": len(all_scenario_ids)}
    (results_dir / "leaderboard.json").write_text(json.dumps(out, indent=2))

    # Print summary table
    print(f"\n{'Model':<25} {'Avg':>6} {'Behavior%':>10} {'Mode1':>7} {'Mode2':>7} {'Mode3':>7}")
    print("-" * 65)
    for entry in leaderboard:
        print(
            f"{entry['model']:<25} "
            f"{entry['avg_score']:>6.1f} "
            f"{entry['behavior_rate']:>9.1f}% "
            f"{entry.get('mode_1', 0):>7.1f} "
            f"{entry.get('mode_2', 0):>7.1f} "
            f"{entry.get('mode_3', 0):>7.1f}"
        )
    print(f"\nLeaderboard saved → {results_dir / 'leaderboard.json'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--tiers", nargs="*", default=[], help="Filter by tier (empty = all)")
    args = parser.parse_args()
    run(args.tiers)
