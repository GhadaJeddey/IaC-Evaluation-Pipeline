# Features Research — FinOps Intelligence Platform

**Domain:** AWS cloud cost optimization / FinOps automation
**Researched:** 2026-04-03
**Confidence:** MEDIUM-HIGH — AWS native tool capabilities sourced directly from AWS documentation (HIGH confidence); commercial tool capabilities (CloudHealth, Spot.io, Harness CCM) sourced from training data cross-referenced with known product positioning (MEDIUM confidence — products evolve fast)

---

## Table Stakes

Features that any credible FinOps platform must have. Absence makes the product feel incomplete or untrustworthy to cloud engineers evaluating it.

| Feature | Why Expected | Complexity | In Scope? | Notes |
|---------|--------------|------------|-----------|-------|
| Idle/underutilized resource detection (EC2) | AWS Trusted Advisor has done this since 2013. If you don't do it, you're behind free tooling. | Low | IN SCOPE | CPU + network threshold, 14-day window. Solid. |
| Ghost resource identification (unattached EIPs, orphaned EBS, idle ELBs, old snapshots) | Every FinOps guide lists these as "low-hanging fruit." They are pure waste with zero justification. | Low | IN SCOPE | Trusted Advisor checks these too, but doesn't remediate them. |
| Estimated monthly savings per finding | Users won't act without seeing "$X/month." Savings quantification is the hook. | Low | PARTIAL | PROJECT.md mentions plain-language justification with dollar amounts (e.g., "$180/month") — confirm savings calculation is wired to pricing data, not just estimated. |
| Cost visibility dashboard | Engineers need to see what they're spending and where. Without a dashboard, the tool is a black box. | Medium | IN SCOPE | React dashboard with findings list, cost savings counter, cost-by-tag view. |
| Tag compliance enforcement | Tags are the only way to allocate costs to teams/projects. Without tag enforcement, chargeback is impossible. | Low | IN SCOPE | Tag scanning + violation detection in scope. |
| Actionable recommendations, not just alerts | The #1 complaint about existing tools: "it told me I have a problem but not how to fix it." Every commercial tool markets against this AWS gap. | Medium | IN SCOPE | LLM generates plain-language explanations + Terraform remediation. This is the core differentiator. |
| Audit trail for every action taken | Required for regulated industries and for engineer trust. "What happened and who approved it?" | Low | IN SCOPE | Immutable audit log in DB. |
| Multi-account / multi-region awareness | Enterprise teams run dozens of accounts. Single-account tools feel like toys. | High | OUT OF SCOPE (reasonable for PFA) | Single-account scope is fine for academic demo. Worth calling out as a known gap vs enterprise tools. |
| Historical cost trend data | Cost Explorer shows 13 months of history. Users need to see trends, not just point-in-time snapshots. | Medium | PARTIAL | TimescaleDB stores time-series metrics, but there is no explicit trend visualization mentioned. Cost Explorer integration via AWS MCP covers some of this. |
| Alerting / notifications | Engineers want proactive "your costs spiked 40% today" alerts, not just a dashboard to check manually. | Medium | PARTIAL | StatusCheckFailed alarm triggers crash diagnosis, but no general cost anomaly alerting in scope. |

**Confidence:** HIGH for AWS native coverage claims (sourced from official Trusted Advisor and Cost Optimization Hub docs). MEDIUM for "why expected" market claims.

---

## Differentiators

Features that set tools apart from the baseline. Not universally expected, but highly valued and cited as reasons to choose one tool over another.

