"""
Critic pass, snapshot + bounded revision, deterministic traceability.

Two things are deliberately split here:

* **Semantics** are judged per endpoint by a critic model — does this match the
  spec, is this status code real. One call per endpoint per stage.
* **Coverage** is a set operation, so Python computes it. Asking a model whether
  every requirement is covered across 200 cases neither fits in context nor
  produces a reliable answer.

Revision is exactly one pass and is never silent: every applied finding records
before/after on the finding itself, which is what the UI renders and what
`revert_finding` undoes.
"""

import copy
import json
from typing import Any, Optional

import structlog
from sqlmodel import Session, select

from .db import (
    ArtifactStatus, GenerationRun, Requirement, Resolution, Review, ReviewStage,
    ReviewVerdict, Severity, TestCase,
)
from .generator import CATEGORIES, PRIORITIES, _obj, _str_list, _text, load_skill
from .llm import LLM
from .spec_parser import declared_statuses

logger = structlog.get_logger()

FINDINGS_CONTRACT = """
## Output contract

Reply with a JSON array of findings and nothing else. No prose, no fences.
An empty array `[]` means everything is correct.

[
  {
    "severity": "high",
    "target_id": "<the req_id or case_id this is about>",
    "field": "<the field at fault>",
    "issue": "what is wrong, in one or two sentences",
    "suggestion": "the corrected value, verbatim, if resolution is applied",
    "resolution": "applied"
  }
]
"""

# Fields a finding is allowed to rewrite, and how to coerce the suggestion.
REQUIREMENT_FIELDS: dict[str, Any] = {
    "title": _text,
    "description": _text,
    "acceptance_criteria": _str_list,
    "priority": lambda v: _text(v).strip().upper(),
    "spec_source": _text,
}

CASE_FIELDS: dict[str, Any] = {
    "title": _text,
    "description": _text,
    "category": lambda v: _text(v).strip().lower(),
    "priority": lambda v: _text(v).strip().upper(),
    "preconditions": _str_list,
    "path_params": _obj,
    "query_params": _obj,
    "headers": _obj,
    "expected_body": _str_list,
    "test_data_notes": _text,
    "expected_status": lambda v: int(v),
    "body": lambda v: json.loads(v) if isinstance(v, str) and v.strip() else v,
}


def _artifact_json(artifact: Requirement | TestCase, fields: dict) -> dict:
    return {"id": artifact.req_id if isinstance(artifact, Requirement) else artifact.case_id,
            **{f: getattr(artifact, f) for f in fields}}


def _normalize(findings: list[dict], valid_targets: set[str], allowed_fields: dict) -> list[dict]:
    """
    Coerce model output into the canonical Finding shape and drop anything that
    points at an artifact this run never produced — a critic cannot raise a
    finding about something that does not exist.
    """
    out: list[dict] = []
    for i, raw in enumerate(findings, start=1):
        if not isinstance(raw, dict):
            continue
        target_id = _text(raw.get("target_id") or raw.get("req_id") or raw.get("case_id")).strip()
        if target_id not in valid_targets:
            logger.warning("finding_dropped_unknown_target", target_id=target_id)
            continue

        severity = _text(raw.get("severity"), Severity.MEDIUM.value).strip().lower()
        if severity not in {s.value for s in Severity}:
            severity = Severity.MEDIUM.value

        field = _text(raw.get("field")).strip()
        suggestion = raw.get("suggestion", raw.get("proposed_value"))
        resolution = _text(raw.get("resolution"), Resolution.RAISED.value).strip().lower()
        if resolution not in {Resolution.APPLIED.value, Resolution.RAISED.value}:
            resolution = Resolution.RAISED.value
        # A finding can only be applied if it names a writable field and supplies a value.
        if field not in allowed_fields or suggestion in (None, ""):
            resolution = Resolution.RAISED.value

        out.append({
            "finding_id": f"F-{i}",
            "severity": severity,
            "target_id": target_id,
            "field": field,
            "issue": _text(raw.get("issue") or raw.get("message") or raw.get("rationale")).strip(),
            "suggestion": suggestion,
            "resolution": resolution,
            "before": None,
            "after": None,
        })
    return out


