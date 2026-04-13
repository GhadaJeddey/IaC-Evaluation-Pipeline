"""
prompt_builder.py
Builds structured prompts for each mode from scenario data.
One template per mode, shared across all models.
Model-specific formatting is handled by the runners.

- Mode 1: LLM validates Agent 2
- Mode 2 : LLM performs crash RCA and suggests fix
"""

import json
from typing import Any

# ============================================================================
# OUTPUT SCHEMA — enforced in every prompt
# ============================================================================

MODE1_SCHEMA = """{
  "verdict": "OPTIMAL | SUBOPTIMAL | INCORRECT | NEEDS_REVIEW",
  "verdict_reason": "<one sentence explaining the verdict>",
  "technical_explanation": "<detailed technical explanation of the finding and decision>",
  "business_justification": "<business impact, cost savings, risk context>",
  "risk_notes": "<any risks, caveats, or warnings the operator should know — null if none>",
  "pipeline_warning_acknowledged": <true | false>,
  "terraform_action": "NONE | SCRIPT_HANDLES | LLM_GENERATED",
  "terraform_block": "<full HCL block if terraform_action is LLM_GENERATED — null otherwise>"
}"""

MODE2_SCHEMA = """{
  "root_cause": "<precise technical root cause in one sentence>",
  "root_cause_category": "OOM_HEAP | OOM_KERNEL | DISK_FULL | CPU_CREDITS | CRASH_LOOP | NETWORK | OTHER",
  "severity": "P1_OUTAGE | P2_DEGRADED | P3_WARNING",
  "timeline_summary": "<chronological narrative of what the logs show happened>",
  "remediation": "<concrete actionable fix — specific, not generic>",
  "aws_specific_notes": "<any AWS-specific context relevant to the fix — t3 credits, EBS limits, etc. — null if none>",
  "terraform_suggested": <true | false>,
  "terraform_block": "<HCL if an infra-level fix is appropriate — null otherwise>"
}"""


# ============================================================================
# SHARED HELPERS
# ============================================================================

def _format_relationships(relationships: list) -> str:
    if not relationships:
        return "None"
    lines = []
    for r in relationships:
        conf = int(r["confidence"] * 100)
        lines.append(
            f"  - {r['relationship_type']} → {r['target_resource_name']} "
            f"({r['target_resource_type']}, confidence: {conf}%, "
            f"source: {r['derivation_source']})"
        )
    return "\n".join(lines)


def _format_agent2_decision(dec: dict) -> str:
    fields = {
        "action":           dec.get("action"),
        "waste_type":       dec.get("waste_type"),
        "safety_status":    dec.get("safety_status"),
        "block_reason":     dec.get("block_reason"),
        "detection_reason": dec.get("detection_reason"),
    }
    if dec.get("recommended_type"):
        fields["recommended_type"]   = dec["recommended_type"]
        fields["projected_cpu_pct"]  = dec.get("projected_cpu_pct")
        fields["projected_ram_pct"]  = dec.get("projected_ram_pct")
    if dec.get("p95_cpu") is not None:
        fields["p95_cpu"]  = dec["p95_cpu"]
        fields["p99_cpu"]  = dec["p99_cpu"]
        fields["max_cpu"]  = dec["max_cpu"]
        fields["p95_ram"]  = dec.get("p95_ram")
        fields["cv"]       = dec.get("cv")
    if dec.get("stopped_days"):
        fields["stopped_days"]          = dec["stopped_days"]
        fields["network_out_bytes_avg"] = dec.get("network_out_bytes_avg")
    if dec.get("blast_radius"):
        fields["blast_radius"] = dec["blast_radius"]
    if dec.get("pipeline_warning"):
        fields["pipeline_warning"] = True
    if dec.get("redundancy_node"):
        fields["redundancy_node"] = True
    return json.dumps(fields, indent=4)


def _format_cost(cost: dict) -> str:
    lines = [f"  current:      ${cost['current_cost_per_hour']:.4f}/hr"]
    if cost.get("recommended_cost_per_hour"):
        savings_hr  = cost["current_cost_per_hour"] - cost["recommended_cost_per_hour"]
        lines.append(f"  recommended:  ${cost['recommended_cost_per_hour']:.4f}/hr")
        lines.append(f"  savings:      ${savings_hr:.4f}/hr  (${cost['waste_per_month']:.2f}/month)")
    else:
        lines.append(f"  waste:        ${cost['waste_per_month']:.2f}/month")
    return "\n".join(lines)


# ============================================================================
# SYSTEM PROMPT — Mode 1
# ============================================================================

