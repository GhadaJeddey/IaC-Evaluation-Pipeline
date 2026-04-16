# Research Summary — FinOps Intelligence Platform

**Project:** FinOps Intelligence Platform (PFA)
**Domain:** AWS cloud cost optimization, multi-agent automation, LLM-assisted IaC generation
**Researched:** 2026-04-03
**Confidence:** MEDIUM (stack HIGH for core libs; LLM/LocalStack areas MEDIUM; Groq model availability LOW)

---

## Executive Summary

This is a multi-agent FinOps platform where a deterministic rules engine identifies AWS waste, an LLM explains findings and generates validated Terraform remediation, and a cryptographically signed human approval gate controls execution. The architecture follows a clear separation of concerns: rules own decisions, LLM owns explanation and code generation, HMAC owns execution safety. This division is not just a design preference — it is the defining characteristic that separates this platform from every existing tool in the market. AWS native tools (Trusted Advisor, Compute Optimizer, Cost Explorer) alert but never act. Commercial tools (CloudHealth, Spot.io, Harness) act but never explain in plain language, never generate validated IaC, and never use cryptographic approval gates. This platform occupies the gap between alert and audit-safe automated action.

The recommended build approach is: Python 3.12 agents, FastAPI backend, SQLAlchemy 2.0 async with TimescaleDB on Neon, LiteLLM over Groq (primary) and Ollama (fallback), Redis Streams for inter-agent coordination, and a four-gate validation loop (terraform init → validate → Checkov → OPA) for all LLM-generated Terraform. The hybrid LLM design — script-generated Terraform for simple actions (Modes 1), full LLM generation for complex changes (Modes 2-3), and log-based RCA (Mode 4) — is the right call. It minimizes Groq RPD consumption, maximises reliability on the most common finding types, and reserves LLM generation for cases where it adds irreplaceable value.

The top three risks are: (1) the demo environment diverges from LocalStack fixtures because LocalStack does not auto-generate CloudWatch metrics, does not emulate Cost Explorer, and does not enforce IAM — requiring a dedicated real-AWS dry-run week; (2) the Groq 1,000 RPD daily budget is exhausted before the live demo if caching is not implemented from day one; (3) the Checkov validation loop never produces a passing result on LLM-generated Terraform because false positives are not suppressed in `.checkov.yaml`, causing every finding to escalate to human and making the core automation story invisible at demo time.

---

## Stack Decisions

### Confirmed Technology Choices

The stack is well-defined in PROJECT.md and validated by research. No changes recommended. Highlights and critical rationale:

**Core technologies:**

- **Python 3.12** — required for SQLAlchemy 2.0 async improvements; do NOT use 3.13 (breaking issues with asyncpg at research cutoff)
- **FastAPI >= 0.111 + uvicorn[standard] >= 0.30** — ASGI server for REST API and WebSocket live dashboard; asyncpg as the underlying Postgres driver via SQLAlchemy async engine
- **boto3 >= 1.34 (synchronous)** — use synchronous boto3 for all agents; aiobotocore rejected due to version coupling risk against botocore; for Agent 2b parallel calls use ThreadPoolExecutor pattern, not aiobotocore
- **SQLAlchemy >= 2.0.30 (AsyncSession) + alembic >= 1.13** — ORM with async sessions; Alembic critical for hypertable migration (must run `CREATE EXTENSION timescaledb` as migration step 0, `create_hypertable` as step 1, before any data is inserted)
- **TimescaleDB on Neon** — metrics hypertable with 7-day chunks; continuous aggregate `metrics_daily` is the query target for Agent 2 rules (not raw hypertable); chunk interval column name must exactly match `time_bucket` queries or performance gains are silently lost
- **`groq` SDK >= 0.9 (primary) + `ollama` Python client (fallback)** — direct SDKs, no intermediary abstraction; thin wrapper function catches `groq.RateLimitError` and reroutes to Ollama; do NOT use LiteLLM (heavy dependency, fast-moving, redundant for a 2-provider setup) or LangChain (300+ transitive deps, agent loop fights deterministic architecture)
- **Redis >= 5.0 [hiredis] + Redis Streams** — at-least-once delivery with replay; preferred over pub/sub (at-most-once, no persistence) and DB polling (adds query load); stream carries only IDs, DB is source of truth for payload
- **Pure Python rules evaluator** — no external rules engine library; table-driven with DB-fetched thresholds; `graphlib.TopologicalSorter` (stdlib, Python 3.9+) for safe execution ordering
- **Terraform >= 1.8 + Checkov >= 3.2 + conftest/OPA >= 0.50** — four-gate validation pipeline; `terraform init` must run before `terraform validate` (LLM hallucinated provider versions cause init to fail, not validate)
- **pydantic >= 2.7 + pydantic-settings >= 2.3** — the single ENV switch (`ENVIRONMENT=dev|prod`) is implemented via `pydantic-settings`; one `aws_endpoint_url` field controls LocalStack vs real AWS across all boto3 clients
- **LocalStack Community 3.x** — dev environment; requires a central boto3 client factory that injects `endpoint_url` in dev; Cost Explorer completely absent (mock required at client-init layer, not call layer)

