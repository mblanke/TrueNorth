# Claude AI Refresh
## TrueNorth AI Engineering Environment: Claude Code + Taz + Codex

**Date:** 2026-08-18  
**Primary project:** TrueNorth Range  
**Mission owner:** Claude Code  
**Scope:** Repair, modernize, validate, and document the AI-assisted development environment for TrueNorth.

---

# 1. Mission

You are Claude Code working on the **TrueNorth Range** project.

Your task is to inspect the current development environment, determine what actually exists today, and build a reliable multi-model engineering workflow around:

1. **Claude Code** — lead engineer, orchestrator, final integrator.
2. **Taz** — private local AI system running on a Dell PowerEdge R7725 with 2 × NVIDIA H200 GPUs.
3. **GLM-5.2** — Taz's current primary local model.
4. **Codex CLI** — independent coding agent/reviewer, preferably exposed to Claude Code through MCP.
5. **A second local coding-specialist model** — evaluate a current Qwen coding model alongside GLM-5.2, but do not replace GLM-5.2 until real TrueNorth benchmarks justify it.

The objective is **not** to replace Claude with a local model.

The desired system is a small AI engineering team in which each model has a clear role, all changes are auditable, and no model can silently damage the TrueNorth working tree.

---

# 2. Critical Architecture Clarification

## 2.1 Taz is the current local AI system

The known current Taz hardware is:

```text
Taz
└── Dell PowerEdge R7725
    ├── NVIDIA H200 #0
    ├── NVIDIA H200 #1
    └── GLM-5.2
```

Taz is intended to become the private local AI engineering and cyber-analysis system supporting TrueNorth development.

---

## 2.2 The two NVIDIA GB10 systems are NOT part of this setup

There are two separate NVIDIA GB10 systems associated with another local-AI experiment.

They are:

- **not currently connected to TrueNorth**
- **not part of Taz**
- **not part of the current Claude/Taz/Codex architecture**
- **not required for this refresh**
- **not migration targets**
- **not to be reconfigured by this task**

Files such as:

```text
CLAUDE_AI.md
ai.yml
```

may describe a historical or separate GB10-based 70B training/RAG project.

Treat those files as **reference material for a separate project**, not as authoritative TrueNorth infrastructure.

### Possible future use

At some later stage of TrueNorth development, the GB10 systems **may** be connected as dedicated content-generation or training-support nodes, for example:

```text
Future Optional TrueNorth Content Lab

GB10-A
├── scenario generation
├── synthetic telemetry generation
├── exercise-content generation
└── dataset preparation

GB10-B
├── detection-content generation
├── AAR training datasets
├── adversary-profile generation
└── offline experimentation
```

That is **future scope only**.

Do not implement it during this refresh unless explicitly instructed.

---

# 3. TrueNorth Working Tree Warning

The TrueNorth repository may contain approximately two months of important work that has not been committed.

Therefore:

> The current working tree is more authoritative than the most recent Git commit and may be more authoritative than the README and architecture documentation.

Do not assume a capability is missing because it is absent from old documentation.

Do not assume a documented capability still matches the code.

Before making architectural recommendations, reconstruct the current state from:

1. actual source code
2. current configuration
3. currently running services
4. uncommitted files
5. Git diff
6. tests
7. documentation

in that order of trust.

---

# 4. Non-Negotiable Safety Rules

## 4.1 Protect the dirty working tree

Do **not** run destructive Git operations without explicit human authorization.

Forbidden unless specifically approved:

```bash
git reset --hard
git clean -fd
git clean -fdx
git checkout -- .
git restore .
git stash
git rebase
git push --force
```

Do not create a giant convenience commit containing the existing dirty working tree simply to simplify this task.

Before editing anything, capture:

```bash
git branch --show-current
git status
git status --short
git diff --stat
git diff --name-status
git log -10 --oneline --decorate
```

Store reports under:

```text
.ai-refresh/audit/
```

If an experiment requires isolation, prefer a dedicated **Git worktree**.

---

## 4.2 Back up AI configuration first

Before changing any relevant configuration, create timestamped backups.

Inspect and back up files if present:

```text
~/.claude.json
~/.claude/
~/.codex/
~/.config/
.mcp.json
CLAUDE.md
CLAUDE.local.md
settings.json
settings.local.json
docker-compose*.yml
compose*.yml
systemd units for Taz/model servers
vLLM configs
SGLang configs
Ollama configs
reverse-proxy configs
environment files
AI wrapper scripts
MCP server configs
```

