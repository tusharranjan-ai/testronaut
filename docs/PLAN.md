# Testronaut — Project Plan (v1)

**Repository:** `tusharranjan-ai/testronaut` (private)
**Date:** 2026-08-23
**Status:** Planning

---

## 1. Vision

An AI-driven test platform that takes a specification, produces test cases, gets them
reviewed and approved by a human, then generates and runs automation against them.

The long-term target spans **multiple languages** and **both API and UI testing**. The
name is deliberately domain-neutral so nothing pins the product to OpenAPI.

Full product arc:

```
spec → functional requirements → review → APPROVE
     → test cases → review → APPROVE
     → generated automation → execution → reports
```

**v1 builds up to the second approval gate.** The generation-and-review loop is the part
that has to feel right, and it is far cheaper to validate before building codegen and
execution on top of it.

### Why a requirements layer

Going spec → test cases directly makes the model do two jobs at once: decide *what the
API is supposed to do*, and *how to test it*. Splitting them pays off three ways.

1. **A human gate at the cheaper point.** Correcting a wrong requirement costs one line.
   Discovering the same misunderstanding spread across fifteen test cases costs fifteen.
2. **Traceability.** Every case links to the requirement it exercises, so "what is
   untested?" becomes a set operation rather than a judgment call.
3. **A better review target.** "Does this requirement match the spec?" is a sharper
   question than "is this test case good?", so the review agent produces sharper findings.

---

## 2. Scope

### In scope for v1

| # | Capability |
|---|---|
| 1 | Upload an OpenAPI 3.x spec by file or URL (JSON or YAML) |
| 2 | Parse the spec into a normalized endpoint list |
| 3 | Select endpoints and coverage categories per run |
| 4 | Select the LLM provider and model per run, from the UI |
| 5 | **Generate functional requirements from the spec, one call per endpoint** |
| 6 | **Requirements review agent critiques them; findings auto-applied in one pass** |
| 7 | **Reviewer findings visible in the UI, each showing the before/after it caused** |
| 8 | **User reviews, edits, and approves the requirements — gate 1** |
| 9 | Generate test cases from the approved requirements plus the spec |
| 10 | **Test case review agent checks cases against spec and requirements** |
| 11 | **Deterministic traceability: every requirement covered, no orphan cases** |
| 12 | User reviews, edits, and approves the test cases — gate 2 |
| 12 | Export to JSON (canonical) and Excel (human-editable) |
| 13 | Re-import an edited JSON or Excel file as the approved set |

### Explicitly out of scope for v1

Named so that scope creep has to be a decision rather than a drift.

- TestNG / Java automation codegen
- Docker execution sandbox
- Test result reports
- Browser / UI testing
- Multi-user auth, roles, teams
- Cloud hosting

v1 is a **single-user local tool**. No login.

---

## 3. Environment findings

Checked before planning. Two of these changed the design.

| Component | Status | Consequence |
|---|---|---|
| Java 24 + Maven 3.9.9 | Installed | Generated TestNG projects will run locally — Phase 2 concern |
| Ollama, `qwen2.5:14b` | Running, 32k ctx | Zero-cost local provider; **its context limit drives per-endpoint generation** |
| `OPENAI_API_KEY` | Set | Works today, no setup |
| Anthropic API key / `ant` CLI | Absent | Claude reachable only via the Agent SDK route |
| Docker | Not installed | No blocker — v1 executes nothing |

### Finding: Claude subscription vs API access

A Claude Pro/Max subscription does **not** grant API access to a third-party app. The
Anthropic API is billed separately through the Console.

The **Claude Agent SDK** (formerly the Claude Code SDK) is Claude Code packaged as a
library. It is tempting to treat it as a way to run on the existing subscription seat, but
the SDK documentation is explicit:

> Unless previously approved, Anthropic does not allow third party developers to offer
> claude.ai login or rate limits for their products, including agents built on the Claude
> Agent SDK. Use the API key authentication methods described in the Quickstart instead.

