"""
FastAPI app: routes, the two approval gates, and the SSE progress stream.

The gates are the point of v1. `POST /runs/{id}/start` runs **stage 1 only** and
stops at `awaiting_requirements_approval`. Stage 2 begins when the user approves
requirements, never automatically. Nothing here auto-approves anything.
"""

import asyncio
import copy
import json
import os
import shutil
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Optional

import structlog
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field as PydField
from sqlmodel import Session, select

from .agent import require_sdk
from .codegen import generate_project
from .netguard import UnsafeURL, fetch_spec_url, validate_docker_network
from .runner import SandboxUnavailable, docker_available, parse_surefire, run_tests
from .db import (
    ArtifactOrigin, ArtifactStatus, Category, GenerationRun, Provider, Requirement,
    Review, ReviewStage, RunStatus, Spec, TestCase, get_engine, get_session, init_db, utcnow,
)
from .exporter import from_json, from_xlsx, to_json, to_xlsx
from .generator import generate_cases_for_endpoint, generate_requirements_for_endpoint
from .llm import LLMError, close_http_client, get_provider, probe_all_providers
from .reviewer import check_traceability, revert_finding, review_cases, review_requirements
from .spec_parser import SpecParseError, parse_spec

logger = structlog.get_logger()

PROVIDER_LABELS = {
    "ollama": "Local (Ollama)",
    "openai": "OpenAI",
    "anthropic": "Anthropic API",
    "claude_agent_sdk": "Claude Agent SDK",
    "omniroute": "OmniRoute (gateway)",
}


# A hosted API is limited by rate limits; a local model is limited by RAM. Each
# concurrent Ollama request needs its own KV cache for the full context window on
# top of the weights, so the safe default there is one at a time.
DEFAULT_CONCURRENCY = {"ollama": 1, "openai": 4, "anthropic": 4, "claude_agent_sdk": 2}


def max_concurrency(provider: str = "") -> int:
    """TESTRONAUT_MAX_CONCURRENCY wins; otherwise pick by what limits the provider."""
    override = os.getenv("TESTRONAUT_MAX_CONCURRENCY")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            logger.warning("bad_concurrency_env", value=override)
    return DEFAULT_CONCURRENCY.get(provider, 4)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield
    for task in list(_tasks.values()):
        task.cancel()
    await close_http_client()


app = FastAPI(title="Testronaut", lifespan=lifespan)

# Vite dev server is a separate origin unless you use its proxy; allow both.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- SSE plumbing ----------

_subscribers: dict[int, set[asyncio.Queue]] = {}
_tasks: dict[int, asyncio.Task] = {}          # keeps pipeline tasks from being GC'd


def publish(run_id: int, event: str, data: dict) -> None:
    for queue in _subscribers.get(run_id, set()):
        queue.put_nowait((event, data))


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---------- request bodies ----------

class SpecFromUrl(BaseModel):
    url: str


class RunCreate(BaseModel):
    spec_id: int
    provider: Provider
    model: str
    reviewer_provider: Optional[Provider] = None
    reviewer_model: Optional[str] = None
    categories: list[Category] = PydField(min_length=1)
    endpoint_keys: list[str] = []


class RequirementUpdate(BaseModel):
    """Explicit allowlist: a PATCH must never reach run_id, req_id, status or timestamps."""
    title: Optional[str] = None
    description: Optional[str] = None
    acceptance_criteria: Optional[list[str]] = None
    spec_source: Optional[str] = None
    priority: Optional[str] = None


class RequirementCreate(BaseModel):
    run_id: int
    req_id: Optional[str] = None
    method: str
    path: str
    operation_id: Optional[str] = None
    title: str
    description: str = ""
    acceptance_criteria: list[str] = []
    priority: str = "P2"


class TestCaseUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    requirement_ids: Optional[list[str]] = None
    category: Optional[Category] = None
    priority: Optional[str] = None
    preconditions: Optional[list[str]] = None
    path_params: Optional[dict] = None
    query_params: Optional[dict] = None
    headers: Optional[dict] = None
    body: Optional[Any] = None
    expected_status: Optional[int] = None
    expected_body: Optional[list[str]] = None
    test_data_notes: Optional[str] = None


class TestCaseCreate(BaseModel):
    run_id: int
    case_id: Optional[str] = None
    method: str
    path: str
    operation_id: Optional[str] = None
    requirement_ids: list[str] = []
    category: Category = Category.HAPPY_PATH
    title: str
    description: str = ""
    priority: str = "P2"
    expected_status: int = 200


class ApproveRequest(BaseModel):
    requirement_ids: Optional[list[str]] = None
    case_ids: Optional[list[str]] = None
    run_id: Optional[int] = None
    all: bool = False


