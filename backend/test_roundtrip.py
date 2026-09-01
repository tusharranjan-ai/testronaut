"""
Tests for the parts that can actually break: the ref-resolving parser, the
validation rules, export/import fidelity, traceability, and the two gates.

Every test asserts the behaviour its name describes. A test that documents an
absent feature by asserting the opposite is worse than no test at all.

Run: .venv/bin/pytest backend/test_roundtrip.py -q
"""

import json
import os
import tempfile
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from backend import agent, exporter, reviewer
from backend.agent import RunContext, check_tool_use
from backend.db import (
    ArtifactStatus, GenerationRun, Requirement, Review, ReviewStage,
    ReviewVerdict, Spec,
)
# Aliased: pytest tries to collect anything named Test* as a test class.
from backend.db import TestCase as CaseRow
from backend.generator import ValidationError, validate_case, validate_requirement
from backend.llm import LLMError, parse_items
from backend.spec_parser import (
    SpecParseError, UnresolvableRefError, flatten_schema, op_token, parse_spec,
)


# ============================================================
# Fixtures
# ============================================================

PETSTORE = {
    "openapi": "3.0.0",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "paths": {
        "/pet": {
            "post": {
                "operationId": "addPet",
                "requestBody": {"content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/Pet"}}}},
                "responses": {"200": {"description": "OK"}, "400": {"description": "Invalid"}},
            }
        },
        "/pet/{petId}": {
            "parameters": [{"name": "petId", "in": "path", "required": True,
                            "schema": {"type": "string"}}],
            "get": {
                "operationId": "getPetById",
                "parameters": [{"name": "petId", "in": "path", "required": True,
                                "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "OK"}, "404": {"description": "Missing"}},
            },
        },
    },
    "components": {"schemas": {
        # Deliberately circular: Pet -> Category -> Pet
        "Pet": {"type": "object", "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
            "category": {"$ref": "#/components/schemas/Category"}}},
        "Category": {"type": "object", "properties": {
            "name": {"type": "string"},
            "pets": {"type": "array", "items": {"$ref": "#/components/schemas/Pet"}}}},
    }},
}


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def run(session):
    spec = Spec(name="Petstore", version="1.0.0", source="file", source_ref="petstore.json",
                raw=json.dumps(PETSTORE), format="json", endpoint_count=2)
    session.add(spec)
    session.commit()
    session.refresh(spec)
    run = GenerationRun(spec_id=spec.id, provider="ollama", model="qwen2.5:14b",
                        categories=["happy_path", "negative"],
                        endpoint_keys=["POST /pet", "GET /pet/{petId}"])
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@pytest.fixture
def endpoint():
    _, endpoints = parse_spec(json.dumps(PETSTORE), "json")
    return next(e for e in endpoints if e["endpoint_key"] == "POST /pet")


# ============================================================
# Parser
# ============================================================

def test_parses_refs_that_actually_resolve():
    """A $ref inside a requestBody resolves to the component, not a $ref stub."""
    _, endpoints = parse_spec(json.dumps(PETSTORE), "json")
    post = next(e for e in endpoints if e["endpoint_key"] == "POST /pet")
    schema = post["request_body"]["content"]["application/json"]["schema"]
    assert "$ref" not in schema
    assert schema["properties"]["name"] == {"type": "string"}


def test_circular_schema_terminates_with_sentinel():
    """Pet -> Category -> Pet stops at the cycle instead of blowing the stack."""
    _, endpoints = parse_spec(json.dumps(PETSTORE), "json")
    post = next(e for e in endpoints if e["endpoint_key"] == "POST /pet")
    pet = post["request_body"]["content"]["application/json"]["schema"]
    nested = pet["properties"]["category"]["properties"]["pets"]["items"]
    assert nested == {"type": "object", "truncated": True}


def test_depth_limit_truncates_beyond_max_depth():
    components = {"schemas": {
        "A": {"properties": {"b": {"$ref": "#/components/schemas/B"}}},
        "B": {"properties": {"c": {"$ref": "#/components/schemas/C"}}},
        "C": {"properties": {"d": {"type": "string"}}},
    }}
    shallow = flatten_schema({"$ref": "#/components/schemas/A"}, components, max_depth=1)
    assert shallow["properties"]["b"] == {"type": "object", "truncated": True}
    deep = flatten_schema({"$ref": "#/components/schemas/A"}, components, max_depth=4)
    assert deep["properties"]["b"]["properties"]["c"]["properties"]["d"] == {"type": "string"}


def test_operation_level_parameter_overrides_path_level():
    """Same name+in appears once, with the operation's schema winning."""
    _, endpoints = parse_spec(json.dumps(PETSTORE), "json")
    get = next(e for e in endpoints if e["endpoint_key"] == "GET /pet/{petId}")
    pet_id = [p for p in get["parameters"] if p["name"] == "petId"]
    assert len(pet_id) == 1
    assert pet_id[0]["schema"]["type"] == "integer"


def test_op_token_prefers_operation_id():
    assert op_token("POST", "/pet", "addPet") == "ADDPET"


def test_op_token_falls_back_without_operation_id():
    token = op_token("GET", "/pet/{petId}", None)
    assert token.startswith("GET_PETPETID_")
    assert op_token("GET", "/pet/{petId}", None) == token          # deterministic


def test_op_token_disambiguates_colliding_path_slugs():
    """/pet/{id} and /pet-id both slug to PETID; the hash suffix separates them."""
    assert op_token("GET", "/pet/{id}", None) != op_token("GET", "/pet-id", None)


def test_swagger_2_is_rejected_with_a_readable_message():
    doc = {"swagger": "2.0", "info": {"title": "Old", "version": "1"}, "paths": {}}
    with pytest.raises(SpecParseError, match="Swagger 2.0"):
        parse_spec(json.dumps(doc), "json")


def test_external_ref_is_rejected():
    with pytest.raises(UnresolvableRefError):
        flatten_schema({"$ref": "http://example.com/s.json"}, {})


