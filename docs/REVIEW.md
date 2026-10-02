# Design Review — Testronaut v1

Reviewed: `PLAN.md`, `HLD.md`, and `LLD.md` (2026-08-23)

## Summary

The plan and HLD define a two-gate requirements-to-test-cases workflow, but the LLD still specifies the earlier case-only workflow. The implementation contract must be brought into alignment before development starts.

## Findings

### [P1] Specify the requirements stage in the LLD

**References:** `LLD.md` §§2–7; `PLAN.md` §2; `HLD.md` §§4.1–4.2.

The stated v1 scope requires generating, reviewing, editing, approving, exporting, and importing functional requirements before test-case generation. However, the LLD has no requirement entity, canonical requirement schema, requirement-to-case link, endpoints, review-finding/change model, or frontend screens/routes for Gate 1. As written, an implementation can only build direct spec-to-case generation and cannot enforce Gate 1 or deterministic traceability. Add these contracts and update the test plan before treating the LLD as implementation-ready.

### [P1] Persist the complete run state machine

**References:** `LLD.md` §2.2; `HLD.md` §4.1.

`generation_run.status` allows only `running`, `done`, and `error`, while the HLD requires distinct generation, review, and approval-waiting states for both gates. This makes it impossible for the API/UI to determine whether a run is awaiting requirements approval or case approval, or to safely resume the correct action after a reload. Align the persisted status enum and route transition rules with the HLD state machine.

### [P1] Scope case ID uniqueness to a run

**References:** `LLD.md` §2.3, especially `case_id`.

The database declares `case_id` globally unique, but generated IDs are only guaranteed unique within a run and will be reused when the same endpoint is generated in a later run (for example, both runs produce `TC-ADDPET-001`). The second run would fail to persist valid generated cases despite the validator explicitly defining uniqueness within a run. Use a composite uniqueness constraint on `(run_id, case_id)` and make import matching run-scoped.

### [P2] Define IDs for operations without `operationId`

**References:** `LLD.md` §§2.3 and 6.3.

OpenAPI `operationId` is optional, yet the prompt mandates IDs in the form `TC-<OPERATION>-<NNN>` and the documented example derives `<OPERATION>` from it. For any valid spec lacking `operationId`, generation cannot produce an ID satisfying the validator. Define a deterministic normalized operation token from method/path (and collision handling) and use it consistently in prompts, validation, exports, and imports.

## Recommended next step

Revise `LLD.md` first to make requirements, review findings, change snapshots, approval transitions, traceability, and run-scoped IDs explicit; then update the HLD component/API diagrams and tests to use the same contract.

---

## Status — 2026-08-24

All four findings are resolved in the implementation, and the LLD was brought
into line during it.

| Finding | Resolution |
|---|---|
| [P1] Requirements stage absent from the LLD | Implemented: `requirement` table with `acceptance_criteria`, `spec_source`, `priority`, `origin`; canonical JSON; Gate 1 routes; `RequirementsReview` screen; requirement→case link enforced at validation time. |
| [P1] Run state machine | All nine `RunStatus` values persisted. `start` runs stage 1 only and stops at `awaiting_requirements_approval`; stage 2 begins on `POST /runs/{id}/approve-requirements` and never automatically. |
| [P1] Case ID uniqueness scoped to a run | `UniqueConstraint("run_id", "case_id")`, same for `("run_id", "req_id")`. Import matching is run-scoped. Covered by `test_case_id_unique_per_run_not_global` and `test_duplicate_case_id_in_one_run_is_rejected_by_the_database`. |
| [P2] IDs for operations without `operationId` | `op_token()` in `spec_parser.py` is the single definition, used by prompts, validation, export and import. Falls back to `<METHOD>_<PATHSLUG>_<HASH4>`; the hash disambiguates slug collisions. |

### Deliberate deviations from the LLD

Documented here rather than silently diverging.

1. **JSON columns, not TEXT holding JSON.** LLD §2.4 says JSON-bearing columns are
   stored as `str` "because SQLite has no native JSON type". SQLite has had JSON1
   since 3.9, and SQLAlchemy's `Column(JSON)` handles it, so the models expose real
   lists and dicts. This removed roughly sixty `json.dumps`/`json.loads` calls across
   four modules and the entire class of bug where one layer forgot to deserialize.

2. **No per-provider class hierarchy.** LLD §6.2's `probe()`/`generate()` contract is
   met, but by one `LLM` dataclass rather than an ABC with three subclasses: the
   providers differ by a URL, a header dict and two payload keys.

3. **Revert is per-finding, not whole-artifact.** LLD §4 describes restoring from
   `snapshot`; PLAN §4 promises "revert any individual change". Both work —
   `POST /runs/{id}/revert/{target_id}` restores the whole snapshot,
   `?finding_id=F-3` undoes one change. Each applied finding carries its own
   `before`/`after`, which is also what the findings panel renders.

4. **`temperature` is not sent to Anthropic.** Sampling parameters are rejected with
   a 400 on the current Claude models, including the default `claude-opus-5`.

