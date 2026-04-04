# Stack Research — FinOps Intelligence Platform

**Researched:** 2026-04-03
**Researcher:** GSD Research Agent (claude-sonnet-4-6)
**Knowledge cutoff:** August 2025
**Overall confidence:** MEDIUM (web lookup denied; all versions verified against training data only — pin with `pip index versions <pkg>` before first install)

---

## Core Agent Stack (Python)

### boto3 / AWS SDK

**Recommendation:** Use `boto3` (synchronous) as the primary AWS SDK for all agents.

| Library | Version (pinned floor) | Purpose | Confidence |
|---------|----------------------|---------|-----------|
| boto3 | >= 1.34.x | AWS API calls (EC2, CloudWatch, S3, Lambda, resource tagging) | HIGH |
| botocore | >= 1.34.x (auto-pinned by boto3) | Core HTTP engine; never pin separately | HIGH |
| boto3-stubs[ec2,cloudwatch,s3,lambda,resourcegroupstaggingapi,sts] | same as boto3 | Type stubs for IDE safety — catches wrong parameter names before runtime | HIGH |

**Why synchronous boto3, not aiobotocore:**

The Collector agent is a scheduled CronJob that runs infrequently (every N minutes). It is I/O-bound, but `asyncio` adds ceremony without meaningful throughput benefit for a CronJob that simply fires, collects, and exits. `aiobotocore` has historically lagged boto3 releases by days-to-weeks, which creates maintenance drag (you must match `aiobotocore` to `botocore` versions exactly). The risk of version skew is not worth the concurrency benefit for batch collection.

**Exception:** If Crash Diagnosis (Agent 2b) needs to pull CloudWatch logs while simultaneously checking instance status, use `asyncio.gather` with standard boto3 + `concurrent.futures.ThreadPoolExecutor`. This gives async-like parallelism without aiobotocore's version coupling.

```python
# Recommended pattern for parallel AWS calls in Agent 2b
import asyncio
from concurrent.futures import ThreadPoolExecutor
import boto3

executor = ThreadPoolExecutor(max_workers=4)

async def get_crash_context(instance_id: str) -> dict:
    loop = asyncio.get_event_loop()
    logs_future = loop.run_in_executor(executor, fetch_last_500_log_lines, instance_id)
    status_future = loop.run_in_executor(executor, fetch_instance_status, instance_id)
    logs, status = await asyncio.gather(logs_future, status_future)
    return {"logs": logs, "status": status}
```

**What NOT to use:**
- `aiobotocore` — version coupling risk, not needed for batch CronJobs
- `s3transfer` pinned separately — boto3 manages this internally; explicit pinning causes conflicts
- The AWS SDK for Pandas (`awswrangler`) — pulls in a heavy pandas/pyarrow stack; unnecessary since you write directly to TimescaleDB

### Scheduling

**Recommendation:** APScheduler 3.x inside the FastAPI process, or a dedicated CronJob manifest (K8s/Docker Compose) that simply runs the agent script.

| Library | Version | Purpose | Confidence |
|---------|---------|---------|-----------|
| APScheduler | >= 3.10.x | In-process scheduling for dev; interval/cron triggers | MEDIUM |

**Pattern rationale:** For Docker Compose dev, a `while True: sleep(interval)` loop or APScheduler inside the agent container is sufficient. For k3s prod, use a Kubernetes `CronJob` manifest — this is the idiomatic K8s pattern and requires no in-process scheduler at all. APScheduler is the right in-process library if you want a unified dev/prod path without switching to K8s primitives immediately.

**What NOT to use:**
- Celery Beat — over-engineered for 4 agents with simple interval schedules; adds a broker dependency that you already have (Redis) but complicates the mental model
- Airflow — academic overkill; full DAG orchestration for what is effectively 4 shell scripts

### Async patterns (FastAPI)