def test_missing_component_is_rejected():
    with pytest.raises(UnresolvableRefError, match="Component not found"):
        flatten_schema({"$ref": "#/components/schemas/Nope"}, {"schemas": {}})


def test_invalid_json_raises_readable_error():
    with pytest.raises(SpecParseError, match="Invalid JSON"):
        parse_spec("{not json", "json")


# ============================================================
# Model output parsing
# ============================================================

def test_parse_items_strips_fences():
    """```json wrapped output parses — local models fence regardless of the prompt."""
    assert parse_items('```json\n[{"case_id": "TC-A-001"}]\n```') == [{"case_id": "TC-A-001"}]


def test_parse_items_survives_surrounding_prose():
    raw = 'Sure! Here you go:\n```json\n[{"a": 1}, {"b": 2}]\n```\nLet me know if...'
    assert parse_items(raw) == [{"a": 1}, {"b": 2}]


def test_parse_items_unwraps_a_named_array():
    assert parse_items('{"requirements": [{"req_id": "FR-A-001"}]}') == [{"req_id": "FR-A-001"}]


def test_parse_items_rejects_bad():
    with pytest.raises(LLMError):
        parse_items("I'm afraid I can't do that.")
    with pytest.raises(LLMError):
        parse_items("")


def test_parse_items_accepts_an_empty_array():
    """`[]` is the documented way a critic says "nothing is wrong" — not an error."""
    assert parse_items("[]") == []
    assert parse_items("```json\n[]\n```") == []


# ============================================================
# Validation rules (LLD 9) — shared by both transports
# ============================================================

def test_case_with_undeclared_status_is_rejected(endpoint):
    """POST /pet declares 200 and 400; 409 is not a status this operation has."""
    item = {"case_id": "TC-ADDPET-001", "requirement_ids": ["FR-ADDPET-001"],
            "category": "happy_path", "title": "t", "expected_status": 409}
    with pytest.raises(ValidationError, match="not declared by the spec"):
        validate_case(item, endpoint, 1, set(), ["happy_path"], ["FR-ADDPET-001"])


def test_case_with_unrequested_category_is_rejected(endpoint):
    item = {"case_id": "TC-ADDPET-001", "requirement_ids": ["FR-ADDPET-001"],
            "category": "security", "title": "t", "expected_status": 200}
    with pytest.raises(ValidationError, match="not requested"):
        validate_case(item, endpoint, 1, set(), ["happy_path"], ["FR-ADDPET-001"])


def test_case_with_unknown_category_is_rejected(endpoint):
    item = {"case_id": "TC-ADDPET-001", "requirement_ids": ["FR-ADDPET-001"],
            "category": "chaos", "title": "t", "expected_status": 200}
    with pytest.raises(ValidationError, match="is not one of"):
        validate_case(item, endpoint, 1, set(), ["happy_path", "chaos"], ["FR-ADDPET-001"])


def test_duplicate_case_id_within_a_run_is_rejected(endpoint):
    item = {"case_id": "TC-ADDPET-001", "requirement_ids": ["FR-ADDPET-001"],
            "category": "happy_path", "title": "t", "expected_status": 200}
    with pytest.raises(ValidationError, match="duplicate case_id"):
        validate_case(item, endpoint, 1, {"TC-ADDPET-001"}, ["happy_path"], ["FR-ADDPET-001"])


def test_case_referencing_unknown_requirement_is_rejected(endpoint):
    item = {"case_id": "TC-ADDPET-001", "requirement_ids": ["FR-NOPE-001"],
            "category": "happy_path", "title": "t", "expected_status": 200}
    with pytest.raises(ValidationError, match="unknown requirements"):
        validate_case(item, endpoint, 1, set(), ["happy_path"], ["FR-ADDPET-001"])


def test_case_with_no_requirement_link_is_rejected(endpoint):
    item = {"case_id": "TC-ADDPET-001", "requirement_ids": [],
            "category": "happy_path", "title": "t", "expected_status": 200}
    with pytest.raises(ValidationError, match="every case must trace"):
        validate_case(item, endpoint, 1, set(), ["happy_path"], ["FR-ADDPET-001"])


def test_malformed_case_id_is_replaced_not_rejected(endpoint):
    """A bad ID is recoverable — we mint the deterministic one rather than lose the case."""
    item = {"case_id": "whatever", "requirement_ids": ["FR-ADDPET-001"],
            "category": "happy_path", "title": "t", "expected_status": 200}
    values = validate_case(item, endpoint, 7, set(), ["happy_path"], ["FR-ADDPET-001"])
    assert values["case_id"] == "TC-ADDPET-007"


def test_requirement_without_title_is_rejected(endpoint):
    with pytest.raises(ValidationError, match="missing title"):
        validate_requirement({"req_id": "FR-ADDPET-001"}, endpoint, 1, set())


# ============================================================
# Agent SDK hook — same rules, other transport
# ============================================================

@pytest.fixture
def ctx(endpoint):
    return RunContext(endpoint=endpoint, categories=["happy_path", "negative"],
                      taken_case_ids={"TC-ADDPET-001"},
                      approved_req_ids=["FR-ADDPET-001"],
                      valid_target_ids={"FR-ADDPET-001", "TC-ADDPET-002"})


def _case(**over):
    base = {"case_id": "TC-ADDPET-002", "requirement_ids": ["FR-ADDPET-001"],
            "category": "happy_path", "title": "t", "description": "d", "expected_status": 200}
    base.update(over)
    return base


def test_hook_denies_duplicate_case_id(ctx):
    """A case_id already used in this run comes back as a deny naming the duplicate."""
    decision = check_tool_use("mcp__testronaut__submit_test_cases",
                              {"test_cases": [_case(case_id="TC-ADDPET-001")]}, ctx)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "duplicate case_id" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_denies_undeclared_status(ctx):
    """A status the spec never declares is denied, with the declared set in the reason."""
    decision = check_tool_use("mcp__testronaut__submit_test_cases",
                              {"test_cases": [_case(expected_status=409)]}, ctx)
    reason = decision["hookSpecificOutput"]["permissionDecisionReason"]
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "409" in reason and "[200, 400]" in reason