class RegenerateRequest(BaseModel):
    endpoint_key: str
    stage: ReviewStage


class CodegenRequest(BaseModel):
    base_url: str = "http://host.docker.internal:8080"
    test_data: dict[str, str] = {}
    only_approved: bool = True


class ExecuteRequest(BaseModel):
    network: Optional[str] = None
    timeout: float = 900.0


class FileUpdate(BaseModel):
    content: str


# ---------- helpers ----------

def workspace(run_id: int) -> Path:
    """Where a run's generated project lives. Gitignored; safe to delete."""
    root = Path(os.getenv("TESTRONAUT_WORKSPACE", "./workspace")).resolve()
    return root / f"run-{run_id}"


def _run_or_404(session: Session, run_id: int) -> GenerationRun:
    run = session.get(GenerationRun, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


def _endpoints_for_run(session: Session, run: GenerationRun) -> list[dict]:
    spec = session.get(Spec, run.spec_id)
    if not spec:
        raise HTTPException(404, "Spec not found")
    _, endpoints = parse_spec(spec.raw, spec.format)
    if run.endpoint_keys:
        wanted = set(run.endpoint_keys)
        endpoints = [e for e in endpoints if e["endpoint_key"] in wanted]
    return endpoints


# ---------- specs ----------

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


async def _read_upload(file: UploadFile, limit: int) -> bytes:
    """Read an upload, refusing anything over `limit` rather than buffering it all."""
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"File is larger than {limit // (1024 * 1024)} MB.")
    return data


@app.post("/api/specs", response_model=Spec, status_code=201)
async def create_spec(file: Optional[UploadFile] = File(None),
                      url: Optional[str] = None,
                      session: Session = Depends(get_session)):
    """Upload by file (multipart) or by URL (?url=... or a JSON body)."""
    if file is not None:
        raw = (await _read_upload(file, MAX_UPLOAD_BYTES)).decode("utf-8", errors="replace")
        source, source_ref, name_hint = "file", file.filename or "upload", file.filename or ""
    elif url:
        try:
            raw = await fetch_spec_url(url)
        except UnsafeURL as e:
            raise HTTPException(400, str(e))
        except Exception as e:
            raise HTTPException(400, f"Could not fetch {url}: {e}")
        source, source_ref, name_hint = "url", url, url
    else:
        raise HTTPException(400, "Provide a multipart `file` or a `url`.")

    fmt = "yaml" if name_hint.lower().endswith((".yaml", ".yml")) else (
        "json" if name_hint.lower().endswith(".json") else
        ("json" if raw.lstrip().startswith("{") else "yaml"))

    try:
        meta, endpoints = parse_spec(raw, fmt)
    except SpecParseError as e:
        raise HTTPException(400, str(e))

    spec = Spec(name=meta.name, version=meta.version, source=source, source_ref=source_ref,
                raw=raw, format=fmt, endpoint_count=len(endpoints))
    session.add(spec)
    session.commit()
    session.refresh(spec)
    return spec


@app.post("/api/specs/from-url", response_model=Spec, status_code=201)
async def create_spec_from_url(body: SpecFromUrl, session: Session = Depends(get_session)):
    return await create_spec(file=None, url=body.url, session=session)


@app.get("/api/specs", response_model=list[Spec])
async def list_specs(session: Session = Depends(get_session)):
    return session.exec(select(Spec).order_by(Spec.created_at.desc())).all()


@app.get("/api/specs/{spec_id}", response_model=Spec)
async def get_spec(spec_id: int, session: Session = Depends(get_session)):
    spec = session.get(Spec, spec_id)
    if not spec:
        raise HTTPException(404, "Spec not found")
    return spec


@app.delete("/api/specs/{spec_id}", status_code=204)
async def delete_spec(spec_id: int, session: Session = Depends(get_session)):
    spec = session.get(Spec, spec_id)
    if not spec:
        raise HTTPException(404, "Spec not found")
    session.delete(spec)
    session.commit()


@app.get("/api/specs/{spec_id}/endpoints")
async def list_endpoints(spec_id: int, session: Session = Depends(get_session)):
    spec = session.get(Spec, spec_id)
    if not spec:
        raise HTTPException(404, "Spec not found")
    try:
        _, endpoints = parse_spec(spec.raw, spec.format)
    except SpecParseError as e:
        raise HTTPException(400, str(e))
    return [{"key": e["endpoint_key"], "method": e["method"], "path": e["path"],
             "operation_id": e["operation_id"], "summary": e["summary"],
             "parameters": e["parameters"], "request_body_schema": e["request_body"],
             "responses": e["responses"], "security": e["security"]} for e in endpoints]