| Library | Version | Purpose | Confidence |
|---------|---------|---------|-----------|
| FastAPI | >= 0.111.x | REST API + WebSocket for live dashboard updates | HIGH |
| uvicorn[standard] | >= 0.30.x | ASGI server with uvloop for performance | HIGH |
| asyncpg | >= 0.29.x | Async PostgreSQL driver (used under SQLAlchemy or directly) | HIGH |
| SQLAlchemy | >= 2.0.x | ORM + async session support (use `AsyncSession`) | HIGH |
| alembic | >= 1.13.x | Schema migrations (critical for TimescaleDB hypertable setup) | HIGH |
| redis[hiredis] | >= 5.0.x | Redis client; hiredis for 10x parse performance | HIGH |
| rq (Redis Queue) | >= 1.16.x | Simple job queue over Redis; fits the agent trigger pattern | MEDIUM |

**Why RQ over Celery:** RQ has a dramatically simpler mental model for a 4-person academic team. Each agent is a Python function; RQ enqueues it, a worker runs it. Celery has serialization gotchas, complex broker URL patterns, and requires understanding `beat`, `flower`, and `worker` processes. RQ is transparent and debuggable in ~30 minutes.

**What NOT to use:**
- Dramatiq — solid library but smaller community; RQ is better documented for beginners
- Huey — minimal features for minimal complexity, but RQ has better FastAPI integration examples

### Data validation

| Library | Version | Purpose | Confidence |
|---------|---------|---------|-----------|
| pydantic | >= 2.7.x | Request/response models, agent config validation | HIGH |
| pydantic-settings | >= 2.3.x | ENV-based config with `.env` support (the single ENV switch) | HIGH |

**Note on the single ENV switch pattern:** `pydantic-settings` with a `Settings` class and `env_file = ".env"` is the cleanest implementation. One variable (`ENVIRONMENT=dev|prod`) controls which AWS endpoint URL gets injected into boto3 clients.

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    environment: str = "dev"
    aws_endpoint_url: str | None = None  # None = real AWS; set to LocalStack URL in dev
    database_url: str
    redis_url: str
    groq_api_key: str
    groq_model: str = "llama-3.3-70b-versatile"

    model_config = {"env_file": ".env"}
```

---

## Deterministic Rules Engine (Agent 2 — Analyse)

**Recommendation:** Do NOT use an external rules engine library. Build a thin, table-driven evaluator in pure Python.

### Why no external library

The rules in this project are fully enumerable, threshold-driven, and stored in the DB. An external rules engine (rule-engine, py_rete, business-rules) adds a DSL you must learn, maintain, and debug. Your rules are trivially expressible as Python functions parameterized by DB-fetched thresholds:

```python
# Rules stored in DB: { "idle_ec2_cpu_threshold": 10, "idle_ec2_net_threshold": 5, ... }
# Evaluator pattern — no library needed

@dataclass
class Rule:
    name: str
    condition: Callable[[MetricSnapshot, dict], bool]
    finding_type: str
    severity: str

RULES = [
    Rule(
        name="idle_ec2",
        condition=lambda m, t: m.cpu_avg <= t["idle_cpu"] and m.net_avg_mbh <= t["idle_net"],
        finding_type="STOP_RECOMMENDATION",
        severity="HIGH",
    ),
    Rule(
        name="oversized_ec2",
        condition=lambda m, t: m.cpu_max <= t["oversize_cpu"],
        finding_type="DOWNSIZE_RECOMMENDATION",
        severity="MEDIUM",
    ),
]
```

This is ~50 lines, fully type-checked by mypy/pyright, trivially unit-tested, and requires zero new dependencies.

### Libraries to reach for IF complexity grows

| Library | Version | When to use | Confidence |
|---------|---------|-------------|-----------|
| rule-engine | >= 4.0.x | If rules must be stored as user-editable strings (not needed here — thresholds in DB is sufficient) | MEDIUM |
| pandas | >= 2.2.x | If metric aggregation outgrows raw SQL (e.g., rolling window calculations not easily done in TimescaleDB) | MEDIUM |

**What NOT to use:**
- `py_rete` — Rete algorithm for complex forward-chaining inference; massive overkill for threshold comparisons
- `drools` / `easy-rules` (Java) — wrong language ecosystem
- `pyknow` — unmaintained (last commit 2019)

### Topological sort (execution ordering)

Use Python's stdlib `graphlib.TopologicalSorter` (available since Python 3.9). Zero dependencies.

```python
from graphlib import TopologicalSorter

