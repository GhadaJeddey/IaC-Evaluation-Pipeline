# FinOps Intelligence Platform

## What This Is

An intelligent AWS cloud cost optimization platform built on a multi-agent architecture and
FinOps principles. A role-aware deterministic rules engine identifies EC2 waste and evaluates
blast radius before acting, an LLM reviews the bundle and generated Terraform for semantic
correctness, and nothing executes until a human provides a cryptographically signed approval.
Built for Cloud engineers who need actionable cost intelligence — not just alerts.

## Core Value

A human always approves before anything changes: the LLM explains and generates code,
the rules engine decides what is wasteful, but only a HMAC-SHA256 signed approval triggers
execution — with automatic rollback if anything goes wrong.

## Requirements

### Validated

(None yet — ship to validate)

### Active

**Agent 1 — Collector (CronJob, Python)**
- [ ] Collect EC2 instance metrics (CPU, network, state) via CloudWatch API
- [ ] Collect S3 bucket metadata (size, access patterns, lifecycle policies, object age distribution via list_objects_v2 sampling)
- [ ] Scan for ghost resources: unattached EIPs, orphaned EBS volumes, old snapshots
- [ ] Scan CloudWatch log groups: retention policy status, ingestion volume (IncomingBytes/day)
- [ ] Derive and persist resource relationships (controlled by `RELATIONSHIP_MODE=auto|manual`):
  - **Auto mode:** derive from AWS APIs — EBS attachment (`describe_volumes`) → `mounted_to`; ALB target groups (`describe_target_health`) → `routes_traffic_to`; IAM role policies (`get_policy_version`) → `sends_messages_to` / `reads_from_queue`; CloudWatch alarms (`describe_alarms`) → `monitored_by`; RDS metadata (`describe_db_instances`) → `replicates_to` / `failover_for`; snapshots (`describe_snapshots`) → `backup_of`; SG rule analysis → `reads_from` / `writes_to` (30% confidence); name patterns → `replicates_to` for EC2 app-level (30% confidence)
  - **Manual mode:** skip derivation entirely — trust existing `resource_relationships` table as-is
  - Confidence scores: 100% for declarative API sources; 30% for inferred (SG rules, name patterns)
- [ ] Derive EC2 instance role from relationships (`dependent_primary`, `dependent_secondary`, `backup`, `bursty`, `steady`) — always auto-derived; explicit `finops:role` tag acts as override only
- [ ] Write all collected data to TimescaleDB (PostgreSQL + TimescaleDB extension on Neon)

**Agent 2 — Analyse (CronJob, Python)**
- [ ] Role-aware EC2 detection (Phase 1 — in memory, no DB write):
  - `backup` / `dependent_secondary` → SKIP immediately; passed to Phase 2 with action = SKIP
  - `bursty` → peak CPU only over 14 days; if peak never exceeded 20% → DOWNSIZE; average CPU ignored
  - `dependent_primary` → tightened thresholds (avg CPU < 3% AND peak < 10% for 14 days → flag); max action capped at DOWNSIZE; STOP and TERMINATE never produced
  - `steady` (default) → three checks in order: zombie (stopped > 30 days + near-zero network → TERMINATE); idle (avg CPU < 5% + peak < 20% over 7 days → STOP); oversized (avg CPU < 20% + peak < 40% over 14 days → DOWNSIZE)
- [ ] Blast radius and guardrails (Phase 2 — one DB write at end):
  - Step 0: role re-check — backup/dependent_secondary → NEEDS_REVIEW, no fallback; dependent_primary → DOWNSIZE cap enforced
  - Step 1: Type E redundancy check — if any active resource has `replicates_to`, `failover_for`, or `backup_of` pointing at this target → NEEDS_REVIEW, no fallback (second safety net for wrong/missing role tags)
  - Step 2: recursive CTE upstream traversal (depth ≤ 3, excludes Type D) — Type A (stateful) → STOP/TERMINATE blocked, fallback DOWNSIZE if smaller type exists in pricing table; Type B (routing) → TERMINATE blocked if last instance behind LB, allowed if others exist; Type C (async) → action unchanged, `pipeline_warning = true`; Type D (observability) → excluded entirely
  - Step 3: nothing blocking → `safety_status = SAFE`, original action passes through
  - Single DB write to `waste` table: all Phase 1 + Phase 2 fields per resource
- [ ] Non-EC2 deterministic rules (thresholds stored in DB, configurable without code changes):
  - Log group no retention → SET_RETENTION finding
  - Log group high volume (>10GB/month) → S3_ARCHIVAL finding
  - Ghost resource confirmed after 48h → DESTROY finding
  - S3 cold data: >50% objects older than 90 days + no lifecycle policy → GLACIER_TRANSITION finding