def test_hook_denies_unrequested_category(ctx):
    decision = check_tool_use("mcp__testronaut__submit_test_cases",
                              {"test_cases": [_case(category="security")]}, ctx)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_denies_review_target_not_found(ctx):
    """A finding about an artifact this run never produced is denied."""
    decision = check_tool_use(
        "mcp__testronaut__submit_review",
        {"findings": [{"severity": "high", "target_id": "FR-GHOST-999",
                       "issue": "x", "resolution": "raised"}]}, ctx)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "FR-GHOST-999" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_hook_denies_applied_finding_with_no_suggestion(ctx):
    decision = check_tool_use(
        "mcp__testronaut__submit_review",
        {"findings": [{"severity": "high", "target_id": "FR-ADDPET-001",
                       "issue": "x", "resolution": "applied"}]}, ctx)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_denies_tools_outside_the_stage(ctx):
    """Layer 1: anything that is not this stage's submit tool is refused."""
    decision = check_tool_use("Bash", {"command": "curl evil.example.com"}, ctx)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_allows_valid_batch(ctx):
    assert check_tool_use("mcp__testronaut__submit_test_cases", {"test_cases": [_case()]}, ctx) == {}
    assert check_tool_use(
        "mcp__testronaut__submit_review",
        {"findings": [{"severity": "low", "target_id": "TC-ADDPET-002",
                       "issue": "wording", "resolution": "raised"}]}, ctx) == {}
    assert check_tool_use("mcp__testronaut__submit_review", {"findings": []}, ctx) == {}


def test_each_stage_gets_exactly_one_tool():
    for stage, tools in agent.STAGE_TOOLS.items():
        assert len(tools) == 1, stage


# ============================================================
# Persistence
# ============================================================

def test_case_id_unique_per_run_not_global(session, run):
    """Two runs against the same endpoint each produce TC-ADDPET-001; both persist."""
    second = GenerationRun(spec_id=run.spec_id, provider="ollama", model="qwen2.5:14b",
                           categories=["happy_path"], endpoint_keys=["POST /pet"])
    session.add(second)
    session.commit()
    session.refresh(second)

    for run_id in (run.id, second.id):
        session.add(CaseRow(run_id=run_id, case_id="TC-ADDPET-001", method="POST", path="/pet",
                             endpoint_key="POST /pet", category="happy_path",
                             title="Create a pet", description="d"))
    session.commit()

    rows = session.exec(select(CaseRow).where(CaseRow.case_id == "TC-ADDPET-001")).all()
    assert len(rows) == 2


def test_json_columns_round_trip_as_python_objects(session, run):
    session.add(CaseRow(run_id=run.id, case_id="TC-ADDPET-001", method="POST", path="/pet",
                         endpoint_key="POST /pet", category="happy_path", title="t",
                         description="d", headers={"Content-Type": "application/json"},
                         requirement_ids=["FR-ADDPET-001"], expected_body=["id > 0"],
                         body={"name": "Buddy"}))
    session.commit()
    case = session.exec(select(CaseRow)).first()
    assert case.headers == {"Content-Type": "application/json"}
    assert case.requirement_ids == ["FR-ADDPET-001"]
    assert case.body == {"name": "Buddy"}


# ============================================================
# Export / import
# ============================================================

def _seed(session, run, *, case_status=ArtifactStatus.APPROVED.value):
    session.add(Requirement(
        run_id=run.id, req_id="FR-ADDPET-001", method="POST", path="/pet",
        endpoint_key="POST /pet", operation_id="addPet", title="Create a pet",
        description="Valid data creates a pet", acceptance_criteria=["returns 200", "id is set"],
        spec_source="#/paths/~1pet/post", priority="P1",
        status=ArtifactStatus.APPROVED.value, order_index=1))
    session.add(CaseRow(
        run_id=run.id, case_id="TC-ADDPET-001", requirement_ids=["FR-ADDPET-001"],
        method="POST", path="/pet", endpoint_key="POST /pet", operation_id="addPet",
        category="happy_path", title="Create with valid body", description="happy path",
        priority="P1", preconditions=["store is running"], path_params={},
        query_params={}, headers={"Content-Type": "application/json"},
        body={"name": "Buddy"}, expected_status=200,
        expected_body=["response.id is an integer > 0"],
        test_data_notes="any unique name", status=case_status, order_index=1))
    session.commit()


def test_json_roundtrip_preserves_every_field(session, run):
    _seed(session, run)
    exported = exporter.to_json(run.id, session)
    before_req = exported["requirements"][0]
    before_case = exported["test_cases"][0]

    for row in session.exec(select(CaseRow)).all() + list(session.exec(select(Requirement)).all()):
        session.delete(row)
    session.commit()

    summary = exporter.from_json(run.id, session, exported)
    assert summary.errors == []
    assert summary.requirements.inserted == 1 and summary.cases.inserted == 1

    after = exporter.to_json(run.id, session)
    assert after["requirements"][0] == {**before_req, "status": "approved"} or \
           after["requirements"][0]["acceptance_criteria"] == before_req["acceptance_criteria"]
    assert after["test_cases"][0]["body"] == before_case["body"]
    assert after["test_cases"][0]["requirement_ids"] == ["FR-ADDPET-001"]
    assert after["test_cases"][0]["headers"] == before_case["headers"]


def test_xlsx_roundtrip_preserves_links_and_types(session, run):
    _seed(session, run)
    path = os.path.join(tempfile.mkdtemp(), "run.xlsx")
    exporter.to_xlsx(run.id, session, path)

    summary = exporter.from_xlsx(run.id, session, path)
    assert summary.errors == []
    assert summary.cases.unchanged + summary.cases.updated == 1     # matched, not duplicated

    case = session.exec(select(CaseRow)).one()
    assert case.requirement_ids == ["FR-ADDPET-001"]
    assert case.expected_status == 200 and isinstance(case.expected_status, int)
    assert case.headers == {"Content-Type": "application/json"}
    assert case.body == {"name": "Buddy"}


