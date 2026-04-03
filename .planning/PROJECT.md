# FinOps Intelligence Platform

## What This Is

An intelligent AWS cloud cost optimization platform built on a 4-agent multi-agent architecture
and FinOps principles. A deterministic rules engine identifies waste, an LLM explains findings
in plain language and generates validated Terraform remediation code, and nothing executes until
a human provides a cryptographically signed approval. Built for Cloud engineers who need
actionable cost intelligence — not just alerts.

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
- [ ] Collect S3 bucket metadata (size, access patterns, lifecycle policies)
- [ ] Collect Lambda function metrics (invocations, duration, errors)
- [ ] Scan for ghost resources: unattached EIPs, orphaned EBS volumes, idle ELBs, old snapshots
- [ ] Scan CloudWatch log groups: retention policy status, ingestion volume (IncomingBytes/day)
- [ ] Scan tag compliance via resourcegroupstaggingapi
- [ ] Write all collected data to TimescaleDB (PostgreSQL + TimescaleDB extension on Neon)

**Agent 2 — Analyse (CronJob, Python)**
- [ ] Deterministic rules engine (thresholds stored in DB, configurable without code changes):
  - Idle EC2: cpu_avg ≤ 10% + network ≤ 5MB/h for 14 days → stop recommendation
  - Oversized EC2: cpu_max ≤ 40% for 14 days → downsize recommendation
  - Underprovisioned EC2: cpu_max ≥ 90% for 7 days → upsize recommendation
  - Log group no retention → set policy recommendation
  - Log group high volume (>10GB/month) → S3 archival recommendation
  - Ghost resource confirmed after 48h → destroy plan recommendation
  - Untagged resource → tag enforcement recommendation
- [ ] Topological sort for safe execution ordering of dependent findings
- [ ] Write enriched findings to DB

**Agent 2b — Crash Diagnosis (event-triggered, Python)**
- [ ] Fire on StatusCheckFailed CloudWatch alarm
- [ ] Pull last 500 CloudWatch log lines from affected instance
- [ ] Package as Mode 4 finding and hand off to Agent 3

**Agent 3 — Recommendation (LLM Job, on-demand)**
- [ ] Hybrid LLM approach: simple actions (stop/resize/delete) → Python script modifies Terraform, LLM generates explanation only; complex actions → LLM generates full Terraform
- [ ] Mode 1 — Simple: stop, resize, delete (script-modified Terraform + LLM explanation)
- [ ] Mode 2 — Complex: new resource types, architecture changes (full LLM Terraform generation)
- [ ] Mode 3 — Risky: multi-resource or irreversible changes (LLM + extra validation pass)
- [ ] Mode 4 — Crash RCA: reads 500 log lines, explains root cause in plain language, generates remediation Terraform
- [ ] Validation loop: terraform validate → Checkov → OPA, max 3 retries before escalating to human
- [ ] Plain language business justification for every finding ("your /aws/lambda/payment-service is ingesting 40GB/month — exporting to S3 after 30 days saves ~$180/month")

**Agent 4 — Action (Job, post-approval)**
- [ ] HMAC-SHA256 signed approval required before any execution
- [ ] Sequential execution with stability checks between steps
- [ ] Automatic rollback on failure
- [ ] Immutable audit log (append-only) for every action taken

**Infrastructure & Platform**
- [ ] FastAPI backend (REST API for dashboard + agent orchestration)
- [ ] React dashboard: findings list, cost savings counter, approval queue, crash alert feed, ghost resource map, cost-by-tag view
- [ ] Redis for job queue and agent state
- [ ] AWS MCP Server integration (official, IAM-scoped read-only, replaces custom Cost Explorer wrapper)
- [ ] Single ENV variable switches dev/prod entirely
- [ ] Docker Compose for local development
- [ ] Kubernetes deployment (k3s, single node) — implemented if time allows after core agents are complete

**Database Schema**
- [ ] Tables: instances · metrics (hypertable) · costs · rules · instance_prices · findings · recommendations · audit_log (immutable) · ghost_resources · logs_audit · tag_map · tag_violations

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
- **S3 intelligent tiering / Glacier migration** — Valid FinOps optimization but outside EC2/Lambda/Logs focus for this milestone

## Context

**Academic context:** Final-year engineering project (PFA), team of 4, ~2.5 months. Evaluated
on architecture quality and live demo. Priority: working demo of core agents > completeness of
peripheral features.

**Team strengths:** Python/backend, Cloud/AWS + Terraform, LLM/AI. Frontend and Kubernetes
are weaker areas — React dashboard uses component library (shadcn/ui) to minimize custom UI
work; K8s deferred to final sprint.

**Target environment:**
- Dev: LocalStack Community (EC2, S3, Lambda, CloudWatch, SQS) + Docker Compose
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
- **AWS free tier:** EC2 t2.micro, 5GB S3, 1M Lambda invocations/month — demo environment sized accordingly
- **LocalStack Community:** Supports EC2, S3, Lambda, CloudWatch, SQS — Cost Explorer not supported, must mock locally
- **Security:** Agent 4 requires HMAC-SHA256 signed approval on every action; IAM credentials are read-only for Agents 1-3, scoped write for Agent 4 only
- **Tech stack:** Python (agents), FastAPI (API), React + shadcn/ui (dashboard), PostgreSQL + TimescaleDB on Neon, Redis, Terraform OSS, Checkov, OPA, Docker/k3s

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| LLM never decides — only explains | Safety model: deterministic rules own all decisions; LLM is an explanation and code generation layer only. Prevents autonomous cost-saving actions with unintended consequences. | — Pending |
| Hybrid LLM approach (script for simple, LLM for complex) | Simple actions (stop/resize) don't need LLM Terraform generation — a script is faster, cheaper, and more reliable. LLM reserved for genuinely complex cases. Reduces Groq RPD usage. | — Pending |
| HMAC-SHA256 approval gate | Cryptographic proof of human intent. Prevents replay attacks, spoofed approvals, and accidental execution. Immutable audit trail for every action. | — Pending |
| Validation loop (Terraform → Checkov → OPA, max 3 retries) | Generated Terraform must pass security and policy checks before presenting to human. 3-retry cap prevents infinite loops on unfixable generations. | — Pending |
| TimescaleDB on Neon (not plain Postgres) | Time-series hypertables for metrics data are the right data model; trivial migration to local Postgres (pg_dump + change DATABASE_URL + install extension). | — Pending |
| AWS MCP Server instead of custom Cost Explorer wrapper | Official AWS-managed, free, IAM-scoped read-only. Eliminates a custom FastAPI wrapper that would need maintenance. | — Pending |
| K8s deferred to final sprint | No team member has strong K8s experience; Docker Compose covers all dev needs; K8s manifests (k3s) written after core agents complete. Code doesn't change. | — Pending |
| EC2-first scope (Lambda/S3 secondary) | EC2 covers 80% of AWS cost waste story and is the richest demo scenario. Lambda and S3 add supporting data. DynamoDB cut entirely. | — Pending |
| Cut to 4 LLM modes (from 7) | Modes 1-4 demonstrate the full capability range (simple/complex/risky/RCA). Modes 5-7 are variations. 4 working modes > 7 half-built modes at demo time. | — Pending |
| Log volume + S3 archival added to scope | CloudWatch Logs retention alone is incomplete — volume analysis (IncomingBytes/day) and S3 export Terraform generation close the full logs cost story at low implementation cost. | — Pending |

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