MODE1_SYSTEM = """You are an expert AWS FinOps engineer and infrastructure architect.

You receive structured findings from an automated analysis agent (Agent 2) that has already analysed EC2 and S3 resource utilisation and made a cost-optimisation decision.

Your job is to:
1. Review Agent 2's decision critically — check for contradictions, missed context, or unsafe assumptions.
2. Decide whether the decision is OPTIMAL, SUBOPTIMAL or INCORRECT.
3. Produce a structured JSON response following the schema exactly.

VERDICT DEFINITIONS:
- OPTIMAL       — Agent 2's decision is correct and the best possible action given the data.
                  Output: NL explanation only. terraform_action = SCRIPT_HANDLES.
- SUBOPTIMAL    — Agent 2's decision is technically correct but a better solution exists.
                  Output: NL explanation + your improved Terraform. terraform_action = LLM_GENERATED.
- INCORRECT     — Agent 2's decision contains an error or contradiction that makes it inapplicable.
                  Output: NL explanation of the error + corrective Terraform. terraform_action = LLM_GENERATED.

TERRAFORM RULES (when terraform_action = LLM_GENERATED):
- Output a complete, valid HCL resource block.
- Keep all existing tags and add: FinOpsAction = "<action>", FinOpsReviewed = "true".
- Never remove encrypted = true if it exists.
- For STOP: use aws_ec2_instance_state resource with state = "stopped".
- For TERMINATE: remove the aws_instance resource block entirely and add a comment.
- For DOWNSIZE: change instance_type only — leave all other attributes unchanged.
- For new resources (S3 archival, log subscription etc): generate complete, minimal, secure HCL.

PIPELINE WARNING RULE:
If pipeline_warning = true, you MUST acknowledge it in risk_notes and set pipeline_warning_acknowledged = true.
A pipeline warning means the instance has a low-confidence relationship to another resource —
you may still proceed with the action but must flag the dependency explicitly.

OUTPUT FORMAT:
Respond with ONLY valid JSON matching this schema — no prose before or after:
""" + MODE1_SCHEMA


# ============================================================================
# USER PROMPT BUILDER — Mode 1 (single instance)
# ============================================================================

def build_mode1_prompt(scenario: dict) -> tuple[str, str]:
    """
    Returns (system_prompt, user_prompt) for a single-instance Mode 1 scenario.
    """
    resource = scenario["flagged_resources"][0]
    dec      = resource["agent2_decision"]
    cost     = resource["cost"]
    rels     = resource.get("relationships", [])

    user = f"""## AGENT 2 FINDING — {scenario['scenario_id']}

### Instance metadata
- instance_id:    {resource['instance_id']}
- instance_name:  {resource['instance_name']}
- instance_type:  {resource['instance_type']}
- role:           {resource['role']}
- status:         {resource['status']}
- os:             {resource['os']}
- region:         {resource['region']}
- environment:    {resource['environment']}

### Agent 2 decision
{_format_agent2_decision(dec)}

### Cost data
{_format_cost(cost)}

### Relationships
{_format_relationships(rels)}

### Current Terraform
```hcl
{scenario['current_terraform']}
```

### Your task
Review Agent 2's decision above. Output your verdict and explanation as JSON.
Remember: if terraform_action is LLM_GENERATED, include a complete valid terraform_block.
"""
    return MODE1_SYSTEM, user


# ============================================================================
# USER PROMPT BUILDER — Mode 1 (multi-instance / Tier B)
# ============================================================================

MULTI_INSTANCE_ADDENDUM = """
### Multi-instance reasoning rules
- Evaluate each instance independently, then check cross-instance safety.
- CLEAN instances must produce no Terraform and no action.
- If you generate Terraform for a CLEAN instance, that is an error.
- For dependent_primary + dependent_secondary pairs: primary must be actioned before secondary.
- State your intended execution order explicitly in each instance's verdict_reason.
- Output a single JSON object with key "instances" containing one entry per instance_id.
"""

MULTI_INSTANCE_SCHEMA = """{
  "instances": {
    "<instance_id>": {
      "verdict": "OPTIMAL | SUBOPTIMAL | INCORRECT | CLEAN",
      "verdict_reason": "...",
      "technical_explanation": "...",
      "business_justification": "...",
      "risk_notes": "...",
      "pipeline_warning_acknowledged": true | false,
      "terraform_action": "NONE | SCRIPT_HANDLES | LLM_GENERATED",
      "terraform_block": "... | null"
    }
  },
  "group_summary": "<overall summary covering all instances, total savings, execution order>",
  "execution_order": ["<instance_id_first>", "<instance_id_second>", "..."]
}"""

MODE1_SYSTEM_MULTI = MODE1_SYSTEM.replace(
    "Respond with ONLY valid JSON matching this schema — no prose before or after:\n" + MODE1_SCHEMA,
    "Respond with ONLY valid JSON matching this schema — no prose before or after:\n" + MULTI_INSTANCE_SCHEMA
) + MULTI_INSTANCE_ADDENDUM


