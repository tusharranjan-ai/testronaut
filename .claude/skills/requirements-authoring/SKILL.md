---
name: requirements-authoring
description: Draft functional requirements from a single OpenAPI endpoint. One invocation per endpoint.
---

# Requirements Authoring

Draft functional requirements (FRs) for exactly one OpenAPI endpoint.

A requirement states **what the API must do**, in behaviour terms. It is not a
test. "The API returns 400 when `name` is missing" is a requirement; "send a
POST without `name` and assert 400" is a test case, and belongs to a later stage.

## Input

Endpoint detail (method, path, operationId, summary, description, parameters,
requestBody, responses, security), the coverage categories selected for this run,
and the OPTOKEN to build IDs from.

## Rules

1. Produce 1–5 requirements. Cover only the categories requested for this run.
2. Each requirement is independently checkable. If you cannot say how you would
   know it holds, it is prose — rewrite it.
3. Ground every requirement in the spec. Never assert a status code, field or
   error shape the spec does not declare for this operation. A requirement the
   spec does not support is worse than a missing one.
4. `acceptance_criteria` carries the checkable statements, one per entry. Do not
   bury them in `description`.
5. `spec_source` is a JSON pointer to what the requirement derives from, e.g.
   `#/paths/~1pet/post/requestBody`.
6. Skip categories that do not apply: no `auth` requirements for an endpoint that
   declares no security, no `boundary` requirements where nothing has bounds.
7. Priority: P1 for the operation's core contract, P2 for standard validation,
   P3 for edge conditions.

## Category guidance

- **happy_path** — valid request, declared success response.
- **negative** — missing required input, wrong type, malformed payload.
- **boundary** — min/max length, numeric limits, empty collections, absent optionals.
- **auth** — missing, expired, or insufficient credentials. Only if security is declared.
- **security** — injection probes, IDOR, mass assignment. Only where the shape allows it.