**What research ruled out explicitly:**
- aiobotocore, LangChain, LlamaIndex, moto, Celery Beat, Airflow, Sentinel, tfsec, terragrunt, `databases` library, tortoise-orm, direct OpenAI SDK for Ollama

**Version confidence note:** All versions verified against training data only (web lookup unavailable). Run `pip index versions <package>` before pinning. Groq model availability (especially Qwen3-32B on free tier) is LOW confidence — verify at `console.groq.com/models` before the benchmark phase.

---

## Architecture Decisions

### Confirmed Patterns

**Agent execution model:** K8s CronJob per scheduled agent (Agent 1, Agent 2), on-demand K8s Job per triggered agent (Agent 3, Agent 4). In Docker Compose dev, agents run as threads via `asyncio.to_thread(agent_main)` — same Redis Streams topology, no K8s objects. All CronJob manifests must set `concurrencyPolicy: Forbid` (default is `Allow` — double-run creates duplicate findings and metrics), `backoffLimit: 2`, `ttlSecondsAfterFinished: 3600`.

**Agent 2b trigger chain:** CloudWatch Alarm (StatusCheckFailed) → SNS → SQS → FastAPI `/webhooks/crash` → K8s Job (agent-crash-diagnosis). In Docker Compose dev, replace SQS with Redis List (LocalStack SQS long-poll has known inconsistencies).

**LLM mode routing:** LLM-decides approach — no deterministic mode classifier in Agent 3 code. LLM receives current Terraform + Agent 2's finding and decides the intervention scope itself. Three modes, determined by input type:
- **Mode 1 (validate):** Standard finding, Agent 2's action is correct → LLM writes NL explanation + trust paragraph only; script applies Terraform; no validation loop needed
- **Mode 2 (override):** Standard finding, Agent 2's action is wrong/incomplete → LLM generates corrective Terraform + explanation + trust paragraph; full validation loop (init → validate → plan → Checkov → OPA, max 3 retries)
- **Mode 3 (crash RCA):** Separate call triggered by Agent 2b on `StatusCheckFailed`; input is 500 CloudWatch log lines + metadata + relationships; LLM diagnoses root cause in plain language first, then generates remediation Terraform only if diagnosis is sound

**Validation loop order:** `terraform init` → `terraform validate` → `checkov` (soft-fail on LOW/MEDIUM only) → `conftest` (OPA). Errors from each gate are fed back into the next LLM prompt. Hard stop at 3 retries — write `escalated_to_human` finding and halt. No autonomous retry on 429.