**Consequence:** the subscription is not a sanctioned auth path for anything built on the
SDK. Personal use on your own machine is a grey area; distribution is clearly out.

**Therefore local Qwen via Ollama is the default provider** — free, offline, no key, and
no licensing question. The Agent SDK and the hosted APIs are optional upgrades the user
selects when they want higher generation quality and have a key configured.

The Agent SDK is still worth using, but for its **guardrails** rather than its billing.
See §9.

---

## 4. Pipeline

```
 upload spec
     │
     ▼
 parse endpoints
     │
     ▼
 ┌─────────────────────────────────────────┐
 │ STAGE 1 — REQUIREMENTS                  │
 │                                         │
 │  generate FRs      (1 call / endpoint)  │
 │        ▼                                │
 │  review agent      (1 call / endpoint)  │
 │        ▼                                │
 │  auto-revise       (1 bounded pass)     │
 └────────────────┬────────────────────────┘
                  ▼
        ╔═══════════════════════╗
        ║  GATE 1 — user edits  ║
        ║  and approves the FRs ║
        ╚═══════════╤═══════════╝
                    ▼
 ┌─────────────────────────────────────────┐
 │ STAGE 2 — TEST CASES                    │
 │                                         │
 │  generate cases    (from FRs + spec)    │
 │        ▼                                │
 │  review agent      (1 call / endpoint)  │
 │        ▼                                │
 │  auto-revise       (1 bounded pass)     │
 │        ▼                                │
 │  traceability check      (deterministic)│
 └────────────────┬────────────────────────┘
                  ▼
        ╔═══════════════════════╗
        ║  GATE 2 — user edits  ║
        ║  and approves cases   ║
        ╚═══════════╤═══════════╝
                    ▼
              export JSON / Excel
```

### Division of labour in review

The two review agents judge **semantics per endpoint** — does this requirement match the
spec, is this case actually testing what it claims, is the expected status right.

**Coverage is not an LLM job.** Asking a model whether every requirement is covered across
200 cases neither fits in context nor produces a reliable answer. It is a set operation, so
Python computes it: requirement→case traceability, orphan cases, duplicate IDs, endpoints
with zero cases. Deterministic, instant, and free.

### Auto-revision is bounded, and visible

Review findings are applied in **exactly one** revision pass. No convergence loop.

Crucially, the revision is **not silent**. Before revising, the artifact's current state is
snapshotted, so the UI can show, for every item the reviewer touched:

- the finding: severity, what the reviewer objected to, and what it suggested
- the resulting change: field-level **before → after**
- whether the finding was applied, or raised but left alone

The user therefore approves with full sight of what the agent changed and why, and can
revert any individual change. An agent that silently rewrites its own output is a
black box; the point of the review stage is that it is inspectable.

```
 ┌─ Findings ──────────────────────────────────────────┐
 │ ● HIGH   FR-ADDPET-003   applied                    │
 │   "Requirement asserts a 409 the spec never          │
 │    declares for this operation."                     │
 │   expected_status:  409  →  400                      │
 │                                          [ revert ]  │
 ├─────────────────────────────────────────────────────┤
 │ ○ LOW    FR-ADDPET-007   raised, not applied        │
 │   "Wording is ambiguous about optional fields."      │
 └─────────────────────────────────────────────────────┘
```

---

## 5. Decisions

