# Pitfalls Research — FinOps Intelligence Platform

**Domain:** Multi-agent FinOps platform (Python agents, FastAPI, React, LLM Terraform generation)
**Researched:** 2026-04-03
**Overall confidence:** MEDIUM — WebSearch and WebFetch unavailable; findings drawn from training
data (cutoff August 2025) cross-referenced against known official documentation, community
post-mortems, and direct experience with each technology. Confidence noted per section.

---

## LocalStack Community Limitations

**Confidence: HIGH** — LocalStack's own coverage matrix is well-documented and stable.

### What LocalStack Community does NOT emulate

**Cost Explorer (ce) — completely absent.**
This is already known and handled via mock. Do not slip on this: any code path that calls
`boto3.client('ce')` against LocalStack will return a connection error, not a mock response.
The mock must be at the client-init layer, not the call layer. If you create the client
conditionally, make sure the `ENV=dev` branch never instantiates the real client.

**CloudWatch Metrics GetMetricStatistics — partial, unreliable.**
`get_metric_statistics` is implemented in Community, but:
- Returned data points are synthetic/empty unless you publish metrics yourself via `put_metric_data`.
- Agent 1 will collect zero real CPU/network data against LocalStack. You must seed LocalStack
  with `put_metric_data` calls in your fixture scripts, or the rules engine will always see empty
  metrics and produce no findings.
- `get_metric_data` (the newer API) has even patchier support than `get_metric_statistics`.
  Stick with `get_metric_statistics` for LocalStack compatibility.

**CloudWatch Logs Insights — not implemented in Community.**
`start_query` / `get_query_results` return errors. Agent 1's log group scanning (IncomingBytes,
retention) must use `describe_log_groups` and `get_metric_statistics` on the
`IncomingBytes` metric, NOT Logs Insights queries. This is compatible with Community.

**Resource Groups Tagging API — partial.**
`get_resources` works for resources created within the same LocalStack session but tag indexes
are not persistent across restarts. Every dev session must re-seed resources and tags. Write
a `seed_localstack.py` script on day 1 — the absence of it will waste hours mid-development.

**EIP / EBS orphan detection — works, with caveats.**
`describe_addresses` and `describe_volumes` work in Community. However, LocalStack does not
enforce the same attachment state machine as real AWS. An EBS volume created without an
instance will show `available` state immediately; an attached volume will not automatically
flip to `available` when a mock instance is terminated unless you call the API explicitly.
Test your ghost-resource detection against explicitly constructed fixture states, not
assumptions about state transitions.

**ELB (Classic/ALB/NLB) — ALBv2 very limited in Community.**
`describe_load_balancers` (v2) is supported for basic operations, but health check state
and target group member state are not realistically simulated. Idle ELB detection (no healthy
targets for N days) is impossible to demonstrate meaningfully in LocalStack Community.
Either mock this finding entirely in dev or skip ELB idle detection in the demo.

**SQS — works well.**
Dead-letter queues, visibility timeout, message attributes — all reliable in Community.
Use SQS for agent-to-agent events without concern.

**IAM — not enforced in Community.**
LocalStack Community does not enforce IAM policies. Your read-only vs. scoped-write IAM
distinction will not be validated locally. This means a bug where Agent 1 accidentally
has write permissions will only surface in the real AWS environment. Write a separate
policy-validation test that checks IAM policy documents are correct before the demo.

**Snapshots (EBS) — basic support.**
`describe_snapshots` works. Age-based filtering (old snapshots) is fine as long as you
set `StartTime` on fixture snapshots explicitly. LocalStack uses the current timestamp
for all created resources by default — you cannot create a snapshot "30 days ago" without
manipulating the fixture data directly in the database, which is fragile.

**Practical implication:** Every dev scenario must be driven by a seed script that creates
fixtures with explicit states. The seed script is as important as the agent code itself.
Build it in Week 1 alongside Agent 1.

---

## Groq Free Tier Risks

