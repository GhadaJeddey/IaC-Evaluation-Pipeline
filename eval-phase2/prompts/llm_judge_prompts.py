"""Prompt templates for the NL quality LLM judge."""

JUDGE_PROMPT_MODE_1_2 = """\
You are an expert FinOps judge evaluating the NATURAL LANGUAGE quality of an LLM-generated AWS cost-optimisation report.

THE REPORT STRUCTURE you are scoring has these fields:
  decision_summary      — action taken, decided_by, and rationale
  technical_explanation — detailed technical narrative
  cost_report           — waste evidence + savings numbers
  risk_assessment       — concrete risks + verification steps

YOUR SCOPE — score only the NL quality of these fields.

DO NOT consider:
  - Whether terraform_block is syntactically correct (it is excluded below).
  - Whether the decided_by value was the right call.

=== GROUND TRUTH — from scenario llm_evaluation ===
Expected verdict : {expected_verdict}
Expected terraform action: {expected_terraform_action}
Key facts the explanation MUST cover:
{key_facts}

Score on exactly these 6 dimensions (0–5 each):

1. key_facts_coverage
   Does technical_explanation address each key fact listed above?
   Award 5 if all facts are explicitly covered with correct details.
   Deduct 1 per missing or vague fact. Score 0 if explanation is generic boilerplate
   that could apply to any resource.

2. explanation_clarity
   Is technical_explanation specific to this resource and finding?
   Does it reference the actual resource name, utilisation metrics, and detection rationale
   from the input — not generic statements that could apply to any resource?

3. factual_grounding
   Are the numbers in cost_report (monthly_savings, annual_savings, current_monthly_cost)
   internally consistent? (e.g. annual_savings ≈ monthly_savings × 12)
   Is waste_evidence populated with specific metrics — not vague phrases like "underutilised"?
   Does technical_explanation cite concrete figures (CPU %, stopped days, log GB, cost/hr)?
   Vague or placeholder values score 0–1.

4. decision_rationale_quality
   Does decision_summary.rationale explain why this specific action over alternatives?
   Is it tied to the actual finding data — not a generic statement?
   For multi-instance: does group_summary cover all instances and state execution order?

5. risk_communication
   Does risk_assessment.risks name every concrete operational risk for this resource type?
   Are verification_steps specific and actionable?
   If pipeline_warning = true was in the input but risks is empty or vague: score ≤ 1.
   If there are genuinely no risks: risks = [] and verification_steps = null is acceptable (score 4–5).
   For destructive actions: data_loss_acknowledged must be true — if absent score ≤ 1.

6. completeness
   Single-instance: are all report fields present and non-trivial?
   Multi-instance: does group_cost_report aggregate total savings and instance count?
   If terraform_action is NONE but terraform_block is not null: penalise completeness (score ≤ 2).

=== LLM RESPONSE (terraform_block excluded — report fields only) ===
{llm_response}

Output ONLY valid JSON, no extra text:
{{
	"key_facts_coverage": <0-5>,
	"explanation_clarity": <0-5>,
	"factual_grounding": <0-5>,
	"decision_rationale_quality": <0-5>,
	"risk_communication": <0-5>,
	"completeness": <0-5>,
	"reasoning": "<one sentence on the main NL quality strength or gap>"
}}
"""

JUDGE_PROMPT_MODE_3 = """\
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