| Question | Choice | Rationale |
|---|---|---|
| Backend | Python + FastAPI | Strong spec-parsing ecosystem; SSE is first-class |
| Frontend | React + Vite + TypeScript | Fast iteration, typed API client |
| Database | SQLite via SQLModel | Zero infra; SQLModel collapses table + API schema into one class |
| LLM provider | Selectable in UI, 4 options; **Ollama is the default** | Free, offline, no key, no licensing question |
| Agent SDK role | Guardrail host, not just a text generator | Restricted tools + validated MCP tool + hooks; see §9 |
| Methodology | A `SKILL.md`, not a Python string | Versioned and PR-reviewable; shared by every provider path |
| Requirements granularity | Per endpoint | Matches case generation; keeps 32k local context viable |
| Review granularity | Semantics per endpoint, coverage in Python | Coverage is a set operation, not a judgment call |
| Auto-revision | Exactly one pass, findings shown | Bounded cost; user keeps visibility of what was caught |
| Reviewer identity | Separate agent role, same provider | A critic prompt with no memory of having authored the draft |
| Canonical artifact | JSON | Reliable round-trip |
| Human artifact | Excel (.xlsx) | QA reviewers actually edit spreadsheets |
| Coverage depth | Configurable per run | Different endpoints warrant different rigor |
| Structured output | Prompt + parse + one retry | One code path across four providers |
| Generation unit | One endpoint per LLM call | Required by 32k local context; also enables parallelism and partial regeneration |
| Auth | None | Single-user local tool |

---

## 5. Build order

Each phase depends on the one before it.

### Phase 0 — Scaffold
- Repo layout, `.gitignore`, `.env.example`, `README.md`
- Backend venv + `requirements.txt`; frontend Vite app
- Health-check endpoint and a frontend that reaches it

**Done when:** `uvicorn` and `vite dev` both run, and the UI shows backend status.

### Phase 1 — Spec ingestion
- Upload by file or URL; store raw spec
- Parse OpenAPI 3.x, resolve `$ref`, normalize to an endpoint list
- Spec list screen and endpoint list screen

**Done when:** a Petstore spec uploads and every endpoint renders with method, path, params, and security.

### Phase 2 — Provider layer and requirements generation
- Provider probe and dispatch for Ollama, OpenAI, Anthropic
- Author the four skills under `.claude/skills/` (see §5): requirements authoring and
  review, test design and review. Used as system prompts here, loaded natively by the
  Agent SDK path in Phase 2b
- Prompt builder with depth-limited schema flattening
- Requirements generation loop with SSE progress
- Run configuration screen (provider, model, categories, endpoint selection)

**Done when:** a run against local Qwen produces parsed requirements with live progress.

### Phase 3 — Requirements review and gate 1
- Requirements review agent, one call per endpoint, structured findings
- One bounded auto-revision pass
- Requirements screen: table, inline edit, add, delete, findings panel
- Approve → advances the run and triggers case generation

**Done when:** requirements can be generated, critiqued, revised, edited, and approved,
and approval kicks off stage 2.

### Phase 4 — Test case generation, review, and gate 2
- Case generation from approved requirements plus the spec, with `requirement_ids` links
- Test case review agent and its bounded revision pass
- Deterministic traceability check: coverage, orphans, duplicates
- Case table: inline edit, add, delete, filter, findings panel, coverage summary
- Approve singly and in bulk

**Done when:** the full two-gate pipeline runs end to end and the coverage summary shows
every approved requirement exercised by at least one case.

### Phase 4b — Agent SDK path with guardrails
- MCP tools `submit_requirements`, `submit_test_cases`, `submit_review` with JSON schemas
- `PreToolUse` hooks enforcing the validation rules in §9
- `allowed_tools` restricted to those tools
- Slash commands under `.claude/commands/` for the CLI path

**Done when:** an Agent SDK run emits artifacts only through validated tools, and a
deliberately malformed submission is denied by the hook and self-corrected in-loop.

### Phase 5 — Round trip
- Export JSON and Excel — requirements and cases on separate sheets, traceability preserved
- Import JSON and Excel, matching on ID, with an add/update/remove summary

**Done when:** export → edit in Excel → import reproduces the edits exactly, and the
requirement links survive the trip.

### Phase 6 — Polish
- `test_roundtrip.py`
- README with setup and usage
- Error states and empty states in the UI

---

## 6. Verification