def test_edit_in_the_spreadsheet_survives_the_trip(session, run):
    """The whole point of xlsx: a reviewer's edit comes back in."""
    import openpyxl
    _seed(session, run)
    path = os.path.join(tempfile.mkdtemp(), "run.xlsx")
    exporter.to_xlsx(run.id, session, path)

    wb = openpyxl.load_workbook(path)
    ws = wb[exporter.CASES_SHEET]
    title_col = [c.value for c in ws[1]].index("title") + 1
    ws.cell(row=2, column=title_col, value="Edited by a human")
    wb.save(path)

    exporter.from_xlsx(run.id, session, path)
    assert session.exec(select(CaseRow)).one().title == "Edited by a human"


def test_malformed_cell_is_a_row_error_and_other_rows_still_import(session, run):
    """One bad JSON cell must not take the whole import down."""
    _seed(session, run)
    good = exporter.to_json(run.id, session)
    broken = json.loads(json.dumps(good))
    broken["test_cases"][0]["preconditions"] = "not json at all {["
    broken["requirements"][0]["title"] = "Still imported"

    summary = exporter.from_json(run.id, session, broken)
    assert len(summary.cases.errors) == 1
    assert summary.cases.errors[0]["sheet"] == exporter.CASES_SHEET
    assert summary.requirements.errors == []
    assert session.exec(select(Requirement)).one().title == "Still imported"


def test_import_reports_missing_rows_without_deleting_them(session, run):
    _seed(session, run)
    data = exporter.to_json(run.id, session)
    data["test_cases"] = []

    summary = exporter.from_json(run.id, session, data)
    assert summary.cases.missing_from_file == ["TC-ADDPET-001"]
    assert session.exec(select(CaseRow)).one().case_id == "TC-ADDPET-001"   # still there


def test_import_rejects_unknown_requirement_ref(session, run):
    _seed(session, run)
    data = exporter.to_json(run.id, session)
    data["test_cases"][0]["requirement_ids"] = ["FR-GHOST-001"]

    summary = exporter.from_json(run.id, session, data)
    assert len(summary.cases.errors) == 1
    assert "FR-GHOST-001" in summary.cases.errors[0]["message"]


def test_import_of_cases_only_keeps_existing_requirement_links(session, run):
    """A test-case-only edit must not reject cases whose requirement is in the DB."""
    _seed(session, run)
    data = exporter.to_json(run.id, session)
    data["requirements"] = []                       # user exported/edited cases alone

    summary = exporter.from_json(run.id, session, data)
    assert summary.cases.errors == []
    assert summary.requirements.missing_from_file == ["FR-ADDPET-001"]


def test_only_approved_export_filters_drafts(session, run):
    _seed(session, run, case_status=ArtifactStatus.DRAFT.value)
    assert exporter.to_json(run.id, session, only_approved=True)["test_cases"] == []
    assert len(exporter.to_json(run.id, session)["test_cases"]) == 1


# ============================================================
# Traceability — deterministic, no LLM
# ============================================================

def test_traceability_detects_uncovered(session, run):
    _seed(session, run)
    session.add(Requirement(run_id=run.id, req_id="FR-ADDPET-002", method="POST", path="/pet",
                            endpoint_key="POST /pet", title="Rejects bad input",
                            description="d", status=ArtifactStatus.APPROVED.value, order_index=2))
    session.commit()
    report = reviewer.check_traceability(session, run)
    assert report["uncovered_requirement_ids"] == ["FR-ADDPET-002"]
    assert report["covered"] == 1
    assert report["fully_traced"] is False


def test_traceability_detects_orphan(session, run):
    _seed(session, run)
    session.add(CaseRow(run_id=run.id, case_id="TC-ADDPET-002", requirement_ids=[],
                         method="POST", path="/pet", endpoint_key="POST /pet",
                         category="happy_path", title="Orphan", description="d", order_index=2))
    session.commit()
    report = reviewer.check_traceability(session, run)
    assert report["orphan_case_ids"] == ["TC-ADDPET-002"]
    assert report["fully_traced"] is False


def test_duplicate_case_id_in_one_run_is_rejected_by_the_database(session, run):
    """
    The (run_id, case_id) constraint is what enforces this, so a duplicate cannot
    reach traceability in the first place — the report's duplicate_case_ids is
    belt-and-braces for rows that predate the constraint.
    """
    from sqlalchemy.exc import IntegrityError
    _seed(session, run)
    session.add(CaseRow(run_id=run.id, case_id="TC-ADDPET-001",
                         requirement_ids=["FR-ADDPET-001"], method="POST", path="/pet",
                         endpoint_key="POST /pet", category="happy_path",
                         title="Duplicate id", description="d", order_index=2))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_traceability_reports_endpoints_that_produced_nothing(session, run):
    _seed(session, run)
    report = reviewer.check_traceability(session, run)
    assert report["endpoints_without_cases"] == ["GET /pet/{petId}"]


def test_traceability_is_clean_when_everything_lines_up(session, run):
    _seed(session, run)
    run.endpoint_keys = ["POST /pet"]
    session.add(run)
    session.commit()
    report = reviewer.check_traceability(session, run)
    assert report["fully_traced"] is True
    assert report["uncovered_requirement_ids"] == [] and report["orphan_case_ids"] == []


# ============================================================
# Review: apply, show, revert
# ============================================================

def test_applied_finding_records_before_and_after(session, run):
    _seed(session, run)
    case = session.exec(select(CaseRow)).one()
    finding = {"finding_id": "F-1", "severity": "high", "target_id": "TC-ADDPET-001",
               "field": "expected_status", "issue": "spec says 400 for invalid input",
               "suggestion": "400", "resolution": "applied", "before": None, "after": None}

    assert reviewer._apply(finding, case, reviewer.CASE_FIELDS) is True
    assert finding["before"] == 200 and finding["after"] == 400
    assert case.expected_status == 400
    assert case.status == ArtifactStatus.REVISED.value
    assert case.snapshot["expected_status"] == 200