ts = TopologicalSorter(dependency_graph)
safe_order = list(ts.static_order())
```

---

## LLM Integration (Groq + Ollama)

### Recommended approach: LiteLLM as the unified interface

**Recommendation:** Use `litellm` as the single abstraction layer over Groq and Ollama. It provides a consistent `completion()` interface, handles provider-specific quirks, and makes model swapping a config change.

| Library | Version | Purpose | Confidence |
|---------|---------|---------|-----------|
| litellm | >= 1.40.x | Unified LLM client: Groq + Ollama + any future provider | HIGH |
| groq | >= 0.9.x | Direct Groq SDK (fallback if LiteLLM has issues; keep as dependency) | HIGH |

**Why LiteLLM over writing your own abstraction:**
- Handles Groq's `application/json` streaming format and Ollama's different response shape transparently
- Rate limit handling: built-in `RateLimitError` catching with configurable fallback routing
- Model aliasing: define `"primary"` → `"groq/llama-3.3-70b-versatile"` and `"fallback"` → `"ollama/qwen2.5-coder:7b"` in config
- The 1,000 RPD Groq constraint is managed by catching `litellm.RateLimitError` and routing to Ollama

**Fallback pattern:**

```python
import litellm
from litellm import completion, RateLimitError

PRIMARY_MODEL = "groq/llama-3.3-70b-versatile"  # or qwen/qwen3-32b when available on Groq
FALLBACK_MODEL = "ollama/qwen2.5-coder:7b"       # local Ollama

def generate_with_fallback(messages: list[dict], **kwargs) -> str:
    try:
        resp = completion(model=PRIMARY_MODEL, messages=messages, **kwargs)
        return resp.choices[0].message.content
    except RateLimitError:
        # Groq 1,000 RPD exhausted — fall to Ollama silently
        resp = completion(
            model=FALLBACK_MODEL,
            api_base="http://localhost:11434",  # or ollama service in Docker Compose
            messages=messages,
            **kwargs,
        )
        return resp.choices[0].message.content
```

**Result caching (critical for 1,000 RPD constraint):**

Use `diskcache` or Redis-backed caching keyed on `(model, hash(prompt))`. Cache TTL should be 24h for recommendation text (findings don't change every minute).

```python
import hashlib, json
import redis

def cache_key(model: str, messages: list[dict]) -> str:
    payload = json.dumps({"model": model, "messages": messages}, sort_keys=True)
    return f"llm:{hashlib.sha256(payload.encode()).hexdigest()}"
```

**What NOT to use:**
- LangChain — adds 300+ transitive dependencies, introduces abstractions (chains, agents) that fight your deterministic architecture; your agent logic is simple enough that LangChain's agent loop would be a liability, not an asset
- LlamaIndex — built for RAG/document retrieval, not for this structured Terraform generation use case
- OpenAI SDK directly — works for Groq (Groq is OpenAI-API-compatible), but doesn't handle Ollama; LiteLLM subsumes it

### Groq model selection (at time of research)

| Model | Groq ID | Best for | Context window |
|-------|---------|---------|----------------|
| Llama 3.3 70B | `llama-3.3-70b-versatile` | Terraform generation (Modes 1-3), general reasoning | 128k |
| Qwen3-32B | Check Groq docs — not confirmed on Groq free tier at cutoff | Coding tasks, Terraform | 32k |
| Qwen 2.5 Coder 7B | Ollama only | Local dev fallback, fast iteration | 32k |

**Confidence on Qwen3-32B on Groq:** LOW — verify availability at `console.groq.com/models` before coding against it. The benchmark plan references it, but Groq model availability changes monthly.

### Ollama setup for Docker Compose

```yaml
# docker-compose.yml excerpt
ollama:
  image: ollama/ollama:latest
  ports:
    - "11434:11434"
  volumes:
    - ollama_models:/root/.ollama
  # GPU passthrough (optional for dev):
  # deploy:
  #   resources:
  #     reservations:
  #       devices:
  #         - capabilities: [gpu]