**HMAC approval workflow:** Token payload must include `action_id`, `finding_id`, `action_type`, `resource_arn`, `terraform_plan_hash` (SHA-256 of the exact plan shown to approver), `approved_by`, `approved_at`. Verify with `hmac.compare_digest()` (never `==`). Enforce single-use via `UNIQUE` constraint on `approval_token_hash` in `audit_log` with `ON CONFLICT DO NOTHING` claimed inside a transaction before execution begins. Token TTL = 5 minutes in prod; use 30 minutes for demo day.

**Audit log immutability:** PostgreSQL trigger (`BEFORE UPDATE OR DELETE`) raises exception; RLS policy as second layer; `GRANT INSERT, SELECT` only to app user. Status progression uses a companion `audit_events` table (append-only child rows) rather than updating the parent `audit_log` row.

**LLM caching:** Cache key = `sha256(finding_type + resource_id + metrics_snapshot)`. Redis TTL = 24 hours. Cache at finding content level, not prompt level — a 14-day idle EC2 finding for the same resource produces the same recommendation across cycles until state changes.

**Prompt storage:** Prompts stored in `prompt_templates` DB table (versioned, `is_active` flag), not in source code strings. Enables prompt iteration without code deploys during the 2.5-month timeline.

---

## Scope Adjustments

### Additions Recommended by Research

**`.checkov.yaml` suppression file (add to Phase 3 scope):** PITFALLS.md identifies this as a demo-day failure mode. Common false positives on LLM-generated EC2 Terraform: `CKV_AWS_8` (IMDSv2), `CKV_AWS_135` (gp3 EBS), `CKV_AWS_126` (detailed monitoring), plus demo-irrelevant checks (`CKV_AWS_18`, `CKV_AWS_144`, `CKV2_AWS_62`). Build the suppression file in the same sprint as Checkov integration — not as a separate cleanup task.

**LocalStack seed script (add to Phase 1 scope, Week 1):** PITFALLS.md is emphatic: the seed script is as important as the agent code. Must create fixture EC2 instances, inject 14 days of CloudWatch metrics via `put_metric_data` (LocalStack does not auto-generate metrics), set explicit EBS attachment states, create log groups with retention/volume data, set tag compliance states. Build alongside Agent 1, not after it.

**Redis volume persistence (add to Docker Compose config):** Add named volume mount and `--appendonly yes` to the Redis service definition. Without it, every `docker compose down` invalidates the LLM recommendation cache and may trigger Groq calls on demo setup restarts.

**HMAC token TTL setting:** Set 30-minute TTL for demo day (not the 5-minute production default). Add a visible countdown timer in the React dashboard approval UI. This prevents the most common demo failure: token expiry during the live approval walkthrough.

### Confirmed Out of Scope (no changes from PROJECT.md)

Multi-account, RI/SP purchase recommendations, DynamoDB analysis, ML anomaly detection, conversational UI, S3 Glacier migration, scheduled start/stop automation. All correctly deferred. LLM Modes 5-7 correctly cut. K8s multi-node correctly deferred to final sprint.

### Scope Clarification: ELB Idle Detection

PITFALLS.md flags that ALBv2 health check state is not realistically simulated in LocalStack Community. The recommendation: either mock ELB idle detection entirely for the dev/demo scenario, or skip it in the demo script and document it as a known LocalStack limitation. Do not spend implementation time on a finding type that cannot be demonstrated meaningfully in the target environment.

---

## Critical Risks

### Risk 1: Dev/Demo Environment Divergence (Severity: CRITICAL)

The demo runs against real AWS. LocalStack does not enforce IAM, does not auto-generate CloudWatch metrics, does not support Cost Explorer, and simulates EBS/EIP state transitions differently from real AWS. Every agent code path tested against LocalStack must be verified against real AWS at least one week before the demo. If this dry run is scheduled on the day before, it will find environment-specific failures with no time to fix them.

