"""
SQLModel models, engine, and session for Testronaut.

Tables: spec, generation_run, requirement, test_case, review.

JSON-bearing columns use a real JSON column (SQLite has had JSON1 since 3.9),
not TEXT holding a JSON string, so callers work with lists/dicts directly and
no layer has to remember to dumps/loads at the boundary.
"""

import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterator, Optional

from sqlalchemy.engine import Engine
from sqlmodel import JSON, Column, Field, Relationship, Session, SQLModel, UniqueConstraint, create_engine


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_field(default_factory):
    """A JSON column with a safe mutable default."""
    return Field(default_factory=default_factory, sa_column=Column(JSON))


def _ts_field(onupdate: bool = False) -> Any:
    kwargs = {"onupdate": utcnow} if onupdate else {}
    return Field(default_factory=utcnow, sa_column_kwargs=kwargs)


# ---------- Enums ----------

class RunStatus(str, Enum):
    CREATED = "created"
    GENERATING_REQUIREMENTS = "generating_requirements"
    REVIEWING_REQUIREMENTS = "reviewing_requirements"
    AWAITING_REQUIREMENTS_APPROVAL = "awaiting_requirements_approval"
    GENERATING_CASES = "generating_cases"
    REVIEWING_CASES = "reviewing_cases"
    AWAITING_CASE_APPROVAL = "awaiting_case_approval"
    DONE = "done"
    ERROR = "error"


class ArtifactStatus(str, Enum):
    DRAFT = "draft"
    REVISED = "revised"
    EDITED = "edited"
    APPROVED = "approved"


class Category(str, Enum):
    HAPPY_PATH = "happy_path"
    NEGATIVE = "negative"
    BOUNDARY = "boundary"
    AUTH = "auth"
    SECURITY = "security"


class Provider(str, Enum):
    CLAUDE_AGENT_SDK = "claude_agent_sdk"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OLLAMA = "ollama"
    OMNIROUTE = "omniroute"


class ArtifactOrigin(str, Enum):
    GENERATED = "generated"
    MANUAL = "manual"
    IMPORTED = "imported"


class ReviewStage(str, Enum):
    REQUIREMENTS = "requirements"
    TEST_CASES = "test_cases"


class ReviewVerdict(str, Enum):
    PASS = "pass"
    REVISED = "revised"