```

Pull models at container start: `ollama pull qwen2.5-coder:7b` — add to a `docker-compose.override.yml` entrypoint script.

---

## Database (TimescaleDB + Neon)

### Schema design for time-series metrics

**Hypertable design principles:**

TimescaleDB partitions data by time via hypertables. The `metrics` table is the primary hypertable — every other table (instances, findings, etc.) is a standard Postgres relational table.

**Core schema decisions:**

```sql
-- Standard relational tables
CREATE TABLE instances (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id VARCHAR(32) UNIQUE NOT NULL,  -- "i-0abc123..."
    region      VARCHAR(32) NOT NULL,
    account_id  VARCHAR(32) NOT NULL,
    instance_type VARCHAR(32),
    state       VARCHAR(32),
    tags        JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Hypertable: time + instance_id is the natural compound partition key
CREATE TABLE metrics (
    time          TIMESTAMPTZ NOT NULL,
    instance_id   VARCHAR(32) NOT NULL,  -- FK to instances.instance_id
    metric_name   VARCHAR(64) NOT NULL,  -- 'cpu_utilization', 'network_in', etc.
    value         DOUBLE PRECISION NOT NULL,
    unit          VARCHAR(32),           -- 'Percent', 'Bytes', etc.
    period_seconds INT DEFAULT 300       -- CloudWatch resolution
);

-- Convert to hypertable AFTER table creation, BEFORE inserting data
SELECT create_hypertable('metrics', 'time', chunk_time_interval => INTERVAL '7 days');

-- Composite index: most queries filter by instance_id + time range
CREATE INDEX ON metrics (instance_id, time DESC);

-- Optional: compression (saves 90%+ disk on old chunks) — enable after 7 days
ALTER TABLE metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instance_id',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('metrics', INTERVAL '7 days');
```

**Chunk interval rationale:**

- 7-day chunks for `metrics`: CloudWatch data arrives in 5-minute intervals; 7 days = ~2,016 rows per instance. For a demo with ~10 instances this is trivially small. Use 1-day chunks if simulating high cardinality (1,000+ instances).
- Do NOT use default 1-week chunk interval if data density is very low — 7 days is fine for this project.

**Continuous aggregates for rule evaluation:**

Rather than computing `AVG(cpu)` over 14 days in Python, use a TimescaleDB continuous aggregate:

```sql
CREATE MATERIALIZED VIEW metrics_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time) AS bucket,
    instance_id,
    metric_name,
    AVG(value)  AS avg_value,
    MAX(value)  AS max_value,
    MIN(value)  AS min_value,
    COUNT(*)    AS sample_count
FROM metrics
GROUP BY bucket, instance_id, metric_name;

