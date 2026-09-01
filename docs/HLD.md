# Testronaut — High Level Design (v1)

**Scope:** OpenAPI spec ingestion through approved, exportable test cases.
**Out of scope:** automation codegen, execution, reporting, UI testing. See [PLAN.md](./PLAN.md).

---

## 1. System context

Testronaut is a **single-user local application**. There is no auth, no tenancy, and no
network exposure beyond localhost.

```
┌──────────┐   uploads spec    ┌──────────────────────┐
│          │──────────────────▶│                      │
│  QA      │   reviews cases   │     Testronaut       │
│ Engineer │◀──────────────────│   (localhost app)    │
│          │   downloads xlsx  │                      │
└──────────┘                   └───────┬──────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
            ┌───────────────┐  ┌──────────────┐  ┌────────────────┐
            │ Claude Agent  │  │  Anthropic / │  │  Ollama        │
            │ SDK (subscr.) │  │  OpenAI API  │  │  localhost:11434│
            └───────────────┘  └──────────────┘  └────────────────┘
                          external LLM providers
```

The only outbound traffic is to the selected LLM provider. Choosing the local Qwen
provider makes the system fully offline.

---

## 2. Architecture overview

Three tiers, deliberately thin.

```
┌───────────────────────────────────────────────────────────────────┐
│  Frontend — React + Vite + TypeScript                              │
│                                                                     │
│  Specs │ Run Config │ Progress │ Requirements ▶ Gate 1 │            │
│                                 (findings panel)                   │
│                        Cases ▶ Gate 2 │ Export                     │
│                        (findings panel, coverage)                  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  REST (JSON) + SSE
┌──────────────────────────────▼──────────────────────────────────────┐
│  Backend — FastAPI                                                  │
│                                                                     │
│  ┌────────────┐ ┌───────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ │
│  │spec_parser │ │ generator │ │  llm /   │ │ reviewer │ │exporter│ │
│  │            │ │(reqs+cases)│ │  agent   │ │+ trace-  │ │json +  │ │
│  │            │ │           │ │(4 prov)  │ │ability   │ │xlsx    │ │
│  └────────────┘ └───────────┘ └──────────┘ └──────────┘ └────────┘ │
│                          ┌──────────────┐                          │
│                          │  db (SQLModel)│                         │
│                          └───────┬──────┘                          │
└──────────────────────────────────┼──────────────────────────────────┘
                                   ▼
                             SQLite file
```

**Why this and not less:** a pure-frontend tool cannot hold an API key or shell to a
local model; a pure-CLI tool cannot deliver the review-and-approve loop that is the whole
point of v1. Two tiers plus a file database is the minimum that satisfies both.

---

## 3. Components

| Component | Responsibility | Key dependency |
|---|---|---|
| `spec_parser` | OpenAPI 3.x → normalized endpoint list; resolve `$ref`; flatten schemas with a depth limit | PyYAML, jsonref |
| `llm` | Probe provider availability, list models, dispatch a prompt to the selected provider | anthropic, openai |
| `agent` | Agent SDK path: per-stage MCP tools, `PreToolUse` hooks, restricted sessions | claude-agent-sdk |
| `generator` | Build per-endpoint prompts for both stages, orchestrate the run, persist artifacts | pydantic |
| `reviewer` | Run the critic pass per endpoint, snapshot-then-revise, deterministic traceability | pydantic |
| `exporter` | Serialize requirements + cases to JSON and Excel; parse both back with a merge summary | openpyxl |
| `db` | SQLModel table definitions, engine, session | sqlmodel |
| `main` | HTTP routes, SSE streaming, static file serving | fastapi, uvicorn |

Eight backend modules. Each has one job; `reviewer` is the only one both `generator` and
`agent` call into, since the traceability check is provider-independent.

---

## 4. Core data flow

### 4.1 Run state machine

A run is a two-stage pipeline with a human gate after each stage.

```
        created
           │
           ▼
  generating_requirements ─────┐
           │                   │
           ▼                   │
  reviewing_requirements       │  any state
           │                   │  can fail to
           ▼                   ├──▶  error
  awaiting_requirements_approval  ← GATE 1 (user)
           │                   │
           ▼                   │
  generating_cases             │
           │                   │
           ▼                   │
  reviewing_cases              │
           │                   │
           ▼                   │
  awaiting_case_approval  ─────┘  ← GATE 2 (user)
           │
           ▼
         done
```

Both gates are blocking: the backend does no further work until the user approves.
Approving requirements is what triggers case generation.

### 4.2 One stage in detail

Stages 1 and 2 have identical shape, differing only in artifact and prompts.

```
 for each selected endpoint:              ◀── streamed over SSE
      │
      ├─▶ GENERATE
      │     build prompt (endpoint spec [+ approved requirements in stage 2])
      │     dispatch to provider
      │     validate → persist as draft
      │
      ├─▶ REVIEW
      │     critic prompt: draft + endpoint spec
      │     returns structured findings, not prose
      │
      ├─▶ SNAPSHOT  ── copy current state into `snapshot` column
      │
      └─▶ REVISE (exactly one pass)
            apply findings, record before/after per changed field
      │
      ▼
 traceability check (stage 2 only, deterministic, whole-run)
      │
      ▼
 await approval
```