# ---------- providers ----------

@app.get("/api/providers")
async def list_providers():
    infos = {p.name: p for p in await probe_all_providers()}
    order = ["ollama", "openai", "anthropic", "claude_agent_sdk", "omniroute"]   # default provider first
    return [{"id": name, "label": PROVIDER_LABELS[name],
             "available": infos[name].available, "reason": infos[name].error,
             "models": infos[name].models} for name in order if name in infos]


# ---------- runs ----------

@app.post("/api/runs", response_model=GenerationRun, status_code=201)
async def create_run(data: RunCreate, session: Session = Depends(get_session)):
    if not session.get(Spec, data.spec_id):
        raise HTTPException(404, "Spec not found")
    try:
        get_provider(data.provider.value, data.model)      # fail here, not mid-run
        if data.provider is Provider.CLAUDE_AGENT_SDK:
            require_sdk()
        if data.reviewer_provider and data.reviewer_provider is not data.provider:
            get_provider(data.reviewer_provider.value, data.reviewer_model or data.model)
            if data.reviewer_provider is Provider.CLAUDE_AGENT_SDK:
                require_sdk()
    except (LLMError, RuntimeError) as e:
        raise HTTPException(400, str(e))

    run = GenerationRun(
        spec_id=data.spec_id, provider=data.provider.value, model=data.model,
        reviewer_provider=data.reviewer_provider.value if data.reviewer_provider else None,
        reviewer_model=data.reviewer_model,
        categories=[c.value for c in data.categories],
        endpoint_keys=data.endpoint_keys, status=RunStatus.CREATED.value)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


@app.get("/api/runs", response_model=list[GenerationRun])
async def list_runs(spec_id: Optional[int] = None, session: Session = Depends(get_session)):
    query = select(GenerationRun).order_by(GenerationRun.created_at.desc())
    if spec_id:
        query = query.where(GenerationRun.spec_id == spec_id)
    return session.exec(query).all()


@app.get("/api/runs/{run_id}", response_model=GenerationRun)
async def get_run(run_id: int, session: Session = Depends(get_session)):
    return _run_or_404(session, run_id)


@app.post("/api/runs/{run_id}/start", response_model=GenerationRun)
async def start_run(run_id: int, session: Session = Depends(get_session)):
    """Kicks off stage 1 only. Stage 2 waits for approve-requirements."""
    run = _run_or_404(session, run_id)
    if run.status != RunStatus.CREATED.value:
        raise HTTPException(409, f"Run is {run.status}; only a `created` run can be started.")
    if run_id in _tasks and not _tasks[run_id].done():
        raise HTTPException(409, "Run is already in progress.")

    run.status = RunStatus.GENERATING_REQUIREMENTS.value
    session.add(run)
    session.commit()
    session.refresh(run)
    _spawn(run_id, _stage1(run_id))
    return run


@app.post("/api/runs/{run_id}/approve-requirements", response_model=GenerationRun)
async def approve_requirements_gate(run_id: int, session: Session = Depends(get_session)):
    """Gate 1. Requires every requirement approved; starts stage 2."""
    run = _run_or_404(session, run_id)
    if run.status != RunStatus.AWAITING_REQUIREMENTS_APPROVAL.value:
        raise HTTPException(409, f"Run is {run.status}; gate 1 is not open.")

    reqs = session.exec(select(Requirement).where(Requirement.run_id == run_id)).all()
    if not reqs:
        raise HTTPException(409, "No requirements to approve.")
    pending = [r.req_id for r in reqs if r.status != ArtifactStatus.APPROVED.value]
    if pending:
        raise HTTPException(409, {"message": "Every requirement must be approved first.",
                                  "pending": pending})

    run.status = RunStatus.GENERATING_CASES.value
    session.add(run)
    session.commit()
    session.refresh(run)
    _spawn(run_id, _stage2(run_id))
    return run


@app.post("/api/runs/{run_id}/approve-cases", response_model=GenerationRun)
async def approve_cases_gate(run_id: int, session: Session = Depends(get_session)):
    """Gate 2. Requires every case approved; completes the run."""
    run = _run_or_404(session, run_id)
    if run.status != RunStatus.AWAITING_CASE_APPROVAL.value:
        raise HTTPException(409, f"Run is {run.status}; gate 2 is not open.")

    cases = session.exec(select(TestCase).where(TestCase.run_id == run_id)).all()
    if not cases:
        raise HTTPException(409, "No test cases to approve.")
    pending = [c.case_id for c in cases if c.status != ArtifactStatus.APPROVED.value]
    if pending:
        raise HTTPException(409, {"message": "Every test case must be approved first.",
                                  "pending": pending})

    run.status = RunStatus.DONE.value
    run.finished_at = utcnow()
    session.add(run)
    session.commit()
    session.refresh(run)
    publish(run_id, "run_done", {
        "run_id": run_id,
        "total_requirements": len(session.exec(
            select(Requirement).where(Requirement.run_id == run_id)).all()),
        "total_cases": len(cases)})
    return run