-- Refresh policy: refresh daily, keep 90 days of aggregates
SELECT add_continuous_aggregate_policy('metrics_daily',
    start_offset => INTERVAL '90 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 day'
);
```

The Analyse agent then queries `metrics_daily` instead of the raw `metrics` hypertable — this is the correct pattern and makes rules like "cpu_avg <= 10% for 14 days" a simple SQL query.

**Neon-specific gotchas:**

| Gotcha | Impact | Mitigation |
|--------|--------|-----------|
| Neon free tier suspends after 5 minutes of inactivity | CronJob agents see cold-start latency (1-3s) | Add a health-check ping before running agents; use `connect_timeout=10` in DATABASE_URL |
| TimescaleDB extension must be enabled per-database | Schema migrations will fail silently if extension missing | Add `CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;` as migration step 0 |
| Neon branches (dev/prod) are separate databases | LocalStack dev doesn't need Neon — use plain Postgres + TimescaleDB in Docker Compose for local dev | Mount a `postgres:16-alpine` with `timescale/timescaledb-ha:pg16` image |
| Connection pooling: Neon uses PgBouncer in transaction mode | SQLAlchemy `pool_size` must be set conservatively (max 5 for free tier); avoid server-side prepared statements | Use `statement_cache_size=0` in asyncpg connection args |
| Continuous aggregates require `timescaledb.enable_cagg_reorder_and_merge = on` | Not always default on managed services | Verify with `SHOW timescaledb.enable_cagg_reorder_and_merge;` after connecting |

**Python libraries for DB access:**

| Library | Version | Purpose | Confidence |
|---------|---------|---------|-----------|
| SQLAlchemy | >= 2.0.30 | Async ORM; use `AsyncSession` + `async_sessionmaker` | HIGH |
| asyncpg | >= 0.29.x | Native async Postgres driver (used by SQLAlchemy async engine) | HIGH |
| alembic | >= 1.13.x | Migrations; hypertable creation in a custom migration step | HIGH |
| psycopg2-binary | >= 2.9.x | Sync fallback for scripts and Alembic CLI | MEDIUM |

**What NOT to use:**
- Raw asyncpg without SQLAlchemy for the main codebase — loses type safety and migration tracking
- `databases` library — stale (last major release 2022), superseded by SQLAlchemy 2.0 async
- `tortoise-orm` — different ORM paradigm, no migration story as mature as Alembic

---

## IaC Validation Pipeline

### Terraform OSS

**Recommended versions:**

| Tool | Version | Confidence |
|------|---------|-----------|
| Terraform | >= 1.8.x (latest stable in 1.9/1.10 range at cutoff) | MEDIUM — pin exact version in `.terraform-version`; use `tfenv` to manage |
| Checkov | >= 3.2.x | HIGH |
| OPA / conftest | OPA >= 0.65.x, conftest >= 0.50.x | MEDIUM |

**Verify before pinning:** `terraform --version`, `checkov --version`, `conftest --version` after install.

### Validation pipeline order

```
terraform fmt --check          # style gate (fast, catches obvious issues)
terraform validate              # syntax + provider schema validation
checkov -d . --framework terraform --output json   # security/compliance scan
conftest test --policy ./policies .                # OPA custom policies
```

Run in this exact order. `terraform validate` requires `terraform init` first — add a `terraform init -backend=false` step for CI.

**Agent 3 validation loop implementation:**

```python
import subprocess, json

MAX_RETRIES = 3

def validate_terraform(tf_dir: str, retry_count: int = 0) -> ValidationResult:
    if retry_count >= MAX_RETRIES:
        return ValidationResult(passed=False, escalate_to_human=True)

    results = {}

    # Step 1: terraform validate
    r = subprocess.run(
        ["terraform", "validate", "-json"],
        cwd=tf_dir, capture_output=True, text=True
    )
    results["validate"] = json.loads(r.stdout)
    if not results["validate"].get("valid"):
        return ValidationResult(passed=False, errors=results, retry_count=retry_count)

    # Step 2: Checkov
    r = subprocess.run(
        ["checkov", "-d", tf_dir, "--framework", "terraform",
         "--output", "json", "--quiet"],
        capture_output=True, text=True
    )
    checkov_output = json.loads(r.stdout) if r.stdout else {}
    results["checkov"] = checkov_output

    # Step 3: OPA via conftest
    r = subprocess.run(
        ["conftest", "test", "--policy", "./policies", tf_dir],
        capture_output=True, text=True
    )
    results["opa"] = {"passed": r.returncode == 0, "output": r.stdout}

    all_passed = (
        results["validate"].get("valid")
        and not checkov_output.get("results", {}).get("failed_checks")
        and results["opa"]["passed"]
    )
    return ValidationResult(passed=all_passed, errors=results, retry_count=retry_count)
```

### Checkov configuration

Create `.checkov.yaml` at repo root:

```yaml
# .checkov.yaml
framework:
  - terraform
compact: true
quiet: true
# Skip checks that are inappropriate for demo/free-tier environments:
skip-check:
  - CKV_AWS_18   # S3 access logging — skip for demo
  - CKV_AWS_144  # S3 cross-region replication — skip for demo
  - CKV2_AWS_62  # S3 event notifications — skip for demo