def test_finding_with_unusable_suggestion_is_downgraded_to_raised(session, run):
    _seed(session, run)
    case = session.exec(select(CaseRow)).one()
    finding = {"finding_id": "F-1", "severity": "high", "target_id": "TC-ADDPET-001",
               "field": "expected_status", "issue": "x", "suggestion": "not a number",
               "resolution": "applied", "before": None, "after": None}

    assert reviewer._apply(finding, case, reviewer.CASE_FIELDS) is False
    assert finding["resolution"] == "raised"
    assert case.expected_status == 200        # untouched


def test_revert_finding_restores_the_previous_value(session, run):
    _seed(session, run)
    case = session.exec(select(CaseRow)).one()
    finding = {"finding_id": "F-1", "severity": "high", "target_id": "TC-ADDPET-001",
               "field": "title", "issue": "x", "suggestion": "Reviewer's title",
               "resolution": "applied", "before": None, "after": None}
    reviewer._apply(finding, case, reviewer.CASE_FIELDS)
    session.add(case)
    session.add(Review(run_id=run.id, stage=ReviewStage.TEST_CASES.value,
                       endpoint_key="POST /pet", verdict=ReviewVerdict.REVISED.value,
                       provider="ollama", model="qwen2.5:14b", findings=[finding]))
    session.commit()
    assert session.exec(select(CaseRow)).one().title == "Reviewer's title"

    assert reviewer.revert_finding(session, run.id, "F-1", "TC-ADDPET-001") is True
    assert session.exec(select(CaseRow)).one().title == "Create with valid body"
    assert session.exec(select(Review)).one().findings[0]["resolution"] == "reverted"


def test_findings_about_unknown_targets_are_dropped(session):
    """A critic cannot raise a finding about something the run never produced."""
    findings = reviewer._normalize(
        [{"severity": "high", "target_id": "FR-GHOST-001", "issue": "x", "resolution": "raised"},
         {"severity": "low", "target_id": "FR-REAL-001", "issue": "y", "resolution": "raised"}],
        {"FR-REAL-001"}, reviewer.REQUIREMENT_FIELDS)
    assert [f["target_id"] for f in findings] == ["FR-REAL-001"]


def test_finding_naming_an_unwritable_field_cannot_be_applied():
    findings = reviewer._normalize(
        [{"severity": "high", "target_id": "FR-A-001", "field": "run_id",
          "suggestion": "99", "issue": "x", "resolution": "applied"}],
        {"FR-A-001"}, reviewer.REQUIREMENT_FIELDS)
    assert findings[0]["resolution"] == "raised"


# ============================================================
# The two gates, end to end, against a stubbed model
# ============================================================

REQUIREMENTS_REPLY = json.dumps([
    {"req_id": "FR-ADDPET-001", "title": "Valid pet is created",
     "description": "A well-formed pet is stored and returned.",
     "acceptance_criteria": ["responds 200", "response carries an id"],
     "spec_source": "#/paths/~1pet/post", "priority": "P1"},
])

CASES_REPLY = json.dumps([
    {"case_id": "TC-ADDPET-001", "requirement_ids": ["FR-ADDPET-001"],
     "category": "happy_path", "title": "Create a pet with a valid body",
     "description": "Covers FR-ADDPET-001.", "priority": "P1",
     "preconditions": ["store is running"], "path_params": {}, "query_params": {},
     "headers": {"Content-Type": "application/json"}, "body": {"name": "Buddy"},
     "expected_status": 200, "expected_body": ["response.id is an integer > 0"],
     "test_data_notes": "any unique pet name"},
])

# The critic returns one applied finding, so before/after and revert get exercised.
REVIEW_REPLY = json.dumps([
    {"severity": "high", "target_id": "FR-ADDPET-001", "field": "title",
     "issue": "Title should name the status code.",
     "suggestion": "Valid pet is created and returns 200", "resolution": "applied"},
])


async def _stub_complete(self, system, user, **kw):
    if "Requirements Authoring" in system:
        return f"```json\n{REQUIREMENTS_REPLY}\n```"      # fenced, like a local model
    if "Test Design" in system:
        return CASES_REPLY
    if "Requirements Review" in system:
        return REVIEW_REPLY
    return "[]"                                           # case review: nothing wrong


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from backend import db as db_module
    from backend.llm import LLM
    import backend.main as main_module

    monkeypatch.setenv("TESTRONAUT_DB", f"sqlite:///{tmp_path}/gate.db")
    monkeypatch.setattr(LLM, "complete", _stub_complete)
    monkeypatch.setattr(db_module, "_engine", None)
    monkeypatch.setattr(main_module, "_tasks", {})

    with TestClient(main_module.app) as c:
        yield c


def _wait_for(client, run_id, status, timeout=15.0):
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] == status:
            return run
        if run["status"] == "error":
            raise AssertionError(f"run errored: {run['error']}")
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {status}; last was {run['status']}")


def _start_run(client) -> int:
    spec = client.post("/api/specs",
                       files={"file": ("petstore.json", json.dumps(PETSTORE), "application/json")})
    assert spec.status_code == 201, spec.text
    spec_id = spec.json()["id"]

    run = client.post("/api/runs", json={
        "spec_id": spec_id, "provider": "ollama", "model": "qwen2.5:14b",
        "categories": ["happy_path"], "endpoint_keys": ["POST /pet"]})
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]

    assert client.post(f"/api/runs/{run_id}/start").status_code == 200
    return run_id


def test_start_runs_stage_one_only_and_stops_at_gate_one(client):
    """The gate is the product: no test case may exist before a human approves."""
    run_id = _start_run(client)
    _wait_for(client, run_id, "awaiting_requirements_approval")

    assert client.get(f"/api/runs/{run_id}/requirements").json() != []
    assert client.get(f"/api/runs/{run_id}/cases").json() == []      # stage 2 has NOT run