@app.post("/api/runs/{run_id}/regenerate")
async def regenerate(run_id: int, body: RegenerateRequest, session: Session = Depends(get_session)):
    """Re-run one endpoint through one stage, replacing what it produced."""
    run = _run_or_404(session, run_id)
    endpoints = {e["endpoint_key"]: e for e in _endpoints_for_run(session, run)}
    endpoint = endpoints.get(body.endpoint_key)
    if not endpoint:
        raise HTTPException(404, f"{body.endpoint_key} is not in this run")

    llm = get_provider(run.provider, run.model)
    try:
        return await _regenerate(run, endpoint, body.stage, session, llm)
    except LLMError as e:
        raise HTTPException(502, str(e))


async def _regenerate(run: GenerationRun, endpoint: dict, stage: ReviewStage,
                      session: Session, llm) -> dict:
    run_id = run.id
    if stage is ReviewStage.REQUIREMENTS:
        for old in session.exec(select(Requirement).where(
                Requirement.run_id == run_id,
                Requirement.endpoint_key == endpoint["endpoint_key"])).all():
            session.delete(old)
        session.commit()
        created, rejected = await generate_requirements_for_endpoint(llm, endpoint, run, session)
    else:
        for old in session.exec(select(TestCase).where(
                TestCase.run_id == run_id, TestCase.endpoint_key == endpoint["endpoint_key"])).all():
            session.delete(old)
        session.commit()
        approved = session.exec(select(Requirement).where(
            Requirement.run_id == run_id, Requirement.endpoint_key == endpoint["endpoint_key"],
            Requirement.status == ArtifactStatus.APPROVED.value)).all()
        created, rejected = await generate_cases_for_endpoint(llm, endpoint, list(approved), run, session)
    return {"created": created, "rejected": rejected}


@app.get("/api/runs/{run_id}/stream")
async def stream_run(run_id: int):
    """
    SSE for the whole run, both stages (LLD 5).

    Deliberately does not take a request-scoped session: FastAPI closes those
    when the route returns, before this generator ever runs.
    """
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(run_id, set()).add(queue)

    async def events():
        try:
            with Session(get_engine()) as session:
                run = session.get(GenerationRun, run_id)
                if not run:
                    yield _sse("error", {"message": "Run not found"})
                    return
                yield _sse("status", {"run_id": run_id, "status": run.status,
                                      "error": run.error,
                                      "total_endpoints": len(run.endpoint_keys) or None})
                terminal = {RunStatus.DONE.value, RunStatus.ERROR.value}
                if run.status in terminal:
                    return
            while True:
                try:
                    event, data = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"      # also surfaces client disconnects
                    continue
                yield _sse(event, data)
                if event in ("run_done", "run_error"):
                    return
        finally:
            _subscribers.get(run_id, set()).discard(queue)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- pipeline ----------

def _spawn(run_id: int, coro) -> None:
    task = asyncio.create_task(coro)
    _tasks[run_id] = task                      # a bare create_task can be GC'd mid-run
    task.add_done_callback(lambda t: _tasks.pop(run_id, None))


async def _run_endpoints(run_id: int, stage: str, endpoints: list[dict], work,
                         emit_progress: bool = True, provider: str = "") -> int:
    """
    Apply `work` to every endpoint, bounded by TESTRONAUT_MAX_CONCURRENCY.
    One endpoint's failure never aborts the run — it emits endpoint_error and the
    rest continue. Returns the number that errored.

    `emit_progress` is False for the review pass: that phase has its own
    review_start/review_done events (LLD §5), and emitting endpoint_start for it
    too made the client label a review as "generating". endpoint_error is emitted
    either way — the protocol has no review_error.

    ponytail: concurrent endpoints share one Session. That is safe only because
    every session mutation happens in an await-free stretch (the LLM call
    completes first, then add/commit run synchronously), so no two coroutines can
    interleave inside a transaction. Adding an `await` between a session write and
    its commit breaks that. Give each endpoint its own Session if this stops
    holding, or if a database with real row locking replaces SQLite.
    """
    semaphore = asyncio.Semaphore(max_concurrency(provider))
    errors = 0
    total = len(endpoints)

    async def one(index: int, endpoint: dict) -> None:
        nonlocal errors
        key = endpoint["endpoint_key"]
        async with semaphore:
            if emit_progress:
                publish(run_id, "endpoint_start", {"stage": stage, "key": key,
                                                   "index": index, "total": total})
            try:
                count = await work(endpoint)
                if emit_progress:
                    publish(run_id, "endpoint_done", {"stage": stage, "key": key,
                                                      "index": index, "items": count})
            except Exception as e:                      # noqa: BLE001 — one endpoint, not the run
                errors += 1
                logger.warning("endpoint_failed", run_id=run_id, stage=stage, key=key, error=str(e))
                publish(run_id, "endpoint_error", {"stage": stage, "key": key,
                                                   "index": index, "error": str(e)})

    await asyncio.gather(*(one(i, e) for i, e in enumerate(endpoints, start=1)))
    return errors


