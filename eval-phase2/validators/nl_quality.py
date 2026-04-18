"""
LLM-as-judge NL quality scorer.
Uses Claude (external, not under evaluation) to score on 6 dimensions.
"""
import json, subprocess
from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_PROMPTS_PATH = Path(__file__).resolve().parents[1] / "prompts" / "llm_judge_prompts.py"
_PROMPTS_SPEC = spec_from_file_location("llm_judge_prompts", _PROMPTS_PATH)
if _PROMPTS_SPEC is None or _PROMPTS_SPEC.loader is None:
    raise ImportError(f"Could not load prompt module at {_PROMPTS_PATH}")
_PROMPTS_MODULE = module_from_spec(_PROMPTS_SPEC)
_PROMPTS_SPEC.loader.exec_module(_PROMPTS_MODULE)
JUDGE_PROMPT_MODE_1_2 = _PROMPTS_MODULE.JUDGE_PROMPT_MODE_1_2
JUDGE_PROMPT_MODE_3 = _PROMPTS_MODULE.JUDGE_PROMPT_MODE_3

@dataclass
class ValidatorResult:
    name:    str
    passed:  bool
    score:   float          # 0.0 – 1.0
    details: str
    breakdown: dict         # per-dimension scores


def _call_judge(prompt: str) -> dict:
    import re
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude -p failed (rc={result.returncode}): {result.stderr[:200]}")
    raw = result.stdout.strip()
    # Strip markdown fences if present (claude -p sometimes wraps output)
    match = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
    if match:
        raw = match.group(1).strip()
    return json.loads(raw)


def validate(
    scenario: dict,
    llm_response: dict,
) -> ValidatorResult:
    mode = scenario.get("terraform_mode", 1)

    try:
        if mode == 3:
            prompt = JUDGE_PROMPT_MODE_3.format(
                expected_root_cause=scenario.get("expected_root_cause", "N/A"),
                log_lines="\n".join(scenario.get("log_lines", [])),
                llm_response=json.dumps(llm_response, indent=2),
            )
            dim_key = "diagnosis_accuracy"
        else:
            # Pass only the report fields to the judge — excludes terraform_block,
            # verdict, terraform_action, execution_order, and other non-NL structural keys.
            _NL_FIELDS = {
                "decision_summary",
                "technical_explanation",
                "cost_report",
                "risk_assessment",
                "group_summary",
                "group_cost_report",
            }
            nl_response = {k: v for k, v in llm_response.items() if k in _NL_FIELDS}
            if "instances" in llm_response:
                nl_response["instances"] = {
                    iid: {k: v for k, v in inst.items() if k in _NL_FIELDS}
                    for iid, inst in llm_response["instances"].items()
                }
            if "findings" in llm_response:
                nl_response["findings"] = {
                    rid: {k: v for k, v in f.items() if k in _NL_FIELDS}
                    for rid, f in llm_response["findings"].items()
                }

            llm_eval = scenario.get("llm_evaluation", {})
            # For multi-finding Tier C, aggregate key_facts from all per_finding entries
            if "per_finding" in llm_eval:
                key_facts = []
                for pf in llm_eval["per_finding"].values():
                    key_facts.extend(pf.get("key_facts", []))
                group_must = llm_eval.get("group_summary_must_mention", [])
                key_facts.extend(group_must)
            else:
                key_facts = llm_eval.get("key_facts", [])
            key_facts_str = "\n".join(f"  - {f}" for f in key_facts) if key_facts else "  (none provided)"

            prompt = JUDGE_PROMPT_MODE_1_2.format(
                expected_verdict=llm_eval.get("expected_verdict", "N/A"),
                expected_terraform_action=llm_eval.get("expected_terraform_action", "N/A"),
                key_facts=key_facts_str,
                llm_response=json.dumps(nl_response, indent=2),
            )
            dim_key = "key_facts_coverage"

        scores = _call_judge(prompt)

    except Exception as e:
        return ValidatorResult(
            name="nl_quality", passed=False, score=0.0,
            details=f"Judge call failed: {e}", breakdown={}
        )

    dims = [k for k in scores if k != "reasoning"]
    total = sum(scores.get(d, 0) for d in dims)
    max_possible = len(dims) * 5
    score = total / max_possible if max_possible > 0 else 0.0

    return ValidatorResult(
        name      = "nl_quality",
        passed    = score >= 0.6,
        score     = score,
        details   = scores.get("reasoning", ""),
        breakdown = {k: v for k, v in scores.items() if k != "reasoning"},
    )
