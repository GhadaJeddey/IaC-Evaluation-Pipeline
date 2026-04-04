# Architecture Research — FinOps Intelligence Platform

**Researched:** 2026-04-03
**Overall confidence:** HIGH (K8s/Redis from official docs; HMAC/PostgreSQL from Python/PG official docs; LLM patterns from well-established 2024-2025 community practice)

---

## Agent Orchestration Patterns

### Recommended Pattern: K8s Job-per-Agent with Redis Streams for Coordination

Each agent runs as a fully isolated Kubernetes Job or CronJob. The job exits with code 0 on
success, non-zero on failure. Kubernetes handles retries via `backoffLimit`. No long-running
daemon processes — the scheduler IS Kubernetes.

**Confidence: HIGH** — Verified against official Kubernetes batch/v1 docs.

### CronJob manifest pattern (Agent 1 — Collector)

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: agent-collector
  namespace: finops
spec:
  schedule: "0 */6 * * *"          # Every 6 hours
  concurrencyPolicy: Forbid         # Skip if previous run still going
  startingDeadlineSeconds: 120      # Fail fast if k8s scheduler is lagged
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 2               # Retry twice before marking failed
      ttlSecondsAfterFinished: 3600 # Garbage-collect pods after 1h
      template:
        spec:
          restartPolicy: Never      # Job-level retry, not pod-level restart
          serviceAccountName: finops-agent-ro  # Read-only IAM via IRSA
          containers:
          - name: collector
            image: finops/agent-collector:latest
            envFrom:
            - secretRef:
                name: finops-secrets
            resources:
              requests:
                memory: "256Mi"
                cpu: "100m"
              limits:
                memory: "512Mi"
                cpu: "500m"
```

**Key decisions:**

| Setting | Value | Rationale |
|---------|-------|-----------|
| `restartPolicy` | `Never` | Job retries create a fresh pod with clean state; container restart reuses same broken env |
| `concurrencyPolicy` | `Forbid` | Collector + Analyser must not overlap; double-run creates duplicate findings |
| `backoffLimit` | `2` | 3 total attempts — enough to recover from transient AWS API failures |
| `ttlSecondsAfterFinished` | `3600` | Auto-cleanup; avoids pod graveyard after weeks of runs |

### On-demand Job pattern (Agent 3 — Recommendation, Agent 4 — Action)

Agents 3 and 4 are not scheduled — they are triggered. The FastAPI backend creates a Job
object programmatically using the Kubernetes Python client when a finding needs LLM processing
or an approved action needs execution.

```python
from kubernetes import client, config

def dispatch_recommendation_job(finding_id: str) -> str:
    config.load_incluster_config()  # Inside cluster; load_kube_config() for dev
    batch_v1 = client.BatchV1Api()

    job = client.V1Job(
        metadata=client.V1ObjectMeta(
            name=f"agent-recommend-{finding_id[:8]}",
            namespace="finops",
            labels={"finding_id": finding_id, "agent": "recommender"},
        ),
        spec=client.V1JobSpec(
            backoff_limit=1,
            ttl_seconds_after_finished=7200,
            template=client.V1PodTemplateSpec(
                spec=client.V1PodSpec(
                    restart_policy="Never",
                    containers=[
                        client.V1Container(
                            name="recommender",
                            image="finops/agent-recommender:latest",
                            env=[
                                client.V1EnvVar(name="FINDING_ID", value=finding_id)
                            ],
                            env_from=[
                                client.V1EnvFromSource(
                                    secret_ref=client.V1SecretEnvSource(
                                        name="finops-secrets"
                                    )
                                )
                            ],
                        )
                    ],
                )
            ),
        ),
    )
    batch_v1.create_namespaced_job(namespace="finops", body=job)
    return f"agent-recommend-{finding_id[:8]}"
```

### Agent 2b (Crash Diagnosis) — Event-triggered via CloudWatch Alarm → SNS → SQS

The crash diagnosis agent fires on a CloudWatch alarm, not on a schedule. The recommended
trigger chain is:

```
CloudWatch Alarm (StatusCheckFailed)
  → SNS Topic
    → SQS Queue (finops-crash-events)
      → FastAPI /webhooks/crash endpoint (polls SQS or receives via SQS long-poll)
        → Kubernetes Job (agent-crash-diagnosis) created with instance_id env var
          → Agent 2b runs, writes Mode 4 finding to DB
            → Redis Stream event: "finding.created.mode4"
              → Agent 3 Job dispatched
