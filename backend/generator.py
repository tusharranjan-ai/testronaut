"""
Stage 1 (requirements) and stage 2 (test cases): prompts, validation, persistence.

The methodology for each stage lives in .claude/skills/<stage>/SKILL.md and is
loaded as the system prompt (PLAN 5: a SKILL.md, not a Python string, so it is
versioned and reviewable). This module appends only the wire contract — the
exact JSON shape — which is code's business, not methodology's.

Validation here is the raw-API half of "one validator, two transports": agent.py
runs the same functions inside its PreToolUse hook.
"""

import json
import re
from pathlib import Path
from typing import Any, Iterable

import structlog
from sqlmodel import Session, select

from .db import ArtifactOrigin, ArtifactStatus, Category, GenerationRun, Priority, Requirement, TestCase
from .llm import LLM, LLMError
from .spec_parser import declared_statuses, make_case_id, make_req_id

logger = structlog.get_logger()

SKILLS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "skills"

REQ_ID_RE = re.compile(r"^FR-[A-Z0-9_]+-\d{3,}$")
CASE_ID_RE = re.compile(r"^TC-[A-Z0-9_]+-\d{3,}$")

PRIORITIES = {p.value for p in Priority}
CATEGORIES = {c.value for c in Category}


class ValidationError(Exception):
    """An artifact the model emitted that we refuse to persist."""


# ---------- methodology ----------

_skill_cache: dict[str, str] = {}


def load_skill(name: str) -> str:
    """Read a SKILL.md, minus its YAML frontmatter. Cached; the file rarely changes."""
    if name not in _skill_cache:
        path = SKILLS_DIR / name / "SKILL.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise ValidationError(f"Methodology file missing: {path} ({e})") from e
        if text.startswith("---"):
            _, _, text = text.partition("---\n")[2].partition("---\n")
        _skill_cache[name] = text.strip()
    return _skill_cache[name]


REQUIREMENTS_CONTRACT = """
## Output contract

Reply with a JSON array and nothing else. No prose, no markdown fences.

[
  {
    "req_id": "FR-<OPTOKEN>-001",
    "title": "one line",
    "description": "what the API must do, in behaviour terms",
    "acceptance_criteria": ["independently checkable statement", "..."],
    "spec_source": "#/paths/~1pet/post/requestBody",
    "priority": "P1"
  }
]
"""

CASES_CONTRACT = """
## Output contract

Reply with a JSON array and nothing else. No prose, no markdown fences.

[
  {
    "case_id": "TC-<OPTOKEN>-001",
    "requirement_ids": ["FR-<OPTOKEN>-001"],
    "category": "happy_path",
    "title": "one line",
    "description": "why this case exists",
    "priority": "P2",
    "preconditions": ["..."],
    "path_params": {},
    "query_params": {},
    "headers": {},
    "body": null,
    "expected_status": 200,
    "expected_body": ["$.id exists", "$.name == 'Fluffy'", "$.tags length == 2"],
    "test_data_notes": "what real data the operator must supply"
  }
]
"""


def _endpoint_block(endpoint: dict) -> str:
    return (
        f"- Method: {endpoint['method']}\n"
        f"- Path: {endpoint['path']}\n"
        f"- Operation ID: {endpoint.get('operation_id') or 'none'}\n"
        f"- Summary: {endpoint.get('summary') or 'none'}\n"
        f"- Description: {endpoint.get('description') or 'none'}\n"
        f"- Parameters: {json.dumps(endpoint['parameters'])}\n"
        f"- Request body: {json.dumps(endpoint['request_body'])}\n"
        f"- Responses: {json.dumps(endpoint['responses'])}\n"
        f"- Security: {json.dumps(endpoint['security'])}\n"
    )


def requirements_prompt(endpoint: dict, categories: list[str]) -> tuple[str, str]:
    system = load_skill("requirements-authoring") + REQUIREMENTS_CONTRACT
    user = (
        f"Endpoint:\n{_endpoint_block(endpoint)}\n"
        f"Categories requested for this run: {', '.join(categories)}\n"
        f"Declared status codes: {sorted(declared_statuses(endpoint['responses'])) or 'none'}\n"
        f"OPTOKEN for this endpoint: {endpoint['op_token']}\n"
    )
    return system, user