class Priority(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Resolution(str, Enum):
    APPLIED = "applied"
    RAISED = "raised"
    REVERTED = "reverted"


# ---------- Models ----------

class Spec(SQLModel, table=True):
    __tablename__ = "spec"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    version: str
    source: str                 # "file" | "url"
    source_ref: str             # original filename or URL
    raw: str                    # verbatim spec text
    format: str                 # "json" | "yaml"
    endpoint_count: int = 0
    created_at: datetime = _ts_field()

    runs: list["GenerationRun"] = Relationship(back_populates="spec")


class GenerationRun(SQLModel, table=True):
    __tablename__ = "generation_run"

    id: Optional[int] = Field(default=None, primary_key=True)
    spec_id: int = Field(foreign_key="spec.id")
    provider: str
    model: str
    # A stronger critic than the generator is a legitimate choice (LLD 2.2), and
    # the most useful pairing is cross-provider: generate free on a local model,
    # review with a strong hosted one. Null means "same as the generator".
    reviewer_provider: Optional[str] = None
    reviewer_model: Optional[str] = None
    categories: list[str] = _json_field(list)
    endpoint_keys: list[str] = _json_field(list)   # "METHOD PATH" selected for this run
    status: str = RunStatus.CREATED.value
    error: Optional[str] = None                    # fatal, run-level failures only
    created_at: datetime = _ts_field()
    finished_at: Optional[datetime] = None
    # Phase 2: the most recent sandbox run of the generated project.
    last_execution: Optional[dict] = Field(default=None, sa_column=Column(JSON))

    spec: Spec = Relationship(back_populates="runs")
    requirements: list["Requirement"] = Relationship(
        back_populates="run", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    test_cases: list["TestCase"] = Relationship(
        back_populates="run", sa_relationship_kwargs={"cascade": "all, delete-orphan"})
    reviews: list["Review"] = Relationship(
        back_populates="run", sa_relationship_kwargs={"cascade": "all, delete-orphan"})


class Requirement(SQLModel, table=True):
    __tablename__ = "requirement"
    # Composite, not global: regenerating the same endpoint in a later run
    # legitimately produces FR-ADDPET-001 again.
    __table_args__ = (UniqueConstraint("run_id", "req_id", name="uq_run_req_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    req_id: str                                    # business key, e.g. FR-ADDPET-003
    run_id: int = Field(foreign_key="generation_run.id", index=True)
    method: str
    path: str
    operation_id: Optional[str] = None
    endpoint_key: str = ""
    title: str
    description: str
    acceptance_criteria: list[str] = _json_field(list)
    spec_source: Optional[str] = None              # JSON pointer into the spec
    priority: str = Priority.P2.value
    origin: str = ArtifactOrigin.GENERATED.value
    status: str = ArtifactStatus.DRAFT.value
    snapshot: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    order_index: int = 0
    created_at: datetime = _ts_field()
    updated_at: datetime = _ts_field(onupdate=True)

    run: GenerationRun = Relationship(back_populates="requirements")


class TestCase(SQLModel, table=True):
    __tablename__ = "test_case"
    __table_args__ = (UniqueConstraint("run_id", "case_id", name="uq_run_case_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    case_id: str                                   # business key, e.g. TC-ADDPET-003
    run_id: int = Field(foreign_key="generation_run.id", index=True)
    requirement_ids: list[str] = _json_field(list)  # traceability link
    method: str
    path: str
    operation_id: Optional[str] = None
    endpoint_key: str = ""
    category: str
    title: str
    description: str
    priority: str = Priority.P2.value
    preconditions: list[str] = _json_field(list)
    path_params: dict = _json_field(dict)
    query_params: dict = _json_field(dict)
    headers: dict = _json_field(dict)
    body: Optional[Any] = Field(default=None, sa_column=Column(JSON))
    expected_status: int = 200
    expected_body: list[str] = _json_field(list)   # assertion strings
    test_data_notes: str = ""
    status: str = ArtifactStatus.DRAFT.value
    origin: str = ArtifactOrigin.GENERATED.value
    snapshot: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    order_index: int = 0
    created_at: datetime = _ts_field()
    updated_at: datetime = _ts_field(onupdate=True)

    run: GenerationRun = Relationship(back_populates="test_cases")


class Review(SQLModel, table=True):
    """One row per critic call — one per endpoint per stage, not per finding."""
    __tablename__ = "review"

    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="generation_run.id", index=True)
    stage: str                                     # ReviewStage
    endpoint_key: str                              # "METHOD PATH" this review covers
    verdict: str                                   # ReviewVerdict
    provider: str
    model: str
    findings: list[dict] = _json_field(list)       # see Finding shape in reviewer.py
    created_at: datetime = _ts_field()

    run: GenerationRun = Relationship(back_populates="reviews")


# ---------- Engine & session ----------

_engine: Optional[Engine] = None


def init_db(database_url: Optional[str] = None) -> Engine:
    """Create the engine and tables. Called on app startup."""
    global _engine
    if database_url is None:
        database_url = os.getenv("TESTRONAUT_DB", "sqlite:///./testronaut.db")
    # check_same_thread=False: FastAPI runs sync endpoints in a threadpool.
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_engine(database_url, echo=False, connect_args=connect_args)
    SQLModel.metadata.create_all(_engine)
    return _engine


def get_engine() -> Engine:
    return _engine if _engine is not None else init_db()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(get_engine()) as session:
        yield session