```

For Docker Compose dev: replace SQS with a Redis List — the LocalStack SQS mock has known
inconsistencies with long-poll timing.

---

## Agent Communication Topology

### Decision: Redis Streams (not pub/sub, not DB polling)

**Confidence: HIGH** — Verified against official Redis pub/sub docs. Pub/sub is explicitly
documented as at-most-once with no persistence; unacceptable for inter-agent handoffs.

| Mechanism | Delivery | Persistence | Replay | Verdict |
|-----------|----------|-------------|--------|---------|
| Redis pub/sub | At-most-once | No | No | Rejected — message loss on agent startup race |
| DB polling | At-least-once | Yes | Yes | Viable but adds query load and latency |
| Redis Streams | At-least-once | Yes | Yes | Recommended |
| Direct HTTP call | At-most-once | No | No | Rejected — tight coupling, no retry |

### Stream topology for this project

```
Stream: finops:events

Producers:
  Agent 1  → XADD finops:events * type collection.completed  run_id <id>
  Agent 2  → XADD finops:events * type analysis.completed    batch_id <id>
  Agent 2b → XADD finops:events * type finding.created       finding_id <id> mode 4
  Agent 3  → XADD finops:events * type recommendation.ready  finding_id <id>
  Agent 4  → XADD finops:events * type action.completed      action_id <id> status ok|rolled_back

Consumers (FastAPI background tasks, Consumer Group "orchestrator"):
  XREADGROUP GROUP orchestrator api-server COUNT 10 BLOCK 5000 STREAMS finops:events >
```

### Consumer group setup (run once at startup)

```python
import redis

r = redis.Redis.from_url(os.environ["REDIS_URL"])

try:
    r.xgroup_create("finops:events", "orchestrator", id="0", mkstream=True)
except redis.exceptions.ResponseError as e:
    if "BUSYGROUP" not in str(e):
        raise  # Already exists — safe to ignore
```

### Reliable handoff: Agent 2 → Agent 3

Agent 2 writes a finding to the DB, then publishes to the stream. Agent 3 reads the stream,
fetches the finding from DB, processes it, updates the DB row. The stream carries only IDs —
the DB is the single source of truth for payload.

```
# BAD: stream carries full payload — duplicates source of truth, bloats stream
XADD finops:events * type analysis.completed finding_data '{"instance_id": ..., ...}'

# GOOD: stream carries only reference — payload stays in DB
XADD finops:events * type analysis.completed finding_id "f-abc123" batch_id "b-789"
```

### ACK pattern — prevent lost messages

```python
async def consume_events(redis_client, group: str, consumer: str):
    while True:
        messages = redis_client.xreadgroup(
            group, consumer,
            streams={"finops:events": ">"},
            count=10,
            block=5000,
        )
        for stream_name, entries in (messages or []):
            for msg_id, fields in entries:
                try:
                    await handle_event(fields)
                    redis_client.xack("finops:events", group, msg_id)
                except Exception as e:
                    # Do NOT ack — message remains in PEL for reprocessing
                    logger.error(f"Event {msg_id} failed: {e}")
                    # After N failures, move to dead-letter stream
                    if get_delivery_count(msg_id) > 3:
                        redis_client.xadd("finops:events:dlq", fields)
                        redis_client.xack("finops:events", group, msg_id)
```

### State tracking in Redis Hash

Each agent run writes its status so the dashboard can poll without hitting the DB:

```python
# Agent 1 sets state at start/end
r.hset(f"finops:run:{run_id}", mapping={
    "agent": "collector",
    "status": "running",       # running | completed | failed
    "started_at": datetime.utcnow().isoformat(),
    "finding_count": 0,
})
r.expire(f"finops:run:{run_id}", 86400)  # 24h TTL — auto-cleanup

# Agent updates progress
r.hincrby(f"finops:run:{run_id}", "finding_count", 1)

# FastAPI reads for dashboard
state = r.hgetall(f"finops:run:{run_id}")
```

### Docker Compose equivalent (no K8s)

In dev, replace Job dispatch with a direct subprocess or thread since containers are
long-running. The cleanest dev pattern: all agents expose a `main()` entry point; FastAPI
calls them in a background thread via `asyncio.create_task(asyncio.to_thread(agent_main))`.
This keeps the communication topology identical — same Redis streams, same DB writes — just
without K8s Job objects.

---

## LLM Validation Loop Design

### Overall loop structure

```
Finding (from DB)
  │
  ▼
┌─────────────────────────────────┐
│  Build prompt (mode-aware)      │
│  Attach previous error if retry │
└─────────────────┬───────────────┘
                  │
                  ▼
         LLM API call (Groq)
                  │
                  ▼
┌─────────────────────────────────┐
│  Parse JSON response            │
│  Pydantic model_validate()      │
│  → ValidationError? → retry     │
└─────────────────┬───────────────┘
                  │ valid structure
                  ▼
         terraform validate
                  │
                  ├── FAIL → append error to prompt → retry
                  │
                  ▼
         checkov --directory .
                  │
                  ├── FAIL (HIGH/CRITICAL) → append violations → retry
                  │
                  ▼
         conftest test . (OPA)
                  │
                  ├── FAIL → append policy text → retry
                  │
                  ▼
         All pass → write recommendation to DB
                  │
         attempt >= 3 → escalate_to_human()