**Confidence: HIGH** — Groq's rate limit behavior is well-documented and the 1,000 RPD hard
cap is a core design constraint of this project.

### The 1,000 RPD limit is per-account, not per-model

All models on the free tier share the 1,000 RPD pool. The benchmark phase (running 3-4 models
across a test suite) will consume this budget faster than production use. If you run 250 test
cases against 4 models, you have consumed your entire day's budget before any production
agent runs. Run benchmarks in a dedicated off-hours window with a hard counter in the benchmark
script. Do not run benchmarks and agents on the same day during development.

### Token-per-minute (TPM) limits are a second independent constraint

The free tier also enforces TPM limits per model. Qwen3-32B and Llama 3.3 70B are large
models with lower TPM caps than smaller models. A long Terraform generation prompt (500 tokens
input, 800 tokens output) against Llama 3.3 70B can hit the TPM limit even when the RPD
counter is healthy. The 429 response body distinguishes between RPD and TPM exhaustion —
parse the `error.message` field, not just the status code, to know which limit you hit and
how long to wait.

### Groq free tier has no SLA and intermittent cold-start latency

Groq free tier requests occasionally take 10-30 seconds to respond if the model is not
warmed up. This is not a rate limit — it is infrastructure latency. Do not set Agent 3's
HTTP timeout below 60 seconds. A 30-second timeout will produce spurious failures that look
like errors but are actually slow cold starts.

### Retry strategy: never autonomous retry on 429

The project already captures this constraint. Reinforce it in code with a hard stop, not
a `time.sleep(60)` loop. An autonomous retry loop can exhaust the daily budget in minutes
if the retry condition is wrong. The correct pattern:

```python
if response.status_code == 429:
    # Log the error, write a FAILED finding to DB, return.
    # Never sleep-and-retry.
    raise GroqRateLimitExceeded("Daily budget exhausted. Agent 3 halted.")
```

Ollama (local fallback) must be ready before you need it. Do not treat it as a theoretical
fallback — test the fallback path in Week 2 so you know it works.

### Caching strategy: cache at the finding hash level, not the prompt level

Cache key should be `sha256(finding_type + resource_id + metric_snapshot)`. If the finding
for a specific resource has not changed since the last analysis cycle, return the cached
recommendation without calling Groq. This is the most effective RPD reduction. A 14-day
idle EC2 finding for `i-0abc123` will be identical across multiple analysis cycles until
the resource state changes.

Redis TTL for cached recommendations: 24 hours. Stale recommendations older than 24 hours
should be regenerated, since metric thresholds may have shifted.

### Groq API key in environment variables — obvious but critical

A leaked Groq API key on a free tier account has limited financial impact, but the daily
budget can be exhausted by a crawler within minutes. Do not commit the key. Use
`python-dotenv` with `.env` in `.gitignore` from day 1. Check `git log` before any public
demo push.

---

## LLM Terraform Generation Failure Modes

**Confidence: HIGH** — These failure modes are well-documented across LLM code generation
literature and Terraform-specific community reports.

### Hallucinated provider versions

LLMs frequently generate `required_providers` blocks with version constraints that reference
non-existent patch versions (e.g., `~> 5.31.0` when the latest is `5.30.x`). This causes
`terraform init` to fail with a provider resolution error — not `terraform validate`. Your
validation loop must run `terraform init` first, before `terraform validate`. If `init` fails,
the validate step is unreachable. Structure the loop as:

```
init → validate → plan (dry-run) → checkov → OPA
```

Not:

```
validate → checkov → OPA
```

### Resource argument drift

AWS provider arguments change between versions. LLMs trained on older data generate arguments
that no longer exist (e.g., deprecated `instance_type` placement in wrong block, removed
`tags` nesting, renamed `security_group_ids` vs `vpc_security_group_ids`). The model will
not know which version of the AWS provider is installed in your Terraform working directory.
Pin the AWS provider version explicitly in your base `provider.tf` template and include it
in the prompt context:

```
You are generating Terraform for AWS provider version 5.x.
```

### Data source vs. resource confusion

LLMs frequently generate `resource "aws_instance" "target"` blocks when the task is to
modify an existing instance (which requires a `data "aws_instance" "target"` lookup plus
a `resource` block for the change, or an `import` block). For stop/resize operations, the
script-based Mode 1 approach sidesteps this entirely — this is the correct design. For
Mode 2 (LLM-generated), include explicit instructions in the prompt:

```
The resource already exists. Use a data source to reference it, then modify via resource block.
Do not create a new resource.
```

### Missing depends_on causing race conditions

Generated Terraform for multi-resource changes frequently omits `depends_on` relationships.
OPA is the right place to catch this: write a policy that requires explicit dependency
declarations for any plan that modifies more than one resource. Without it, `terraform apply`
will attempt parallel execution and may fail on dependency races.

### terraform plan output is not the same as terraform apply behavior

The validation loop uses `terraform plan` (dry-run). Plan can succeed while apply fails due
to:
- Eventual consistency in AWS API (resource not yet visible after creation)
- State drift between plan and apply time
- Permissions differences (plan uses read scope; apply needs write scope)

For the demo, this matters for Agent 4. The approval workflow must communicate to the user
that the plan is a snapshot, not a guarantee. Put this in the UI: "Plan generated at [time].
Execute within 1 hour."

### Max 3 retries is the right limit — enforce it strictly

After 3 failed validation passes, the correct behavior is to write a human-escalation finding
and stop. Do not adjust the threshold. Teams that allow 5+ retries find that the LLM enters
a local optimum and generates nearly identical invalid Terraform on each retry. Add
a `failure_reason` field to the retry loop that passes the specific error output back to the
LLM on each retry — this is the only thing that makes retries useful.

### Checkov false positives on generated code

Checkov will flag legitimate Terraform patterns as violations. Common false positives for
this project:
- `CKV_AWS_8`: EC2 instance without IMDSv2 — generated code often omits `metadata_options`
- `CKV_AWS_135`: EC2 not using gp3 — generated code defaults to `gp2`
- `CKV_AWS_126`: EC2 detailed monitoring disabled — benign for this use case

Write a `.checkov.yaml` baseline file that suppresses known false positives with documented
reasons. Without it, every Checkov run will fail on generated code and you will hit the
3-retry limit on valid Terraform. This is one of the most common demo-week failures.

### OPA policy complexity trap

Writing Rego is non-trivial. Teams underestimate the debugging time. Write exactly 3-5
high-value policies (e.g., no public S3 buckets in generated code, no wildcard IAM in
generated code, explicit `depends_on` for multi-resource plans) and stop. Do not attempt
to write comprehensive policy suites in a 2.5-month project. Complex Rego that is wrong
is worse than no Rego.

Test OPA policies independently with `opa eval` against fixture plan JSON before integrating
into the validation loop. The integration point (feeding `terraform show -json` into OPA)
is where most teams lose days.

---

## TimescaleDB / Neon Gotchas

**Confidence: MEDIUM** — Neon's free tier details and TimescaleDB behavior on managed Postgres
are well-documented; some specifics may have changed since training cutoff.

### Neon free tier connection limits