Never commit secrets.

---

## 4.3 Secrets

Never print or save complete:

- API keys
- bearer tokens
- passwords
- SSH private keys
- Git credentials
- hypervisor credentials
- database credentials
- Keycloak secrets
- MinIO secrets
- cloud credentials

Redact them in reports.

Example:

```text
OPENAI_API_KEY=REDACTED
ANTHROPIC_API_KEY=REDACTED
Authorization: Bearer REDACTED
```

---

## 4.4 Initial AI permissions

During initial setup:

### Taz

Start Taz with:

- repository read access
- architecture analysis
- code analysis
- diff review
- test generation
- cyber review
- query generation

Do not initially give Taz unrestricted shell or infrastructure administration.

### Codex

Use Codex primarily for:

- independent code review
- implementation proposals
- bug hunting
- test review
- diff review
- adversarial critique

### Claude

Claude Code remains responsible for:

- deciding what changes are accepted
- editing the canonical working tree
- executing approved commands
- coordinating tests
- reconciling disagreements
- final integration

---

# 5. Phase 0 — Discover What Exists

Do not assume how Taz, Claude Code, or Codex currently run.

Inspect first.

Create:

```text
.ai-refresh/
├── audit/
├── reports/
├── benchmarks/
├── configs/
└── logs/
```

Ensure `.ai-refresh/` contains no credentials.

---

# 6. Inventory the TrueNorth Development Machine

Capture relevant environment data:

```bash
hostnamectl
uname -a
cat /etc/os-release
git --version
python3 --version
node --version
npm --version
docker --version
docker compose version
```

Also determine whether these are installed and their versions:

```text
claude
codex
uv
pip
poetry
terraform
packer
kubectl
helm
ruff
pytest
```

Record results in:

```text
.ai-refresh/reports/dev-environment.md
```

Do not install or upgrade anything until the current environment is documented.

---

# 7. Inventory Taz

Connect to Taz using the existing approved method.

Determine the actual configuration.

Run safe inspection commands such as:

```bash
hostnamectl
uname -a
cat /etc/os-release
nvidia-smi
nvidia-smi -L
free -h
df -h
lsblk
```

Record:

- host name
- OS
- RAM
- GPU model
- GPU count
- VRAM
- NVIDIA driver version
- CUDA version
- model storage paths
- network interfaces
- active AI services
- container runtime
- service manager

Save:

```text
.ai-refresh/reports/taz-hardware.md
```

---

# 8. Discover How GLM-5.2 Is Actually Running

Do not assume vLLM, SGLang, Ollama, or Docker.

Inspect:

```bash
ps aux | grep -Ei 'vllm|sglang|ollama|glm|python'
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}'
systemctl --type=service --state=running | grep -Ei 'taz|glm|vllm|sglang|ollama'
ss -ltnp
```

Determine:

- serving framework
- exact model ID/path
- model revision
- quantization
- tensor parallel size
- data parallel size
- context length
- GPU memory utilization
- KV cache configuration
- reasoning parser
- tool-call parser
- API port
- bind address
- startup mechanism
- health check
- restart behavior
- log location
- environment file

Create:

```text
.ai-refresh/reports/taz-glm52-runtime.md
```

---

# 9. Normalize Taz Behind a Stable API

The rest of the system must not depend on whatever command happens to launch GLM-5.2 today.

Create a stable Taz service boundary.

Preferred logical interface:

```text
Claude Code
     │
     ▼
  Taz MCP
     │
     ▼
 Taz Gateway
     │
     ├── GLM-5.2
     └── Qwen coding model
```

The underlying model servers should preferably expose an OpenAI-compatible API.

Do not expose model endpoints publicly.

Prefer:

```text
127.0.0.1
private management VLAN
authenticated internal reverse proxy
or another explicitly controlled private interface
```

Do not bind an unauthenticated LLM endpoint to all interfaces unless there is an intentional network control around it.

---

# 10. Taz Should Become a Service, Not a Terminal Session

If GLM-5.2 currently dies when an SSH terminal closes, correct that.

Use an appropriate persistent mechanism such as:

- systemd
- Docker Compose with restart policies
- another existing supervised service mechanism already used on Taz

The service must provide:

```bash
systemctl status <service>
systemctl restart <service>
journalctl -u <service>
```

or equivalent operational controls.