- [ ] Topological sort (`graphlib.TopologicalSorter`) for safe execution ordering of dependent findings
- [ ] Write enriched findings to DB (`findings` table for non-EC2; `waste` table for EC2)

**Agent 2b — Crash Diagnosis (event-triggered, Python)**
- [ ] Fire on StatusCheckFailed CloudWatch alarm
- [ ] Pull last 500 CloudWatch log lines from affected instance
- [ ] Package as Mode 4 finding and hand off to Agent 3

**Agent 3 — Recommendation (LLM Job, on-demand)**
- [ ] App-group bundling: group EC2 findings and CloudWatch log group findings by app group (`resource_groups` where `group_type = app`) before LLM call — one LLM call per app group, not per instance; S3 and ghost resource findings passed individually
- [ ] LLM-decides approach: Agent 3 sends the LLM the current Terraform file + Agent 2's finding and suggested action; LLM determines the appropriate intervention — if Agent 2's action is implementable with a simple edit it outputs a script-level diff only, if architecture changes are needed it generates full Terraform and explains; no deterministic mode routing in Agent 3 code
- [ ] Standard findings (STOP / DOWNSIZE / TERMINATE / SET_RETENTION / GLACIER_TRANSITION / DESTROY): single LLM call receives waste bundle (metrics, role, relationships with confidence scores) + current Terraform; LLM outputs either a minimal diff or full rewrite + `trust_paragraph`; for SET_RETENTION findings, LLM infers correct retention period from log group context
- [ ] Crash RCA (Mode 4 — separate call): triggered by Agent 2b on `StatusCheckFailed` alarm; separate prompt and input (500 CloudWatch log lines); LLM first diagnoses root cause in plain language, then generates remediation Terraform only if diagnosis is sound
- [ ] LLM output always includes: Terraform change (diff or full rewrite), explanation of decision, `trust_paragraph` (relationship confidence warnings, derivation evidence, any corrections)
- [ ] Validation loop: `terraform init` → `terraform validate` → `terraform plan` → Checkov → OPA, max 3 retries before escalating to human (`status = escalated_to_human`, no PR opened)
- [ ] Redis recommendation cache: key = `sha256(finding_type + resource_id + metrics_snapshot)`, TTL = 24h
- [ ] Prompt templates stored in `prompt_templates` DB table (versioned, `is_active` flag)
- [ ] GitOps: after validation passes, commit generated Terraform to branch `remediation/<finding-id>` via PyGitHub; PR body includes finding summary, trust paragraph, and Checkov + OPA scan results

**Agent 4 — Action (Job, post-approval)**
- [ ] HMAC-SHA256 signed approval required before any execution
- [ ] Approval triggers Git PR merge (via GitHub API) — CI pipeline runs `terraform apply`; Agent 4 does not call `terraform apply` directly
- [ ] Agent 4 polls GitHub Actions API to monitor apply status; writes `APPLY_FAILED` finding on failure (no automatic rollback — human resolves; prior `.tfstate` snapshot stored in S3)
- [ ] Sequential execution with stability checks between steps
- [ ] Immutable audit log (append-only) for every action taken

**Infrastructure & Platform**
- [ ] FastAPI backend (REST API for dashboard + agent orchestration)
- [ ] React dashboard: findings list, cost savings counter, approval queue, crash alert feed, ghost resource map
- [ ] Redis for job queue and agent state
- [ ] AWS MCP Server integration (official, IAM-scoped read-only, replaces custom Cost Explorer wrapper)
- [ ] Single ENV variable switches dev/prod entirely
- [ ] Docker Compose for local development
- [ ] Kubernetes deployment (k3s, single node) — implemented if time allows after core agents are complete
- [ ] GitOps CI pipeline (GitHub Actions, self-hosted runner on same VM): PR triggers `terraform init` + `terraform plan` (diff posted as PR comment only — validation already passed in Agent 3); merge to main triggers `terraform apply` targeting LocalStack (`localhost:4566`) in dev, real AWS in prod
- [ ] Terraform remote state: S3 backend + DynamoDB lock table; separate state per finding branch; `.tfstate` snapshot stored in S3 before every apply
- [ ] Separate `infra/remediation/` folder for Agent 3-generated Terraform (isolated from platform infra state)
- [ ] `RELATIONSHIP_MODE=auto|manual` ENV variable — controls whether Agent 1 derives relationships from AWS APIs or trusts manually declared `resource_relationships` table

**Database Schema**
- [ ] Tables: instances (+ `role` field) · metrics (hypertable) · costs · rules · instance_prices · findings · recommendations · audit_log (immutable) · ghost_resources · logs_audit · resource_relationships · resource_groups · waste · prompt_templates