output: json
```

**What checks to keep enabled:** All EC2, IAM, and security group checks. These are the checks most relevant to cost-related Terraform changes.

### OPA policies for this project

Write policies in `./policies/terraform.rego`. The key policies for a cost optimization platform:

```rego
# policies/terraform.rego
package main

# Deny any Terraform that creates resources without required tags
deny[msg] {
    resource := input.resource.aws_instance[name]
    not resource.config.tags.Environment
    msg := sprintf("aws_instance.%v must have an Environment tag", [name])
}

# Deny instance types larger than allowed in validation loop
deny[msg] {
    resource := input.resource.aws_instance[name]
    not startswith(resource.config.instance_type, "t")
    not startswith(resource.config.instance_type, "m5")
    msg := sprintf("aws_instance.%v uses non-approved instance type", [name])
}
```

**What NOT to use:**
- Sentinel (HashiCorp) — requires Terraform Cloud/Enterprise; not available with Terraform OSS
- tfsec — deprecated in favour of Checkov and trivy; do not introduce a second scanning tool
- terragrunt — adds a wrapper abstraction that is not worth the complexity for this project scope

---

## LocalStack Community Gotchas

**Critical:** LocalStack Community is free and open-source but is NOT a full AWS emulator. It emulates a specific subset. Knowing the boundaries before building Agent 1 saves days of debugging.

### Service support matrix (Community vs Pro)

| Service | Community support | Known gaps | Impact on this project |
|---------|------------------|-----------|----------------------|
| EC2 | Partial | Describe operations mostly work; `run_instances` works; StatusCheckFailed alarms are simulated but not real | Agent 1 Collector works; Agent 2b Crash Diagnosis needs mock alarm injection |
| CloudWatch Metrics | Partial | `get_metric_statistics` works; custom namespace ingestion works; `put_metric_data` works | Metric collection works; historical data must be seeded manually |
| CloudWatch Logs | Partial | `get_log_events`, `describe_log_groups`, `filter_log_events` work; `get_log_record` unreliable | Agent 2b log pulling: use `get_log_events` not `get_log_record` |
| CloudWatch Alarms | Partial | `put_metric_alarm` works; alarm state transitions are manual (use `set_alarm_state` for testing) | Crash Diagnosis trigger must be tested by calling `set_alarm_state` directly |
| S3 | Good | Most operations supported; S3 Intelligent-Tiering not supported (out of scope anyway) | Fully usable |
| Lambda | Good | Invocation, listing, metrics work; runtime execution sandboxing differs from real AWS | Metric collection works |
| Cost Explorer | NOT supported | Entirely absent from Community | Must mock; use a fixtures file that returns realistic CUR-format JSON |
| Resource Groups Tagging API | Partial | `get_resources` works; tag-based filtering is limited | Tag compliance scan works; validate response format differs slightly |
| SQS | Good | Reliable; use for agent job triggers | Fully usable |
| EIP / EBS / ELB (ghost resources) | Partial | `describe_addresses`, `describe_volumes`, `describe_load_balancers` work; state machine for "unattached" detection requires manual setup | Seed test data via boto3 `create_*` calls in fixtures |

### Critical configuration gotchas

**1. Endpoint URL injection (the most important gotcha)**

LocalStack runs at `http://localhost:4566`. Every boto3 client must receive `endpoint_url` when in dev mode. Forgetting even one client sends a real AWS request.

```python
import boto3
from app.config import settings

def get_boto3_client(service: str, **kwargs):
    """Factory that injects LocalStack endpoint in dev, real AWS in prod."""
    client_kwargs = {
        "region_name": settings.aws_region,
        **kwargs,
    }
    if settings.environment == "dev":
        client_kwargs["endpoint_url"] = "http://localhost:4566"
        # LocalStack Community accepts any credentials
        client_kwargs["aws_access_key_id"] = "test"
        client_kwargs["aws_secret_access_key"] = "test"
    return boto3.client(service, **client_kwargs)
```

**Never** hardcode `endpoint_url` anywhere except this factory.

**2. Resource state is ephemeral**

LocalStack Community state is lost on container restart by default. For development:

```yaml
# docker-compose.yml
localstack:
  image: localstack/localstack:latest  # pin to 3.x when project starts
  ports:
    - "4566:4566"
  environment:
    - SERVICES=ec2,s3,cloudwatch,logs,lambda,sqs,resourcegroupstaggingapi
    - DEBUG=0
    - PERSISTENCE=1   # saves state to /var/lib/localstack/state
  volumes:
    - localstack_data:/var/lib/localstack
    - /var/run/docker.sock:/var/run/docker.sock  # required for Lambda execution
```

Add a `fixtures/seed_localstack.py` script that creates test EC2 instances, injects CloudWatch metrics, creates log groups — run once after `docker compose up`.

**3. CloudWatch metric gaps**

LocalStack does NOT auto-generate CloudWatch metrics for EC2 instances it creates. You must inject metrics via `put_metric_data`:

```python
cw = get_boto3_client("cloudwatch")
cw.put_metric_data(
    Namespace="AWS/EC2",
    MetricData=[{
        "MetricName": "CPUUtilization",
        "Dimensions": [{"Name": "InstanceId", "Value": "i-0abc123"}],
        "Timestamp": datetime.utcnow(),
        "Value": 3.5,  # seed an idle instance
        "Unit": "Percent",
        "StorageResolution": 60,
    }]
)
```

For 14-day rule evaluation, seed metrics going back 14 days using a loop.

**4. Cost Explorer is completely absent**

There is no `ce` (Cost Explorer) service in LocalStack Community. Create a mock:

```python
# app/aws/cost_explorer.py
from app.config import settings

def get_cost_and_usage(*args, **kwargs):
    if settings.environment == "dev":
        # Return realistic fixture data
        return _load_fixture("cost_explorer_response.json")
    # Real AWS call
    client = boto3.client("ce", region_name="us-east-1")  # CE is global, always us-east-1
    return client.get_cost_and_usage(*args, **kwargs)
```

**5. LocalStack Pro vs Community version drift**

LocalStack's Community Docker image (`localstack/localstack:latest`) follows the same version as Pro but with Pro features stubbed out (they return empty results or NotImplemented, not errors). This means:

- Some Pro-only API calls silently return empty responses instead of raising `NotImplementedError`
- Always test that a response you receive is non-empty, not just non-error

**6. IAM is not enforced in Community**

LocalStack Community does not enforce IAM permissions. This means your Agent 4 scoped-write IAM model will not be tested locally. Add an integration test that runs against real AWS (even a dry-run) to validate IAM boundaries.

**What NOT to use:**
- `localstack/localstack-pro` — requires a paid license; Community is sufficient for this project's services
- `moto` (mocking library) — valid alternative but requires in-process mocking setup; LocalStack is superior because it runs as a real service that all containers can reach, matching prod topology more closely
- `localstack-client` pip package — deprecated; use standard boto3 with `endpoint_url`

---

## Recommendations & Versions

### Pinned dependency manifest (requirements.in / pyproject.toml)

```toml
# pyproject.toml — tool.poetry.dependencies or [project.dependencies]

# AWS
boto3 = ">=1.34,<2.0"
boto3-stubs = {extras = ["ec2","cloudwatch","s3","lambda","resourcegroupstaggingapi","sts"], version = ">=1.34"}

# FastAPI
fastapi = ">=0.111,<1.0"
uvicorn = {extras = ["standard"], version = ">=0.30"}
python-multipart = ">=0.0.9"

# Database
sqlalchemy = {extras = ["asyncio"], version = ">=2.0.30"}
asyncpg = ">=0.29"
alembic = ">=1.13"
psycopg2-binary = ">=2.9"   # alembic CLI sync driver

# Redis / queuing
redis = {extras = ["hiredis"], version = ">=5.0"}
rq = ">=1.16"

# LLM
litellm = ">=1.40"
groq = ">=0.9"

# Validation / config
pydantic = ">=2.7"
pydantic-settings = ">=2.3"

# Scheduling (optional, only if not using K8s CronJob)
apscheduler = ">=3.10"

# IaC tooling (install via apt/brew/tfenv, not pip):
# terraform >= 1.8.x
# checkov >= 3.2.x  (pip install checkov)
# conftest >= 0.50.x (binary from github releases)
# opa >= 0.65.x (binary)
```