| Feature | Value Proposition | Complexity | In Scope? | Notes |
|---------|-------------------|------------|-----------|-------|
| LLM-generated plain-language explanations | "CPU avg 3% for 14 days — this instance appears to be completely idle. Stopping it saves ~$47/month." vs. "Idle instance detected." The gap between alert fatigue and actionability. | Medium | IN SCOPE | Agent 3, Mode 1-4. This is the core bet. |
| LLM-generated Terraform remediation code | Engineers know what to fix but don't want to write the IaC by hand. Code generation that produces validated, runnable Terraform is rare in the market. AWS native tools never generate IaC. | High | IN SCOPE | Hybrid script (simple) + LLM (complex). Validation loop (Terraform validate → Checkov → OPA) is genuinely unusual. |
| Crash RCA from logs (EC2 StatusCheckFailed → log pull → LLM diagnosis) | No AWS native tool does this. Trusted Advisor flags unhealthy instances; it does not explain why they crashed. Bridging cost and reliability is a market gap. | High | IN SCOPE | Agent 2b → Agent 3 Mode 4. Powerful demo scenario. |
| Cryptographic approval gate (HMAC-SHA256 signed) | Most automation tools either require no approval or use a soft "confirm" button. Cryptographic proof of intent with replay protection is enterprise-grade. | Medium | IN SCOPE | Agent 4. Strong differentiator for regulated environments. |
| Automated rollback on failure | "It applied the change and something broke — now what?" Most tools walk away after the change. Rollback-on-failure closes the loop. | Medium | IN SCOPE | Agent 4, sequential execution with stability checks. |
| IaC policy validation before human sees the output (Checkov + OPA) | Generating Terraform is easy; generating Terraform that passes security policy is hard. Pre-validated IaC prevents the approver from seeing unsafe code. | High | IN SCOPE | Validation loop in Agent 3. Unusual in commercial market — Infracost estimates cost but doesn't validate security. |
| Configurable thresholds without code changes | Rigid thresholds ("CPU < 10% = idle") break in different environments. Thresholds stored in DB, editable at runtime, is a mature feature of commercial tools. | Low | IN SCOPE | Rules stored in DB, configurable. |
| Topological execution ordering | Multi-resource changes have dependencies. Deleting an EBS volume before the snapshot is taken is destructive. Safe ordering matters. | Medium | IN SCOPE | Topological sort in Agent 2. Uncommon in tools that just generate recommendation lists. |
| CloudWatch Logs cost analysis (volume + retention) | Logs cost is an invisible killer — teams enable verbose logging in Lambda and forget. No AWS native tool surfaces "your /aws/lambda/payment-service is ingesting 40GB/month." | Medium | IN SCOPE | Log group scanning, retention enforcement, S3 archival recommendation. |
| Spot/Reserved Instance optimization | Automated RI purchase recommendations and spot fleet management. Spot.io built an entire company on this. | High | OUT OF SCOPE | Requires commitment management, financial modeling. Out of PFA scope. AWS Cost Optimization Hub now covers RI/SP recommendations natively. |
| Kubernetes cost allocation (pod-level) | K8s cost visibility requires cluster-level metric integration (Kubecost pattern). Only relevant for k8s workloads. | High | OUT OF SCOPE | EC2-native scope. K8s is deployment target, not analysis target. |
| Anomaly detection (ML-based spike detection) | AWS Cost Anomaly Detection (native) covers this for billing spikes. Commercial tools add workload-level anomaly detection. | High | OUT OF SCOPE | Correctly cut. Deterministic rules cover demo scenarios. |

**Confidence:** MEDIUM — LLM-in-FinOps features are fast-moving; market positioning claims based on training data as of 2025.

---

## Competitive Gap Analysis

What existing tools cover vs. what they miss. This section explains why the planned platform has a credible market thesis.

### AWS-Native Tools

#### AWS Trusted Advisor
**Covers (HIGH confidence — sourced from AWS docs):**
- EC2 idle/over-provisioned detection (low utilization flags)
- Idle RDS instances (no connections 7+ days)
- Idle load balancers (<100 requests/day or no instances)
- Unassociated Elastic IP addresses
- EBS over-provisioned volumes
- S3 buckets without lifecycle policies
- Lambda over-provisioned memory, excessive timeouts, high error rates
- RI/Savings Plan purchase recommendations
- NAT gateway idle detection
- Stopped EC2 instances (>30 days)

**Gaps:**
- No automated remediation whatsoever — recommendations only
- No IaC generation — tells you what to fix, never how
- No natural language explanation — output is a structured alert, not an explanation an engineer can act on without further research
- No crash diagnosis or RCA capability
- Business Support plan required for most cost checks (free tier only gets ~6 checks)
- Tag compliance awareness is minimal — flags untagged resources but offers no enforcement
- Log ingestion volume costs invisible (no CloudWatch Logs cost analysis)
- No audit trail for actions taken (there are no actions taken)

#### AWS Compute Optimizer
**Covers (HIGH confidence — sourced from AWS docs):**
- EC2 instance rightsizing (14-day default lookback, up to 93 days with paid enhanced metrics)
- EBS volume rightsizing
- Lambda memory rightsizing
- ECS Fargate rightsizing
- Auto Scaling group optimization
- RDS and Aurora recommendations

**Gaps:**
- Opt-in required and time-gated (needs 14 days of metric data before first recommendations)
- EC2-focused; does not cover network resources, EIPs, snapshots, log groups
- No automated remediation — recommendations only
- No IaC generation
- No natural language explanation
- No tag compliance coverage
- No crash diagnosis