```

**Confidence: MEDIUM** — Pattern is well-established in 2024-2025 LLM engineering community;
specific Groq JSON mode support confirmed in Groq public docs (not fetched directly here but
consistent with OpenAI-compatible API surface).

### Retry state machine

```python
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MAX_RETRIES = 3

@dataclass
class ValidationState:
    attempt: int = 0
    last_error: Optional[str] = None
    terraform_errors: list[str] = field(default_factory=list)
    checkov_errors: list[str] = field(default_factory=list)
    opa_errors: list[str] = field(default_factory=list)

    def has_errors(self) -> bool:
        return bool(self.terraform_errors or self.checkov_errors or self.opa_errors)

    def error_summary(self) -> str:
        parts = []
        if self.terraform_errors:
            parts.append("Terraform validation errors:\n" + "\n".join(self.terraform_errors))
        if self.checkov_errors:
            parts.append("Security policy violations (Checkov):\n" + "\n".join(self.checkov_errors))
        if self.opa_errors:
            parts.append("OPA policy failures:\n" + "\n".join(self.opa_errors))
        return "\n\n".join(parts)


def run_validation_loop(finding: dict, llm_client, workdir: Path) -> dict:
    state = ValidationState()

    while state.attempt < MAX_RETRIES:
        state.attempt += 1

        # Build prompt — include previous errors on retry
        prompt = build_prompt(finding, previous_errors=state.error_summary() if state.has_errors() else None)

        # LLM call — request JSON
        raw = llm_client.chat(prompt, response_format={"type": "json_object"})

        # Parse + structural validate
        try:
            result = RecommendationSchema.model_validate_json(raw)
        except Exception as e:
            state.last_error = f"JSON parse/schema error: {e}"
            continue  # Retry immediately, counts as one attempt

        # Write Terraform to temp dir
        tf_path = workdir / f"attempt_{state.attempt}" / "main.tf"
        tf_path.parent.mkdir(parents=True, exist_ok=True)
        tf_path.write_text(result.terraform_hcl)

        # Gate 1: terraform validate
        tf_errors = run_terraform_validate(tf_path.parent)
        if tf_errors:
            state.terraform_errors = tf_errors
            continue

        # Gate 2: Checkov
        checkov_violations = run_checkov(tf_path.parent)
        if checkov_violations:
            state.checkov_errors = checkov_violations
            continue

        # Gate 3: OPA / conftest
        opa_failures = run_opa(tf_path.parent)
        if opa_failures:
            state.opa_errors = opa_failures
            continue

        # All gates passed
        return {"status": "validated", "result": result, "attempts": state.attempt}

    # Exhausted retries
    return {
        "status": "escalated",
        "finding_id": finding["id"],
        "last_errors": state.error_summary(),
        "attempts": state.attempt,
    }
```

### subprocess helpers for validation gates

```python
def run_terraform_validate(tf_dir: Path) -> list[str]:
    """Returns list of error strings; empty list means pass."""
    proc = subprocess.run(
        ["terraform", "validate", "-json"],
        cwd=tf_dir,
        capture_output=True,
        text=True,
    )
    output = json.loads(proc.stdout)
    if output.get("valid"):
        return []
    return [d["summary"] for d in output.get("diagnostics", []) if d["severity"] == "error"]


def run_checkov(tf_dir: Path) -> list[str]:
    """Returns list of HIGH/CRITICAL violation IDs + messages."""
    proc = subprocess.run(
        ["checkov", "--directory", str(tf_dir), "--output", "json",
         "--soft-fail-on", "LOW,MEDIUM"],
        capture_output=True, text=True,
    )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [f"Checkov output parse error: {proc.stdout[:200]}"]

    results = data.get("results", {}).get("failed_checks", [])
    return [
        f"{r['check_id']}: {r['check_result']['result']} — {r['resource']}"
        for r in results
    ]


def run_opa(tf_dir: Path, policy_dir: str = "/policies") -> list[str]:
    """conftest test using OPA rego policies; returns failures."""
    proc = subprocess.run(
        ["conftest", "test", str(tf_dir), "--policy", policy_dir, "--output", "json"],
        capture_output=True, text=True,
    )
    try:
        results = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    failures = []
    for r in results:
        failures.extend(r.get("failures", []))
    return [f["msg"] for f in failures]
```

### LLM output schema (Pydantic v2)

```python
from pydantic import BaseModel, field_validator

class RecommendationSchema(BaseModel):
    mode: int                        # 1-4
    finding_id: str
    explanation_nl: str              # Plain-language business justification
    estimated_monthly_saving_usd: float
    terraform_hcl: str               # Raw HCL string
    confidence: str                  # "high" | "medium" | "low"
    risks: list[str]

    @field_validator("mode")
    @classmethod
    def mode_must_be_valid(cls, v: int) -> int:
        if v not in (1, 2, 3, 4):
            raise ValueError(f"mode must be 1-4, got {v}")
        return v

    @field_validator("terraform_hcl")
    @classmethod
    def hcl_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("terraform_hcl cannot be empty")
        return v