def cases_prompt(endpoint: dict, requirements: Iterable[Requirement], categories: list[str]) -> tuple[str, str]:
    system = load_skill("test-design") + CASES_CONTRACT
    reqs = [
        {"req_id": r.req_id, "title": r.title, "description": r.description,
         "acceptance_criteria": r.acceptance_criteria, "priority": r.priority}
        for r in requirements
    ]
    user = (
        f"Endpoint:\n{_endpoint_block(endpoint)}\n"
        f"Approved requirements for this endpoint:\n{json.dumps(reqs, indent=2)}\n\n"
        f"Categories requested for this run: {', '.join(categories)}\n"
        f"Status codes the spec declares: {sorted(declared_statuses(endpoint['responses'])) or 'none'}\n"
        f"OPTOKEN for this endpoint: {endpoint['op_token']}\n"
    )
    return system, user


# ---------- validation (shared with the Agent SDK hook) ----------

def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return value if isinstance(value, str) else json.dumps(value)


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v if isinstance(v, str) else json.dumps(v) for v in value]
    return [json.dumps(value)]


def _obj(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def validate_requirement(item: dict, endpoint: dict, index: int,
                         taken_ids: set[str]) -> dict:
    """
    Normalize one model-emitted requirement, or raise ValidationError.
    Returns a dict of column values ready for Requirement(**...).
    """
    if not isinstance(item, dict):
        raise ValidationError(f"expected an object, got {type(item).__name__}")

    req_id = _text(item.get("req_id")).strip()
    if not REQ_ID_RE.match(req_id):
        req_id = make_req_id(endpoint["method"], endpoint["path"], endpoint.get("operation_id"), index)
    if req_id in taken_ids:
        raise ValidationError(f"duplicate req_id {req_id} within this run")

    title = _text(item.get("title")).strip()
    if not title:
        raise ValidationError("missing title")

    priority = _text(item.get("priority"), Priority.P2.value).strip().upper()
    if priority not in PRIORITIES:
        priority = Priority.P2.value

    return {
        "req_id": req_id,
        "method": endpoint["method"],
        "path": endpoint["path"],
        "operation_id": endpoint.get("operation_id"),
        "endpoint_key": endpoint["endpoint_key"],
        "title": title,
        "description": _text(item.get("description")).strip() or title,
        "acceptance_criteria": _str_list(item.get("acceptance_criteria")),
        "spec_source": _text(item.get("spec_source")).strip() or None,
        "priority": priority,
    }


def validate_case(item: dict, endpoint: dict, index: int, taken_ids: set[str],
                  run_categories: Iterable[str], approved_req_ids: Iterable[str]) -> dict:
    """
    Normalize one model-emitted test case, or raise ValidationError.

    Enforces the four rules from LLD 9: ID format and run-uniqueness, category
    allowed and requested, expected_status declared by the spec, endpoint in scope.
    """
    if not isinstance(item, dict):
        raise ValidationError(f"expected an object, got {type(item).__name__}")

    case_id = _text(item.get("case_id")).strip()
    if not CASE_ID_RE.match(case_id):
        case_id = make_case_id(endpoint["method"], endpoint["path"], endpoint.get("operation_id"), index)
    if case_id in taken_ids:
        raise ValidationError(f"duplicate case_id {case_id} within this run")

    title = _text(item.get("title")).strip()
    if not title:
        raise ValidationError("missing title")

    category = _text(item.get("category")).strip().lower()
    requested = set(run_categories)
    if category not in CATEGORIES:
        raise ValidationError(f"category {category!r} is not one of {sorted(CATEGORIES)}")
    if requested and category not in requested:
        raise ValidationError(f"category {category!r} was not requested for this run")

    allowed = declared_statuses(endpoint["responses"])
    try:
        expected_status = int(item.get("expected_status"))
    except (TypeError, ValueError):
        raise ValidationError(f"expected_status {item.get('expected_status')!r} is not an integer")
    if allowed and expected_status not in allowed:
        raise ValidationError(
            f"expected_status {expected_status} is not declared by the spec for "
            f"{endpoint['endpoint_key']} (declared: {sorted(allowed)})")

    approved = set(approved_req_ids)
    req_ids = [r for r in _str_list(item.get("requirement_ids")) if r]
    unknown = [r for r in req_ids if approved and r not in approved]
    if unknown:
        raise ValidationError(f"requirement_ids reference unknown requirements: {unknown}")
    if approved and not req_ids:
        raise ValidationError("requirement_ids is empty; every case must trace to a requirement")

    priority = _text(item.get("priority"), Priority.P2.value).strip().upper()
    if priority not in PRIORITIES:
        priority = Priority.P2.value

    body = item.get("body")
    if isinstance(body, str) and body.strip():
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass  # a scalar/plain-text body is legitimate

    return {
        "case_id": case_id,
        "requirement_ids": req_ids,
        "method": endpoint["method"],
        "path": endpoint["path"],
        "operation_id": endpoint.get("operation_id"),
        "endpoint_key": endpoint["endpoint_key"],
        "category": category,
        "title": title,
        "description": _text(item.get("description")).strip() or title,
        "priority": priority,
        "preconditions": _str_list(item.get("preconditions")),
        "path_params": _obj(item.get("path_params")),
        "query_params": _obj(item.get("query_params")),
        "headers": _obj(item.get("headers")),
        "body": body,
        "expected_status": expected_status,
        "expected_body": _str_list(item.get("expected_body")),
        "test_data_notes": _text(item.get("test_data_notes")).strip(),
    }


# ---------- orchestration ----------

def _taken(session: Session, model, column, run_id: int) -> set[str]:
    return set(session.exec(select(column).where(model.run_id == run_id)).all())


async def generate_requirements_for_endpoint(
    llm: LLM, endpoint: dict, run: GenerationRun, session: Session,
) -> tuple[list[Requirement], list[str]]:
    """
    One LLM call for one endpoint. Returns (persisted, rejected_reasons).

    A rejected item is skipped, not fatal: partial results are worth keeping,
    especially on slow local models.
    """
    system, user = requirements_prompt(endpoint, run.categories)
    items = await llm.complete_json(system, user)

    taken = _taken(session, Requirement, Requirement.req_id, run.id)
    next_index = 1 + len(session.exec(
        select(Requirement).where(Requirement.run_id == run.id,
                                  Requirement.endpoint_key == endpoint["endpoint_key"])).all())

    created: list[Requirement] = []
    rejected: list[str] = []
    for offset, item in enumerate(items):
        try:
            values = validate_requirement(item, endpoint, next_index + offset, taken)
        except ValidationError as e:
            rejected.append(str(e))
            logger.warning("requirement_rejected", endpoint=endpoint["endpoint_key"], reason=str(e))
            continue
        taken.add(values["req_id"])
        req = Requirement(run_id=run.id, order_index=next_index + offset,
                          origin=ArtifactOrigin.GENERATED.value,
                          status=ArtifactStatus.DRAFT.value, **values)
        session.add(req)
        created.append(req)

    session.commit()
    for req in created:
        session.refresh(req)
    return created, rejected


async def generate_cases_for_endpoint(
    llm: LLM, endpoint: dict, approved: list[Requirement], run: GenerationRun, session: Session,
) -> tuple[list[TestCase], list[str]]:
    """One LLM call for one endpoint. Returns (persisted, rejected_reasons)."""
    if not approved:
        return [], ["no approved requirements for this endpoint"]

    system, user = cases_prompt(endpoint, approved, run.categories)
    # A case carries a body, headers and assertions, and there is one per
    # requirement — output scales with len(approved), unlike stage 1.
    budget = max(4000, 1500 * len(approved))

    taken = _taken(session, TestCase, TestCase.case_id, run.id)
    next_index = 1 + len(session.exec(
        select(TestCase).where(TestCase.run_id == run.id,
                               TestCase.endpoint_key == endpoint["endpoint_key"])).all())
    approved_ids = [r.req_id for r in approved]

    def sift(items: list[dict], start: int) -> tuple[list[dict], list[str]]:
        good, bad = [], []
        for offset, item in enumerate(items):
            try:
                good.append(validate_case(item, endpoint, start + offset, taken,
                                          run.categories, approved_ids))
                taken.add(good[-1]["case_id"])
            except ValidationError as e:
                bad.append(str(e))
                logger.warning("case_rejected", endpoint=endpoint["endpoint_key"], reason=str(e))
        return good, bad

    values, rejected = sift(await llm.complete_json(system, user, max_tokens=budget), next_index)

    # One bounded retry with the reasons fed back. Without it a single bad
    # assumption (a status the spec does not declare) silently costs whole
    # requirements' worth of coverage.
    if rejected:
        repair = (
            f"{user}\n\n---\nYour previous reply had {len(rejected)} case(s) rejected:\n"
            + "\n".join(f"- {r}" for r in rejected)
            + "\n\nReturn ONLY corrected replacements for the rejected cases. "
              "Use a status code the spec actually declares."
        )
        try:
            more, still_bad = sift(await llm.complete_json(system, repair, max_tokens=budget),
                                   next_index + len(values))
            values += more
            rejected = still_bad
        except LLMError as e:
            logger.warning("case_retry_failed", endpoint=endpoint["endpoint_key"], error=str(e))

    created = []
    for offset, item in enumerate(values):
        case = TestCase(run_id=run.id, order_index=next_index + offset,
                        origin=ArtifactOrigin.GENERATED.value,
                        status=ArtifactStatus.DRAFT.value, **item)
        session.add(case)
        created.append(case)

    session.commit()
    for case in created:
        session.refresh(case)
    return created, rejected
