"""
prompt_builder.py
Builds structured prompts for each mode from scenario data.
One template per mode, shared across all models.
Model-specific formatting is handled by the runners.

- Mode 1/2 (unified): LLM validates Agent 2 for any resource type (EC2 or non-EC2).
                      OPTIMAL  → NL explanation only, no Terraform.
                      SUBOPTIMAL / INCORRECT → NL explanation + LLM-generated Terraform.
- Mode 3: Crash RCA from StatusCheckFailed logs.
"""

import json
from typing import Any

# ============================================================================
# OUTPUT SCHEMA — enforced in every prompt
# ============================================================================

MODE1_2_SCHEMA = """{
  "verdict": "OPTIMAL | SUBOPTIMAL | INCORRECT",
  "decision_summary": {
    "action": "<STOP | DOWNSIZE | TERMINATE | KEEP | SET_RETENTION | DESTROY | GLACIER_TRANSITION | S3_ARCHIVAL | NONE — the concrete action>",
    "decided_by": "AGENT_VALIDATED | LLM_OVERRIDDEN",
    "rationale": "<one sentence explaining why this action over alternatives>"
  },
  "technical_explanation": "<detailed technical explanation of the finding and what drives the decision>",
  "cost_report": {
    "waste_evidence": "<key metrics confirming waste — e.g. p95_cpu=2.1%, stopped_days=18, unattached_days=12, log_gb=12.8>",
    "current_monthly_cost": <float>,
    "projected_monthly_cost": <float or null — post-action monthly cost; null if terminated/destroyed>,
    "monthly_savings": <float>,
    "annual_savings": <float>
  },
  "risk_assessment": {
    "risks": ["<concrete risk 1>", "<concrete risk 2>"],
    "requires_manual_verification": <true | false>,
    "verification_steps": "<specific steps the engineer must check before acting — null if no risks>"
  },
  "pipeline_warning_acknowledged": <true | false>,
  "data_loss_acknowledged": <true | false — true only for destructive actions with irreversible data loss>,
  "terraform_action": "NONE | SCRIPT_HANDLES | LLM_GENERATED",
  "terraform_block": "<full HCL block if terraform_action is LLM_GENERATED — null otherwise>"
}"""

MODE3_SCHEMA = """{
  "root_cause": "<precise technical root cause in one sentence>",
  "root_cause_category": "OOM_HEAP | OOM_KERNEL | DISK_FULL | CPU_CREDITS | CRASH_LOOP | NETWORK | OTHER",
  "severity": "P1_OUTAGE | P2_DEGRADED | P3_WARNING",
  "timeline_summary": "<chronological narrative of what the logs show happened>",
  "remediation": "<concrete actionable fix — specific, not generic>",
  "aws_specific_notes": "<any AWS-specific context relevant to the fix — null if none>",
  "risk_assessment": {
    "risks": ["<risk 1 — e.g. dependent services may be affected>"],
    "requires_manual_verification": <true | false>,
    "verification_steps": "<what the engineer must verify before applying the fix — null if none>"
  },
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
# SYSTEM PROMPT — Mode 1/2 unified (EC2 + non-EC2)
# ============================================================================

MODE1_2_SYSTEM = """You are an expert AWS FinOps engineer and infrastructure architect.

You receive a finding from an automated analysis agent (Agent 2) about AWS resources.
The resources may be an EC2 instance or a non-EC2 resources (CloudWatch log group, EIP, EBS volume, S3 bucket, etc.).
Agent 2 has already analysed the resource and made a cost-optimisation decision.

Your job is to:
1. Review Agent 2's decision critically — check for contradictions, missed context, or unsafe assumptions.
2. Decide whether the decision is OPTIMAL, SUBOPTIMAL, or INCORRECT.
3. If OPTIMAL: output the NL report only — no Terraform needed.
   If SUBOPTIMAL or INCORRECT: output the NL report AND generate corrective Terraform.

VERDICT DEFINITIONS:
- OPTIMAL       — Agent 2's decision is correct and the best possible action given the data.
                  This includes: CLEAN/no-action findings where the resource is already compliant,
                  and NEEDS_REVIEW cases where you agree no safe automated action exists.
                  Output: report only.
                    terraform_action = SCRIPT_HANDLES  (standard EC2 ops the automation script handles)
                    terraform_action = NONE            (resource already compliant — nothing to generate)
- SUBOPTIMAL    — Agent 2's decision is technically valid but a better solution exists,
                  OR Agent 2 issued NEEDS_REVIEW but you can identify a safe action it missed.
                  Output: report + your improved Terraform. terraform_action = LLM_GENERATED.