**Checkov is installed via pip** (it is a Python tool):
```
pip install checkov>=3.2
```

### Python version

Use **Python 3.12** (current stable at cutoff). It is supported by all listed libraries. FastAPI's async improvements and SQLAlchemy 2.0 async perform better on 3.12 than 3.11 due to free-threading improvements in 3.12.

**Do NOT use Python 3.13** — as of research cutoff it had breaking changes in some SQLAlchemy + asyncpg interaction scenarios; wait for community validation.

### Version confidence summary

| Component | Recommended version | Confidence | Verify with |
|-----------|--------------------|-----------|----|
| Python | 3.12.x | HIGH | `python --version` |
| boto3 | >=1.34,<2.0 | HIGH | `pip index versions boto3` |
| FastAPI | >=0.111 | HIGH | `pip index versions fastapi` |
| SQLAlchemy | >=2.0.30 | HIGH | `pip index versions sqlalchemy` |
| asyncpg | >=0.29 | HIGH | `pip index versions asyncpg` |
| TimescaleDB | 2.14.x on Postgres 16 | MEDIUM | Neon dashboard shows extension version |
| litellm | >=1.40 | MEDIUM | `pip index versions litellm` (changes fast) |
| Checkov | >=3.2 | MEDIUM | `checkov --version` |
| Terraform | >=1.8 | MEDIUM | `terraform version` |
| conftest/OPA | >=0.50 / >=0.65 | LOW | Check OpenPolicyAgent releases |
| LocalStack | 3.x (latest stable) | MEDIUM | `localstack --version` |
| Qwen3-32B on Groq | Unverified | LOW | `console.groq.com/models` |
| RQ | >=1.16 | MEDIUM | `pip index versions rq` |
| APScheduler | >=3.10 | MEDIUM | `pip index versions apscheduler` |

### Architecture integration summary

```
Agent 1 (Collector)
  boto3 → CloudWatch, EC2, S3, Lambda, ResourceTagging
  SQLAlchemy (async) → TimescaleDB metrics hypertable
  APScheduler / K8s CronJob trigger

Agent 2 (Analyse)
  SQLAlchemy (async) → read from metrics_daily continuous aggregate
  Pure Python rules evaluator (no external library)
  graphlib.TopologicalSorter → execution ordering
  SQLAlchemy (async) → write findings table

Agent 2b (Crash Diagnosis)
  boto3 → CloudWatch Logs (get_log_events)
  ThreadPoolExecutor for parallel log + status fetch
  RQ job → enqueues Agent 3

Agent 3 (Recommendation / LLM)
  litellm → Groq (primary) / Ollama (fallback on RateLimitError)
  Redis (via rq) → job state
  subprocess → terraform validate, checkov, conftest
  SQLAlchemy (async) → write recommendations table

Agent 4 (Action)
  HMAC-SHA256 validation (stdlib hmac)
  boto3 → EC2 stop/resize (scoped write IAM)
  subprocess → terraform apply
  SQLAlchemy (async) → append-only audit_log
```

---

## Sources

All findings based on training data (knowledge cutoff August 2025). External lookup was unavailable during this research session.

- boto3 documentation: https://boto3.amazonaws.com/v1/documentation/api/latest/index.html
- TimescaleDB documentation: https://docs.timescale.com/
- LiteLLM documentation: https://docs.litellm.ai/
- LocalStack Community documentation: https://docs.localstack.cloud/
- Checkov documentation: https://www.checkov.io/
- FastAPI documentation: https://fastapi.tiangolo.com/
- SQLAlchemy 2.0 async docs: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html
- OPA / conftest: https://www.conftest.dev/

**Confidence note:** Version numbers reflect stable releases known at training cutoff. Run `pip index versions <package>` for each library before pinning in `pyproject.toml`. Groq model availability is especially volatile — verify at `console.groq.com/models` before benchmarking.