**Mitigation:** Allocate Week 9 (of 10) as a dedicated real-AWS dry run week. This is not optional. Also: build the LocalStack seed script in Week 1 alongside Agent 1 — without it, Agent 1 collects empty metrics and Agent 2 produces zero findings, making all downstream agents untestable during the first half of development.

### Risk 2: Groq 1,000 RPD Budget Exhaustion (Severity: HIGH)

The 1,000 RPD limit is per-account, shared across all models. The benchmark phase (3-4 models × test suite) can consume the entire day's budget before any agent runs. On demo day, any exploratory call, debug session, or autonomous retry loop exhausts the budget and the live demo silently produces no LLM recommendations — the core feature disappears.

**Mitigation:** Implement Redis recommendation caching from the start of Agent 3 development. Cache key must be content-based (`sha256(finding_type + resource_id + metrics_snapshot)`), not prompt-based. Pre-generate and cache all demo-scenario recommendations the day before the demo. Run benchmarks in off-hours windows with a hard RPD counter. Never allow autonomous retry on 429 — hard stop and write a `groq_rate_limited` status to the findings table.

### Risk 3: Validation Loop Never Passing (Severity: HIGH)

If `.checkov.yaml` suppression is not in place, Checkov will fail on every LLM-generated Terraform file due to false positives (`CKV_AWS_8`, `CKV_AWS_135`, etc.). The loop hits 3 retries, escalates to human, and the demo shows the failure path on every finding. The success path — validated Terraform presented to the approver — never appears.

**Mitigation:** Build `.checkov.yaml` in the same sprint as Checkov integration (not later). Test the full validation loop (init → validate → plan → Checkov → OPA → PASS) against a known-good, hand-written Terraform file before integrating LLM generation. The loop must prove it can pass before it is trusted with LLM output. Keep OPA policies to 3-5 high-value rules — complex Rego that is wrong is worse than no Rego.

### Risk 4: Schema Migration Not Applied to Production Database (Severity: HIGH)

Development happens against a local or Neon dev branch. The production Neon branch accumulates schema lag. On demo day, Agent 1 writes to a column that does not exist in production and crashes with a PostgreSQL error. TimescaleDB compounds this: if the `metrics` table is dropped and recreated during development iteration, it loses hypertable status silently.

**Mitigation:** Use Alembic exclusively — no manual schema changes ever. Run `alembic upgrade head` against the production Neon database as the first step of every deployment. Keep `alembic history` linear with no branching migrations. Use `IF NOT EXISTS` guards on `create_hypertable` and check whether the table is already a hypertable before calling it.

### Risk 5: HMAC Token Expiry During Live Demo Approval (Severity: MEDIUM)

If the demo approval walkthrough takes longer than the token TTL (default: 5 minutes), Agent 4 rejects the token with a security error on the core feature. This is especially likely when walking an audience through the UI for the first time.

**Mitigation:** Set token expiry to 30 minutes for demo day. Add a visible countdown timer in the approval UI. Rehearse the full approval flow and time it at least twice before the live demo. Have a second pre-cached recommendation ready if the first token expires.

---

## Open Questions

These must be resolved before or during Phase 1 — they affect architectural decisions, not just implementation details.

1. **Which Groq model will be used in production?** Qwen3-32B on Groq is LOW confidence (unverified at research cutoff). Verify at `console.groq.com/models` before designing the benchmark. If Qwen3-32B is unavailable on the free tier, Llama 3.3 70B is the fallback — this is fine, but the benchmark scope changes.

2. **Will LocalStack seed fixtures cover all demo scenarios end-to-end?** The seed script must simulate: at least one idle EC2 instance (14 days of CPU ≤ 10%), at least one oversized instance (14 days of CPU ≤ 40%), at least one ghost EBS/EIP, at least one log group with no retention policy, at least one untagged resource, and one instance with a StatusCheckFailed alarm for Agent 2b. Confirm this scope in the first sprint before writing agent code.