def build_mode1_multi_prompt(scenario: dict) -> tuple[str, str]:
    """
    Returns (system_prompt, user_prompt) for a multi-instance Tier B scenario.
    """
    blocks = []
    for r in scenario["flagged_resources"]:
        dec  = r["agent2_decision"]
        cost = r["cost"]
        rels = r.get("relationships", [])
        blocks.append(f"""
#### Instance: {r['instance_id']} ({r['instance_name']})
- instance_type: {r['instance_type']}  role: {r['role']}  status: {r['status']}
- environment:   {r['environment']}    region: {r['region']}

Agent 2 decision:
{_format_agent2_decision(dec)}

Cost:
{_format_cost(cost)}

Relationships:
{_format_relationships(rels)}
""")

    user = f"""## AGENT 2 FINDING — {scenario['scenario_id']} ({scenario.get('app_group_name', scenario['app_group'])})

{chr(10).join(blocks)}

### Current Terraform (entire group)
```hcl
{scenario['current_terraform']}
```

### Your task
Evaluate all instances above. Output your verdict per instance plus execution_order and group_summary.
"""
    return MODE1_SYSTEM_MULTI, user


# ============================================================================
# USER PROMPT BUILDER — Mode 2 (LLM-generated Terraform, Tier C)
# ============================================================================

MODE2_SYSTEM = """You are an expert AWS FinOps engineer and infrastructure architect.

You receive a finding from an automated analysis agent about a non-EC2 AWS resource
(CloudWatch log group, EBS volume, EIP, S3 bucket, etc.).

Agent 2 has identified a cost problem but the fix requires generating new or substantially
modified Terraform that the script layer cannot handle automatically.

Your job is to:
1. Understand the finding and verify it makes sense.
2. Generate complete, production-safe Terraform that implements the recommended fix.
3. Provide a clear NL explanation of what you changed and why.

TERRAFORM RULES:
- Output complete, valid HCL — not snippets.
- For S3 lifecycle policies: use aws_s3_bucket_lifecycle_configuration (separate resource, not inline).
- For CloudWatch log subscription to S3: include aws_cloudwatch_log_subscription_filter +
  aws_s3_bucket + aws_iam_role + aws_iam_role_policy (minimum required resources).
- For retention policies: use aws_cloudwatch_log_group with retention_in_days set.
- For EBS/EIP destroy: output the resource block with a lifecycle { prevent_destroy = false } comment.
- Always add tag: FinOpsAction = "<action_type>".
- Never hardcode account IDs — use data "aws_caller_identity" current {}.

DATA LOSS WARNING:
For any destructive action (EBS delete, EIP release), you MUST include an explicit
data_loss_acknowledged field set to true in your JSON response, and explain the
irreversibility in business_justification.

OUTPUT FORMAT:
Respond with ONLY valid JSON — no prose before or after:
{
  "verdict": "OPTIMAL | SUBOPTIMAL | INCORRECT",
  "verdict_reason": "<one sentence>",
  "technical_explanation": "<what the finding means technically>",
  "business_justification": "<cost impact, savings, risk — include dollar amounts from the finding>",
  "risk_notes": "<warnings, irreversibility notes — null if none>",
  "data_loss_acknowledged": <true | false>,
  "terraform_action": "LLM_GENERATED",
  "terraform_block": "<complete HCL>"
}"""


def build_mode2_prompt(scenario: dict) -> tuple[str, str]:
    """
    Returns (system_prompt, user_prompt) for a Mode 2 Tier C scenario.
    """
    finding = scenario.get("finding", {})

    user = f"""## AGENT 2 FINDING — {scenario['scenario_id']}

### Finding
{json.dumps(finding, indent=2)}

### Current Terraform
```hcl
{scenario['current_terraform']}
```

### Your task
Generate the corrective Terraform and explain the change.
The current Terraform above shows the existing state — your terraform_block should
contain the full corrected or supplemented HCL.
"""
    return MODE2_SYSTEM, user


# ============================================================================
# SYSTEM PROMPT — Mode 3 (Crash RCA)
# ============================================================================