- INCORRECT     — Agent 2's decision contains a clear factual error or contradiction.
                  Output: report explaining the error + corrective Terraform (or NONE if the
                  correct action is to do nothing). terraform_action = LLM_GENERATED or NONE.

DECISION SUMMARY RULES:
- decided_by = AGENT_VALIDATED when verdict is OPTIMAL.
- decided_by = LLM_OVERRIDDEN when verdict is SUBOPTIMAL or INCORRECT.
- action must name the concrete operation:
    EC2:     STOP | DOWNSIZE | TERMINATE | KEEP
    Non-EC2: SET_RETENTION | DESTROY | GLACIER_TRANSITION | S3_ARCHIVAL |
             SET_LIFECYCLE | REMOVE_PROVISIONED_CONCURRENCY | NONE
- rationale must explain why this action and not an alternative.

COST REPORT RULES:
- waste_evidence: cite the specific metrics from the finding:
    EC2:           p95_cpu, stopped_days, waste_per_month
    Log groups:    log_gb_per_month, retention_days (or null), cost_per_month
    Ghost EIP/EBS: unattached_days, cost_per_month
    S3:            objects_older_than_90d_pct, estimated_savings_per_month
    NAT Gateway:   bytes_processed_avg_per_day, idle_days, cost_per_month
    ALB:           healthy_host_count, request_count_avg_per_day, idle_days, cost_per_month
    ECR:           untagged_images, total_size_gb, cost_per_month
    Lambda:        provisioned_concurrency, invocations_last_30d, cost_per_month
- current_monthly_cost: for EC2 use cost.current_cost_per_hour × 730; for non-EC2 use finding cost.
- projected_monthly_cost: null if destroyed; 0.0 for resources with no ongoing cost after action.
- monthly_savings and annual_savings: derive from the finding's cost data.

RISK ASSESSMENT RULES:
- List every concrete operational risk (data loss, pipeline dependency, traffic disruption).
- If pipeline_warning = true: add it to risks, set requires_manual_verification = true,
  and set pipeline_warning_acknowledged = true.
- For any destructive action (EBS delete, EIP release, NAT destroy, ALB destroy, ECR lifecycle):
  set requires_manual_verification = true. Set data_loss_acknowledged = true only when data is
  permanently deleted (EBS volumes, ECR images — not NAT/ALB/EIP which carry no stored data).
- verification_steps must be specific and actionable — not generic "check dependencies".
- If no risks exist: risks = [], requires_manual_verification = false, verification_steps = null.

TERRAFORM RULES — only when terraform_action = LLM_GENERATED:
  EC2 operations:
  - For STOP:      use aws_ec2_instance_state resource with state = "stopped".
  - For TERMINATE: remove the aws_instance block entirely and add a comment.
  - For DOWNSIZE:  change instance_type only — leave all other attributes unchanged.

  Non-EC2 operations:
  - For SET_RETENTION (CloudWatch):    add retention_in_days to the existing aws_cloudwatch_log_group.
  - For S3_ARCHIVAL (CloudWatch):      output aws_cloudwatch_log_subscription_filter + aws_s3_bucket +
                                       aws_iam_role + aws_iam_role_policy. Never hardcode account IDs —
                                       use data "aws_caller_identity" current {}.
  - For DESTROY (EIP / EBS):           output the resource block with lifecycle { prevent_destroy = false }.
  - For GLACIER_TRANSITION (S3):       output aws_s3_bucket_lifecycle_configuration as a separate resource.
  - For DESTROY (NAT Gateway):         output the aws_nat_gateway block with lifecycle { prevent_destroy = false }.
                                       Also include the associated aws_eip block if present.
  - For DESTROY (ALB / NLB):           output aws_lb with lifecycle { prevent_destroy = false }.
                                       Include dependent aws_lb_listener and aws_lb_target_group blocks.
  - For SET_LIFECYCLE (ECR):           output aws_ecr_lifecycle_policy as a separate resource.
                                       Rules must expire untagged images after 1 day and cap tagged
                                       image count to a reasonable limit (e.g. 30).
  - For REMOVE_PROVISIONED_CONCURRENCY (Lambda): remove the aws_lambda_provisioned_concurrency_config
                                       resource block entirely and add a comment explaining the removal.

  All generated Terraform:
  - Keep all existing tags and add: FinOpsAction = "<action>", FinOpsReviewed = "true".
  - Never remove encrypted = true if it exists.
  - Output complete, valid HCL — not snippets.