async def _stage(run_id: int, stage: str, generating: RunStatus, reviewing: RunStatus,
                 gate: RunStatus, gate_name: str) -> None:
    """
    Generate -> review -> stop at the gate. Shared by both stages; the only
    difference is which functions do the generating and reviewing.
    """
    with Session(get_engine()) as session:
        run = session.get(GenerationRun, run_id)
        if run is None:
            return
        try:
            endpoints = _endpoints_for_run(session, run)
            llm = get_provider(run.provider, run.model)
            critic = get_provider(run.reviewer_provider or run.provider,
                                  run.reviewer_model or run.model)

            publish(run_id, "status", {"run_id": run_id, "status": generating.value,
                                       "stage": stage, "total_endpoints": len(endpoints)})
            publish(run_id, "stage_start", {"stage": stage, "total_endpoints": len(endpoints)})

            if stage == "requirements":
                async def generate(endpoint: dict) -> int:
                    created, _ = await generate_requirements_for_endpoint(llm, endpoint, run, session)
                    return len(created)
            else:
                async def generate(endpoint: dict) -> int:
                    approved = session.exec(select(Requirement).where(
                        Requirement.run_id == run_id,
                        Requirement.endpoint_key == endpoint["endpoint_key"],
                        Requirement.status == ArtifactStatus.APPROVED.value)).all()
                    created, _ = await generate_cases_for_endpoint(
                        llm, endpoint, list(approved), run, session)
                    return len(created)

            errored = await _run_endpoints(run_id, stage, endpoints, generate,
                                           provider=run.provider)

            run.status = reviewing.value
            session.add(run)
            session.commit()
            publish(run_id, "status", {"run_id": run_id, "status": reviewing.value,
                                       "stage": stage, "total_endpoints": len(endpoints)})

            review = review_requirements if stage == "requirements" else review_cases

            async def critique(endpoint: dict) -> int:
                publish(run_id, "review_start", {"stage": stage, "key": endpoint["endpoint_key"]})
                result = await review(critic, run, endpoint, session)
                if result is None:
                    return 0
                applied = sum(1 for f in result.findings if f["resolution"] == "applied")
                publish(run_id, "review_done", {"stage": stage, "key": endpoint["endpoint_key"],
                                                "verdict": result.verdict,
                                                "finding_count": len(result.findings)})
                publish(run_id, "revision_done", {"stage": stage, "key": endpoint["endpoint_key"],
                                                  "applied": applied,
                                                  "raised": len(result.findings) - applied})
                return len(result.findings)

            errored += await _run_endpoints(run_id, stage, endpoints, critique,
                                            emit_progress=False,
                                            provider=run.reviewer_provider or run.provider)

            if stage == "test_cases":
                publish(run_id, "traceability", check_traceability(session, run))

            run.status = gate.value
            session.add(run)
            session.commit()
            publish(run_id, "status", {"run_id": run_id, "status": gate.value, "stage": stage})
            publish(run_id, "gate_reached", {"run_id": run_id, "gate": gate_name,
                                             "errored_endpoints": errored})
        except Exception as e:                          # noqa: BLE001 — fatal, run-level
            logger.exception("run_failed", run_id=run_id, stage=stage)
            session.rollback()
            run = session.get(GenerationRun, run_id)
            if run is not None:
                run.status = RunStatus.ERROR.value
                run.error = f"{type(e).__name__}: {e}"
                run.finished_at = utcnow()
                session.add(run)
                session.commit()
            publish(run_id, "run_error", {"run_id": run_id, "error": str(e)})


async def _stage1(run_id: int) -> None:
    await _stage(run_id, "requirements", RunStatus.GENERATING_REQUIREMENTS,
                 RunStatus.REVIEWING_REQUIREMENTS,
                 RunStatus.AWAITING_REQUIREMENTS_APPROVAL, "requirements")