The **snapshot before revision** is what makes the agent's edits inspectable in the UI.
Without it the user would see only a post-revision artifact and have to take the agent's
word for what changed.

### 4.3 Review model

| Concern | Handled by | Why |
|---|---|---|
| Does this requirement match the spec? | LLM, per endpoint | Semantic judgment |
| Is this case testing what it claims? | LLM, per endpoint | Semantic judgment |
| Is the expected status declared in the spec? | Hook / Python | Deterministic lookup |
| Is every requirement covered by a case? | Python, whole run | Set operation |
| Are there orphan cases or duplicate IDs? | Python, whole run | Set operation |

**Coverage is deliberately not an LLM job.** Across 200 cases it neither fits in context
nor yields a reliable answer, and it is a set difference. Python computes it instantly,
for free, and correctly.

The reviewer runs as a **distinct agent role with a critic prompt** and no memory of having
authored the draft. On the Agent SDK path it is a separate session with only
`submit_review` available; on raw providers it is a second call with the review skill as
its system prompt.

### 4.4 Artifact lifecycle

Requirements and cases share one lifecycle:

```
  draft ──revise──▶ revised ──edit──▶ edited ──approve──▶ approved
    │                  │                 │                    ▲
    │                  └── revert ───────┘                    │
    └──────────────────── approve ─────────────────────────────┘
```

`revised` means the review agent changed it; that state is what the UI badges. `revert`
restores the pre-revision `snapshot` — per item, so the user can accept most of the
agent's edits while rejecting one.

Approval is a state change only. Nothing is immutable in v1: an approved artifact can be
edited again, returning it to `edited`.

### 4.5 Round trip

```
  DB ──export──▶ JSON  (canonical, exact)
     └─export──▶ XLSX  (flattened for humans)
                   │
              edited offline
                   │
  DB ◀──import─────┘   match on case_id
                        ├─ present in both → update
                        ├─ only in file    → insert
                        └─ only in DB      → report, do not delete
```

Import never deletes silently. Removals are reported and require an explicit action.

---

## 5. Key design decisions

### 5.1 One LLM call per endpoint

**Decision:** generate per endpoint, not per spec.

**Driver:** the local `qwen2.5:14b` has a 32k context. A real spec plus instructions plus
output will not fit.

**Additional payoff:** parallelism, incremental progress for the UI, cheap regeneration of
a single endpoint, and blast-radius containment when one endpoint's generation fails.

### 5.2 Provider abstraction at one function

**Decision:** all providers sit behind `generate_cases(endpoint, categories, provider, model)
-> list[CaseModel]`, returning validated models rather than raw text.

Ollama exposes an OpenAI-compatible endpoint, so the OpenAI client covers **both** OpenAI
and local Qwen with only a `base_url` change — three raw paths collapse to two. The Agent
SDK is the fourth and behaves differently enough (tools, hooks) to warrant its own branch
behind the same signature.

**Ollama is the default:** free, offline, no key, and free of the licensing constraint in
§5.4.

The UI calls `GET /api/providers`, which probes availability live (key present? Ollama
reachable? Agent SDK importable?) and returns each provider's model list from its own API.
No model IDs are hardcoded, so nothing goes stale.

### 5.3 Two transports, one validator

**Decision:** the Agent SDK path receives cases through a schema-validated MCP tool. The
raw-API providers (Ollama, OpenAI, Anthropic) prompt for JSON, strip fences, validate with
Pydantic, and retry once with the error fed back.

**Rejected:** each raw provider's native JSON-schema mode. That is three different APIs
with three different failure modes, and the local model's support is the weakest. A
uniform text-and-parse path is less code and behaves identically across all three.

The two transports share **one Pydantic model**, which backs both the MCP tool's JSON
schema and the raw parse path. The paths therefore do not diverge in what they accept.

### 5.4 The Agent SDK is a guardrail host, not a billing trick

**Decision:** use the Agent SDK for deterministic control, and default to local Qwen.

The SDK docs prohibit offering claude.ai login or subscription rate limits for products
built on it, so the subscription is not a sanctioned auth path. What the SDK *does* give,
and nothing else in the design does, is **capability restriction**: every stage's session
has exactly one MCP tool in `allowed_tools` and no filesystem or network reach at all.
Given that specs are untrusted input flowing into a prompt, that is the difference
between mitigating prompt injection and merely hoping. Detail in §6.

### 5.5 Methodology as four filesystem artifacts

**Decision:** each stage's methodology lives in its own `.claude/skills/<stage>/SKILL.md`
— `requirements-authoring`, `requirements-review`, `test-design`, `test-case-review`.

The Agent SDK discovers each one natively; the raw providers inline the same file as their
system prompt. One source of truth, versioned in git, changed by PR rather than by editing
a Python string.

### 5.6 SQLModel over SQLite