5. **One retry when validation rejects a case.** Rejection reasons are fed back once,
   the same shape as the JSON-parse retry PLAN §5 specifies. A live run showed a
   single bad status-code assumption otherwise costing two requirements' worth of
   coverage in silence.

6. **`duplicate_case_ids` in the traceability report is vestigial.** The composite
   unique constraint makes duplicates unreachable; the key is kept because the LLD
   documents it, and the test asserts the constraint is what enforces it.

### Known limitation

The requirements critic can still *raise* a wrong objection — a local 14b model
reliably wants to "correct" a spec-declared 405 to a conventional 400. It can no
longer apply that silently: the review skill is given the declared status codes
and an explicit rule, and any test case built on an undeclared status is rejected
before it is stored. The finding surfaces to the user as `raised`, which is the
right outcome, but it is noise a stronger critic model would not produce.

---

## Fixes found by running it — 2026-08-25/26

Four defects that only a live run against a real model surfaced. All four have a
regression test.

1. **Case generation truncated at `max_tokens`.** A case carries a body, headers
   and assertions, one per requirement, so output scales with the number of
   approved requirements — unlike stage 1. A fixed 4000 cut the JSON mid-string
   for an endpoint with five requirements, and the retry re-sent the identical
   prompt so it truncated identically: the endpoint produced **zero** cases. The
   budget is now `max(4000, 1500 * len(approved))`, and the repair prompt tells
   the model when its previous reply was cut off. Covered by
   `test_case_generation_budget_scales_with_requirement_count`.

2. **`parse_items("[]")` raised.** The review contract says an empty array means
   nothing is wrong — so every *clean* review burned a retry and then failed its
   endpoint. An empty array is now a valid answer. Covered by
   `test_parse_items_accepts_an_empty_array`.

3. **The review pass emitted generation progress.** `_run_endpoints` published
   `endpoint_start`/`endpoint_done` for the critique phase as well, so the UI
   labelled a review as "generating". The review phase has its own
   `review_start`/`review_done` (LLD §5) and now suppresses the generation events;
   `endpoint_error` still fires either way, since the protocol has no
   `review_error`. Covered by `test_review_pass_does_not_emit_generation_progress`.

4. **Progress showed `N / ?` and a stale status pill.** `stage_start` fires before
   `EventSource` finishes connecting, so a client that attached a moment late never
   learned the denominator, and nothing refreshed the pill between the initial
   snapshot and `gate_reached`. Every status transition now publishes a `status`
   event carrying `total_endpoints`, and the connect-time snapshot carries it too.

### Known limitation, unchanged

The critic can still raise a wrong objection — a local 14b model wants to
"correct" a spec-declared 405 to a conventional 400. It cannot apply that
silently, and any case built on an undeclared status is rejected before storage,
but the noise is real. A stronger critic model removes it.

Separately, coverage is only as good as the generator: in a live run the model
wrote 2 cases for 5 approved requirements. That is not a defect — the traceability
panel names the 3 uncovered requirements so the operator can regenerate or add
cases by hand, which is exactly what the deterministic check is for.

---

## Phase 2 — codegen, execution, reports (2026-08-26)

PLAN §10 listed four things. All four are built.

| Item | Where | Note |
|---|---|---|
| Codegen | `backend/codegen.py` | Approved cases → Maven + TestNG + REST Assured. One class per endpoint, one `@Test` per case, requirement IDs carried into Javadoc so traceability survives into the source. |
| Test data | `Config.java` + `testronaut.properties` | Base URL, optional auth header, and `${placeholder}` resolution. Overridable with `-Dkey=value`, so a project can be committed without its secrets. |
| Execution | `backend/runner.py` | `docker run --cap-drop ALL --security-opt no-new-privileges --memory 2g --pids-limit 512`. Refuses to run on the host when Docker is absent rather than falling back. |
| Reports | `backend/runner.py` | Surefire XML parsed and joined back to case IDs via `testronaut-map.json`, written at generation time. |

### The assertion grammar is now real

LLD §3 defined six forms and said they were "validated loosely in v1, compiled in
Phase 2". `backend/assertions.py` is that compiler, and it is the single parser
used by both generation-time guidance and codegen.

Two things fell out of building it:

1. **The `test-design` skill was teaching prose.** It said to write
   `response.id is an integer > 0`, which nothing can compile. It now documents the
   grammar, and the contract example uses it. Cases generated before this change
   still work — their assertions become TODO comments rather than vanishing.
2. **`response.` is not a field.** REST Assured's GPath is already rooted at the
   response body, so `response.name == 'x'` compiled to `.body("response.name", …)`
   and would never match. The root prefix is stripped.

### Verified

The generated project compiles (`mvn test-compile`, exit 0) and runs: against a
mock Petstore, 4 tests, 3 passed, 1 failed — and the failure was real, the mock
returning 422 where the spec declares 400. Exactly the discrepancy the tool exists
to surface.

### Sandbox verification

v1 executed nothing, so Docker was not a requirement then. Phase 2 needs it for the
sandbox. At the time of this work the sandbox path was verified by its refusal
behaviour and unit tests; the Maven run that produced the results above was executed
directly to exercise the report parser. Run `docker compose up` and an end-to-end
`/execute` on a clean machine before relying on it.