async def _stage2(run_id: int) -> None:
    await _stage(run_id, "test_cases", RunStatus.GENERATING_CASES,
                 RunStatus.REVIEWING_CASES,
                 RunStatus.AWAITING_CASE_APPROVAL, "cases")


# ---------- requirements (gate 1) ----------

@app.get("/api/runs/{run_id}/requirements", response_model=list[Requirement])
async def list_requirements(run_id: int, session: Session = Depends(get_session)):
    _run_or_404(session, run_id)
    return session.exec(select(Requirement).where(Requirement.run_id == run_id)
                        .order_by(Requirement.order_index)).all()


@app.patch("/api/requirements/{pk}", response_model=Requirement)
async def update_requirement(pk: int, data: RequirementUpdate, session: Session = Depends(get_session)):
    req = session.get(Requirement, pk)
    if not req:
        raise HTTPException(404, "Requirement not found")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(req, key, value)
    req.status = ArtifactStatus.EDITED.value       # 2.6: an edit always lands in `edited`
    session.add(req)
    session.commit()
    session.refresh(req)
    return req


@app.post("/api/requirements", response_model=Requirement, status_code=201)
async def create_requirement(data: RequirementCreate, session: Session = Depends(get_session)):
    from .spec_parser import endpoint_key as make_key, make_req_id
    run = _run_or_404(session, data.run_id)
    count = len(session.exec(select(Requirement).where(Requirement.run_id == run.id)).all())
    req = Requirement(
        run_id=run.id,
        req_id=data.req_id or make_req_id(data.method, data.path, data.operation_id, count + 1),
        method=data.method.upper(), path=data.path, operation_id=data.operation_id,
        endpoint_key=make_key(data.method, data.path),
        title=data.title, description=data.description,
        acceptance_criteria=data.acceptance_criteria, priority=data.priority,
        origin=ArtifactOrigin.MANUAL.value, status=ArtifactStatus.EDITED.value,
        order_index=count + 1)
    session.add(req)
    session.commit()
    session.refresh(req)
    return req


@app.delete("/api/requirements/{pk}", status_code=204)
async def delete_requirement(pk: int, session: Session = Depends(get_session)):
    req = session.get(Requirement, pk)
    if not req:
        raise HTTPException(404, "Requirement not found")
    session.delete(req)
    session.commit()


@app.post("/api/requirements/approve")
async def approve_requirements(body: ApproveRequest, session: Session = Depends(get_session)):
    query = select(Requirement)
    if body.all and body.run_id:
        query = query.where(Requirement.run_id == body.run_id)
    elif body.requirement_ids:
        query = query.where(Requirement.req_id.in_(body.requirement_ids))
        if body.run_id:
            query = query.where(Requirement.run_id == body.run_id)
    else:
        raise HTTPException(400, "Provide requirement_ids, or run_id with all=true.")

    rows = session.exec(query).all()
    for req in rows:
        req.status = ArtifactStatus.APPROVED.value
        session.add(req)
    session.commit()
    return {"approved": len(rows)}


# ---------- test cases (gate 2) ----------

@app.get("/api/runs/{run_id}/cases", response_model=list[TestCase])
async def list_cases(run_id: int, session: Session = Depends(get_session)):
    _run_or_404(session, run_id)
    return session.exec(select(TestCase).where(TestCase.run_id == run_id)
                        .order_by(TestCase.order_index)).all()


@app.patch("/api/cases/{pk}", response_model=TestCase)
async def update_case(pk: int, data: TestCaseUpdate, session: Session = Depends(get_session)):
    case = session.get(TestCase, pk)
    if not case:
        raise HTTPException(404, "Test case not found")
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(case, key, value.value if isinstance(value, Category) else value)
    case.status = ArtifactStatus.EDITED.value
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


@app.post("/api/cases", response_model=TestCase, status_code=201)
async def create_case(data: TestCaseCreate, session: Session = Depends(get_session)):
    from .spec_parser import endpoint_key as make_key, make_case_id
    run = _run_or_404(session, data.run_id)
    count = len(session.exec(select(TestCase).where(TestCase.run_id == run.id)).all())
    case = TestCase(
        run_id=run.id,
        case_id=data.case_id or make_case_id(data.method, data.path, data.operation_id, count + 1),
        method=data.method.upper(), path=data.path, operation_id=data.operation_id,
        endpoint_key=make_key(data.method, data.path),
        requirement_ids=data.requirement_ids, category=data.category.value,
        title=data.title, description=data.description, priority=data.priority,
        expected_status=data.expected_status,
        origin=ArtifactOrigin.MANUAL.value, status=ArtifactStatus.EDITED.value,
        order_index=count + 1)
    session.add(case)
    session.commit()
    session.refresh(case)
    return case