Neon free tier allows 5 concurrent connections per branch (as of training data; verify current
limits at neon.tech). This is the most common cause of `too many clients` errors. SQLAlchemy
with `pool_size=5, max_overflow=10` will exceed this limit under concurrent agent load.
Set `pool_size=2, max_overflow=2, pool_timeout=30` for development. Use a connection pooler
(PgBouncer or Neon's built-in pooling) for production.

### Neon suspends idle compute after 5 minutes

On the free tier, Neon's compute suspends after 5 minutes of inactivity. The first query
after suspension takes 1-3 seconds to cold-start. This causes spurious SQLAlchemy connection
timeouts if `connect_timeout` is set too low. Use `connect_timeout=10` in the DATABASE_URL
and implement connection retry with exponential backoff on startup.

Agent CronJobs that run every 15-30 minutes will frequently hit a cold Neon instance. The
first query of each agent run must be treated as potentially slow.

### TimescaleDB hypertable creation is a one-time migration

`SELECT create_hypertable('metrics', 'time')` must be called exactly once, after the table
is created and before any data is inserted. If you run `CREATE TABLE` migrations more than
once (common during development schema iteration), you will get an error on re-creating
the hypertable. Use `IF NOT EXISTS` guards and check whether the table is already a hypertable
before calling `create_hypertable`. Alembic migrations that drop and recreate tables during
development will silently lose hypertable status.

### Compression policy timing

TimescaleDB compression requires data to be at least `compress_after` old before it compresses.
Setting `compress_after => INTERVAL '7 days'` on 2-week-old metrics is correct, but running
`SELECT compress_chunk(...)` manually during development will fail if the chunk is newer than
the interval. Do not set compression policies until you have actual data older than the interval.
For the demo, compression is cosmetic — skip it and add it post-PFA.

### `time_bucket` queries require the column to be a hypertable dimension

If you query `time_bucket('1 hour', created_at)` but the hypertable was partitioned on
`collected_at`, TimescaleDB will run the query correctly but without the performance benefits.
It will not error. This is a silent performance bug that only manifests at scale. Verify
that the dimension column name in `create_hypertable` exactly matches the column used in
`time_bucket` queries in Agent 2.

### Neon does not persist schema changes between dropped branches

If you use Neon branching for dev/prod isolation, schema migrations applied to a branch
are not automatically applied to other branches. Use a single `main` branch for the demo.
Branch management adds operational complexity that is not worth the benefit on a 2.5-month
project.

### append-only audit_log table

PostgreSQL has no native append-only constraint. The "immutable audit log" must be enforced
via a trigger that raises an exception on UPDATE or DELETE:

```sql
CREATE OR REPLACE FUNCTION prevent_audit_modification()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is immutable';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_immutable
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION prevent_audit_modification();
```

Without this trigger, the audit log is "append-only by convention" — which means it is not
actually immutable. A bug in Agent 4 could silently overwrite records. Add this trigger
in the initial migration and test it.

---

## HMAC Approval Security Pitfalls

**Confidence: HIGH** — HMAC-SHA256 approval workflow design is a well-understood security
domain; these pitfalls are standard.

### Replay attack if token has no expiry

An HMAC-SHA256 token that does not include a timestamp in the signed payload can be
replayed indefinitely. Include `issued_at` (Unix timestamp) and `expires_at` in the signed
payload, not as separate URL parameters. Verify on the server side that:

```python
now = int(time.time())
if token_data['expires_at'] < now:
    raise TokenExpired()
if token_data['expires_at'] - token_data['issued_at'] > 3600:
    raise TokenTooLong()  # Reject tokens with >1 hour validity window
```

The payload to sign must be a canonical serialization (e.g., JSON with sorted keys), not
a string concatenation. String concatenation allows parameter shuffling attacks.

### Replay attack if used tokens are not tracked

Even with expiry, a valid token can be replayed multiple times within the validity window
unless used tokens are tracked. Store executed token hashes in Redis (or the audit_log table)
and reject any token whose hash already exists:

```python
token_hash = sha256(raw_token).hexdigest()
if redis.exists(f"used_token:{token_hash}"):
    raise TokenAlreadyUsed()
redis.setex(f"used_token:{token_hash}", 3600, "1")  # TTL matches token expiry
```

### HMAC secret key management

The HMAC secret must be at least 32 bytes of cryptographic random data. `secrets.token_bytes(32)`.
Do not derive it from a password, project name, or environment name. Store it in an
environment variable, not in code or config files. Rotate it if it is ever logged.

If the secret changes between signing and verification (e.g., environment variable set
differently in two containers), all pending tokens become invalid. This is a demo-day failure
mode: ensure the secret is set identically in all services that verify tokens (FastAPI backend)
and any service that generates them (if different from the backend).

### Approval token sent via insecure channel

If the approval link is sent via email or Slack in plaintext, anyone who intercepts it
can replay it within the validity window. For the demo, this is acceptable. For any
real-world deployment description, note that the channel carrying the approval token must
be as secure as the action it authorizes.

### No rate limiting on the approval endpoint

A brute-force attack on the approval endpoint (guessing tokens) is theoretically possible
if the endpoint is not rate-limited. HMAC-SHA256 with 32-byte keys makes brute force
computationally infeasible, but add rate limiting anyway: 5 failed verification attempts
per IP per minute. Use FastAPI's `slowapi` middleware. This also catches misconfigured
clients that loop on retries.

### Action ID not included in the signed payload

A critical pitfall: if the HMAC token only signs `{user_id, timestamp}` but not the
specific finding/action ID, a valid approval token can be reused across different actions
by a malicious insider. The signed payload must include:

```json
{
  "finding_id": "uuid",
  "action_type": "stop_ec2",
  "resource_id": "i-0abc123",
  "issued_at": 1700000000,
  "expires_at": 1700003600
}
```

Every field in this payload is verified server-side against the pending action before
execution begins.

---

## Kubernetes Agent Failure Modes

**Confidence: MEDIUM** — Kubernetes Job/CronJob patterns are well-documented; k3s-specific
behaviors are based on training data.

### CronJob "concurrency policy" default allows overlapping runs

If Agent 1 (CronJob) takes longer than its schedule interval, Kubernetes will start a second
instance while the first is still running. Two collectors writing to the same TimescaleDB
table simultaneously will produce duplicate metrics. Set:

```yaml
spec:
  concurrencyPolicy: Forbid
```

on all CronJob definitions. The default is `Allow`. This is the single most common CronJob
mistake.

### Job pod OOMKilled silently

A Python agent that loads 500 CloudWatch log lines and performs NLP processing can easily
exceed its memory limit. Kubernetes kills the pod with `OOMKilled` exit code 137. The Job
marks the pod as failed and creates a new pod, up to `backoffLimit` times. You will see
repeated pod restarts in `kubectl get pods` but the agent appears to "keep retrying" with
no obvious error in the application logs.

Set resource limits conservatively for the demo:

```yaml
resources:
  requests:
    memory: "256Mi"
    cpu: "100m"
  limits:
    memory: "512Mi"
    cpu: "500m"
```

Monitor with `kubectl top pods` during the demo dry-run to catch OOMKill before the live demo.

### Failed Job pods accumulate and fill disk

By default, Kubernetes keeps all completed and failed Job pods indefinitely. On a k3s single
node with limited disk, this exhausts storage. Set:

```yaml
spec:
  ttlSecondsAfterFinished: 3600  # Clean up after 1 hour
  backoffLimit: 2  # Not 6 (default)
```

Without `ttlSecondsAfterFinished`, a node running CronJobs every 15 minutes will have
hundreds of dead pods within a week.

### No dead letter queue without explicit implementation

Kubernetes Jobs have no built-in dead letter queue. A failed Job just marks itself failed.
If Agent 3 fails because of a Groq 429, the finding that triggered it is lost unless you
explicitly write a "failed" status back to the database before the pod exits. Implement
a try/finally in every agent's main function:

```python
try:
    process_finding(finding_id)
except GroqRateLimitExceeded:
    db.update_finding(finding_id, status="groq_rate_limited")
except Exception as e:
    db.update_finding(finding_id, status="agent_error", error=str(e))
finally:
    db.close()
```

Without this, failed findings disappear silently and the dashboard shows no pending
recommendations even though findings exist.

### k3s and Docker Compose are different networking contexts

In Docker Compose, services communicate via service names (e.g., `redis`, `postgres`).
In k3s, services communicate via Kubernetes Service names and namespaces. If the DATABASE_URL
hardcodes `postgres:5432`, it will work in Docker Compose and fail in k3s. Use a single
`DATABASE_URL` environment variable that is overridden per environment. The "single ENV
variable switches dev/prod" design decision already accounts for this — enforce it strictly
and do not let individual service URLs leak into agent code.

### ConfigMap / Secret sync during demo

If a k3s ConfigMap or Secret is updated during the demo (e.g., to change a threshold),
running pods do not automatically see the change. CronJob pods that start after the update
will pick up the new value; pods that are already running will not. For the demo, treat
all configuration as immutable after the demo starts. Make all threshold changes before
starting the demo run.

---

## Top Project Killers (Final 2 Weeks)

**Confidence: HIGH** — These are consistent failure patterns across academic and startup
projects with similar profiles (multi-agent, LLM-integrated, tight demo deadline).

### 1. The demo environment is not the dev environment

This is the top killer. The demo runs against real AWS (not LocalStack). Every code path
that was tested against LocalStack Community must be verified against real AWS at least
one week before the demo — not the day before. Specific risks:

- Cost Explorer calls that were mocked in dev will hit real `ce` endpoints in prod, which
  requires IAM permissions that may not be set up correctly.
- CloudWatch metric data in a real AWS account may be in different units, aggregation periods,
  or namespaces than LocalStack fixtures assumed.
- The Neon database connection string changes between dev and prod. If `ENV=prod` is not
  wired to the right DATABASE_URL, all agents connect to the dev database in the demo.

Allocate one full week (Week 9 of 10) for "real AWS dry run." Not a day. A week.

### 2. Groq rate limit exhausted on demo day

If the team runs debugging sessions, benchmark tests, or retry loops against Groq on demo
day, the 1,000 RPD budget is consumed before the live demo. The demo then silently produces
no LLM recommendations, which is the core feature.

Mitigation: On demo day, treat the Groq budget as a hard resource. No exploratory calls.
Pre-generate and cache all recommendations for the demo scenario the day before. Have Ollama
running and tested as fallback. Verify the cache hit rate is 100% for the demo script before
the audience arrives.

### 3. HMAC token expiry fires during the demo

If the demo script includes a live approval flow and the approval step takes longer than
the token expiry window (e.g., walking the audience through the UI), the token expires
before the instructor clicks "Approve." Agent 4 rejects it, and the demo shows a security
error on the core feature.

Mitigation: Set token expiry to 30 minutes for the demo, not 5 minutes. Add a visible
countdown timer in the UI. Rehearse the approval flow and time it.

### 4. Schema migration not applied to the production database

The team develops against a local Neon dev branch or local PostgreSQL. The production Neon
branch has an older schema. On demo day, Agent 1 writes to a column that does not exist
in production and crashes with a PostgreSQL error.

Mitigation: Use Alembic. Run `alembic upgrade head` against the production Neon database
as the first step in every deployment, including the demo setup. Never apply schema changes
manually. Keep `alembic history` clean and linear — no branching migrations.

### 5. The validation loop never produces a passing result

If Checkov false positives are not suppressed, the validation loop will always fail on
generated Terraform, hit the 3-retry limit, and escalate to human. The demo will show
"escalated to human review" for every finding, which demonstrates the failure path, not
the success path.

Mitigation: Build a `.checkov.yaml` suppression file in Week 3, alongside Checkov integration.
Test the full validation loop (init → validate → plan → Checkov → OPA → PASS) against
a known-good Terraform file before integrating LLM generation. The loop must pass on
hand-written Terraform before it is trusted with LLM-generated Terraform.

---

## Additional Cross-Cutting Pitfalls

### React + FastAPI CORS

FastAPI's `CORSMiddleware` requires explicit origin configuration. `allow_origins=["*"]`
works in development but is insecure. For the demo, set:

```python
allow_origins=["http://localhost:3000", "http://<demo-host>:3000"]
```

The demo host URL must be known before the demo. If it changes (e.g., different machine,
different IP), CORS will block all API calls and the dashboard will show a blank state.
Test CORS from the actual demo machine, not from the development machine.

### WebSocket for live dashboard updates

FastAPI WebSocket support is stable, but the connection drops if the client goes idle for
more than the server's keepalive interval. React's `useEffect` cleanup must close the
WebSocket on component unmount, or the browser will hold stale connections that produce
duplicate events. Use a library like `reconnecting-websocket` rather than the native
WebSocket API to handle reconnection automatically.

If the demo includes live updates (findings appearing in real time), the WebSocket server
must be running before the dashboard loads. A startup order race (React loads before FastAPI
is ready) will produce a failed WebSocket connection that requires a page reload. Add a
`/health` endpoint and poll it from the React app before initiating the WebSocket connection.

### AWS MCP Server IAM scoping

The AWS MCP Server uses boto3 under the hood and inherits the credentials from the
environment. If the MCP server process has write permissions (e.g., the full `AdministratorAccess`
policy used during development), it is not read-only. Scope the IAM role/user used for the
MCP server to `ReadOnlyAccess` plus specific Cost Explorer permissions before the demo.
The security model of the project depends on this separation — test it explicitly.

### Redis not persisting between Docker Compose restarts

Redis in Docker Compose with no volume mount loses all cached recommendations on every
`docker compose down`. If the cache strategy relies on Redis persistence across restarts,
add a named volume:

```yaml
redis:
  image: redis:7-alpine
  volumes:
    - redis_data:/data
  command: redis-server --appendonly yes
```

Without this, every `docker compose down && docker compose up` during demo setup invalidates
the cache and may trigger Groq calls on the first demo run.

---

## Phase-Specific Warnings

| Phase Topic | Likely Pitfall | Mitigation |
|-------------|---------------|------------|
| Agent 1 (Collector) | LocalStack CloudWatch returns empty metrics | Seed script with `put_metric_data` fixtures |
| Agent 1 (Ghost resources) | EBS state transitions not auto-updated in LocalStack | Explicitly set volume states in fixtures |
| Agent 2 (Rules engine) | Threshold config in DB not loaded on startup | Warm-up query at agent init, cache in memory |
| Agent 3 (Validation loop) | Checkov false positives halt loop permanently | `.checkov.yaml` suppression file built Week 3 |
| Agent 3 (LLM retry) | Autonomous retry exhausts Groq budget | Hard stop on 429, no sleep-retry |
| Agent 3 (Terraform init) | Provider version hallucination fails init | Pin provider version in base template |
| Agent 4 (HMAC) | Token expiry during demo approval | 30-minute window, visible countdown in UI |
| Agent 4 (Rollback) | Rollback plan not generated if forward plan fails | Generate rollback plan at approval time, not execution time |
| Demo prep | Real AWS environment diverges from LocalStack fixtures | Full real-AWS dry run Week 9 |
| Demo prep | Groq budget exhausted before live demo | Cache all demo recommendations day before |

---

## Sources

**Confidence notes:** WebSearch and WebFetch were unavailable during this research session.
All findings are drawn from training data (knowledge cutoff August 2025) covering:

- LocalStack Community coverage matrix (official LocalStack documentation)
- Groq API documentation and rate limit specifications (console.groq.com/docs)
- Terraform CLI behavior, provider initialization, and validation pipeline patterns
- TimescaleDB official documentation (hypertable creation, compression policies)
- Neon PostgreSQL free tier specifications (neon.tech/docs)
- HMAC-SHA256 token design (IETF RFC 2104, common implementation patterns)
- Kubernetes Job/CronJob specification (kubernetes.io/docs)
- FastAPI CORS and WebSocket documentation
- Community post-mortems from LLM Terraform generation projects (GitHub issues, Reddit r/devops)

Items marked MEDIUM confidence should be spot-checked against current official documentation
before implementation, particularly Neon free tier connection limits (may have changed)
and LocalStack Community API coverage (updated frequently).