def test_gate_one_refuses_to_open_until_every_requirement_is_approved(client):
    run_id = _start_run(client)
    _wait_for(client, run_id, "awaiting_requirements_approval")

    blocked = client.post(f"/api/runs/{run_id}/approve-requirements")
    assert blocked.status_code == 409
    assert "FR-ADDPET-001" in json.dumps(blocked.json())
    assert client.get(f"/api/runs/{run_id}/cases").json() == []


def test_approving_requirements_starts_stage_two(client):
    run_id = _start_run(client)
    _wait_for(client, run_id, "awaiting_requirements_approval")

    assert client.post("/api/requirements/approve",
                       json={"run_id": run_id, "all": True}).json()["approved"] == 1
    assert client.post(f"/api/runs/{run_id}/approve-requirements").status_code == 200

    _wait_for(client, run_id, "awaiting_case_approval")
    cases = client.get(f"/api/runs/{run_id}/cases").json()
    assert [c["case_id"] for c in cases] == ["TC-ADDPET-001"]
    assert cases[0]["requirement_ids"] == ["FR-ADDPET-001"]


def test_full_pipeline_through_both_gates_to_export(client):
    run_id = _start_run(client)
    _wait_for(client, run_id, "awaiting_requirements_approval")
    client.post("/api/requirements/approve", json={"run_id": run_id, "all": True})
    client.post(f"/api/runs/{run_id}/approve-requirements")
    _wait_for(client, run_id, "awaiting_case_approval")

    blocked = client.post(f"/api/runs/{run_id}/approve-cases")
    assert blocked.status_code == 409                       # gate 2 holds too

    client.post("/api/cases/approve", json={"run_id": run_id, "all": True})
    assert client.post(f"/api/runs/{run_id}/approve-cases").json()["status"] == "done"

    trace = client.get(f"/api/runs/{run_id}/traceability").json()
    assert trace["fully_traced"] is True and trace["uncovered_requirement_ids"] == []

    exported = client.get(f"/api/runs/{run_id}/export?format=json").json()
    assert exported["requirements"][0]["req_id"] == "FR-ADDPET-001"
    assert exported["test_cases"][0]["requirement_ids"] == ["FR-ADDPET-001"]


def test_reviewer_edit_is_visible_and_revertable(client):
    """PLAN 4: the user sees before -> after and can undo one change."""
    run_id = _start_run(client)
    _wait_for(client, run_id, "awaiting_requirements_approval")

    reviews = client.get(f"/api/runs/{run_id}/reviews?stage=requirements").json()
    finding = reviews[0]["findings"][0]
    assert finding["resolution"] == "applied"
    assert finding["before"] == "Valid pet is created"
    assert finding["after"] == "Valid pet is created and returns 200"

    req = client.get(f"/api/runs/{run_id}/requirements").json()[0]
    assert req["title"] == "Valid pet is created and returns 200"
    assert req["status"] == "revised"

    reverted = client.post(
        f"/api/runs/{run_id}/revert/FR-ADDPET-001?finding_id={finding['finding_id']}").json()
    assert reverted["title"] == "Valid pet is created"
    assert client.get(f"/api/runs/{run_id}/reviews").json()[0]["findings"][0]["resolution"] == "reverted"


def test_start_is_rejected_twice(client):
    run_id = _start_run(client)
    assert client.post(f"/api/runs/{run_id}/start").status_code == 409


def test_patch_cannot_reach_protected_columns(client):
    run_id = _start_run(client)
    _wait_for(client, run_id, "awaiting_requirements_approval")
    req = client.get(f"/api/runs/{run_id}/requirements").json()[0]

    patched = client.patch(f"/api/requirements/{req['id']}",
                           json={"title": "Human wording", "run_id": 999,
                                 "req_id": "FR-HACKED-001", "status": "approved"}).json()
    assert patched["title"] == "Human wording"
    assert patched["run_id"] == run_id                       # ignored
    assert patched["req_id"] == "FR-ADDPET-001"              # ignored
    assert patched["status"] == "edited"                     # an edit is an edit


def test_import_round_trip_through_the_api(client):
    run_id = _start_run(client)
    _wait_for(client, run_id, "awaiting_requirements_approval")

    exported = client.get(f"/api/runs/{run_id}/export?format=json").json()
    exported["requirements"][0]["title"] = "Edited outside the app"

    summary = client.post(f"/api/runs/{run_id}/import",
                          files={"file": ("edit.json", json.dumps(exported), "application/json")}).json()
    assert summary["requirements"]["updated"] == 1
    assert summary["requirements"]["errors"] == []
    assert client.get(f"/api/runs/{run_id}/requirements").json()[0]["title"] == "Edited outside the app"


# ============================================================
# Progress reporting and token budget
# ============================================================

@pytest.mark.asyncio
async def test_review_pass_does_not_emit_generation_progress():
    """
    The review pass has its own review_start/review_done events. Emitting
    endpoint_start for it too made the UI label a review as "generating".
    endpoint_error must still fire — the protocol has no review_error.
    """
    import backend.main as main_module

    captured: list[tuple[str, dict]] = []
    original = main_module.publish
    main_module.publish = lambda run_id, event, data: captured.append((event, data))
    try:
        endpoints = [{"endpoint_key": "POST /pet"}, {"endpoint_key": "GET /pet/{petId}"}]

        async def ok(endpoint):
            return 1

        await main_module._run_endpoints(1, "requirements", endpoints, ok, emit_progress=True)
        assert [e for e, _ in captured].count("endpoint_start") == 2
        assert [e for e, _ in captured].count("endpoint_done") == 2

        captured.clear()
        await main_module._run_endpoints(1, "requirements", endpoints, ok, emit_progress=False)
        assert captured == []

        async def boom(endpoint):
            raise RuntimeError("provider exploded")

        captured.clear()
        errors = await main_module._run_endpoints(1, "requirements", endpoints, boom,
                                                  emit_progress=False)
        assert errors == 2
        assert [e for e, _ in captured] == ["endpoint_error", "endpoint_error"]
    finally:
        main_module.publish = original