def _apply(finding: dict, artifact: Requirement | TestCase, allowed_fields: dict) -> bool:
    """Apply one finding to one artifact, recording before/after on the finding."""
    field = finding["field"]
    coerce = allowed_fields[field]
    try:
        after = coerce(finding["suggestion"])
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        finding["resolution"] = Resolution.RAISED.value
        finding["issue"] = f"{finding['issue']} (suggestion could not be applied: {e})"
        return False

    if field == "category" and after not in CATEGORIES:
        finding["resolution"] = Resolution.RAISED.value
        return False
    if field == "priority" and after not in PRIORITIES:
        finding["resolution"] = Resolution.RAISED.value
        return False

    before = getattr(artifact, field)
    if before == after:
        finding["resolution"] = Resolution.RAISED.value
        return False

    if artifact.snapshot is None:
        artifact.snapshot = _artifact_json(artifact, allowed_fields)
    setattr(artifact, field, after)
    finding["before"] = before
    finding["after"] = after
    artifact.status = ArtifactStatus.REVISED.value
    return True


async def _review_stage(
    llm: LLM, run: GenerationRun, endpoint: dict, session: Session,
    stage: ReviewStage, skill: str, artifacts: list, allowed_fields: dict, payload: dict,
) -> Optional[Review]:
    if not artifacts:
        return None

    system = load_skill(skill) + FINDINGS_CONTRACT
    user = "\n\n".join(f"{k}:\n{v}" for k, v in payload.items())
    findings = _normalize(
        await llm.complete_json(system, user),
        {a.req_id if isinstance(a, Requirement) else a.case_id for a in artifacts},
        allowed_fields,
    )

    by_id = {(a.req_id if isinstance(a, Requirement) else a.case_id): a for a in artifacts}
    applied_any = False
    for finding in findings:
        if finding["resolution"] == Resolution.APPLIED.value:
            applied_any |= _apply(finding, by_id[finding["target_id"]], allowed_fields)

    review = Review(
        run_id=run.id,
        stage=stage.value,
        endpoint_key=endpoint["endpoint_key"],
        verdict=ReviewVerdict.REVISED.value if applied_any else ReviewVerdict.PASS.value,
        provider=llm.provider,
        model=llm.model,
        findings=findings,
    )
    session.add(review)
    session.commit()
    session.refresh(review)
    return review


async def review_requirements(llm: LLM, run: GenerationRun, endpoint: dict,
                              session: Session) -> Optional[Review]:
    reqs = session.exec(
        select(Requirement).where(Requirement.run_id == run.id,
                                  Requirement.endpoint_key == endpoint["endpoint_key"])
        .order_by(Requirement.order_index)
    ).all()
    payload = {
        "Endpoint": json.dumps({k: endpoint[k] for k in
                                ("method", "path", "operation_id", "parameters",
                                 "request_body", "responses", "security")}, indent=2),
        "Requirements under review": json.dumps(
            [_artifact_json(r, REQUIREMENT_FIELDS) for r in reqs], indent=2),
        "Status codes the spec declares for this operation (the ONLY ones a "
        "requirement may reference)": json.dumps(
            sorted(declared_statuses(endpoint["responses"]))),
        "Categories requested for this run": ", ".join(run.categories),
    }
    return await _review_stage(llm, run, endpoint, session, ReviewStage.REQUIREMENTS,
                               "requirements-review", list(reqs), REQUIREMENT_FIELDS, payload)


async def review_cases(llm: LLM, run: GenerationRun, endpoint: dict,
                       session: Session) -> Optional[Review]:
    cases = session.exec(
        select(TestCase).where(TestCase.run_id == run.id,
                               TestCase.endpoint_key == endpoint["endpoint_key"])
        .order_by(TestCase.order_index)
    ).all()
    approved = session.exec(
        select(Requirement).where(Requirement.run_id == run.id,
                                  Requirement.endpoint_key == endpoint["endpoint_key"],
                                  Requirement.status == ArtifactStatus.APPROVED.value)
    ).all()
    payload = {
        "Endpoint": json.dumps({k: endpoint[k] for k in
                                ("method", "path", "operation_id", "parameters",
                                 "request_body", "responses", "security")}, indent=2),
        "Test cases under review": json.dumps(
            [{**_artifact_json(c, CASE_FIELDS), "requirement_ids": c.requirement_ids}
             for c in cases], indent=2),
        "Approved requirements they must trace to": json.dumps(
            [{"req_id": r.req_id, "title": r.title,
              "acceptance_criteria": r.acceptance_criteria} for r in approved], indent=2),
        "Status codes the spec declares": json.dumps(
            sorted(int(c) for c in endpoint["responses"] if str(c).isdigit())),
    }
    return await _review_stage(llm, run, endpoint, session, ReviewStage.TEST_CASES,
                               "test-case-review", list(cases), CASE_FIELDS, payload)