MODE3_SYSTEM = """You are an expert AWS site reliability engineer and Linux systems specialist.

You receive crash logs from an EC2 instance that has triggered a StatusCheckFailed alarm,
along with instance metadata, relationships, and the current Terraform definition.

Your job is to:
1. Analyse the log lines chronologically and identify the precise root cause.
2. Assess severity and the likely business impact.
3. Recommend a concrete, actionable remediation.
4. If the fix is infrastructure-level (instance resize, EBS expansion, instance type change),
   generate the corrective Terraform. If the fix is application-level (JVM heap, log rotation,
   config change), set terraform_suggested = false and explain the app-level fix in remediation.

ROOT CAUSE CATEGORIES:
- OOM_HEAP      — JVM/application heap exhaustion (OutOfMemoryError, GC overhead)
- OOM_KERNEL    — Linux kernel OOM killer (killed process, anon-rss)
- DISK_FULL     — No space left on device (filesystem full)
- CPU_CREDITS   — T3/T2 CPU credit exhaustion (CPUCreditBalance: 0)
- CRASH_LOOP    — Repeated process restart / systemd failure
- NETWORK       — Network connectivity / timeout cascade
- OTHER         — Does not fit above categories

SEVERITY:
- P1_OUTAGE   — Service is completely down (StatusCheckFailed, health check failing)
- P2_DEGRADED — Service is running but degraded (high latency, partial failures)
- P3_WARNING  — No current outage but trend is concerning

TERRAFORM RULES (when terraform_suggested = true):
- For instance resize: change instance_type only, keep all other attributes.
- For EBS expansion: change volume_size only in root_block_device.
- For instance type change (t3 → c5): change instance_type, add comment explaining why.
- Keep all existing tags, add: FinOpsAction = "crash-remediation".
- Never change AMI, subnet, or security group settings.

AWS-SPECIFIC KNOWLEDGE TO APPLY:
- T3/T2 instances use CPU credits. CPUCreditBalance = 0 means throttled to baseline (20% for t3.medium).
  Fix: switch to fixed-performance instance (c5, m5, etc.) — not to t3.unlimited (still credits).
- Linux OOM killer logs: "Out of memory: Kill process <pid>" — root cause is RAM exhaustion.
- Java OOM: GC pause > 98% time → GC overhead limit exceeded → JVM exits. Fix: more RAM or tune -Xmx.
- Disk full on /dev/xvda1: root volume. Fix: resize root_block_device volume_size in Terraform.

OUTPUT FORMAT:
Respond with ONLY valid JSON — no prose before or after:
""" + MODE2_SCHEMA


def build_mode3_prompt(scenario: dict) -> tuple[str, str]:
    """
    Returns (system_prompt, user_prompt) for a Mode 3 Tier D scenario.
    """
    instance  = scenario["instance"]
    log_lines = scenario["log_lines"]
    rels      = instance.get("relationships", [])

    logs_formatted = "\n".join(log_lines)

    user = f"""## CRASH REPORT — {scenario['scenario_id']}

### Instance metadata
- instance_id:   {instance['instance_id']}
- instance_name: {instance['instance_name']}
- instance_type: {instance['instance_type']}
- alarm:         {instance['alarm_name']}
- alarm_time:    {instance['alarm_trigger_time']}

### Relationships
{_format_relationships(rels)}

### Current Terraform
```hcl
{scenario['current_terraform']}
```

### Log lines (last recorded before crash)
```
{logs_formatted}
```

### Your task
Diagnose the crash. Identify root cause, severity, and remediation.
If the fix requires an infrastructure change, include terraform_block.
"""
    return MODE3_SYSTEM, user


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def build_prompt(scenario: dict) -> tuple[str, str]:
    """
    Dispatcher — returns (system_prompt, user_prompt) for any scenario.

    Routing rules:
    - Tier D → Mode 3 crash RCA prompt
    - Tier C → Mode 2 always (script layer cannot handle non-EC2 resources)
    - Tier A/B → Mode 1; multi-instance prompt when len(flagged_resources) > 1
    """
    tier = scenario.get("scenario_id", "")[0].upper()

    if tier == "D":
        return build_mode3_prompt(scenario)

    if tier == "C":
        return build_mode2_prompt(scenario)

    # Tier A / B — route on actual instance count, not tier label
    if len(scenario.get("flagged_resources", [])) > 1:
        return build_mode1_multi_prompt(scenario)
    return build_mode1_prompt(scenario)


# ============================================================================
# DEBUG HELPER
# ============================================================================

if __name__ == "__main__":
    import sys, pathlib

    scenario_file = sys.argv[1] if len(sys.argv) > 1 else None
    scenario_id   = sys.argv[2] if len(sys.argv) > 2 else None

    if not scenario_file:
        print("Usage: python prompt_builder.py <tier_json_file> <scenario_id>")
        sys.exit(1)

    data = json.loads(pathlib.Path(scenario_file).read_text())
    # Structure: {tier_x: {description: ..., scenarios: {...}}}
    tier_key = next(k for k in data if k.startswith("tier_"))
    scenarios = data[tier_key]["scenarios"]

    if scenario_id and scenario_id in scenarios:
        s = scenarios[scenario_id]
    else:
        s = next(iter(scenarios.values()))
        print(f"Using first scenario: {s['scenario_id']}")

    sys_p, usr_p = build_prompt(s)

    print("=" * 60)
    print("SYSTEM PROMPT")
    print("=" * 60)
    print(sys_p)
    print()
    print("=" * 60)
    print("USER PROMPT")
    print("=" * 60)
    print(usr_p)