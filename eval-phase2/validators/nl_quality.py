"""
LLM-as-judge NL quality scorer.
Uses Claude (external, not under evaluation) to score on 5 dimensions.
"""
import json, os
from dataclasses import dataclass
import anthropic

@dataclass
class ValidatorResult:
    name:    str
    passed:  bool
    score:   float          # 0.0 – 1.0
    details: str
    breakdown: dict         # per-dimension scores


_JUDGE_PROMPT_MODE_1_2 = """\
You are an expert FinOps judge evaluating LLM-generated recommendations for AWS cost optimization.

Score the response below on exactly these 5 dimensions (0–5 each):

1. decision_faithfulness  — Does the LLM's action match what Agent 2 decided (or correctly override it with evidence)?
2. factual_correctness    — Are metrics, costs, and relationships cited accurately from the input?
3. completeness           — Are all flagged resources addressed? Are savings estimates included?
4. trust_paragraph        — Does it communicate relationship confidence warnings and derivation evidence clearly?
5. actionability          — Would an engineer know exactly what to do and why after reading this?

=== SCENARIO INPUT ===
{scenario_summary}

=== LLM RESPONSE ===
{llm_response}

Output ONLY valid JSON, no extra text:
{{
  "decision_faithfulness": <0-5>,
  "factual_correctness": <0-5>,
  "completeness": <0-5>,
  "trust_paragraph": <0-5>,
  "actionability": <0-5>,
  "reasoning": "<one sentence>"
}}
"""

_JUDGE_PROMPT_MODE_3 = """\
You are an expert evaluating LLM-generated crash root cause analysis for AWS EC2 instances.

Score the response below on exactly these 5 dimensions (0–5 each):

1. diagnosis_accuracy     — Does the root cause match the log evidence? Is it specific and correct?
2. factual_correctness    — Are log timestamps, error messages, and instance details cited accurately?
3. completeness           — Is the remediation suggestion present and appropriate (or correctly absent)?
4. trust_paragraph        — Does it warn about dependent instances affected by the remediation?
5. actionability          — Would an on-call engineer know exactly what to do next?

Expected root cause: {expected_root_cause}

=== LOG LINES ===
{log_lines}

=== LLM RESPONSE ===
{llm_response}

Output ONLY valid JSON, no extra text:
{{
  "diagnosis_accuracy": <0-5>,
  "factual_correctness": <0-5>,
  "completeness": <0-5>,
  "trust_paragraph": <0-5>,
  "actionability": <0-5>,
  "reasoning": "<one sentence>"
}}
"""


def _call_judge(prompt: str, judge_model: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    msg = client.messages.create(
        model=judge_model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    return json.loads(raw)


def validate(
    scenario: dict,
    llm_response: dict,
    judge_model: str = "claude-sonnet-4-6",
) -> ValidatorResult:
    mode = scenario.get("terraform_mode", 1)

    try:
        if mode == 3:
            inst = scenario.get("instance", {})
            prompt = _JUDGE_PROMPT_MODE_3.format(
                expected_root_cause=scenario.get("expected_root_cause", "N/A"),
                log_lines="\n".join(scenario.get("log_lines", [])),
                llm_response=json.dumps(llm_response, indent=2),
            )
            dim_key = "diagnosis_accuracy"
        else:
            resources = scenario.get("flagged_resources", [scenario.get("finding", {})])
            scenario_summary = json.dumps(
                {"expected_action": scenario.get("expected_action"),
                 "expected_llm_behavior": scenario.get("expected_llm_behavior"),
                 "resources": [r.get("agent2_decision", r) for r in (resources if isinstance(resources, list) else [resources])]},
                indent=2
            )
            prompt = _JUDGE_PROMPT_MODE_1_2.format(
                scenario_summary=scenario_summary,
                llm_response=json.dumps(llm_response, indent=2),
            )
            dim_key = "decision_faithfulness"

        scores = _call_judge(prompt, judge_model)

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