Required behavior:

- starts after reboot
- restarts after failure
- logs persistently
- reports health
- does not depend on an interactive terminal
- does not expose secrets in process arguments
- does not silently auto-upgrade model weights

---

# 11. Do Not Replace GLM-5.2 Yet

GLM-5.2 is already running successfully on Taz and has been considered acceptable for this local environment.

Treat it as the baseline model.

Do not remove it.

Do not overwrite its weights.

Do not replace its service definition until the replacement configuration has been tested and rollback is documented.

Logical role:

```text
GLM-5.2
├── architecture reasoning
├── large-context analysis
├── cyber reasoning
├── scenario analysis
├── threat-hunt reasoning
├── design review
├── planning
└── complex cross-file analysis
```

---

# 12. Evaluate a Qwen Coding Model

A trusted recommendation exists for a newer Qwen model said to perform in the same general coding class as Claude Opus 4.6.

Do **not** blindly install a model based solely on the name in this document.

Before downloading anything:

1. identify the exact intended current Qwen model
2. verify the official publisher
3. verify license
4. verify model card
5. verify checksums/revision
6. verify serving requirements
7. verify tool-calling support
8. verify context limits
9. verify H200 compatibility
10. estimate VRAM/KV-cache requirements
11. confirm that it is appropriate for a fully local deployment

Prefer official model repositories.

Avoid random third-party repacks unless they are independently reviewed.

If the intended candidate is **Qwen3.8-27B** or another newer Qwen coding/agent model, verify that this is still the correct candidate before installation.

---

# 13. Model Provenance and Supply-Chain Review

Before any new model is trusted with TrueNorth source code, produce:

```text
.ai-refresh/reports/model-provenance.md
```

For each model record:

```yaml
model:
  name:
  publisher:
  country_of_origin:
  repository:
  revision:
  license:
  weight_format:
  quantization:
  downloaded_from:
  checksum:
  requires_remote_code:
  tokenizer_source:
  serving_framework:
  outbound_network_required:
  telemetry:
  notes:
```

The country of origin alone is **not** a determination that a model is safe or unsafe.

Assess:

- provenance
- exact weights
- dependencies
- remote code
- serving stack
- network behavior
- telemetry
- license
- reproducibility
- model behavior
- tool permissions

Prefer `safetensors`.

Avoid `trust_remote_code=True` unless the required code has been reviewed and there is a documented reason.

---

# 14. Desired Taz Model Architecture

If benchmarking supports it, evolve Taz into a two-role model system:

```text
                         TAZ
                Dell PowerEdge R7725
                    2 × NVIDIA H200
                          │
             ┌────────────┴────────────┐
             │                         │
             ▼                         ▼
         GLM-5.2                 Qwen Coder
       Taz Architect             Taz Engineer
             │                         │
       architecture                  coding
       cyber analysis                refactors
       planning                      tests
       deep reasoning                debugging
       repo analysis                 tool use
       scenario design               implementation
             │                         │
             └────────────┬────────────┘
                          ▼
                      Taz Gateway
                          │
                          ▼
                        Taz MCP
```

This architecture is a goal, not a mandate.

If GLM-5.2 proves better for both roles, keep one model.

If another verified model clearly performs better, document the evidence.

---

# 15. Build a Taz Gateway

Create a small maintainable gateway rather than hard-coding model URLs into Claude configuration.

Suggested project:

```text
tools/
└── taz-gateway/
    ├── README.md
    ├── pyproject.toml
    ├── app/
    │   ├── main.py
    │   ├── config.py
    │   ├── models.py
    │   ├── router.py
    │   ├── health.py
    │   └── audit.py
    ├── tests/
    └── config.example.yaml
```

The gateway should provide logical model aliases:

```text
taz-architect
taz-code
```

Do not force callers to know the underlying model name.

Example internal mapping:

```yaml
models:
  taz-architect:
    backend: glm52
    endpoint: ${TAZ_GLM_ENDPOINT}

  taz-code:
    backend: qwen
    endpoint: ${TAZ_QWEN_ENDPOINT}
```

If only GLM-5.2 is available:

```yaml
models:
  taz-architect:
    backend: glm52

  taz-code:
    backend: glm52
```

This permits gradual migration.

---

# 16. Build a Taz MCP Server

Claude Code should interact with Taz through explicit bounded tools rather than a generic unrestricted shell.

Suggested MCP tools:

```text
taz_health
taz_ask
taz_analyze_repo
taz_review_architecture
taz_review_security
taz_review_diff
taz_generate_tests
taz_review_tests
taz_review_scenario
taz_review_detection
taz_generate_vql
taz_generate_opensearch_query
taz_compare_docs_to_code
```

Each tool should:

- have a clear schema
- limit path scope
- log invocation metadata
- avoid leaking secrets
- return structured results
- have sensible timeouts
- fail cleanly
- avoid arbitrary shell execution

Prefer read-only tools initially.

---

# 17. Recommended Taz MCP Response Shape

Where practical, use structured output:

```json
{
  "summary": "Short conclusion",
  "findings": [
    {
      "severity": "high",
      "file": "path/to/file.py",
      "line": 123,
      "issue": "Description",
      "recommendation": "Recommended action"
    }
  ],
  "assumptions": [],
  "tests_recommended": [],
  "confidence": 0.91
}
```

For cyber analysis, allow optional:

```json
{
  "mitre_attack": [],
  "evidence": [],
  "queries": [],
  "false_positive_notes": []
}
```

---

# 18. Connect Codex to Claude Code

Determine the currently installed Codex CLI version and supported integration methods.

Prefer using the official Codex MCP-server capability if supported by the installed version.

The intended architecture is:

```text
Claude Code
    │
    ├── Taz MCP
    │
    └── Codex MCP
```

Do not route Codex through Taz.

Do not route Taz through Codex.

They should provide independent opinions.

Codex's initial role is:

```text
Codex
├── independent code review
├── patch critique
├── regression analysis
├── test-gap detection
├── API-contract review
├── async/concurrency review
├── security review
└── alternative implementation proposal
```

---

# 19. Claude Is the Lead Engineer

Claude Code should own orchestration.

For a significant TrueNorth task, target this flow:

```text
User Request
      │
      ▼
Claude inspects relevant code
      │
      ├───────────────┐
      ▼               ▼
Taz analysis      Codex analysis
      │               │
      └───────┬───────┘
              ▼
     Claude reconciles
        disagreements
              │
              ▼
       implementation
              │
              ▼
          test suite
              │
      ┌───────┴────────┐
      ▼                ▼
Taz review        Codex review
      │                │
      └───────┬────────┘
              ▼
            fixes
              │
              ▼
         DoD / gates
              │
              ▼
       human-readable
          summary
```

Do not require all three models for trivial edits.

Use the multi-agent review loop for:

- architecture changes
- security-sensitive changes
- scenario-engine changes
- provisioning logic
- authentication/RBAC
- telemetry/scoring logic
- AI orchestration
- significant refactors
- complicated bugs
- changes spanning multiple subsystems

---

# 20. Prevent AI Groupthink

Claude must not tell Taz or Codex what the other model concluded before obtaining their initial analysis.

Use:

```text
Claude -> Taz: independent review
Claude -> Codex: independent review
```

Then compare.

Only after independent results should Claude ask:

```text
Taz: critique this competing conclusion.
Codex: critique this competing conclusion.
```

This preserves useful disagreement.

---

# 21. Claude Code Specialist Agents

Create or refresh project-scoped Claude agents if they improve the current setup.

Suggested structure:

```text
.claude/
└── agents/
    ├── truenorth-architect.md
    ├── cyber-reviewer.md
    ├── range-engineer.md
    ├── scenario-engineer.md
    ├── telemetry-engineer.md
    ├── detection-engineer.md
    ├── frontend-reviewer.md
    ├── test-engineer.md
    └── adversarial-reviewer.md
```

Do not create agents merely for decoration.

Each agent must have:

- a specific responsibility
- bounded tools
- expected output
- escalation criteria
- clear relationship to Taz/Codex

---

# 22. Recommended Agent Responsibilities

## truenorth-architect

Focus:

- cross-system architecture
- service boundaries
- scalability
- data flows
- multi-tenancy
- integration design
- technical debt

Use Taz heavily.

---

## cyber-reviewer

Focus:

- defensive cybersecurity
- IR workflows
- threat hunting
- detection engineering
- attack simulation safety
- telemetry quality
- exercise realism

Use Taz for cyber reasoning and Codex for implementation review.

---

## scenario-engineer

Focus:

- scenario YAML
- injectors
- validators
- branches
- timing
- ground truth
- scoring
- scenario reproducibility

---

## telemetry-engineer