3. **What is the Neon free tier's current concurrent connection limit?** Research data says 5 connections per branch. Verify at `neon.tech` — this directly constrains SQLAlchemy pool configuration (`pool_size`, `max_overflow`). If the limit has increased, the pool settings can be relaxed.

4. **Does the AWS MCP Server require a separate IAM role/user, or does it inherit the Collector's read-only role?** The MCP Server must be scoped to `ReadOnlyAccess` + specific Cost Explorer permissions. This is a security model question that affects IAM policy setup in the real AWS environment (Week 9 dry run).

5. **What is the Agent 1 collection interval?** The CronJob manifest shows `0 */6 * * *` (every 6 hours) in architecture research. PROJECT.md says "every N minutes." Clarify: 6 hours is appropriate for a demo with static LocalStack fixtures; it may be too infrequent to demonstrate real-time behavior during the live demo. Consider a shorter interval (15-30 minutes) for the demo environment.

---

## Roadmap Implications

### Suggested Phase Structure

Based on dependency ordering from research: data pipeline must exist before rules, rules must produce findings before LLM can process them, LLM must produce recommendations before the approval gate can execute anything, and the demo environment must be validated against real AWS before presenting. The React dashboard can develop in parallel with agents after the API contract is established.

---

**Phase 1 — Foundation and Data Pipeline**

Rationale: Nothing else is possible without data. Agent 1 must collect real metrics before Agent 2 can detect waste. TimescaleDB schema, LocalStack fixtures, and the environment switch must be in place before any agent code is written. The seed script belongs here, not later.

Delivers: Agent 1 (Collector) fully functional against LocalStack; TimescaleDB schema with hypertable and continuous aggregate; LocalStack seed script covering all demo scenarios; single ENV switch wiring dev/prod; Docker Compose environment running.

Addresses: EC2/CloudWatch collection, S3 metadata, ghost resource scanning, log group scanning.

Must avoid: Building Agent 1 without the seed script (leaves Agent 2 untestable); skipping `CREATE EXTENSION timescaledb` as migration step 0; hardcoding LocalStack endpoint URL anywhere outside the boto3 client factory.

Research flag: Standard patterns — skip research phase. boto3 + TimescaleDB + Alembic are well-documented.

---

**Phase 2 — Rules Engine and Findings**

Rationale: Agent 2 depends on Agent 1's data. The rules engine must be validated against known metric scenarios before Agent 3 is built — if findings are not being generated correctly, LLM recommendations will be wrong regardless of model quality.

Delivers: Agent 2 deterministic rules evaluator (all 7 rules); topological sort for execution ordering; Agent 2b crash diagnosis trigger chain; findings written to DB.

Addresses: All finding types: idle EC2, oversized EC2, underprovisioned EC2, log retention, log volume, ghost resources (after 48h).

Must avoid: Querying raw metrics hypertable instead of `metrics_daily` continuous aggregate; forgetting to load threshold config from DB at agent startup (warm-up query required); allowing Agent 1 and Agent 2 CronJobs to overlap (concurrencyPolicy: Forbid).

Research flag: Standard patterns for rules engine. Agent 2b's SQS trigger chain may need light research on SNS-to-SQS subscription setup if team is unfamiliar.

---

**Phase 3 — LLM Recommendation Engine**

Rationale: Agent 3 is the most complex component and the longest to get right. It depends on validated findings from Phase 2. The validation loop (init → validate → Checkov → OPA) must be built and tested against hand-written Terraform before LLM generation is added — this is the sequencing that prevents the "loop never passes" demo risk.

Delivers: direct `groq`/`ollama` SDK integration with fallback wrapper; Mode 1 (script + LLM explanation); Mode 2 (full LLM Terraform); Mode 3 (risky, extra validation); Mode 4 (crash RCA, 2-call pattern); validation loop with `.checkov.yaml` suppression; Redis recommendation cache; prompt templates in DB; LLM benchmark pipeline.

