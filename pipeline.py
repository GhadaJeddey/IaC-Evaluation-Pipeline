"""
Orchestrator — runs every model against every scenario tier.

Usage:
  python pipeline.py                         # all models, all tiers
  python pipeline.py --models qwen3-coder-32b
  python pipeline.py --tiers tier_a tier_d
  python pipeline.py --scenario A1           # single scenario

Re-evaluation mode (reads existing outputs, re-runs selected validators):
  python pipeline.py --reeval --validators behavior_correct nl_quality
  python pipeline.py --reeval --validators terraform_validate checkov opa
  python pipeline.py --reeval --models qwen3-coder-32b --scenario A1 --validators all
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
# Available validators — used for --reeval argument validation & dispatch
# ---------------------------------------------------------------------------

AVAILABLE_VALIDATORS = [
    "terraform_validate",
    "terraform_plan",
    "checkov",
    "opa",
    "execution_order",
    "nl_quality",
    "behavior_correct",
    "diagnosis_correct",
]

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

def _dict_to_hcl(block: dict) -> str:
    resource_type = "aws_instance"
    resource_name = None
    attrs = None

    raw_resource = block.get("resource")

    if isinstance(raw_resource, dict):
        # Shape: {"resource": {"aws_instance": "name", "ami": ..., ...}}
        # or:    {"resource": {"aws_instance": {"name": {...attrs}}}}
        inner = raw_resource
        # find the resource type key (e.g. "aws_instance")
        type_key = next((k for k in inner if k.startswith("aws_")), None)
        if type_key:
            resource_type = type_key
            name_or_attrs = inner[type_key]
            if isinstance(name_or_attrs, str):
                # name is the value, attrs are sibling keys
                resource_name = name_or_attrs
                attrs = {k: v for k, v in inner.items() if k != type_key}
            elif isinstance(name_or_attrs, dict):
                # {"aws_instance": {"resource_name": {...attrs}}}
                resource_name = next(iter(name_or_attrs))
                attrs = name_or_attrs[resource_name]
        else:
            return ""
    elif isinstance(raw_resource, str):
        resource_type = raw_resource
        resource_keys = [k for k in block if k != "resource"]
        if not resource_keys:
            return ""
        resource_name = resource_keys[0]
        attrs = block[resource_name]
    elif raw_resource is None:
        # Shape: {"aws_instance": {"resource_name": {...attrs}}} — no "resource" key at all
        type_key = next((k for k in block if k.startswith("aws_")), None)
        if type_key:
            resource_type = type_key
            name_or_attrs = block[type_key]
            if isinstance(name_or_attrs, dict):
                resource_name = next(iter(name_or_attrs))
                attrs = name_or_attrs[resource_name]
            else:
                return ""
        else:
            return ""
    else:
        # Original shape: {"resource": "aws_instance", "name": {...attrs}}
        resource_type = raw_resource
        resource_keys = [k for k in block if k != "resource"]
        if not resource_keys:
            return ""
        resource_name = resource_keys[0]
        attrs = block[resource_name]

    if not resource_name or not isinstance(attrs, dict):
        return ""

    # These dict keys are HCL map arguments (use = { }), not blocks (no =)
    MAP_ARGUMENTS = {"tags", "labels", "environment", "metadata", "annotations"}

    def _render_value(val):
        if isinstance(val, bool):
            return str(val).lower()
        elif isinstance(val, (int, float)):
            return str(val)
        else:
            return f'"{val}"'

    def _render_map(d: dict, indent: int) -> list[str]:
        pad = "  " * indent
        lines = ["{"]
        for k, v in d.items():
            lines.append(f"{pad}  {k} = {_render_value(v)}")
        lines.append(f"{pad}}}")
        return lines

    def _render_block(d: dict, indent: int) -> list[str]:
        pad = "  " * indent
        lines = []
        for k, v in d.items():
            if isinstance(v, dict):
                if k in MAP_ARGUMENTS:
                    # map argument: tags = { ... }
                    map_lines = _render_map(v, indent)
                    lines.append(f"{pad}{k} = " + map_lines[0])
                    lines.extend(f"{pad}{l}" if i > 0 else l
                                 for i, l in enumerate(map_lines[1:], 1))
                else:
                    # nested block: root_block_device { ... }
                    lines.append(f"{pad}{k} {{")
                    lines.extend(_render_block(v, indent + 1))
                    lines.append(f"{pad}}}")
            else:
                lines.append(f"{pad}{k} = {_render_value(v)}")
        return lines

    lines = [f'resource "{resource_type}" "{resource_name}" {{']
    lines.extend(_render_block(attrs, indent=1))
    lines.append("}")

    return "\n".join(lines)


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


def _run_validators(scenario: dict, llm_response: dict, workspace: Path,
                    selected: set[str] | None = None) -> dict:
    """
    Runs validators against an llm_response.

    Args:
        scenario:     The original scenario dict (used for context/metadata).
        llm_response: The parsed LLM output.
        workspace:    Path to the Terraform workspace.
        selected:     Set of validator names to run. None means run all applicable.
    """
    def _should_run(name: str) -> bool:
        return selected is None or name in selected

    mode     = scenario.get("terraform_mode", 1)
    tier     = scenario.get("_tier", "")
    llm_eval = scenario.get("llm_evaluation", {})

    # Resolve terraform content — convert dict → HCL if LLM returned wrong type
    _raw_tf   = llm_response.get("terraform_block")
    tf_action = llm_response.get("terraform_action", "")

    if isinstance(_raw_tf, dict):
        converted = _dict_to_hcl(_raw_tf)
        if converted:
            print(f"    [warn] terraform_block was a dict — converted to HCL")
            tf = converted
            llm_response = {**llm_response, "terraform_block": converted}
        else:
            print(f"    [warn] terraform_block dict could not be converted — skipping TF validators")
            tf = ""
    else:
        tf = _raw_tf if isinstance(_raw_tf, str) else ""
        # String may itself be a JSON-encoded dict — try to convert
        if tf and tf.strip().startswith("{"):
            try:
                import json as _json
                parsed_tf = _json.loads(tf)
                if isinstance(parsed_tf, dict):
                    converted = _dict_to_hcl(parsed_tf)
                    if converted:
                        print(f"    [warn] terraform_block was JSON string — converted to HCL")
                        tf = converted
                        llm_response = {**llm_response, "terraform_block": converted}
            except Exception:
                pass

    def _resolve_tf_block(block) -> str:
        """Normalize a terraform_block value to HCL string."""
        if isinstance(block, dict):
            return _dict_to_hcl(block) or ""
        if isinstance(block, str) and block.strip().startswith("{"):
            try:
                import json as _json
                parsed = _json.loads(block)
                if isinstance(parsed, dict):
                    return _dict_to_hcl(parsed) or block
            except Exception:
                pass
        return block if isinstance(block, str) else ""

    # Multi-instance Tier B: concatenate all per-instance terraform blocks
    if "instances" in llm_response:
        generated_blocks = [
            _resolve_tf_block(inst["terraform_block"])
            for inst in llm_response["instances"].values()
            if isinstance(inst, dict)
            and inst.get("terraform_action") == "LLM_GENERATED"
            and inst.get("terraform_block")
        ]
        generated_blocks = [b for b in generated_blocks if b.strip()]
        if generated_blocks:
            tf        = "\n\n".join(generated_blocks)
            tf_action = "LLM_GENERATED"

    # Multi-finding Tier C: concatenate all per-finding terraform blocks
    if "findings" in llm_response:
        generated_blocks = [
            _resolve_tf_block(f["terraform_block"])
            for f in llm_response["findings"].values()
            if isinstance(f, dict)
            and f.get("terraform_action") == "LLM_GENERATED"
            and f.get("terraform_block")
        ]
        generated_blocks = [b for b in generated_blocks if b.strip()]
        if generated_blocks:
            tf        = "\n\n".join(generated_blocks)
            tf_action = "LLM_GENERATED"

    results = {}

    # ── Terraform validators — only when LLM actually generated a block ──────
    tf_validators = {
        "terraform_validate": lambda: vars(terraform_validate.validate(tf, workspace)),
        "terraform_plan":     lambda: vars(terraform_plan.validate(tf, workspace)),
        "checkov":            lambda: vars(checkov_runner.validate(tf, workspace)),
        "opa":                lambda: vars(opa_runner.validate(tf, workspace)),
    }

    if tf_action == "LLM_GENERATED" and tf:
        for name, fn in tf_validators.items():
            if _should_run(name):
                results[name] = fn()
    elif tf_action == "LLM_GENERATED" and not tf:
        for name in tf_validators:
            if _should_run(name):
                results[name] = {
                    "name": name, "passed": False, "score": 0.0,
                    "details": "LLM_GENERATED declared but terraform_block is empty",
                }

    # ── Execution order — Tier B only ────────────────────────────────────────
    if tier == "tier_b" and _should_run("execution_order"):
        results["execution_order"] = vars(execution_order.validate(scenario, llm_response))

    # ── NL quality — every mode ──────────────────────────────────────────────
    if _should_run("nl_quality"):
        results["nl_quality"] = vars(nl_quality.validate(scenario, llm_response))

    # ── Behavior / verdict correctness ───────────────────────────────────────
    if mode == 3:
        if _should_run("behavior_correct"):
            expected_v = llm_eval.get("expected_root_cause_category", "")
            actual_v   = llm_response.get("root_cause_category", "")
            correct    = actual_v == expected_v
            results["behavior_correct"] = {
                "name": "behavior_correct", "passed": correct,
                "score": 1.0 if correct else 0.0,
                "details": f"expected={expected_v}  actual={actual_v}",
            }
        if _should_run("diagnosis_correct"):
            if "nl_quality" not in results:
                results["nl_quality"] = vars(nl_quality.validate(scenario, llm_response))
            diag_score = results.get("nl_quality", {}).get("breakdown", {}).get("diagnosis_accuracy", 0)
            results["diagnosis_correct"] = {
                "name": "diagnosis_correct", "passed": diag_score >= 3,
                "score": diag_score / 5.0,
                "details": f"diagnosis_accuracy={diag_score}/5",
            }

    elif "findings" in scenario:
        if _should_run("behavior_correct"):
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
        if _should_run("behavior_correct"):
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
            elif llm_eval.get("expected_verdict"):
                # All-CLEAN Tier B: single group-level verdict, no per_instance breakdown
                expected_v = llm_eval["expected_verdict"]
                inst_responses = llm_response.get("instances", {})
                all_optimal = all(
                    v.get("verdict") == expected_v
                    for v in inst_responses.values()
                ) if inst_responses else llm_response.get("verdict") == expected_v
                results["behavior_correct"] = {
                    "name": "behavior_correct", "passed": all_optimal,
                    "score": 1.0 if all_optimal else 0.0,
                    "details": f"expected={expected_v}  all_instances={'OK' if all_optimal else 'WRONG'}",
                }
            else:
                results["behavior_correct"] = {
                    "name": "behavior_correct", "passed": False,
                    "score": 0.0, "details": "No per_instance llm_evaluation defined",
                }

    else:
        if _should_run("behavior_correct"):
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
        return llm_eval.get("per_finding")
    if "per_instance" in llm_eval:
        return llm_eval.get("per_instance")
    return llm_eval.get("expected_verdict")


def _save_output(model_name: str, scenario_id: str, data: dict) -> None:
    out_dir = config.OUTPUTS_DIR / model_name
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{scenario_id}.json").write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Re-evaluation mode
# ---------------------------------------------------------------------------

def reeval(
    models: list[str],
    tiers: list[str],
    scenario_filter: str | None,
    selected_validators: set[str],
) -> None:
    """
    Reads existing outputs under config.OUTPUTS_DIR and re-runs the
    selected validators, merging results back into the saved file.
    """
    workspace = _ensure_workspace()

    all_scenarios: dict[str, dict] = {}
    for tier in tiers:
        path = config.SCENARIOS_DIR / f"{tier}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        for sid, s in data[tier]["scenarios"].items():
            s["_scenario_id"] = sid
            s["_tier"]        = tier
            all_scenarios[sid] = s

    outputs_root = config.OUTPUTS_DIR
    if not outputs_root.exists():
        print(f"[error] outputs directory not found: {outputs_root}")
        sys.exit(1)

    model_dirs = (
        [outputs_root / m for m in models]
        if models
        else [d for d in outputs_root.iterdir() if d.is_dir()]
    )

    print(f"\nRe-eval mode — validators: {sorted(selected_validators)}\n")

    for model_dir in model_dirs:
        if not model_dir.exists():
            print(f"[warn] no outputs found for model: {model_dir.name}")
            continue

        output_files = sorted(model_dir.glob("*.json"))
        if scenario_filter:
            output_files = [f for f in output_files if f.stem == scenario_filter]

        if not output_files:
            print(f"[skip] {model_dir.name} — no matching output files")
            continue

        print(f"{'='*60}")
        print(f"Model: {model_dir.name}  ({len(output_files)} file(s))")
        print(f"{'='*60}")

        for out_file in output_files:
            saved = json.loads(out_file.read_text())
            sid   = saved.get("scenario_id", out_file.stem)

            saved_tier = saved.get("tier", "")
            if tiers and saved_tier not in tiers:
                continue

            scenario = all_scenarios.get(sid)
            if scenario is None:
                print(f"  [{sid}] scenario definition not found — skipping")
                continue

            llm_response = saved.get("llm_response", {})
            if not llm_response:
                print(f"  [{sid}] empty llm_response — skipping")
                continue

            print(f"  [{sid}] re-running {sorted(selected_validators)} ...", end=" ", flush=True)

            def _normalize_tf(block):
                if isinstance(block, dict):
                    return _dict_to_hcl(block) or None
                if isinstance(block, str) and block.strip().startswith("{"):
                    try:
                        parsed = json.loads(block)
                        if isinstance(parsed, dict):
                            return _dict_to_hcl(parsed) or block
                    except Exception:
                        pass
                return block

            # Normalize top-level terraform_block
            raw_tf = saved["llm_response"].get("terraform_block")
            normalized = _normalize_tf(raw_tf)
            if normalized and normalized != raw_tf:
                saved["llm_response"]["terraform_block"] = normalized

            # Normalize per-finding terraform_blocks (Tier C multi-finding)
            for fid, f in saved["llm_response"].get("findings", {}).items():
                if isinstance(f, dict) and f.get("terraform_block"):
                    norm = _normalize_tf(f["terraform_block"])
                    if norm and norm != f["terraform_block"]:
                        saved["llm_response"]["findings"][fid]["terraform_block"] = norm

            llm_response = saved["llm_response"]

            new_validator_results = _run_validators(
                scenario, llm_response, workspace, selected=selected_validators
            )

            existing_validators = saved.get("validators", {})
            existing_validators.update(new_validator_results)
            saved["validators"] = existing_validators

            out_file.write_text(json.dumps(saved, indent=2))

            verdict_ok = existing_validators.get("behavior_correct", {}).get("passed", False)
            print(f"{'OK' if verdict_ok else 'WRONG_VERDICT'}  saved")

    print("\nDone. Run scorer.py to generate leaderboard.")


# ---------------------------------------------------------------------------
# Main loop (normal mode)
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

            out_path = config.OUTPUTS_DIR / model_name / f"{sid}.json"
            if out_path.exists():
                print(f"  [{sid}] already done — skipping")
                continue

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

            # LLM occasionally returns a JSON array instead of an object
            if not isinstance(llm_response, dict):
                print(f"MALFORMED_RESPONSE (got {type(llm_response).__name__}, expected dict)")
                llm_response = {}

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

    parser.add_argument(
        "--reeval", action="store_true",
        help="Re-evaluation mode: reads existing outputs and re-runs selected validators",
    )
    parser.add_argument(
        "--validators", nargs="+", metavar="VALIDATOR",
        help=(
            f"Validators to re-run in --reeval mode. Use 'all' to run every applicable "
            f"validator. Choices: {AVAILABLE_VALIDATORS}"
        ),
    )

    args = parser.parse_args()

    invalid = [m for m in args.models if m not in config.MODELS]
    if invalid:
        print(f"Unknown models: {invalid}\nAvailable: {list(config.MODELS.keys())}")
        sys.exit(1)

    if args.reeval:
        if not args.validators:
            print(
                "[error] --reeval requires --validators. "
                f"Choose from: {AVAILABLE_VALIDATORS} (or 'all')"
            )
            sys.exit(1)

        if "all" in args.validators:
            selected = set(AVAILABLE_VALIDATORS)
        else:
            unknown = [v for v in args.validators if v not in AVAILABLE_VALIDATORS]
            if unknown:
                print(f"Unknown validators: {unknown}\nAvailable: {AVAILABLE_VALIDATORS}")
                sys.exit(1)
            selected = set(args.validators)

        reeval(args.models, args.tiers, args.scenario, selected)
    else:
        run(args.models, args.tiers, args.scenario)