@app.delete("/api/cases/{pk}", status_code=204)
async def delete_case(pk: int, session: Session = Depends(get_session)):
    case = session.get(TestCase, pk)
    if not case:
        raise HTTPException(404, "Test case not found")
    session.delete(case)
    session.commit()


@app.post("/api/cases/approve")
async def approve_cases(body: ApproveRequest, session: Session = Depends(get_session)):
    query = select(TestCase)
    if body.all and body.run_id:
        query = query.where(TestCase.run_id == body.run_id)
    elif body.case_ids:
        query = query.where(TestCase.case_id.in_(body.case_ids))
        if body.run_id:
            query = query.where(TestCase.run_id == body.run_id)
    else:
        raise HTTPException(400, "Provide case_ids, or run_id with all=true.")

    rows = session.exec(query).all()
    for case in rows:
        case.status = ArtifactStatus.APPROVED.value
        session.add(case)
    session.commit()
    return {"approved": len(rows)}


@app.get("/api/runs/{run_id}/traceability")
async def traceability(run_id: int, session: Session = Depends(get_session)):
    return check_traceability(session, _run_or_404(session, run_id))


# ---------- reviews ----------

@app.get("/api/runs/{run_id}/reviews", response_model=list[Review])
async def list_reviews(run_id: int, stage: Optional[ReviewStage] = None,
                       session: Session = Depends(get_session)):
    _run_or_404(session, run_id)
    query = select(Review).where(Review.run_id == run_id)
    if stage:
        query = query.where(Review.stage == stage.value)
    return session.exec(query.order_by(Review.created_at)).all()


@app.post("/api/runs/{run_id}/revert/{target_id}")
async def revert(run_id: int, target_id: str, finding_id: Optional[str] = None,
                 session: Session = Depends(get_session)):
    """
    Undo the reviewer. With ?finding_id=F-3, undoes that one change; without it,
    restores every field from the artifact's snapshot.
    """
    _run_or_404(session, run_id)
    if finding_id:
        if not revert_finding(session, run_id, finding_id, target_id):
            raise HTTPException(404, "No applied finding matches that finding_id and target_id.")
    else:
        artifact = (session.exec(select(Requirement).where(
                        Requirement.run_id == run_id, Requirement.req_id == target_id)).first()
                    if target_id.startswith("FR-") else
                    session.exec(select(TestCase).where(
                        TestCase.run_id == run_id, TestCase.case_id == target_id)).first())
        if artifact is None:
            raise HTTPException(404, f"{target_id} not found in this run")
        if not artifact.snapshot:
            raise HTTPException(409, f"{target_id} has no snapshot; it was never revised.")
        for key, value in artifact.snapshot.items():
            if key != "id" and hasattr(artifact, key):
                setattr(artifact, key, value)
        artifact.snapshot = None
        artifact.status = ArtifactStatus.DRAFT.value
        for review in session.exec(select(Review).where(Review.run_id == run_id)).all():
            findings = copy.deepcopy(review.findings)   # never mutate the loaded JSON in place
            touched = False
            for finding in findings:
                if finding.get("target_id") == target_id and finding.get("resolution") == "applied":
                    finding["resolution"] = "reverted"
                    touched = True
            if touched:
                review.findings = findings
                session.add(review)
        session.add(artifact)
        session.commit()

    artifact = (session.exec(select(Requirement).where(
                    Requirement.run_id == run_id, Requirement.req_id == target_id)).first()
                if target_id.startswith("FR-") else
                session.exec(select(TestCase).where(
                    TestCase.run_id == run_id, TestCase.case_id == target_id)).first())
    return artifact


# ---------- export / import ----------

@app.get("/api/runs/{run_id}/export")
async def export_run(run_id: int, format: str = Query("json", pattern="^(json|xlsx)$"),
                     only_approved: bool = False, session: Session = Depends(get_session)):
    _run_or_404(session, run_id)
    if format == "json":
        payload = to_json(run_id, session, only_approved=only_approved)
        return JSONResponse(payload, headers={
            "Content-Disposition": f'attachment; filename="testronaut-run-{run_id}.json"'})

    fd, path = tempfile.mkstemp(prefix=f"testronaut-run-{run_id}-", suffix=".xlsx")
    os.close(fd)
    to_xlsx(run_id, session, path, only_approved=only_approved)
    return FileResponse(
        path, background=BackgroundTask(os.remove, path), filename=f"testronaut-run-{run_id}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/runs/{run_id}/import")