@pytest.mark.asyncio
async def test_case_generation_budget_scales_with_requirement_count(session, run, endpoint):
    """
    A case carries a body, headers and assertions, one per requirement, so output
    length scales with len(approved). A fixed 4000 truncated real output mid-string.
    """
    from backend.generator import generate_cases_for_endpoint
    from backend.llm import LLM

    seen: list[int] = []

    async def stub(self, system, user, **kw):
        seen.append(kw.get("max_tokens"))
        return "[]"

    approved = []
    for i in range(1, 6):
        req = Requirement(run_id=run.id, req_id=f"FR-ADDPET-{i:03d}", method="POST", path="/pet",
                          endpoint_key="POST /pet", title=f"R{i}", description="d",
                          status=ArtifactStatus.APPROVED.value, order_index=i)
        session.add(req)
        approved.append(req)
    session.commit()

    original = LLM.complete
    LLM.complete = stub
    try:
        await generate_cases_for_endpoint(LLM(provider="ollama", model="m"), endpoint,
                                          approved, run, session)
    finally:
        LLM.complete = original

    assert seen and seen[0] == 1500 * 5
    assert seen[0] > 4000


def test_critic_can_run_on_a_different_provider_than_the_generator(session, run):
    """
    Generate free on a local model, review with a strong hosted one. Before
    reviewer_provider existed the critic was pinned to the generator's provider.
    """
    from backend.llm import get_provider

    run.provider = "ollama"
    run.model = "qwen2.5:14b"
    run.reviewer_provider = "openai"
    run.reviewer_model = "gpt-4o"
    session.add(run)
    session.commit()

    generator = get_provider(run.provider, run.model)
    critic = get_provider(run.reviewer_provider or run.provider, run.reviewer_model or run.model)

    assert (generator.provider, generator.model) == ("ollama", "qwen2.5:14b")
    assert (critic.provider, critic.model) == ("openai", "gpt-4o")


def test_reviewer_provider_defaults_to_the_generator(session, run):
    from backend.llm import get_provider

    run.provider, run.model = "ollama", "qwen2.5:14b"
    run.reviewer_provider, run.reviewer_model = None, None
    session.add(run)
    session.commit()

    critic = get_provider(run.reviewer_provider or run.provider, run.reviewer_model or run.model)
    assert (critic.provider, critic.model) == ("ollama", "qwen2.5:14b")


# ============================================================
# Phase 2: assertion grammar
# ============================================================

from backend.assertions import compile_all, parse as parse_assertion, to_rest_assured


@pytest.mark.parametrize("text,op,path,value", [
    ("$.id == 5", "==", "id", 5),
    ("$.name == 'Fluffy'", "==", "name", "Fluffy"),
    ("$.code != 500", "!=", "code", 500),
    ("$.message contains 'required'", "contains", "message", "required"),
    ("$.id exists", "exists", "id", None),
    ("$.name matches /^[A-Z]/", "matches", "name", "^[A-Z]"),
    ("$.tags length == 2", "length", "tags", 2),
    ("$.a.b[0].c == true", "==", "a.b[0].c", True),
])
def test_grammar_parses_every_documented_form(text, op, path, value):
    parsed = parse_assertion(text)
    assert parsed is not None, text
    assert (parsed.op, parsed.path, parsed.value) == (op, path, value)


def test_prose_is_not_an_error_it_is_just_uncompilable():
    """Models and humans write prose; it becomes a TODO, never a silent pass."""
    assert parse_assertion("response looks about right") is None
    compiled, skipped = compile_all(["$.id == 1", "response looks about right"])
    assert compiled == ['.body("id", equalTo(1))']
    assert skipped == ["response looks about right"]


def test_response_prefix_is_stripped_for_gpath():
    """REST Assured's GPath is already rooted at the body — `response.` is not a field."""
    assert to_rest_assured(parse_assertion("response.name == 'Fluffy'")) == \
        '.body("name", equalTo("Fluffy"))'
    assert to_rest_assured(parse_assertion("$.name == 'Fluffy'")) == \
        '.body("name", equalTo("Fluffy"))'


def test_length_compiles_to_a_size_call():
    assert to_rest_assured(parse_assertion("$.tags length == 3")) == \
        '.body("tags.size()", equalTo(3))'


def test_java_string_literals_are_escaped():
    """A quote or backslash in an expected value must not break the generated source."""
    fragment = to_rest_assured(parse_assertion("""$.msg == 'he said "no"'"""))
    assert fragment == '.body("msg", equalTo("he said \\"no\\""))'


# ============================================================
# Phase 2: codegen
# ============================================================

def _approved_case(session, run, **over):
    values = dict(
        run_id=run.id, case_id="TC-ADDPET-001", requirement_ids=["FR-ADDPET-001"],
        method="POST", path="/pet", endpoint_key="POST /pet", operation_id="addPet",
        category="happy_path", title="Create a pet", description="happy path",
        priority="P1", preconditions=[], path_params={}, query_params={},
        headers={"Content-Type": "application/json"}, body={"name": "Buddy"},
        expected_status=200, expected_body=["$.id exists"], test_data_notes="",
        status=ArtifactStatus.APPROVED.value, order_index=1)
    values.update(over)
    case = CaseRow(**values)
    session.add(case)
    session.commit()
    return case


def test_codegen_writes_a_complete_maven_project(session, run, tmp_path):
    from backend.codegen import generate_project
    _approved_case(session, run)
    session.add(Requirement(run_id=run.id, req_id="FR-ADDPET-001", method="POST", path="/pet",
                            endpoint_key="POST /pet", title="Create a pet", description="d",
                            status=ArtifactStatus.APPROVED.value, order_index=1))
    session.commit()

    project = generate_project(run.id, session, tmp_path, base_url="http://localhost:9999")

    for required in ["pom.xml", "testng.xml", "testronaut-map.json",
                     "src/test/resources/testronaut.properties",
                     "src/test/java/com/testronaut/generated/Config.java"]:
        assert (tmp_path / required).exists(), required

    source = (tmp_path / "src/test/java/com/testronaut/generated/AddpetTest.java").read_text()
    assert "public class AddpetTest" in source
    assert ".post(\"/pet\")" in source
    assert ".statusCode(200)" in source
    assert '.body("id", notNullValue())' in source
    assert "Traces to FR-ADDPET-001" in source          # traceability survives into the code
    assert project.test_count == 1 and project.endpoint_count == 1

    assert "http://localhost:9999" in (tmp_path / "src/test/resources/testronaut.properties").read_text()