OUTPUT FORMAT:
Respond with ONLY valid JSON matching this schema — no prose before or after:
""" + MODE1_2_SCHEMA


# ============================================================================
# MULTI-INSTANCE SCHEMA + SYSTEM VARIANT (Tier B)
# ============================================================================

MULTI_INSTANCE_ADDENDUM = """
### Multi-instance reasoning rules
- Evaluate each instance independently, then check cross-instance safety.
- If Agent 2 marked an instance CLEAN (no action) and you agree: verdict = OPTIMAL, action = NONE, terraform_action = NONE.
- If you generate Terraform for an instance Agent 2 marked CLEAN, that is an error.
- For dependent_primary + dependent_secondary pairs: dependents must be actioned before their primary.
- State your intended execution order in each instance's decision_summary.rationale.
- Populate group_cost_report with the sum of savings across all actioned instances.
- Output a single JSON object with keys "instances", "group_summary", "group_cost_report", and "execution_order".
"""

MULTI_INSTANCE_SCHEMA = """{
  "instances": {
    "<instance_id>": {
      "verdict": "OPTIMAL | SUBOPTIMAL | INCORRECT",
      "decision_summary": {
        "action": "<STOP | DOWNSIZE | TERMINATE | KEEP | NONE>",
        "decided_by": "AGENT_VALIDATED | LLM_OVERRIDDEN",
        "rationale": "<one sentence — include position in execution order if relevant>"
      },
      "technical_explanation": "...",
      "cost_report": {
        "waste_evidence": "...",
        "current_monthly_cost": <float>,
        "projected_monthly_cost": <float or null>,
        "monthly_savings": <float>,
        "annual_savings": <float>
      },
      "risk_assessment": {
        "risks": ["..."],
        "requires_manual_verification": <true | false>,
        "verification_steps": "... | null"
      },
      "pipeline_warning_acknowledged": true | false,
      "terraform_action": "NONE | SCRIPT_HANDLES | LLM_GENERATED",
      "terraform_block": "... | null"
    }
  },
  "group_summary": "<overall narrative: all instances covered, execution order rationale, total savings>",
  "group_cost_report": {
    "total_monthly_savings": <float>,
    "total_annual_savings": <float>,
    "instances_actioned": <int>
  },
  "execution_order": ["<instance_id_first>", "<instance_id_second>", "..."]
}"""

MODE1_2_SYSTEM_MULTI = MODE1_2_SYSTEM.replace(
    "Respond with ONLY valid JSON matching this schema — no prose before or after:\n" + MODE1_2_SCHEMA,
    "Respond with ONLY valid JSON matching this schema — no prose before or after:\n" + MULTI_INSTANCE_SCHEMA
) + MULTI_INSTANCE_ADDENDUM


# ============================================================================
# MULTI-FINDING SCHEMA + SYSTEM VARIANT (Tier C multi-finding)
# ============================================================================

MULTI_FINDING_C_SCHEMA = """{
  "findings": {
    "<resource_id>": {
      "verdict": "OPTIMAL | SUBOPTIMAL | INCORRECT",
      "decision_summary": {
        "action": "<SET_RETENTION | DESTROY | GLACIER_TRANSITION | S3_ARCHIVAL | SET_LIFECYCLE | REMOVE_PROVISIONED_CONCURRENCY | NONE — the concrete action>",
        "decided_by": "AGENT_VALIDATED | LLM_OVERRIDDEN",
        "rationale": "<one sentence explaining why this action over alternatives>"
      },
      "technical_explanation": "<detailed technical explanation of the finding and what drives the decision>",
      "cost_report": {
        "waste_evidence": "<key metrics confirming waste>",
        "current_monthly_cost": <float>,
        "projected_monthly_cost": <float or null>,
        "monthly_savings": <float>,
        "annual_savings": <float>
      },
      "risk_assessment": {
        "risks": ["..."],
        "requires_manual_verification": <true | false>,
        "verification_steps": "... | null"
      },
      "data_loss_acknowledged": <true | false>,
      "terraform_action": "NONE | LLM_GENERATED",
      "terraform_block": "<full HCL block if terraform_action is LLM_GENERATED — null otherwise>"
    }
  },
  "group_summary": "<narrative covering all findings, total savings, key risks>",
  "group_cost_report": {
    "total_monthly_savings": <float>,
    "total_annual_savings": <float>,
    "findings_actioned": <int>
  }
}"""

MULTI_FINDING_C_ADDENDUM = """
### Multi-finding reasoning rules
- Evaluate each finding independently — do not conflate verdicts or Terraform between resources.
- If a finding is already compliant (OPTIMAL, no action needed): terraform_action = NONE, terraform_block = null.
- If a finding needs a fix (SUBOPTIMAL or INCORRECT): terraform_action = LLM_GENERATED, include the complete HCL block.
- Do NOT mix Terraform blocks between findings — each resource_id entry is self-contained.
- group_cost_report.findings_actioned = count of findings where terraform_action = LLM_GENERATED.
- Non-EC2 findings have no dependency order — evaluate them independently.
- Output a single JSON object with keys "findings", "group_summary", and "group_cost_report".
"""

MODE1_2_SYSTEM_MULTI_C = MODE1_2_SYSTEM.replace(
    "Respond with ONLY valid JSON matching this schema — no prose before or after:\n" + MODE1_2_SCHEMA,
    "Respond with ONLY valid JSON matching this schema — no prose before or after:\n" + MULTI_FINDING_C_SCHEMA
) + MULTI_FINDING_C_ADDENDUM


# ============================================================================
# USER PROMPT BUILDER — Mode 1/2 unified (Tier A, B, C)
# ============================================================================

def build_mode1_2_prompt(scenario: dict) -> tuple[str, str]:
    """
    Single builder for all Mode 1/2 scenarios (Tier A, B, C).

    Routing by scenario shape:
      - flagged_resources with >1 entry  → multi-instance user prompt (Tier B)
      - flagged_resources with 1 entry   → single-instance user prompt (Tier A)
      - no flagged_resources (finding)   → non-EC2 resource user prompt (Tier C)
    """
    resources = scenario.get("flagged_resources", [])

    # ── Tier B: multiple EC2 instances ──────────────────────────────────────
    if len(resources) > 1:
        blocks = []
        for r in resources:
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
        user = f"""## AGENT 2 FINDING — {scenario['scenario_id']} ({scenario.get('app_group_name', scenario.get('app_group', ''))})