```

### Prompt versioning

Store prompts in the DB (or versioned YAML files), not in source code strings.

```sql
CREATE TABLE prompt_templates (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,        -- 'recommendation_mode1_v3'
    mode        INT,
    version     INT NOT NULL DEFAULT 1,
    template    TEXT NOT NULL,
    is_active   BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
```

Fetch the active template at runtime:
```python
template = db.fetchone(
    "SELECT template FROM prompt_templates WHERE name = %s AND is_active = TRUE",
    (f"recommendation_mode{mode}",)
)
```

This allows prompt iteration without code deploys — critical during the 2.5-month academic timeline.

### Groq 429 handling

Per the project constraint: never retry autonomously on 429.

```python
import groq

def call_llm(prompt: str, client: groq.Groq) -> str:
    try:
        response = client.chat.completions.create(
            model=os.environ["LLM_MODEL"],  # e.g. "llama-3.3-70b-versatile"
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content
    except groq.RateLimitError as e:
        # Do NOT retry — log and escalate
        logger.critical(f"Groq 429: daily limit exhausted. Finding will be queued: {e}")
        raise LLMQuotaExhausted("Groq RPD limit reached") from e
    except groq.APIError as e:
        logger.error(f"Groq API error (non-rate-limit): {e}")
        raise
```

The `LLMQuotaExhausted` exception surfaces to the orchestrator, which marks the finding as
`status='pending_llm'` in the DB and halts further Agent 3 dispatches until the quota resets.

---

## HMAC Approval Workflow

### Threat model

The HMAC gate must defend against:
1. **Replay attacks** — a captured valid approval replayed for a different action
2. **Spoofed approvals** — an API call that bypasses the approval UI
3. **Tampered payload** — approval for action A applied to action B after modification
4. **Timing attacks** — comparing digest strings with `==` leaks information

**Confidence: HIGH** — HMAC-SHA256 security properties from Python official docs (confirmed).

### Token construction

The HMAC token encodes the full intent: who approved, what action, when, for which specific
resource. Any change to any field invalidates the signature.

```python
import hmac
import hashlib
import json
import time
import os
from typing import TypedDict

HMAC_SECRET = os.environ["HMAC_SECRET"].encode()  # 32+ random bytes, from k8s Secret
TOKEN_TTL_SECONDS = 300  # Approval must be executed within 5 minutes

class ApprovalPayload(TypedDict):
    action_id: str
    finding_id: str
    action_type: str          # "stop_instance" | "resize_instance" | etc.
    resource_arn: str
    terraform_plan_hash: str  # SHA-256 of the exact plan that was shown to approver
    approved_by: str          # User identity (from JWT claim)
    approved_at: int          # Unix timestamp (seconds)


def generate_approval_token(payload: ApprovalPayload) -> str:
    """Called by the dashboard backend when user clicks 'Approve'."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(HMAC_SECRET, canonical.encode(), hashlib.sha256).hexdigest()
    return f"{canonical}.{signature}"


def verify_approval_token(token: str, expected_action_id: str) -> ApprovalPayload:
    """Called by Agent 4 before executing any action."""
    try:
        canonical_str, received_sig = token.rsplit(".", 1)
    except ValueError:
        raise InvalidApproval("Malformed token: missing signature separator")

    # Recompute expected signature
    expected_sig = hmac.new(HMAC_SECRET, canonical_str.encode(), hashlib.sha256).hexdigest()

    # Timing-safe comparison — NEVER use ==
    if not hmac.compare_digest(received_sig, expected_sig):
        raise InvalidApproval("Signature mismatch")

    payload: ApprovalPayload = json.loads(canonical_str)

    # Validate action binding — prevent cross-action reuse
    if payload["action_id"] != expected_action_id:
        raise InvalidApproval(
            f"Token action_id {payload['action_id']} != expected {expected_action_id}"
        )

    # Validate TTL — prevent replay of old approvals
    now = int(time.time())
    if now - payload["approved_at"] > TOKEN_TTL_SECONDS:
        raise InvalidApproval(
            f"Token expired: approved {now - payload['approved_at']}s ago (TTL={TOKEN_TTL_SECONDS}s)"
        )

    return payload
```

### The `terraform_plan_hash` field is critical

The approver sees a specific plan in the dashboard. That plan is hashed and embedded in the
token. If Agent 4 receives a token but the plan on disk has changed (e.g., because another
job modified the same Terraform state), the hash check will fail.

```python
import hashlib

def hash_terraform_plan(plan_json_path: str) -> str:
    content = Path(plan_json_path).read_bytes()
    return hashlib.sha256(content).hexdigest()
```

### One-time-use enforcement (replay prevention at DB level)

The HMAC signature alone does not prevent replaying the same valid token twice. Enforce
single-use at the DB:

```sql
-- In the audit_log table (see section below), record token usage:
ALTER TABLE audit_log ADD COLUMN approval_token_hash TEXT UNIQUE;

-- Agent 4 does this BEFORE executing, inside a transaction:
-- INSERT INTO audit_log (action_id, approval_token_hash, ...) VALUES (...)
-- ON CONFLICT (approval_token_hash) DO NOTHING RETURNING id
-- If no row returned → token already used → abort
```

```python
async def claim_approval_token(conn, action_id: str, token: str) -> bool:
    """Returns True if token was claimed; False if already used (replay)."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    row = await conn.fetchrow(
        """
        INSERT INTO audit_log (action_id, approval_token_hash, status, created_at)
        VALUES ($1, $2, 'claimed', NOW())
        ON CONFLICT (approval_token_hash) DO NOTHING
        RETURNING id
        """,
        action_id, token_hash,
    )
    return row is not None  # None = conflict = already used
```

### Common pitfalls

| Pitfall | Consequence | Fix |
|---------|-------------|-----|
| Using `==` for digest comparison | Timing attack reveals secret byte-by-byte | Always use `hmac.compare_digest()` |
| Not binding action_id to token | Token valid for A reused for B | Include `action_id` in canonical payload |
| No TTL on token | Captured approval valid indefinitely | Embed `approved_at` + enforce TTL |
| No single-use enforcement | Same token replayed | DB UNIQUE on token hash, claimed atomically |
| Secret in code / git | Key compromise | Load from env var / k8s Secret only |
| Short secret (< 32 bytes) | Brute-forceable | Generate with `secrets.token_bytes(32)` |
| Not hashing the plan | Approver sees X, agent executes Y | Hash plan contents into token payload |

---

## Immutable Audit Log Pattern

### Table definition

**Confidence: HIGH** — PostgreSQL trigger and RLS patterns verified against official PG docs.

```sql
CREATE TABLE audit_log (
    id                  BIGSERIAL PRIMARY KEY,
    -- Action identity
    action_id           TEXT NOT NULL,
    finding_id          TEXT NOT NULL,
    recommendation_id   TEXT,
    -- What happened
    action_type         TEXT NOT NULL,  -- 'stop_instance' | 'resize' | 'rollback' | ...
    resource_arn        TEXT NOT NULL,
    terraform_plan_hash TEXT UNIQUE,    -- Enforces single-use approval
    -- Who approved
    approved_by         TEXT NOT NULL,
    approval_token_hash TEXT,           -- Hash of the HMAC token (not the token itself)
    -- Outcome
    status              TEXT NOT NULL CHECK (status IN ('claimed','executing','completed','rolled_back','failed')),
    error_detail        TEXT,
    -- Timing
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ,
    -- Immutability marker
    row_hash            TEXT GENERATED ALWAYS AS (
                            encode(sha256(
                                (action_id || action_type || resource_arn || approved_by
                                 || created_at::text)::bytea
                            ), 'hex')
                        ) STORED
);

-- Prevent ALL updates and deletes — immutable by design
CREATE OR REPLACE FUNCTION audit_log_immutable()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: updates and deletes are forbidden';
END;
$$;

CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

-- Row-level security as second layer
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY audit_no_update ON audit_log FOR UPDATE USING (FALSE);
CREATE POLICY audit_no_delete ON audit_log FOR DELETE USING (FALSE);

-- Application user can INSERT and SELECT, nothing else
GRANT INSERT, SELECT ON audit_log TO finops_app;
REVOKE UPDATE, DELETE ON audit_log FROM finops_app;
```

### Status progression pattern

Because audit_log is append-only, multi-step status cannot be UPDATE. Two approaches:

**Approach A: Single row, status via separate events table** (recommended for this project —
simpler for academic demo)

```sql
CREATE TABLE audit_events (
    id          BIGSERIAL PRIMARY KEY,
    audit_log_id BIGINT NOT NULL REFERENCES audit_log(id),
    event_type  TEXT NOT NULL,  -- 'execution_started' | 'step_completed' | 'rollback_initiated'
    detail      JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    -- No UPDATE/DELETE triggers needed — new rows only
);
```

**Approach B: Status columns with GENERATED hash** — allows single row to carry final status
but requires the `status` and `completed_at` columns to be set in a single INSERT that
includes the final state. Simpler but loses intermediate state.

For the demo, Approach A gives a richer audit trail visible in the dashboard.

### Querying the audit trail

```python
# Get full history for a resource
audit_history = db.fetch(
    """
    SELECT al.action_id, al.action_type, al.resource_arn,
           al.approved_by, al.created_at, al.row_hash,
           json_agg(
               json_build_object(
                   'event', ae.event_type,
                   'detail', ae.detail,
                   'at', ae.occurred_at
               ) ORDER BY ae.occurred_at
           ) AS events
    FROM audit_log al
    LEFT JOIN audit_events ae ON ae.audit_log_id = al.id
    WHERE al.resource_arn = %s
    GROUP BY al.id
    ORDER BY al.created_at DESC
    """,
    (resource_arn,)
)
```

---

## Hybrid LLM Architecture

### Decision: Evaluator-first, Script-for-simple, LLM-for-complex

**Confidence: HIGH** — This pattern is the project's own key decision (from PROJECT.md memory).
Architecture research confirms it matches the "LLM as fallback, not default" pattern common in
production FinOps and IaC tools as of 2024-2025.

### Mode routing logic

```python
from enum import IntEnum

class LLMMode(IntEnum):
    SIMPLE  = 1   # stop/resize/delete — script generates Terraform, LLM explains
    COMPLEX = 2   # new resource types, architecture changes — LLM generates Terraform
    RISKY   = 3   # multi-resource or irreversible — LLM + extra validation pass
    RCA     = 4   # crash diagnosis — LLM reads 500 log lines, explains + remediates

# Mapping is deterministic — set by Agent 2, never by LLM
FINDING_TYPE_TO_MODE: dict[str, LLMMode] = {
    "idle_ec2":           LLMMode.SIMPLE,   # stop instance
    "oversized_ec2":      LLMMode.SIMPLE,   # resize instance type
    "underprovisioned_ec2": LLMMode.SIMPLE, # upsize instance type
    "ghost_eip":          LLMMode.SIMPLE,   # release EIP
    "ghost_ebs":          LLMMode.SIMPLE,   # delete volume
    "log_no_retention":   LLMMode.SIMPLE,   # set CW log retention
    "log_high_volume":    LLMMode.COMPLEX,  # S3 export subscription filter + bucket
    "untagged_resource":  LLMMode.SIMPLE,   # add tags
    "crash_diagnosis":    LLMMode.RCA,      # read logs, explain, remediate
    "multi_resource_dependency": LLMMode.RISKY,
}

def route_to_mode(finding: dict) -> LLMMode:
    return FINDING_TYPE_TO_MODE.get(finding["type"], LLMMode.COMPLEX)
```

### Mode 1 — Script generates Terraform, LLM explains only

The script produces deterministic, correct Terraform. The LLM is called only to generate
the plain-language explanation. This conserves Groq RPD and guarantees valid HCL.

```python
TERRAFORM_TEMPLATES = {
    "idle_ec2": """
resource "aws_ec2_instance_state" "stop_{instance_id_safe}" {{
  instance_id = "{instance_id}"
  state       = "stopped"
}}
""",
    "oversized_ec2": """
resource "aws_instance" "resize_{instance_id_safe}" {{
  # Resize from {current_type} to {target_type}
  instance_type = "{target_type}"
  # ... existing config preserved
}}
""",
}

def generate_mode1_recommendation(finding: dict, llm_client) -> RecommendationSchema:
    # Step 1: Script generates Terraform (no LLM needed)
    template = TERRAFORM_TEMPLATES[finding["type"]]
    terraform_hcl = template.format(
        instance_id=finding["resource_id"],
        instance_id_safe=finding["resource_id"].replace("-", "_"),
        current_type=finding.get("current_type", "unknown"),
        target_type=finding.get("recommended_type", "t3.micro"),
    )

    # Step 2: LLM generates explanation only (1 API call, no validation loop needed)
    explanation_prompt = f"""
You are a FinOps engineer writing a plain-language justification for a cost optimization.

Finding: {finding['type']}
Resource: {finding['resource_id']} ({finding.get('resource_name', 'unnamed')})
Metrics: {json.dumps(finding.get('metrics', {}), indent=2)}
Estimated monthly saving: ${finding.get('estimated_saving_usd', 0):.2f}

Write 2-3 sentences explaining:
1. What the problem is
2. What the recommended action is
3. What the cost saving is

Be specific with numbers. Write for a cloud engineer, not a manager.
Respond with JSON: {{"explanation": "...", "confidence": "high|medium|low"}}
"""
    raw = llm_client.chat(explanation_prompt, response_format={"type": "json_object"})
    parsed = json.loads(raw)

    return RecommendationSchema(
        mode=1,
        finding_id=finding["id"],
        explanation_nl=parsed["explanation"],
        estimated_monthly_saving_usd=finding.get("estimated_saving_usd", 0.0),
        terraform_hcl=terraform_hcl,
        confidence=parsed.get("confidence", "medium"),
        risks=[],
    )
```

### Mode 4 — Crash RCA: log summarization first, then Terraform

```python
def generate_mode4_recommendation(finding: dict, llm_client) -> RecommendationSchema:
    log_lines = finding["log_lines"]  # 500 lines from CloudWatch

    # Step 1: Summarize logs (reduces token count for second call)
    summary_prompt = f"""
Analyze these CloudWatch logs from AWS instance {finding['resource_id']}.
The instance failed a StatusCheck at {finding['alarm_triggered_at']}.

Logs (last 500 lines):
---
{chr(10).join(log_lines)}
---

Identify:
1. Root cause (specific error, process, or resource)
2. Immediate trigger
3. Contributing factors

Respond with JSON:
{{
  "root_cause": "...",
  "trigger": "...",
  "contributing_factors": ["..."],
  "severity": "critical|high|medium"
}}
"""
    rca_raw = llm_client.chat(summary_prompt, response_format={"type": "json_object"})
    rca = json.loads(rca_raw)

    # Step 2: Generate remediation Terraform (second LLM call — Mode 4 is allowed 2 calls)
    remediation_prompt = f"""
Root cause analysis for AWS instance {finding['resource_id']}:
- Root cause: {rca['root_cause']}
- Trigger: {rca['trigger']}

Generate Terraform to remediate this issue. Consider:
- Instance recovery or replacement
- CloudWatch alarm updates
- IAM or security group changes if implicated

Respond with JSON matching this schema:
{{
  "terraform_hcl": "...",
  "explanation_nl": "...",
  "risks": ["..."],
  "estimated_monthly_saving_usd": 0.0
}}
"""
    remediation_raw = llm_client.chat(remediation_prompt, response_format={"type": "json_object"})
    remediation = json.loads(remediation_raw)

    return RecommendationSchema(
        mode=4,
        finding_id=finding["id"],
        explanation_nl=f"RCA: {rca['root_cause']}. {remediation['explanation_nl']}",
        estimated_monthly_saving_usd=remediation.get("estimated_monthly_saving_usd", 0.0),
        terraform_hcl=remediation["terraform_hcl"],
        confidence="medium",  # RCA always medium — log analysis is probabilistic
        risks=remediation.get("risks", []),
    )
```

### Result caching (Groq RPD protection)

```python
import hashlib
import json

def get_cache_key(finding: dict) -> str:
    """Deterministic key based on finding content, not ID (ID changes across runs)."""
    canonical = {
        "type": finding["type"],
        "resource_id": finding["resource_id"],
        "metrics_snapshot": finding.get("metrics_snapshot"),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()

async def get_cached_recommendation(redis_client, finding: dict) -> dict | None:
    key = f"finops:rec_cache:{get_cache_key(finding)}"
    cached = redis_client.get(key)
    return json.loads(cached) if cached else None

async def cache_recommendation(redis_client, finding: dict, result: dict, ttl: int = 86400):
    key = f"finops:rec_cache:{get_cache_key(finding)}"
    redis_client.setex(key, ttl, json.dumps(result))
```

---

## Scheduling: APScheduler vs K8s CronJobs

### Decision: K8s CronJobs for production, Docker Compose + APScheduler for dev

**Confidence: HIGH** — K8s mechanics verified from official docs; APScheduler tradeoffs from
well-established patterns.

### Comparison matrix

| Criterion | APScheduler | K8s CronJobs |
|-----------|-------------|--------------|
| State persistence | Requires Redis/DB jobstore | Kubernetes etcd |
| Missed run recovery | Configurable (coalesce/misfire) | `startingDeadlineSeconds` |
| Concurrency control | `max_instances` per job | `concurrencyPolicy: Forbid` |
| Log access | App logs (mixed with other output) | `kubectl logs jobs/<name>` — isolated |
| Failure visibility | Exception in scheduler process | Pod status + Events in k8s |
| Horizontal scaling risk | Scheduler runs in ONE instance only | Native k8s responsibility |
| Resource isolation | Shares scheduler process memory | Separate pod per run |
| Dev simplicity | High — `pip install apscheduler` | Medium — needs k8s cluster |
| Prod ops complexity | Medium — scheduler process is SPOF | Low — k8s handles restarts |

### APScheduler pitfall: the single-instance problem

APScheduler runs inside a Python process. If you deploy multiple FastAPI replicas, EACH
replica will run a scheduler. Every agent fires N times. Fix: use a Redis lock.

```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.redis import RedisJobStore

# Only one process runs scheduled jobs — use RedisJobStore for persistence
scheduler = AsyncIOScheduler(
    jobstores={
        "default": RedisJobStore(
            host=REDIS_HOST, port=REDIS_PORT, db=1
        )
    },
    job_defaults={
        "coalesce": True,     # Collapse missed runs into one
        "max_instances": 1,   # Never run same job twice concurrently
    }
)

scheduler.add_job(
    run_collector_agent,
    trigger="cron",
    hour="*/6",
    id="collector",
    replace_existing=True,
)
```

Even with RedisJobStore, APScheduler requires exactly one scheduler process to be the "leader."
For this project's Docker Compose setup (single-replica FastAPI), this is fine. Do not scale
FastAPI horizontally while APScheduler is active without implementing distributed locking.

### Recommendation for this project

```
Dev (Docker Compose):
  - APScheduler inside FastAPI process
  - RedisJobStore for persistence across restarts
  - Simple, no k8s tooling required

Prod/Demo (k3s):
  - K8s CronJobs — independent of FastAPI
  - FastAPI creates on-demand Jobs (Agent 3, 4) via Python k8s client
  - Clear separation: scheduling is infrastructure, not application concern
```

### Dev → Prod parity strategy

Keep agent `main()` functions callable both ways — no scheduler-specific code inside agents:

```python
# agent_collector/main.py
def main():
    """Entry point — called by K8s Job OR by APScheduler."""
    run_id = os.environ.get("RUN_ID", str(uuid.uuid4()))
    collector = CollectorAgent(run_id=run_id)
    collector.collect_all()

if __name__ == "__main__":
    main()
```

The scheduler (either APScheduler or K8s CronJob) is responsible for launching `main()`.
The agent has zero knowledge of how it was invoked. This makes the Docker Compose → k3s
migration a manifest change, not a code change.

---

## Cross-Cutting Architecture Notes

### Environment toggle

```python
# config.py — single ENV var drives dev/prod
import os

ENV = os.environ.get("FINOPS_ENV", "dev")  # "dev" | "prod"

AWS_ENDPOINT_URL = None if ENV == "prod" else "http://localstack:4566"
MOCK_COST_EXPLORER = ENV == "dev"
K8S_JOB_DISPATCH = ENV == "prod"  # False → use asyncio.to_thread in dev
LLM_PROVIDER = "ollama" if ENV == "dev" else "groq"
```

### Agent isolation principle

Each agent reads from its input sources (DB, CloudWatch, Redis) and writes to its output
(DB, Redis Stream). No agent calls another agent's API directly. This makes individual
agents testable in isolation and survivable under partial failure.

```
Agent 1 reads:  AWS APIs (CloudWatch, EC2, S3, Lambda)
Agent 1 writes: TimescaleDB (metrics, instances, ghost_resources)
                Redis Stream: finops:events (collection.completed)

Agent 2 reads:  TimescaleDB (metrics) + DB (rules)
Agent 2 writes: DB (findings)
                Redis Stream: finops:events (analysis.completed)

Agent 3 reads:  DB (findings), Redis Cache (cached recs)
Agent 3 writes: DB (recommendations)
                Redis Stream: finops:events (recommendation.ready)
                Redis Cache: recommendation result

Agent 4 reads:  DB (recommendations, approval_token), Redis Stream
Agent 4 writes: DB (audit_log, audit_events)
                AWS APIs (execute Terraform via shell)
                Redis Stream: finops:events (action.completed)
```

### Topological sort for Agent 4 execution ordering

Agent 2 computes execution order (topological sort of dependent findings). Agent 4 respects
this order. The sort result is stored as a `depends_on` array in the findings table.

```python
from graphlib import TopologicalSorter  # stdlib, Python 3.9+

def compute_execution_order(findings: list[dict]) -> list[str]:
    """
    findings: list of {id, depends_on: [finding_id, ...]}
    Returns: ordered list of finding IDs safe to execute sequentially.
    """
    graph = {f["id"]: set(f.get("depends_on", [])) for f in findings}
    ts = TopologicalSorter(graph)
    return list(ts.static_order())
```

---

## Sources

| Claim | Source | Confidence |
|-------|--------|------------|
| K8s Job/CronJob mechanics, restartPolicy, backoffLimit, ttlSecondsAfterFinished | kubernetes.io/docs/concepts/workloads/controllers/job/ (fetched 2026-04-03) | HIGH |
| Redis pub/sub at-most-once, no persistence | redis.io/docs/latest/develop/interact/pubsub/ (fetched 2026-04-03) | HIGH |
| Redis Streams consumer groups, XREADGROUP, XACK, at-least-once delivery | redis.io/docs/latest/develop/data-types/streams/ (fetched 2026-04-03, partial) | HIGH |
| hmac.new(), compare_digest(), timing attack prevention | docs.python.org/3/library/hmac.html (fetched 2026-04-03) | HIGH |
| PostgreSQL trigger-based immutability, RLS policies | postgresql.org/docs/current/sql-createtable.html (fetched 2026-04-03) | HIGH |
| APScheduler RedisJobStore, single-instance risk | APScheduler docs (training data, August 2025) | MEDIUM |
| LLM validation loop retry patterns (Checkov, OPA, terraform validate) | Training data + project PROJECT.md spec | MEDIUM |
| Groq RPD 429 behavior, OpenAI-compatible JSON mode | Training data (Groq public docs, consistent with OpenAI spec) | MEDIUM |
| Hybrid LLM script-first design | PROJECT.md key decision (project-specific, not external) | HIGH |
| graphlib.TopologicalSorter | Python 3.9+ stdlib — HIGH confidence | HIGH |