#### AWS Cost Explorer / Cost Optimization Hub
**Covers (HIGH confidence — sourced from AWS docs):**
- Historical cost visualization (13 months)
- Cost forecasting (18 months ahead)
- RI and Savings Plan purchase recommendations
- Rightsizing recommendations (now deferred to Cost Optimization Hub)
- Cost by service, tag, account, region grouping
- Anomaly detection for billing spikes (separate product: Cost Anomaly Detection)

**Gaps:**
- No automated remediation — analysis and recommendations only
- Cost Optimization Hub recommends but never executes
- No ghost resource scanning (EIPs, EBS, snapshots)
- No log ingestion cost analysis
- No tag compliance enforcement
- No IaC generation
- No crash diagnosis
- API costs $0.01/paginated request — costs money at scale to query programmatically
- 24-hour data refresh lag (not real-time)

**AWS native gap summary:** All three tools converge on the same failure mode: they tell you what is wrong and give estimated savings, but they never explain it in plain language, never generate the code to fix it, never execute anything, and never validate that the fix is safe. The "alert-and-abandon" pattern.

### Commercial Tools

#### CloudHealth (VMware/Broadcom)
**Covers (MEDIUM confidence — training data, product evolves):**
- Multi-cloud cost visibility (AWS, Azure, GCP)
- Cost allocation by team, project, environment via tags and perspectives
- Reserved Instance and Savings Plan lifecycle management (purchase, track, exchange)
- Policy-based governance (budget alerts, tag policy enforcement, auto-stop policies)
- Chargeback and showback reports for internal billing
- Rightsizing recommendations
- Multi-account consolidated billing view

**Gaps:**
- No IaC generation
- No LLM explanations
- Remediation is limited to pre-configured policy actions (stop instances on schedule, alert on budget breach) — not flexible enough for arbitrary resource changes
- No crash diagnosis
- Expensive ($$$) — enterprise pricing, not accessible for small teams
- Complexity of setup means time-to-first-insight is high

#### Spot.io (NetApp)
**Covers (MEDIUM confidence):**
- Automated Spot instance management (core product) — continuously replaces On-Demand with Spot, manages interruptions transparently
- Ocean for Kubernetes: pod-level bin-packing, right-sizing, spot-aware scheduling
- Elastigroup: workload-level instance fleet management
- Reserved Instance and Savings Plan optimization and trading
- Cost analysis and allocation reporting
- Rightsizing recommendations for non-Spot workloads

**Gaps:**
- Core value is Spot optimization — if you don't use Spot instances, value drops sharply
- No IaC generation
- No LLM explanations or crash diagnosis
- Tag compliance enforcement is limited
- Not designed for teams who want to understand and approve changes — it acts autonomously (which is a risk trade-off many teams are unwilling to accept)
- No log cost analysis

#### Harness Cloud Cost Management (CCM)
**Covers (MEDIUM confidence):**
- Multi-cloud cost visibility with auto-stopping (idle resource detection + scheduled stop/start)
- Kubernetes cost allocation (namespace, pod, workload level)
- Asset governance (policy rules that trigger automated actions)
- Anomaly detection for cost spikes
- RI and Savings Plan recommendations
- Cost allocation by tag, label, cluster

**Gaps:**
- No IaC generation
- No LLM-generated explanations (as of training data — this may have changed in 2025)
- Auto-stopping is schedule-based, not intention-based (no approval gate)
- No crash diagnosis
- Governance rules are pre-defined policy patterns, not adaptive recommendations
- No audit trail with cryptographic proof of approval

#### Infracost
**Covers (MEDIUM confidence):**
- Pre-deployment cost estimation for Terraform plans (shows cost delta before apply)
- CI/CD integration (PR comments with cost impact)
- Policy enforcement on cost changes (reject PRs that exceed cost thresholds)
- Cloud pricing data API

**Gaps:**
- Purely pre-deployment — has no awareness of running resources or actual utilization
- No waste detection, no ghost resources, no idle EC2 detection
- No execution or remediation — estimation only
- No crash diagnosis
- Complementary to, not competitive with, a runtime optimization platform

**Commercial tool gap summary:** Commercial tools solve cost *visibility* and *scheduling* well. They solve *commitment management* (RI/SP) well. They do not solve: plain-language explanation of why a resource is wasteful, IaC generation for arbitrary remediation, crash diagnosis from logs, or cryptographically enforced human approval gates. These are genuine gaps.

---

## Missing from Current Scope

High-value features absent from the current plan that are worth considering (with honest assessment of whether they fit the PFA timeline).