**LLM Evaluation Pipeline**
- [ ] Benchmark 3-4 models: Qwen3-32B (Groq), Llama 3.3 70B (Groq), Qwen 2.5 Coder 7B (Ollama)
- [ ] Scoring: terraform validate 15% · terraform plan 15% · Checkov 20% · OPA 15% · execution order 10% · NL quality 25%
- [ ] Select best model for prod deployment on Groq free tier

### Out of Scope

- **DynamoDB analysis** — Adds significant complexity (query pattern analysis, hot partition detection); EC2 covers 80% of cost waste story; post-PFA addition
- **LLM Modes 5-7** (log cleanup Terraform, ghost resource Terraform, DynamoDB redesign) — Variations of Modes 1-2; the 4 core modes demonstrate the full capability range; extend post-PFA
- **Log level / verbosity detection** — Requires parsing log content; high complexity, low reliability; retention + volume analysis covers the cost story adequately
- **Redundant log group consolidation** — Requires understanding service architecture; out of scope for automated analysis
- **ML-based anomaly detection** — Adds complexity without payoff until real production data exists; deterministic rules cover all demo cases
- **Multi-node Kubernetes** — 3-node topology described in architecture docs; implemented as single k3s node for demo; scaling is an operational concern not a feature
- **Full conversational cost bar** — AWS MCP Server provides query capability; full chat UI is a product feature beyond PFA scope
- **ELB/ALB idle detection** — Not free on LocalStack; EIPs + EBS + snapshots cover the ghost resource story without it; post-PFA addition
- **S3 Intelligent-Tiering** — Overlaps with Glacier Instant Retrieval rule; more complex, adds no demo value over the simpler lifecycle rule
- **S3 Glacier Deep Archive** — Valid for objects >180 days; post-PFA extension of the Glacier IR rule
- **Standalone semantic review agent (LangChain ReAct)** — Integrated into Agent 3's existing LLM call instead; separate agent adds orchestration overhead with no RPD benefit
- **VPC Flow Logs for relationship derivation** — Not free on LocalStack Community or AWS free tier; SG rule analysis (30% confidence) used as fallback for reads_from/writes_to

## Context

**Academic context:** Final-year engineering project (PFA), team of 4, ~2.5 months. Evaluated
on architecture quality and live demo. Priority: working demo of core agents > completeness of
peripheral features.

**Team strengths:** Python/backend, Cloud/AWS + Terraform, LLM/AI. Frontend and Kubernetes
are weaker areas — React dashboard uses component library (shadcn/ui) to minimize custom UI
work; K8s deferred to final sprint.

**Target environment:**
- Dev: LocalStack Community (EC2, S3, CloudWatch, SQS) + Docker Compose
- Prod/demo: Real AWS free tier account (Cost Explorer via real AWS, ~$0.01/request)
- LLM: Groq free tier (1,000 RPD) — hard 429 stop, never charges; Ollama as local fallback

**Competitive positioning:** Existing tools (AWS Trusted Advisor, CloudHealth, Spot.io,
Infracost) either alert without acting or act without explaining. This platform is the only
architecture where a deterministic rules engine decides, an LLM explains and generates
validated IaC, and a cryptographic gate enforces human approval before execution.

**Cost Explorer constraint:** Not available in LocalStack Community — mocked for local dev,
real AWS API for demo. AWS MCP Server covers most Cost Explorer use cases via boto3.

## Constraints