Focus:

- OpenSearch
- event pipelines
- schemas
- indexing
- scale
- retention
- correlation
- WebSocket update paths

---

## detection-engineer

Focus:

- Sigma
- YARA
- Velociraptor VQL
- OpenSearch DSL
- Suricata
- Zeek
- rule validation
- false positives

---

## test-engineer

Focus:

- pytest
- integration tests
- contract tests
- regression tests
- performance tests
- failure injection

Codex should be used frequently here.

---

## adversarial-reviewer

Instruction:

> Assume the proposed implementation contains subtle mistakes. Find the strongest technical reasons not to merge it.

This agent should preferentially use Codex for an independent review.

---

# 23. Add a TrueNorth AI Review Command

Create a reusable project workflow such as:

```text
/truenorth-review
```

or its current Claude Code equivalent.

The workflow should:

1. inspect Git status
2. identify changed areas
3. determine which subsystems are affected
4. ask Taz for an independent architecture/security review
5. ask Codex for an independent code/regression review
6. reconcile findings
7. run relevant tests
8. report failures
9. propose fixes
10. rerun relevant tests after fixes
11. summarize remaining risk

Do not automatically commit.

---

# 24. Add a Current-State Reconstruction Command

Create:

```text
/truenorth-baseline
```

Purpose:

> Reconstruct what TrueNorth actually contains today from the working tree rather than stale documentation.

Output should classify capabilities as:

```text
IMPLEMENTED
PARTIAL
BROKEN
UNTESTED
UNDOCUMENTED
PLANNED
STALE-DOC
DUPLICATED
DEPRECATED
```

Required analysis areas:

```text
control-plane/API
Angular frontend
worker/Celery
scenario engine
injectors
validators
range lifecycle
Proxmox/Terraform
Packer
Keycloak/RBAC
Redis
OpenSearch
MinIO
WebSockets
xAPI/cmi5
AAR
PDF reporting
AI orchestrator
Velociraptor
HELK
content library
testing
CI/CD
security
multi-tenancy
observability
```

Save the report as:

```text
docs/current-state.md
```

Do not overwrite existing architecture docs until the report has been reviewed.

---

# 25. Benchmark GLM-5.2 Against the Qwen Candidate

Do not use only generic coding benchmarks.

Create a **TrueNorth-specific evaluation harness**.

Directory:

```text
.ai-refresh/benchmarks/truenorth-model-eval/
```

At minimum include tasks covering:

1. repository architecture comprehension
2. FastAPI implementation
3. Pydantic/SQLAlchemy correctness
4. async Python
5. Celery/Redis workflow debugging
6. Angular/API contract review
7. Terraform/Proxmox review
8. OpenSearch query generation
9. Velociraptor VQL generation
10. defensive cyber-analysis reasoning
11. scenario-engine implementation
12. pytest generation
13. security review
14. refactoring
15. undocumented-feature reconstruction
16. Git diff review
17. tool-use reliability
18. long-context reasoning

Where possible, use existing TrueNorth code and tests without exposing secrets.

---

# 26. Evaluation Metrics

Score each model on:

```yaml
correctness:
tests_passed:
tests_failed:
hallucinated_api_calls:
hallucinated_files:
unnecessary_files_changed:
security_findings_valid:
tool_calls_successful:
tool_calls_failed:
time_to_solution:
tokens_used:
context_failures:
patch_size:
regressions_introduced:
human_quality_score:
```

Also record:

```text
Did it understand the architecture?
Did it preserve existing conventions?
Did it invent nonexistent APIs?
Did it notice security implications?
Did it write useful tests?
Did it over-edit?
Did it recover after tool failure?
Did it know when it lacked evidence?
```

---

# 27. Blind Review the Benchmark Results

Do not let model branding decide the winner.

Where possible:

```text
GLM output -> Candidate A
Qwen output -> Candidate B
```

Have Claude and Codex independently assess results without being told which local model produced which result.

Then compare.

Possible outcomes:

### Outcome A

```text
GLM-5.2 wins architecture
Qwen wins coding
```

Use dual-model Taz.

### Outcome B

```text
GLM-5.2 wins both
```

Keep GLM-5.2 only.

### Outcome C

```text
Qwen materially wins both
```

Do not immediately delete GLM-5.2.

Change default routing only after a documented acceptance decision.

---

# 28. Performance Testing on the H200s

Capture for each local model:

- load time
- model memory
- KV-cache memory
- tokens/sec
- time to first token
- concurrency
- context length tested
- GPU utilization
- GPU memory
- stability over long tool-driven runs

Use:

```bash
nvidia-smi
```

and the serving framework's metrics.

Do not optimize solely for maximum benchmark context.

TrueNorth needs stable long-running agent sessions more than a flashy theoretical context maximum.

---

# 29. Model Routing

If two local models are retained, route by task.

Initial candidate policy:

```yaml
routing:
  architecture:
    model: taz-architect

  cyber_analysis:
    model: taz-architect

  threat_hunting:
    model: taz-architect

  scenario_design:
    model: taz-architect

  implementation:
    model: taz-code

  refactoring:
    model: taz-code

  unit_tests:
    model: taz-code

  debugging:
    model: taz-code

  diff_review:
    model: taz-code
```

Allow explicit override.

Do not hide routing decisions.

Include selected backend/model in debug metadata.

---

# 30. TrueNorth-Specific Taz Context

Taz should understand TrueNorth but should not depend on stale README text.

Build local retrieval/indexing over selected repository content if useful.

Potential sources:

```text
docs/
control-plane/
scenario-engine/
ai-orchestrator/
telemetry/
infra/
content/
tests/
scripts/
```

Exclude:

```text
.git/
node_modules/
dist/
build/
.venv/
venv/
__pycache__/
coverage/
large binary artifacts
secrets
Terraform state
credential files
```

Use Git-aware change detection so indexes can be incrementally updated.

---

# 31. Repository Context Must Respect the Dirty Tree

If creating embeddings or repository maps, index the **working tree**, not only `HEAD`.

Track metadata such as:

```yaml
git_commit:
git_branch:
git_dirty:
file_path:
file_hash:
indexed_at:
```

This allows Taz to reason about current uncommitted TrueNorth code.

---

# 32. Security Boundaries

The local nature of Taz is an advantage only if the surrounding system preserves that boundary.

For Taz:

```text
✓ official/verified weights
✓ pinned revision
✓ hashes recorded
✓ private network
✓ no unnecessary Internet egress
✓ no vendor-hosted inference required
✓ local prompts
✓ local logs
✓ audited dependencies
✓ authenticated gateway
✓ explicit tool allowlist
✓ tool audit logging
```

Do not assume an open-weight model is safe merely because inference is local.

Model weights, tokenizer code, Python packages, containers, MCP servers, and model tooling are all part of the supply chain.

---

# 33. Do Not Give Models Uncontrolled Infrastructure Access

TrueNorth can eventually control significant range infrastructure.

Do not let a model directly execute destructive infrastructure actions such as:

```text
terraform destroy
VM deletion
Proxmox cluster modification
firewall modification
Keycloak realm destruction
database drops
index deletion
MinIO bucket deletion
credential rotation
```

Design high-risk actions as:

```text
AI recommendation
      ↓
validated structured request
      ↓
policy check
      ↓
human approval
      ↓
bounded executor
      ↓
audit log
```

---

# 34. Logging and Audit

For AI-assisted engineering actions, log where reasonable:

```yaml
timestamp:
initiator:
tool:
model_alias:
model_backend:
task:
repository:
branch:
git_head:
git_dirty:
files_considered:
action_type:
result:
duration:
```

Never log secrets or complete sensitive prompts unnecessarily.

---

# 35. Health Checks

Create a simple health command or script that checks:

```text
Claude Code available
Codex available
Codex MCP available
Taz reachable
Taz Gateway healthy
GLM-5.2 healthy
Qwen healthy, if installed
Taz MCP healthy
TrueNorth Git status
```

Suggested command:

```bash
./scripts/ai-health.sh
```

Expected output should be concise:

```text
Claude Code      OK
Codex            OK
Codex MCP        OK
Taz Gateway      OK
GLM-5.2          OK
Qwen             OK
Taz MCP          OK
TrueNorth Repo   DIRTY (expected)
```

A dirty repo is not a health failure.

---

# 36. Operational Commands

Create or document simple commands for:

```text
start Taz services
stop Taz services
restart Taz services
show Taz status
tail Taz logs
show GPU state
show loaded models
test inference
test tool calling
test Taz MCP
test Codex MCP
run TrueNorth model benchmark
```

The operator should not need to remember a 300-character vLLM launch command.

---

# 37. Documentation to Produce

At the end of the refresh, produce:

```text
docs/ai/
├── architecture.md
├── taz.md
├── claude-code.md
├── codex.md
├── model-provenance.md
├── model-benchmark.md
├── operations.md
├── security.md
└── troubleshooting.md
```

Also update the main TrueNorth documentation only where appropriate.

Do not rewrite large sections of project documentation unless the code proves they are stale.

---

# 38. Architecture Document

`docs/ai/architecture.md` should show the actual deployed architecture.

Target conceptual diagram:

```text
                         HUMAN
                           │
                           ▼
                     CLAUDE CODE
                    Lead Engineer
                           │
             ┌─────────────┴─────────────┐
             │                           │
             ▼                           ▼
          TAZ MCP                    CODEX MCP
             │                           │
             ▼                           │
        Taz Gateway                      │
             │                           │
     ┌───────┴────────┐                  │
     ▼                ▼                  │
 GLM-5.2          Qwen Coder             │
 Architect          Engineer             │
     │                │                  │
     └────────┬───────┘                  │
              │                          │
              └────────────┬─────────────┘
                           ▼
                       CLAUDE
                 reconciles results
                           │
                           ▼
                       TrueNorth
                    working tree
                           │
                           ▼
                         Tests
```

If the final deployed architecture differs, document reality.

---

# 39. Future GB10 Boundary

Add a small explicit section to the architecture document:

```text
Future / Not Connected

GB10-A
GB10-B
```

State:

> These systems are currently independent of TrueNorth and Taz. They may later be evaluated as dedicated offline content-generation resources. They are not dependencies of the current AI engineering workflow.

Do not draw them as active nodes.

---

# 40. First TrueNorth Collaborative Mission

Once the AI engineering setup is healthy, do **not** immediately start randomly improving code.

The first mission is:

> Reconstruct the current TrueNorth architecture and capabilities from the working tree. Treat existing documentation and the last Git commit as potentially stale. Identify what is implemented, partially implemented, broken, untested, undocumented, duplicated, deprecated, and still planned. Do not modify production code during the initial analysis.

Have:

1. Claude inspect the repository.
2. Taz independently analyze architecture and cyber-related implementation.
3. Codex independently review software implementation and technical debt.
4. Claude reconcile all findings.
5. Produce `docs/current-state.md`.

Only after this baseline exists should the multi-agent team prioritize code changes.

---

# 41. Second Mission — Prioritized Improvement Plan

After `docs/current-state.md`, generate:

```text
docs/improvement-plan.md
```

Rank improvements using:

```yaml
priority:
impact:
risk:
effort:
dependency:
security_relevance:
training_value:
scale_relevance:
technical_debt_reduction:
```

Prefer improvements that:

- close actual gaps
- stabilize existing features
- improve testing
- increase observability
- improve exercise realism
- improve ground-truth/scoring
- improve EXCON workflows
- strengthen telemetry
- improve cyber-analysis capability
- reduce operator burden

Do not add features simply because an LLM thinks they sound impressive.

---

# 42. Change Workflow

For every significant implementation:

```text
1. Define the problem.
2. Identify affected components.
3. Inspect current implementation.
4. Obtain independent Taz analysis.
5. Obtain independent Codex analysis.
6. Reconcile recommendations.
7. Create implementation plan.
8. Make the smallest correct change.
9. Run focused tests.
10. Run broader relevant tests.
11. Ask Taz to review architecture/security impact.
12. Ask Codex to review the diff.
13. Fix valid findings.
14. Re-run tests.
15. Report what changed.
```

Do not automatically commit.

---

# 43. Disagreement Policy

If Claude, Taz, and Codex disagree:

Do not choose a winner by reputation.

Resolve using:

1. source code
2. tests
3. official documentation
4. reproducible experiments
5. benchmark results
6. explicit architectural requirements

Record material unresolved disagreements.

---

# 44. Failure Policy

If a model or tool fails:

```text
Do not silently substitute an answer.
Do not pretend a review occurred.
Do not hide timeout/errors.
```

Report:

```yaml
component:
failure:
impact:
fallback_used:
confidence_change:
```

---

# 45. Rollback

Every service/configuration change must have a documented rollback.

Before changing a working Taz configuration, capture:

```text
old configuration
old service definition
old model revision
old launch command
old environment-variable names
old port
old health result
```

The operator must be able to return to the known-good GLM-5.2 setup.

---

# 46. Definition of Done