Addresses: All recommendation modes, plain-language business justifications with dollar amounts, IaC generation, pre-validated Terraform presented to approver.

Must avoid: Building the validation loop without `.checkov.yaml` from the start; allowing autonomous retry on Groq 429; setting HTTP timeout below 60 seconds for Groq (cold-start latency can reach 30 seconds); using `==` for HMAC digest comparison (use `hmac.compare_digest`).

Research flag: LLM validation loop and OPA policy writing warrant a research phase. OPA Rego is non-trivial to debug; limit to 3-5 high-value policies and test each independently with `opa eval` before integration.

---

**Phase 4 — Approval Gate, Execution, and Dashboard**

Rationale: Agent 4 depends on Agent 3's validated recommendations. The HMAC approval workflow and React dashboard can be developed in parallel once the Agent 3 API contract is defined (recommendation schema, approval endpoint signature). Dashboard development should start in Phase 3 but the approval flow completes in Phase 4.

Delivers: HMAC-SHA256 approval token generation and verification; single-use enforcement via `audit_log` UNIQUE constraint; Agent 4 sequential execution with stability checks; automatic rollback on failure; immutable `audit_log` table with trigger enforcement; React dashboard (findings list, cost savings counter, approval queue, crash alert feed, ghost resource map, cost-by-tag view); WebSocket live updates; AWS MCP Server integration.

Addresses: Every table stakes feature: audit trail, savings quantification visible in dashboard, cost-by-tag view, approval workflow.

Must avoid: HMAC token not binding `terraform_plan_hash` to the payload (approver sees X, agent executes Y); token expiry window too short for demo (use 30-minute TTL); CORS not configured for the actual demo host IP; WebSocket reconnection not handled (use `reconnecting-websocket`).

Research flag: Standard patterns for HMAC and PostgreSQL triggers. FastAPI WebSocket patterns are well-documented. React dashboard with shadcn/ui is straightforward given team's plan to use component library.

---

**Phase 5 — Real AWS Validation and Demo Hardening**

Rationale: This phase is not optional. Every assumption made about LocalStack behavior must be verified against real AWS. IAM policies must be tested for correctness (LocalStack does not enforce IAM). The Groq budget must be pre-allocated for the demo. This phase should start no later than Week 9 of 10.

Delivers: Full agent pipeline running against real AWS free tier; IAM policies validated (read-only for Agents 1-3, scoped write for Agent 4); all demo-scenario recommendations pre-generated and cached in Redis; Alembic migration applied to production Neon; CORS configured for demo machine; k3s deployment manifests (if time allows after core pipeline is validated).

Addresses: All pitfalls related to demo environment divergence. K8s manifests are the "if time allows" deliverable — Docker Compose is the demo fallback.

Must avoid: Skipping real AWS dry run; running any exploratory Groq calls on demo day before the live presentation; deploying with the HMAC secret set differently across services (invalidates all pending approval tokens).

Research flag: K8s deployment warrants review if the team is proceeding with k3s. Standard patterns otherwise.

---

### Phase Ordering Rationale

