---
name: test-design
description: Draft executable test cases from approved requirements plus the OpenAPI spec. One invocation per endpoint.
---

# Test Design

Draft test cases for one endpoint from its **approved** requirements and the spec.

Requirements say what must hold; a test case says how you would demonstrate it.
Every case exists to exercise a requirement — if you cannot name the requirement,
do not write the case.

## Rules

1. Every case links to at least one approved `req_id` via `requirement_ids`.
2. Cover every approved requirement with at least one case. An uncovered
   requirement is a gap the traceability check will report.
3. `expected_status` must be a status code the spec declares for this operation.
   This is enforced; a status the spec does not declare is rejected.
4. `category` must be one of happy_path, negative, boundary, auth, security, and
   must have been requested for this run. Also enforced.
5. Inputs — `path_params`, `query_params`, `headers`, `body` — must be valid per
   the spec: real parameter names, real types, required fields present. A
   negative case breaks exactly one rule on purpose and states which.
6. `expected_body` holds assertions in the grammar below, one claim per entry.
   Prose is not compiled into the generated test — it survives only as a TODO
   comment a human has to finish, so an assertion you can express in the grammar
   is worth far more than one you cannot.
7. `test_data_notes` says what real data the operator must supply — "a petId that
   exists in the store", not "user provides data".
8. `preconditions` are things that must be true before the request, not steps.
9. Priority inherits from the requirement unless the case is a narrower edge of it.

## Assertion grammar

`expected_body` entries must be one of these six forms. `$.` addresses the
response body; `$.a.b[0].c` walks nested objects and arrays.

```
$.path == value            $.id == 5          $.name == 'Fluffy'
$.path != value            $.code != 500
$.path contains 'text'     $.message contains 'required'
$.path exists              $.id exists
$.path matches /regex/     $.name matches /^[A-Z]/
$.array length == N        $.tags length == 2
```

Assert what the spec's response schema actually promises. Do not assert on a
field the schema does not declare.

## IDs

`case_id` is `TC-<OPTOKEN>-<NNN>`, numbered from 001 within this endpoint, using
the OPTOKEN given to you. Duplicate IDs within a run are rejected.