This refresh is complete only when all applicable items below are true.

## Taz

- [ ] R7725/H200 hardware documented.
- [ ] GLM-5.2 runtime documented.
- [ ] GLM-5.2 starts reliably as a managed service.
- [ ] GLM-5.2 has a reliable health check.
- [ ] GLM-5.2 endpoint is not unnecessarily publicly exposed.
- [ ] Logs and restart procedure are documented.
- [ ] Model provenance is documented.

## Qwen candidate

- [ ] Exact model identified and verified.
- [ ] Provenance documented.
- [ ] Local serving tested.
- [ ] TrueNorth benchmark completed.
- [ ] Results compared against GLM-5.2.
- [ ] Role decision based on evidence.
- [ ] GLM-5.2 preserved for rollback.

## Taz integration

- [ ] Stable Taz gateway exists or an equivalent stable interface is documented.
- [ ] Taz MCP tools are available.
- [ ] Taz tools are bounded and auditable.
- [ ] Claude Code can invoke Taz.

## Codex

- [ ] Codex CLI version documented.
- [ ] Codex works independently.
- [ ] Codex MCP integration is configured if supported/appropriate.
- [ ] Claude can request a Codex review.

## Claude

- [ ] Claude Code remains the final integrator.
- [ ] Specialist agents are created only where useful.
- [ ] Multi-agent review workflow works.
- [ ] `/truenorth-review` or equivalent exists.
- [ ] `/truenorth-baseline` or equivalent exists.

## TrueNorth

- [ ] Dirty working tree preserved.
- [ ] No destructive Git action occurred.
- [ ] Current-state report generated.
- [ ] Improvement plan generated.
- [ ] Existing docs corrected only after code inspection.
- [ ] Relevant tests pass or failures are explicitly documented.

## GB10 systems

- [ ] Clearly documented as separate.
- [ ] Not modified.
- [ ] Not configured as current TrueNorth dependencies.
- [ ] Future content-generation possibility documented only as future scope.

---

# 47. Required Final Report

When complete, provide a concise operator report:

```text
AI REFRESH RESULT

Claude Code:
  Status:
  Version:
  MCP:

Taz:
  Host:
  GPUs:
  Runtime:
  GLM-5.2:
  Qwen:
  Gateway:
  MCP:

Codex:
  Version:
  MCP:
  Status:

TrueNorth:
  Branch:
  Git head:
  Dirty:
  Tests:

Models:
  Architecture winner:
  Coding winner:
  Review winner:
  Benchmark report:

Security:
  External exposure:
  Secrets found in config:
  Tool permissions:
  Remaining risks:

GB10:
  Current status: SEPARATE / NOT CONNECTED

Next recommended action:
```

---

# 48. Execution Instruction

Begin with **inspection only**.

Do not begin by installing Qwen.

Do not begin by rewriting `.mcp.json`.

Do not begin by changing the GLM-5.2 launch command.

Do not begin by updating TrueNorth documentation.

Do not touch the GB10 systems.

First:

1. inspect the TrueNorth working tree
2. inventory Claude Code
3. inventory Codex
4. inventory Taz
5. discover how GLM-5.2 is currently served
6. map existing MCP configuration
7. map existing AI scripts/configuration
8. identify conflicts and stale configuration
9. create an implementation plan
10. then make reversible changes in small tested stages

**Preserve what works. Replace only what evidence says should be replaced.**

---

# 49. Desired End State

The intended result is:

```text
                       TrueNorth AI Team

                          HUMAN
                            │
                            ▼
                       Claude Code
                       Lead Engineer
                            │
                 ┌──────────┴──────────┐
                 │                     │
                 ▼                     ▼
               Taz                   Codex
         Private Local AI       Independent Reviewer
                 │
          ┌──────┴──────┐
          ▼             ▼
      GLM-5.2       Qwen Coder
      Architect       Engineer
          │             │
          └──────┬──────┘
                 │
                 ▼
          bounded MCP tools
                 │
                 └──────────────┐
                                ▼
                         Claude integrates
                                │
                                ▼
                           TrueNorth
                                │
                                ▼
                       tests + review gates
```

with:

```text
GB10-A / GB10-B
    │
    └── separate today
        possible future offline
        TrueNorth content generation
```

The goal is not the maximum number of models.

The goal is a **reliable, private, testable AI engineering system that makes TrueNorth better without making the development environment harder to trust.**