SQLModel defines the table and the API schema in a single class. Against plain `sqlite3`
it removes hand-written CRUD and duplicate Pydantic models; against full SQLAlchemy it
removes the second schema definition. SQLite itself needs no server.

### 5.7 JSON canonical, Excel derived

JSON round-trips exactly, including nested request bodies and assertion lists. Excel is
what reviewers will actually open, so it is generated **from** the JSON with nested
structures flattened to JSON-in-a-cell. On import, the JSON columns are re-parsed.

---

## 6. Guardrail architecture (Agent SDK path)

Every stage — generate-requirements, review-requirements, generate-cases, review-cases —
runs as its own session with its own single tool. The shape repeats four times:

```
                  untrusted spec [+ prior-stage artifact]
                          │
                          ▼
        ┌─────────────────────────────────────┐
        │  Agent SDK session (one per stage)  │
        │                                     │
        │  allowed_tools = [submit_<stage>]   │ ◀── Layer 1
        │  no Read / Write / Bash / WebFetch  │     capability restriction
        │                                     │
        │        model emits a tool call      │
        │                 │                   │
        │                 ▼                   │
        │  ┌───────────────────────────────┐  │
        │  │ MCP tool JSON schema          │  │ ◀── Layer 2
        │  └───────────────┬───────────────┘  │     schema validation
        │                  ▼                  │
        │  ┌───────────────────────────────┐  │
        │  │ PreToolUse hook               │  │ ◀── Layer 3
        │  │  allow │ deny + reason ───────┼──┼──▶ model self-corrects
        │  └───────────────┬───────────────┘  │     in-loop
        │                  ▼                  │
        │  ┌───────────────────────────────┐  │
        │  │ PostToolUse → audit log       │  │
        │  └───────────────┬───────────────┘  │
        └──────────────────┼──────────────────┘
                           ▼
              persisted, snapshotted artifact
```

`submit_<stage>` is one of `submit_requirements`, `submit_test_cases`, or `submit_review`
— never more than one per session. The review sessions get the draft artifact as read-only
prompt content, not as a tool result, so a review session cannot itself call a submission
tool from an earlier stage.

**Layer 1 is the one that matters most.** Specs are untrusted input reaching a prompt, and
a `description` field can carry an injection payload. With no filesystem or network tools
in any session, a successful injection can do nothing except emit a bad artifact — which
Layer 3 rejects. Restriction beats validation, so it comes first, in every stage.

**Layer 3 denials are cheap.** `permissionDecision: "deny"` returns a reason the model
reads and corrects against inside the same loop, rather than failing a parser and burning
a full retry.

**On `submit_review`:** the hook additionally rejects a review whose findings reference a
`target_id` that does not exist in the artifact being reviewed — a review agent cannot
hallucinate a finding about a case that was never generated.

The raw-API providers have no equivalent, so they keep the parse-validate-retry path and a
correspondingly weaker posture. This is an accepted, documented asymmetry rather than an
oversight: the shared Pydantic models mean both paths accept exactly the same artifacts,
and both approval gates are a human checkpoint regardless of provider.

---

## 7. Non-functional considerations

### Performance
The bottleneck is entirely LLM latency. A local 14b model may take 10–30 s per endpoint,
so a 40-endpoint spec is a multi-minute run. This is why SSE progress is a Phase 2
requirement rather than a nicety — a silent spinner would read as a hang. Endpoint
generation is independent and can be run concurrently with a small worker pool.

### Security
- Uploaded specs are **untrusted input** reaching an LLM prompt. Spec text is passed as
  data with instructions held outside it. All output is human-reviewed before use.
- Specs frequently embed real auth material. The SQLite file, uploads, and exports are
  gitignored.
- API keys come from the environment and are never persisted to the database or returned
  by any endpoint. `GET /api/providers` returns availability booleans, never key values.

### Extensibility
Two seams are deliberately left open for Phase 2:

1. **Spec source.** `spec_parser` produces a normalized endpoint list. A future Postman
   or GraphQL parser can emit the same structure without touching the generator.
2. **Case schema.** The test case JSON is **language- and framework-agnostic** — it
   describes intent, request, and expected outcome, never TestNG or Java. Codegen consumes
   it later without a migration.

### Reliability
Per-endpoint failures are isolated: one endpoint's parse failure marks that endpoint
errored and the run continues. The run record retains partial results.

**Gates tolerate partial failure.** A run can reach a gate with some endpoints errored.
The gate still opens — the user approves what succeeded and can trigger `/regenerate` for
the rest, rather than the whole run blocking on one bad endpoint. This extends the
existing per-endpoint isolation rule up to the approval boundary rather than stopping at
it.

---

## 8. Phase 2 preview

Not built, recorded so v1 does not foreclose it.

```
approved cases ──▶ codegen ──▶ Maven project (TestNG + REST Assured)
                                      │
                   test data prompts ──┤
                                      ▼
                            Docker sandbox execution
                                      │
                              Surefire XML
                                      ▼
                              report screen
```

Execution belongs in Docker because generated code is untrusted and should not run on the
host. Docker is not currently installed, which is acceptable because v1 executes nothing.