{chr(10).join(blocks)}

### Current Terraform (entire group)
```hcl
{scenario['current_terraform']}
```

### Your task
Evaluate all instances above. Output your verdict per instance plus execution_order and group_summary.
"""
        return MODE1_2_SYSTEM_MULTI, user

    # ── Tier A: single EC2 instance ─────────────────────────────────────────
    if len(resources) == 1:
        resource = resources[0]
        dec  = resource["agent2_decision"]
        cost = resource["cost"]
        rels = resource.get("relationships", [])
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
        return MODE1_2_SYSTEM, user

    # ── Tier C multi-finding: multiple non-EC2 resources ───────────────────
    findings = scenario.get("findings", [])
    if len(findings) > 1:
        blocks = []
        for f in findings:
            rid = f["resource_id"]
            blocks.append(f"""
#### Resource: {rid}
**Finding:**
{json.dumps(f['finding'], indent=2)}

**Agent 2 decision:**
{json.dumps(f['agent2_decision'], indent=2)}

**Current Terraform:**
```hcl
{f['current_terraform']}
```
""")
        user = f"""## AGENT 2 FINDING — {scenario['scenario_id']} (multi-finding)

{chr(10).join(blocks)}

### Your task
Evaluate each finding independently. Output your verdict per resource_id in the findings dict,
plus group_summary and group_cost_report.
If a resource is already compliant (OPTIMAL): terraform_action = NONE.
If a resource needs a fix (SUBOPTIMAL/INCORRECT): terraform_action = LLM_GENERATED with the complete terraform_block.
"""
        return MODE1_2_SYSTEM_MULTI_C, user

    # ── Tier C: non-EC2 resource (no flagged_resources) ─────────────────────
    finding = scenario.get("finding", {})
    dec     = scenario.get("agent2_decision", {})
    user = f"""## AGENT 2 FINDING — {scenario['scenario_id']}

### Resource finding
{json.dumps(finding, indent=2)}

### Agent 2 decision
{json.dumps(dec, indent=2)}

### Current Terraform
```hcl
{scenario['current_terraform']}
```

### Your task
Review Agent 2's decision above. Decide OPTIMAL / SUBOPTIMAL / INCORRECT.
If OPTIMAL: output the report with terraform_action = NONE — do not generate Terraform.
If SUBOPTIMAL or INCORRECT: output the report and generate the corrective terraform_block.
"""
    return MODE1_2_SYSTEM, user


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
""" + MODE3_SCHEMA


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
    - Tier C → unified Mode 1/2 prompt (non-EC2 user prompt, same verdict logic)
    - Tier A/B → unified Mode 1/2 prompt; multi-instance variant when len(flagged_resources) > 1
    """
    tier = scenario.get("scenario_id", "")[0].upper()

    if tier == "D":
        return build_mode3_prompt(scenario)

    # Tier A / B / C — all handled by the unified builder
    return build_mode1_2_prompt(scenario)


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