- **Timeline:** 2.5 months, 4 people — scope cuts are non-negotiable if agents 1-3 slip
- **Budget:** Everything free — Groq free tier (1,000 RPD), Neon free tier, AWS free tier, open-source tooling only; no paid APIs until free tier is exhausted
- **LLM rate limits:** Groq 1,000 RPD hard cap — Agent 3 must cache results; never retry autonomously on 429
- **AWS free tier:** EC2 t2.micro, 5GB S3 — demo environment sized accordingly
- **LocalStack Community:** Supports EC2, S3, CloudWatch, SQS — Cost Explorer not supported, must mock locally
- **Security:** Agent 4 requires HMAC-SHA256 signed approval on every action; IAM credentials are read-only for Agents 1-3, scoped write for Agent 4 only
- **Tech stack:** Python (agents), FastAPI (API), React + shadcn/ui (dashboard), PostgreSQL + TimescaleDB on Neon, Redis, Terraform OSS, Checkov, OPA, Docker/k3s

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| LLM never decides — only explains | Safety model: deterministic rules own all decisions; LLM is an explanation and code generation layer only. Prevents autonomous cost-saving actions with unintended consequences. | — Pending |
| LLM-decides approach (replaces hybrid mode routing) | No deterministic mode classifier in Agent 3 code. LLM receives current Terraform + Agent 2's finding and decides whether a simple diff or full rewrite is needed. Simpler Agent 3 code, same RPD cost (combined review pass was already happening in all modes), and LLM judgement on intervention scope is more robust than a hardcoded classifier. Crash RCA remains a separate call (different input: 500 log lines, different prompt). | — Pending |
| HMAC-SHA256 approval gate | Cryptographic proof of human intent. Prevents replay attacks, spoofed approvals, and accidental execution. Immutable audit trail for every action. | — Pending |
| Validation loop (Terraform → Checkov → OPA, max 3 retries) | Generated Terraform must pass security and policy checks before presenting to human. 3-retry cap prevents infinite loops on unfixable generations. | — Pending |
| TimescaleDB on Neon (not plain Postgres) | Time-series hypertables for metrics data are the right data model; trivial migration to local Postgres (pg_dump + change DATABASE_URL + install extension). | — Pending |
| AWS MCP Server instead of custom Cost Explorer wrapper | Official AWS-managed, free, IAM-scoped read-only. Eliminates a custom FastAPI wrapper that would need maintenance. | — Pending |
| K8s deferred to final sprint | No team member has strong K8s experience; Docker Compose covers all dev needs; K8s manifests (k3s) written after core agents complete. Code doesn't change. | — Pending |
| EC2-first scope | EC2 covers 80% of AWS cost waste story and is the richest demo scenario. S3 and CloudWatch logs add supporting cost data. Lambda and DynamoDB cut entirely. | — Pending |
| Cut to 4 LLM modes (from 7) | Modes 1-4 demonstrate the full capability range (simple/complex/risky/RCA). Modes 5-7 are variations. 4 working modes > 7 half-built modes at demo time. | — Pending |
| Log volume + S3 archival added to scope | CloudWatch Logs retention alone is incomplete — volume analysis (IncomingBytes/day) and S3 export Terraform generation close the full logs cost story at low implementation cost. | — Pending |
| GitOps for Terraform execution (Agent 3 opens PR, HMAC triggers merge, CI applies) | `terraform apply` never runs from Python code — only from CI after a human-approved merge. Keeps apply inside an auditable pipeline. Agent 4 watches CI status instead of running apply directly. GitHub Actions chosen over Atlantis to minimize operational overhead given timeline. | — Pending |
| No automatic Terraform rollback (replaced with APPLY_FAILED alert) | Full state-based rollback is high-risk and high-complexity within timeline. Prior `.tfstate` snapshot stored in S3; human resolves on failure. APPLY_FAILED status in audit log is sufficient for demo and architecture story. | — Pending |
| Role-based EC2 detection (steady/bursty/dependent_primary/dependent_secondary/backup) | Flat thresholds applied to all instances produce false positives — a batch job and a web server need different algorithms. Role-aware detection is more accurate and more defensible. | — Pending |
| Roles auto-derived from relationships; explicit tag is override only | Engineers forget to tag or tag incorrectly. Deriving role from `resource_relationships` (who replicates to/from this instance) is more reliable than trusting a human-declared tag. Tag mismatch logged as signal. | — Pending |
| Confidence scores on relationships (100% declarative, 30% inferred) | Not all relationships are equally certain. Declarative sources (EBS, ALB, IAM, RDS) are 100% reliable. Inferred sources (SG rules, name patterns) are 30%. Confidence controls Phase 2 behavior: 100% → hard block, 30% → pipeline_warning only. | — Pending |
| RELATIONSHIP_MODE=auto\|manual | Engineers may prefer to declare all relationships explicitly. Auto-derive mode derives from AWS APIs on every Agent 1 run. Manual mode trusts the existing table. Single ENV flag switches between them. | — Pending |
| App-group bundling scoped to EC2 + CloudWatch log groups only | S3 and ghost resources have no meaningful cross-resource app context. EC2 instances and log groups belong to named applications — bundling them gives the LLM the full application cost picture in one call, reducing Groq RPD usage. | — Pending |
| LLM combined review: bundle + Terraform in one pass | Separating semantic review from Terraform generation wastes RPD and loses context. Sending the waste bundle and generated Terraform together lets the LLM validate the code against the actual metrics and flag low-confidence relationship issues in the same call. | — Pending |
| CI pipeline simplified to terraform plan only | Agent 3 already runs full validation (init → validate → plan → Checkov → OPA) before opening the PR. Repeating these in CI on unchanged code is redundant. CI only runs terraform plan to produce the visible diff for the engineer. | — Pending |
| Self-hosted GitHub Actions runner on same VM | Runner needs direct network access to LocalStack at localhost:4566. GitHub-hosted runners can't reach a local service. Self-hosted runner on the k3s VM eliminates the need for tunneling and keeps credentials local. | — Pending |
| Semantic review as standalone agent — cut | Integrated into Agent 3's existing LLM call instead. Bundle + Terraform reviewed in one pass. No extra agent, no extra RPD cost, no extra orchestration complexity. | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd:transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-03 after initialization*