| Feature | Value | Complexity | Recommendation |
|---------|-------|------------|----------------|
| **Cost anomaly alerting (billing spike detection)** | Engineers want to know when costs spike unexpectedly — e.g., "your EC2 spend is 3x the 7-day average today." AWS Cost Anomaly Detection covers this natively but only at billing granularity (daily, 24h lag). A real-time or near-real-time spike alert on collected metrics would be valuable. | Medium | CONSIDER — Could be triggered from the TimescaleDB metrics without a new agent. A simple threshold rule ("EC2 cost delta >50% vs 7-day average") surfaced as a finding type adds value with low implementation cost. |
| **Savings Plan / Reserved Instance purchase recommendations** | RI/SP is where the biggest AWS discounts live (30-60%). Every commercial tool leads with this. AWS Cost Explorer already covers it, but the platform could surface these recommendations alongside operational findings. | Medium | LOW PRIORITY — AWS MCP Server + Cost Explorer API already provides this data. Surfacing it in the dashboard is a UI task, not a new agent capability. Add to dashboard backlog. |
| **Scheduled resource start/stop (office hours automation)** | Dev/test environments running 24/7 when engineers only work 8 hours a day. Classic "turn it off at night" pattern. Commercial tools (Harness auto-stop) and AWS Instance Scheduler both do this. | Medium | OUT OF SCOPE for PFA — Requires a scheduler agent and cron-based execution pipeline. Scope risk is high. Mention as post-PFA extension. |
| **Lambda function cost analysis (memory + duration profiling)** | Lambda cost = duration × memory. Over-provisioned memory in Lambda wastes money even at high invocation counts. Compute Optimizer covers this, but the platform only collects Lambda invocations/duration/errors — does it surface a "reduce Lambda memory from 1024MB to 256MB, save $X/month" finding? | Medium | GAP — Lambda is in the collector scope but Analysis agent rules don't include Lambda memory rightsizing. This is a one-rule addition to Agent 2 with measurable dollar impact. Consider adding. |
| **S3 storage class optimization (Intelligent Tiering, Glacier)** | S3 Standard costs 5-10x more than Glacier for infrequently accessed data. Lifecycle policy generation is a legitimate FinOps activity. | Medium | Correctly cut from scope. EC2/Logs focus is the right choice. Note it as a post-PFA addition. |
| **Budget alerts with team notification** | Set a budget ($500/month) and get Slack/email alerts when approaching it. Table stakes in commercial tools. | Low | OUT OF SCOPE — AWS Budgets API covers this natively. Not worth building custom. |
| **Cost allocation reporting (chargeback/showback)** | Multi-team environments need to see "Team A spent $X this month." Tags + Cost Explorer grouping covers this, but a built-in report builder is a commercial differentiator. | High | OUT OF SCOPE — Enterprise feature, not relevant for PFA demo. |
| **Drift detection (Terraform state vs. actual AWS state)** | Resources provisioned outside Terraform don't appear in state, creating "shadow IT" that FinOps platforms miss. Detecting state drift would make remediation Terraform more reliable. | High | OUT OF SCOPE — Complex, requires full Terraform state management. Post-PFA. |
| **Real-time cost streaming (AWS Cost and Usage Report + S3 + Athena)** | Near-real-time cost data vs. Cost Explorer's 24h lag. Commercial tools with CUR processing can show costs updated every hour. | High | OUT OF SCOPE — CUR processing pipeline is a significant infrastructure investment. Cost Explorer via MCP is adequate for demo. |

**Key gap to address:** Lambda memory rightsizing is the one item that fits the existing architecture, adds a concrete dollar-savings story, and is a single additional rule in Agent 2. Everything else is correctly deferred or out of scope.

---

## Anti-Features