async def import_run(run_id: int, file: UploadFile = File(...),
                     session: Session = Depends(get_session)):
    _run_or_404(session, run_id)
    name = (file.filename or "").lower()
    content = await _read_upload(file, MAX_UPLOAD_BYTES)

    if name.endswith(".xlsx"):
        fd, path = tempfile.mkstemp(prefix=f"testronaut-import-{run_id}-", suffix=".xlsx")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            summary = from_xlsx(run_id, session, path)
        finally:
            os.remove(path)
    elif name.endswith(".json") or not name:
        try:
            data = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HTTPException(400, f"Not valid JSON: {e}")
        summary = from_json(run_id, session, data)
    else:
        raise HTTPException(400, "Upload a .json or .xlsx file.")
    return summary.as_dict()


# ---------- phase 2: codegen, execution, reports ----------

@app.post("/api/runs/{run_id}/codegen")
async def codegen(run_id: int, body: CodegenRequest, session: Session = Depends(get_session)):
    """Approved cases -> a runnable Maven + TestNG + REST Assured project."""
    _run_or_404(session, run_id)
    target = workspace(run_id)
    shutil.rmtree(target, ignore_errors=True)
    try:
        project = generate_project(run_id, session, target, base_url=body.base_url,
                                   test_data=body.test_data, only_approved=body.only_approved)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return project.as_dict()


@app.get("/api/runs/{run_id}/codegen/download")
async def download_project(run_id: int, session: Session = Depends(get_session)):
    _run_or_404(session, run_id)
    project = workspace(run_id)
    if not (project / "pom.xml").exists():
        raise HTTPException(404, "No generated project for this run. Generate it first.")
    base = os.path.join(tempfile.mkdtemp(prefix="testronaut-zip-"), f"testronaut-run-{run_id}")
    archive = shutil.make_archive(base, "zip", project)
    return FileResponse(archive, background=BackgroundTask(shutil.rmtree, os.path.dirname(archive),
                                                           ignore_errors=True),
                        filename=f"testronaut-run-{run_id}.zip",
                        media_type="application/zip")


def _project_file(run_id: int, rel_path: str) -> Path:
    """Resolve a path within a run's generated project, refusing to escape it."""
    project = workspace(run_id)
    target = (project / rel_path).resolve()
    if project.resolve() not in target.parents:
        raise HTTPException(400, "Invalid path")
    if not target.is_file():
        raise HTTPException(404, f"No such file: {rel_path}")
    return target


@app.get("/api/runs/{run_id}/codegen/files/{path:path}")
async def read_project_file(run_id: int, path: str, session: Session = Depends(get_session)):
    """Review a generated file's source before deciding whether to run it."""
    _run_or_404(session, run_id)
    return {"path": path, "content": _project_file(run_id, path).read_text(encoding="utf-8")}


@app.put("/api/runs/{run_id}/codegen/files/{path:path}")
async def update_project_file(run_id: int, path: str, body: FileUpdate,
                              session: Session = Depends(get_session)):
    """Hand-edit a generated file — e.g. fix an assertion — before running it."""
    _run_or_404(session, run_id)
    _project_file(run_id, path).write_text(body.content, encoding="utf-8")
    return {"path": path, "saved": True}


@app.get("/api/sandbox")
async def sandbox_status():
    """Whether the generated project can be executed here, and why not if not."""
    available = docker_available()
    return {"available": available,
            "reason": None if available else
                      "Docker is not running. Generated code is untrusted and is never "
                      "run on the host — start Docker, or download the project and run "
                      "`mvn test` yourself."}


@app.post("/api/runs/{run_id}/execute")
async def execute(run_id: int, body: ExecuteRequest, session: Session = Depends(get_session)):
    """Run the generated project in a container and report per-case results."""
    run = _run_or_404(session, run_id)
    project = workspace(run_id)
    if not (project / "pom.xml").exists():
        raise HTTPException(409, "No generated project for this run. Generate it first.")
    try:
        network = validate_docker_network(body.network)
        result = await run_tests(project, timeout=min(max(body.timeout, 1.0), 3600.0), network=network)
    except SandboxUnavailable as e:
        raise HTTPException(503, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))

    run.last_execution = result.as_dict()
    session.add(run)
    session.commit()
    return run.last_execution


@app.get("/api/runs/{run_id}/execution")
async def last_execution(run_id: int, session: Session = Depends(get_session)):
    run = _run_or_404(session, run_id)
    if run.last_execution:
        return run.last_execution
    project = workspace(run_id)
    if (project / "target" / "surefire-reports").exists():
        return parse_surefire(project).as_dict()     # ran outside the app
    raise HTTPException(404, "This run has not been executed yet.")


# ---------- health ----------

@app.get("/api/health")
async def health():
    return {"status": "ok"}