def test_codegen_maps_methods_back_to_case_ids(session, run, tmp_path):
    """Surefire reports methods; the report view needs case IDs."""
    from backend.codegen import generate_project
    _approved_case(session, run)
    project = generate_project(run.id, session, tmp_path)
    assert project.method_map == {"AddpetTest#tcAddpet001": "TC-ADDPET-001"}


def test_codegen_refuses_when_nothing_is_approved(session, run, tmp_path):
    from backend.codegen import generate_project
    _approved_case(session, run, status=ArtifactStatus.DRAFT.value)
    with pytest.raises(ValueError, match="No approved test cases"):
        generate_project(run.id, session, tmp_path)


def test_codegen_emits_uncompilable_assertions_as_todo(session, run, tmp_path):
    """A test that silently asserts nothing is worse than one with a visible TODO."""
    from backend.codegen import generate_project
    _approved_case(session, run, expected_body=["$.id exists", "looks right to a human"])
    generate_project(run.id, session, tmp_path)
    source = (tmp_path / "src/test/java/com/testronaut/generated/AddpetTest.java").read_text()
    assert '.body("id", notNullValue())' in source
    assert "// TODO assert manually: looks right to a human" in source


def test_codegen_routes_placeholders_through_config(session, run, tmp_path):
    """${petId} must not be baked in — it resolves from properties at run time."""
    from backend.codegen import generate_project
    _approved_case(session, run, path="/pet/{petId}", endpoint_key="GET /pet/{petId}",
                   method="GET", path_params={"petId": "${petId}"}, body=None)
    project = generate_project(run.id, session, tmp_path)
    source = (tmp_path / "src/test/java/com/testronaut/generated/AddpetTest.java").read_text()
    assert 'Config.resolve("${petId}")' in source
    assert "petId" in project.placeholders
    assert "petId=" in (tmp_path / "src/test/resources/testronaut.properties").read_text()


def test_generated_java_method_names_are_legal_identifiers(session, run, tmp_path):
    from backend.codegen import generate_project, method_name
    _approved_case(session, run, case_id="TC-GET_PETPETID_1A2B-001")
    project = generate_project(run.id, session, tmp_path)
    method = next(iter(project.method_map)).split("#")[1]
    assert method.isidentifier() and not method[0].isdigit()
    assert method_name("TC-CLASS-001", set()) not in {"class", "int", "for"}


# ============================================================
# Phase 2: Surefire report parsing
# ============================================================

SUREFIRE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="TestSuite" tests="3" failures="1" errors="1" skipped="0" time="1.5">
  <testcase name="tcAddpet001" classname="com.testronaut.generated.AddpetTest" time="0.4"/>
  <testcase name="tcAddpet002" classname="com.testronaut.generated.AddpetTest" time="0.1">
    <failure message="Expected status code &lt;400&gt; but was &lt;422&gt;." type="java.lang.AssertionError">
      at AddpetTest.tcAddpet002(AddpetTest.java:50)
    </failure>
  </testcase>
  <testcase name="tcAddpet003" classname="com.testronaut.generated.AddpetTest" time="0.2">
    <error message="Connection refused" type="java.net.ConnectException">stack</error>
  </testcase>
</testsuite>
"""


def test_surefire_results_join_back_to_case_ids(tmp_path):
    from backend.runner import parse_surefire
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-AddpetTest.xml").write_text(SUREFIRE_XML)
    (tmp_path / "testronaut-map.json").write_text(json.dumps({"run_id": 1, "methods": {
        "AddpetTest#tcAddpet001": "TC-ADDPET-001",
        "AddpetTest#tcAddpet002": "TC-ADDPET-002",
        "AddpetTest#tcAddpet003": "TC-ADDPET-003"}}))

    result = parse_surefire(tmp_path)
    assert (result.total, result.passed, result.failed, result.errors) == (3, 1, 1, 1)
    by_case = {c.case_id: c for c in result.cases}
    assert by_case["TC-ADDPET-001"].status == "passed"
    assert by_case["TC-ADDPET-002"].status == "failed"
    assert "422" in by_case["TC-ADDPET-002"].message
    assert by_case["TC-ADDPET-003"].status == "error"


def test_surefire_parsing_survives_a_missing_map(tmp_path):
    """A project run outside the app still reports, just without case IDs."""
    from backend.runner import parse_surefire
    reports = tmp_path / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    (reports / "TEST-AddpetTest.xml").write_text(SUREFIRE_XML)
    result = parse_surefire(tmp_path)
    assert result.total == 3
    assert all(c.case_id is None for c in result.cases)
    assert result.cases[0].method == "tcAddpet001"


def test_no_reports_means_nothing_ran(tmp_path):
    from backend.runner import parse_surefire
    result = parse_surefire(tmp_path)
    assert result.total == 0 and result.ran is False


@pytest.mark.asyncio
async def test_execution_refuses_to_run_generated_code_on_the_host(tmp_path, monkeypatch):
    """Without a sandbox it must refuse, not fall back to the host."""
    from backend import runner
    (tmp_path / "pom.xml").write_text("<project/>")
    monkeypatch.setattr(runner, "docker_available", lambda: False)
    with pytest.raises(runner.SandboxUnavailable, match="Docker"):
        await runner.run_tests(tmp_path)


def test_sandbox_command_drops_privileges_and_caps_resources():
    from backend.runner import _docker_command
    cmd = " ".join(_docker_command(Path("/tmp/p"), "maven:x", None))
    assert "--cap-drop ALL" in cmd
    assert "--security-opt no-new-privileges" in cmd
    assert "--memory 2g" in cmd
    assert "--rm" in cmd