- **Round-trip test** (`test_roundtrip.py`) — parse a Petstore spec, generate against a
  stubbed LLM, export to JSON and Excel, re-import, assert the case set is unchanged.
  Covers the two genuinely non-trivial paths: the ref-resolving parser and export
  fidelity. No framework beyond pytest.
- **Live local run** — exercise the real Ollama Qwen endpoint. It is the zero-cost
  default and its context limit is the tightest constraint in the system.
- **Manual pass** — upload spec → configure → generate → edit a case → approve →
  download → re-upload.

---

## 7. Security notes

- An uploaded OpenAPI spec is **untrusted input** flowing into an LLM prompt. A
  `description` field can carry prompt-injection text. Spec content is passed as data
  with instructions held outside it, and all output is human-reviewed before use.
- Specs often carry real auth details. The SQLite DB, uploads, and exports are gitignored.
- **Action required:** the `GITHUB_TOKEN` was echoed into a terminal transcript during
  the initial clone. It has been scrubbed from `.git/config` and moved to the macOS
  Keychain, but it should still be rotated at github.com/settings/tokens.

---

## 9. Agent SDK guardrails

The Agent SDK earns its place through **deterministic control**, not billing. Three layers,
strongest first.

### Layer 1 — Capability restriction (strongest)

Each stage gets exactly the one tool it needs:

```python
# generation stages
allowed_tools = ["mcp__testronaut__submit_requirements"]   # stage 1
allowed_tools = ["mcp__testronaut__submit_test_cases"]     # stage 2
# review stages
allowed_tools = ["mcp__testronaut__submit_review"]
```

No `Read`, no `Write`, no `Bash`, no `WebFetch`. This is the answer to the threat that the
rest of the design has no answer for: **an uploaded OpenAPI spec is untrusted input reaching
an LLM prompt.** A `description` field carrying an injection payload cannot read the
filesystem or exfiltrate anything, because those tools do not exist in the session. Even a
fully successful injection can only emit malformed test cases — which Layer 3 then rejects.

### Layer 2 — Schema-validated tool

Cases arrive through an MCP tool whose JSON schema the SDK enforces. This **replaces** the
"prompt for JSON, strip markdown fences, retry once" approach on this path. That hack
remains only for the raw-API providers, which have no tool contract.

### Layer 3 — `PreToolUse` hook

Returns `permissionDecision: "deny"` with a reason, which the model sees and self-corrects
against in-loop — no wasted round trip through the parser. Rules enforced:

- `case_id` matches `TC-<OP>-<NNN>` and is unique within the run
- `category` is one of the five allowed values, and was actually requested for this run
- `expected_status` is a status code the spec declares for this operation
- the endpoint matches the one the run asked for

`PostToolUse` additionally logs every emitted case as an audit trail.

### Shared validation

The same Pydantic model backs both the MCP tool schema and the raw-API parse path. One
validator, two transports — the provider paths do not meaningfully diverge.

### Methodology as four skills

`.claude/skills/` holds one `SKILL.md` per stage: `requirements-authoring`,
`requirements-review`, `test-design`, `test-case-review`. The Agent SDK loads the one
each stage needs via `setting_sources=["project"]` — deliberately **not** `"user"`, so a
pipeline run never pulls in the operator's personal `~/.claude/` config and stays
reproducible across machines. The raw-API providers inline the matching file as their
system prompt. Editing methodology for any stage is a Markdown PR, not a Python change,
and both transports move together.

---

## 10. Phase 2 preview (not built yet)

Recorded so v1 does not paint us into a corner.

- **Codegen:** approved cases → TestNG + Java project (Maven), REST Assured for HTTP.
- **Test data:** prompt the user for the data each case needs before generating code.
- **Execution:** Docker sandbox — generated code is untrusted and should not run on the host.
- **Reports:** parse Surefire XML into a result view.

v1 keeps the approved-case JSON schema stable and language-agnostic so the codegen phase
can consume it without a migration.