- Data before rules: Agent 2 has nothing to evaluate without Agent 1's metrics and the seed fixture data.
- Rules before LLM: Agent 3 must receive confirmed, correctly formed findings to generate meaningful recommendations. Connecting Agent 3 to Agent 2 before Agent 2 is validated produces garbage-in/garbage-out LLM calls that waste the Groq RPD budget.
- Validation loop before LLM generation: The four-gate pipeline must prove it can pass on known-good Terraform before LLM output is trusted to it. This ordering is the single most important sequencing decision for Phase 3.
- Dashboard in parallel: The React dashboard API contract (finding schema, recommendation schema) is established in Phase 2/3. Dashboard components can develop against mock data from Phase 2 onward; they do not need Agent 3 or Agent 4 to be complete.
- Real AWS last: LocalStack is sufficient for development; real AWS is required for demo correctness. One week minimum for drift discovery and remediation.

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Core libraries (boto3, FastAPI, SQLAlchemy, Redis, Pydantic) well-documented with stable APIs. Version ranges verified against training data. |
| Features | MEDIUM-HIGH | AWS native tool capabilities sourced from official docs (HIGH). Commercial tool gaps sourced from training data (MEDIUM — products evolve). Lambda memory rightsizing gap confirmed by reading PROJECT.md Agent 2 rules directly. |
| Architecture | HIGH | K8s/Redis from official docs; HMAC from Python stdlib docs; LLM validation loop from established 2024-2025 community practice. Confidence on Groq JSON mode is MEDIUM (confirmed OpenAI-compatible, not fetched directly). |
| Pitfalls | HIGH | LocalStack coverage matrix from official LocalStack docs. Groq rate limit behavior from documented API constraints. TimescaleDB/Neon gotchas from official docs (Neon free tier specifics MEDIUM — may have changed). |

**Overall confidence: MEDIUM-HIGH**

### Gaps to Address

- **Qwen3-32B on Groq free tier:** LOW confidence on availability. Resolve before benchmark phase by checking `console.groq.com/models`. Fallback: use Llama 3.3 70B as the sole Groq model.
- **Neon free tier connection limit:** Research data says 5 concurrent connections. Verify current limit at `neon.tech/docs` before setting SQLAlchemy pool configuration. Affects `pool_size` and `max_overflow` settings.
- **LocalStack 3.x exact version pinning:** Pin to a specific LocalStack version in `docker-compose.yml` at project start (not `latest`) to prevent API surface changes mid-development. Check `hub.docker.com/r/localstack/localstack` for latest stable tag.
- **OPA/conftest integration point:** Feeding `terraform show -json` plan output into conftest is where teams most commonly lose days. Test this integration step in isolation before connecting it to the validation loop. Research identified this as the highest-complexity integration in Phase 3.
- **IAM policy correctness:** LocalStack does not enforce IAM. The scoped-write IAM model for Agent 4 is untested until Week 9. Write a separate IAM policy validation test that checks policy documents against the AWS IAM Policy Simulator (or a real AWS dry-run) before the demo.

---

## Sources

### Primary (HIGH confidence)
- AWS official documentation (Trusted Advisor, Compute Optimizer, Cost Explorer, Cost Optimization Hub) — features and gap analysis
- Python stdlib documentation — `hmac`, `hashlib`, `graphlib`, `concurrent.futures`
- PostgreSQL official docs — triggers, RLS, `ON CONFLICT`
- TimescaleDB official documentation — hypertable creation, continuous aggregates, compression
- Kubernetes official docs — CronJob spec, concurrencyPolicy, Job backoffLimit
- Redis official docs — Streams, XREADGROUP, consumer groups, pub/sub delivery guarantees
- FastAPI official docs — CORS middleware, WebSocket patterns
- SQLAlchemy 2.0 async docs — AsyncSession, async_sessionmaker

### Secondary (MEDIUM confidence)
- Groq Python SDK documentation — `groq.RateLimitError`, timeout config
- Ollama Python client documentation — `ollama.chat()`, local endpoint config
- LocalStack Community coverage matrix — service support boundaries
- Groq API documentation — rate limit tiers, JSON mode, model availability
- Neon documentation — free tier limits, connection pooling, compute suspend behavior
- Checkov documentation — check IDs, `.checkov.yaml` configuration
- OPA/conftest documentation — Rego policy patterns, `conftest test` CLI

### Tertiary (LOW confidence — validate before use)
- Groq model availability (Qwen3-32B on free tier) — verify at `console.groq.com/models`
- Neon free tier concurrent connection count — verify at `neon.tech/docs`
- Commercial tool capabilities (CloudHealth, Spot.io, Harness CCM) — training data as of Aug 2025, products evolve

---
*Research completed: 2026-04-03*
*Ready for roadmap: yes*