Things to deliberately NOT build, with explicit rationale.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| **Autonomous execution without approval** | The defining failure mode of automation tools: "it deleted a production database." The HMAC-SHA256 approval gate exists precisely to prevent this. Removing it or making it optional for "small" changes erodes trust catastrophically. Any finding, however small, must go through the approval gate. | Always require cryptographic approval. "Small" changes are where autonomous tools cause the most surprising damage. |
| **LLM making cost decisions** | LLMs hallucinate. An LLM that decides "this EC2 is idle" based on log analysis can be fooled by bursty workloads, cron jobs, or monitoring agents. The deterministic rules engine is the decision-maker — the LLM is the explainer. Inverting this creates a platform that is confidently wrong. | Keep the rules engine as the sole source of findings. LLM only gets invoked after a finding is confirmed. |
| **Rewriting Terraform state** | Modifying existing Terraform state files autonomously creates drift and breaks the IaC contract. The platform generates new Terraform that applies changes, it does not modify state. | Generate additive or targeted Terraform (resource changes, not state manipulation). |
| **Multi-cloud scope creep** | Azure and GCP support requires new collector agents, new pricing APIs, new Terraform providers, and new testing environments. The AWS story is already comprehensive. Multi-cloud is a feature for v2 or v3 with a real team. | Explicitly document AWS-only scope. Demo the depth, not the breadth. |
| **Over-explaining remediation with multiple options** | Giving engineers "here are 5 options to consider" is the same as giving them nothing — it recreates the analysis paralysis the tool is meant to eliminate. The LLM should provide one recommended action with a clear rationale. | One recommendation, one Terraform plan, one business justification. Alternatives in the audit log if needed. |
| **Aggressive ghost resource deletion** | Deleting an EBS volume that happens to be detached during a maintenance window is irreversible. Ghost resource detection should have a waiting period (the 48-hour rule in scope) and clear state tracking before escalating to a deletion recommendation. | Require the 48-hour confirmation window before a ghost resource finding graduates to a destroy plan. |
| **Building a custom cost pricing API** | AWS pricing data is complex (on-demand vs. reserved vs. spot, per-region, per-OS, per-tenancy). Building and maintaining a custom pricing scraper is a project unto itself. | Use the AWS Pricing API (`pricing.us-east-1.amazonaws.com`) or the instance-prices table populated from public pricing data. |
| **Chat / conversational FinOps UI** | A full chat interface where engineers ask "what's my most expensive service?" is a product, not a PFA project. AWS MCP Server already provides some of this. The dashboard with structured findings is the right scope. | Dashboard with structured findings + cost-by-tag view. Mention conversational interface as a post-PFA addition. |

---

## Sources and Confidence Notes

| Claim | Source | Confidence |
|-------|--------|------------|
| Trusted Advisor cost optimization checks (47+) | AWS official docs: `docs.aws.amazon.com/awssupport/latest/user/cost-optimization-checks.html` | HIGH |
| Compute Optimizer resource types and 14-day lookback | AWS official docs: `docs.aws.amazon.com/compute-optimizer/latest/ug/what-is-compute-optimizer.html` | HIGH |
| Cost Explorer features and 13-month history | AWS official docs: `docs.aws.amazon.com/cost-management/latest/userguide/ce-what-is.html` | HIGH |
| Cost Optimization Hub — no automated remediation | AWS official docs: `docs.aws.amazon.com/cost-management/latest/userguide/cost-optimization-hub.html` | HIGH |
| Cost Explorer rightsizing now deprecated in favor of Cost Optimization Hub | AWS official docs: rightsizing page explicitly states "We recommend that you use Cost Optimization Hub" | HIGH |
| CloudHealth, Spot.io, Harness CCM capabilities | Training data (knowledge cutoff Aug 2025) — product features may have changed | MEDIUM |
| "Alert-and-abandon" pattern as the primary market complaint | Widely documented industry pain point; corroborated by all three AWS tool docs (none mention remediation) | MEDIUM-HIGH |
| Infracost capabilities | Training data | MEDIUM |
| Lambda memory rightsizing gap in scope | Analysis of PROJECT.md Agent 2 rules — no Lambda memory rule present | HIGH (gap confirmed by reading PROJECT.md) |

---

## Summary for Roadmap

**Strengths of current scope:**

The planned platform covers table-stakes detection (idle EC2, ghost resources, log costs, tag compliance) and then leaps significantly ahead of AWS native tools on the execution side: LLM explanations, validated IaC generation, cryptographic approval gates, rollback, and crash RCA. This combination does not exist in any single tool in the 2025 market as a complete open-source system.

**One genuine gap to address:**

Lambda memory rightsizing is the highest-value missing feature that fits existing architecture. Agent 2 already collects Lambda metrics; adding a "Lambda over-provisioned memory" rule (memory > 2x p99 required memory → downsize recommendation) would close a real cost story with minimal implementation cost.

**What to say about multi-account and RI/SP:**

Both are table stakes for enterprise FinOps but correctly out of scope for PFA. In a demo context, acknowledge these as "the natural next phase" rather than gaps — it shows architectural awareness.

**The competitive positioning holds:**

AWS native tools recommend but never explain, generate, or execute. Commercial tools execute but never explain in plain language, never generate validated IaC, and never use cryptographic approval gates. This platform sits in the gap between "alert" and "audit-safe automated action" — and that gap is real.