# ---------- revert ----------

def revert_finding(session: Session, run_id: int, finding_id: str, target_id: str) -> bool:
    """
    Undo one applied finding: write `before` back to the field and mark the
    finding reverted. Per-finding, not whole-artifact, so the user can keep the
    reviewer's other edits (PLAN 4).
    """
    reviews = session.exec(select(Review).where(Review.run_id == run_id)).all()
    for review in reviews:
        # Work on a copy: mutating the loaded JSON value in place leaves the new
        # value equal to what SQLAlchemy thinks is on disk, so the write is dropped.
        findings = copy.deepcopy(review.findings)
        for finding in findings:
            if finding.get("finding_id") != finding_id or finding.get("target_id") != target_id:
                continue
            if finding.get("resolution") != Resolution.APPLIED.value:
                return False

            artifact = _find_artifact(session, run_id, target_id)
            if artifact is None:
                return False
            setattr(artifact, finding["field"], finding["before"])
            finding["resolution"] = Resolution.REVERTED.value
            review.findings = findings
            session.add(artifact)
            session.add(review)
            session.commit()
            return True
    return False


def _find_artifact(session: Session, run_id: int, target_id: str):
    if target_id.startswith("FR-"):
        return session.exec(select(Requirement).where(
            Requirement.run_id == run_id, Requirement.req_id == target_id)).first()
    return session.exec(select(TestCase).where(
        TestCase.run_id == run_id, TestCase.case_id == target_id)).first()


# ---------- deterministic traceability ----------

def check_traceability(session: Session, run: GenerationRun) -> dict:
    """
    Run-wide coverage, computed in Python. Reports:
      uncovered   — approved requirements no case references
      orphans     — cases referencing no requirement, or an unknown one
      duplicates  — case_ids used more than once in this run
      endpoints_without_cases — selected endpoints that produced nothing
    """
    reqs = session.exec(select(Requirement).where(Requirement.run_id == run.id)).all()
    cases = session.exec(select(TestCase).where(TestCase.run_id == run.id)).all()

    approved = {r.req_id for r in reqs if r.status == ArtifactStatus.APPROVED.value}
    known = {r.req_id for r in reqs}

    covered: set[str] = set()
    orphans: list[dict] = []
    for case in cases:
        linked = [rid for rid in case.requirement_ids if rid in known]
        unknown = [rid for rid in case.requirement_ids if rid not in known]
        covered.update(linked)
        if not linked:
            orphans.append({"case_id": case.case_id,
                            "reason": f"references unknown requirements {unknown}" if unknown
                                      else "references no requirement"})

    seen: dict[str, int] = {}
    for case in cases:
        seen[case.case_id] = seen.get(case.case_id, 0) + 1

    with_cases = {c.endpoint_key for c in cases}
    selected = run.endpoint_keys or sorted({r.endpoint_key for r in reqs})

    uncovered = sorted(approved - covered)
    duplicates = sorted(cid for cid, n in seen.items() if n > 1)
    return {
        "covered": len(covered & approved),
        "total_requirements": len(approved),
        "uncovered_requirement_ids": uncovered,
        "orphan_case_ids": [o["case_id"] for o in orphans],
        "duplicate_case_ids": duplicates,
        # Beyond the LLD shape, and worth surfacing: an endpoint that produced
        # nothing at all is invisible in a per-requirement coverage count.
        "orphan_details": orphans,
        "endpoints_without_cases": sorted(k for k in selected if k not in with_cases),
        "cases_total": len(cases),
        "requirements_total": len(reqs),
        "fully_traced": not uncovered and not orphans and not duplicates,
    }
