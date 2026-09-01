"""
Phase 2, steps 3 and 4: execute the generated project in a sandbox, parse the results.

Generated code is untrusted — it came from a model that read an untrusted spec —
so it runs in a container, never on the host (PLAN §10). The container gets the
project directory and a Maven cache, and nothing else.

Surefire reports test methods; Testronaut cares about case IDs. `testronaut-map.json`,
written at generation time, is the join — reversing a camelCase method name back
into `TC-FOO-001` would be guesswork.
"""

import asyncio
import json
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

# Java 17 bytecode target, so any JDK >= 17 image works.
DEFAULT_IMAGE = "maven:3.9-eclipse-temurin-21"
DEFAULT_TIMEOUT = 900.0
CACHE_VOLUME = "testronaut-m2"          # named volume: the second run is far faster


class SandboxUnavailable(RuntimeError):
    """Docker is not installed or not running."""


@dataclass
class CaseResult:
    case_id: Optional[str]
    class_name: str
    method: str
    status: str                          # passed | failed | error | skipped
    time: float = 0.0
    message: Optional[str] = None
    detail: Optional[str] = None

    def as_dict(self) -> dict:
        return {"case_id": self.case_id, "class_name": self.class_name, "method": self.method,
                "status": self.status, "time": self.time,
                "message": self.message, "detail": self.detail}


@dataclass
class ExecutionResult:
    exit_code: int
    duration: float
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    cases: list[CaseResult] = field(default_factory=list)
    output: str = ""
    timed_out: bool = False

    @property
    def ran(self) -> bool:
        return self.total > 0

    def as_dict(self) -> dict:
        return {"exit_code": self.exit_code, "duration": round(self.duration, 1),
                "total": self.total, "passed": self.passed, "failed": self.failed,
                "errors": self.errors, "skipped": self.skipped, "timed_out": self.timed_out,
                "cases": [c.as_dict() for c in self.cases],
                "output": self.output[-8000:]}


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return os.system("docker info >/dev/null 2>&1") == 0
    except OSError:
        return False


def _docker_command(project: Path, image: str, network: Optional[str]) -> list[str]:
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{project}:/work",
        "-v", f"{CACHE_VOLUME}:/root/.m2",
        "-w", "/work",
        # The sandbox is the security boundary: no host filesystem beyond the
        # project, no extra capabilities, and a memory ceiling so a runaway
        # generated test cannot take the machine down.
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", "2g",
        "--pids-limit", "512",
    ]
    if network:
        cmd += ["--network", network]
    # host.docker.internal is how a container reaches a service on the host; on
    # Linux it needs adding explicitly, on Docker Desktop it already resolves.
    cmd += ["--add-host", "host.docker.internal:host-gateway"]
    cmd += [image, "mvn", "-B", "-ntp", "test"]
    return cmd


async def run_tests(project_dir: Path, image: str = DEFAULT_IMAGE,
                    timeout: float = DEFAULT_TIMEOUT,
                    network: Optional[str] = None) -> ExecutionResult:
    """Run `mvn test` inside a container and parse what Surefire wrote."""
    project = Path(project_dir).resolve()
    if not (project / "pom.xml").exists():
        raise ValueError(f"No pom.xml in {project} — generate the project first.")
    if not docker_available():
        raise SandboxUnavailable(
            "Docker is not available. Generated code is untrusted and is not run on "
            "the host — start Docker Desktop, or download the project and run "
            "`mvn test` yourself.")

    # A previous run's reports would otherwise be reported as this run's.
    shutil.rmtree(project / "target" / "surefire-reports", ignore_errors=True)

    cmd = _docker_command(project, image, network)
    logger.info("sandbox_start", project=str(project), image=image)

    loop = asyncio.get_running_loop()
    started = loop.time()
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    timed_out = False
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        process.kill()
        stdout, _ = await process.communicate()
    duration = loop.time() - started

    result = parse_surefire(project)
    result.exit_code = process.returncode if process.returncode is not None else -1
    result.duration = duration
    result.timed_out = timed_out
    result.output = (stdout or b"").decode("utf-8", errors="replace")

    logger.info("sandbox_done", exit_code=result.exit_code, total=result.total,
                passed=result.passed, failed=result.failed, seconds=round(duration, 1))
    return result


def parse_surefire(project_dir: Path) -> ExecutionResult:
    """Read target/surefire-reports/*.xml and join method names back to case IDs."""
    project = Path(project_dir)
    result = ExecutionResult(exit_code=0, duration=0.0)

    mapping: dict[str, str] = {}
    map_file = project / "testronaut-map.json"
    if map_file.exists():
        try:
            mapping = json.loads(map_file.read_text()).get("methods", {})
        except (OSError, json.JSONDecodeError):
            logger.warning("method_map_unreadable", path=str(map_file))

    reports = sorted((project / "target" / "surefire-reports").glob("TEST-*.xml"))
    for report in reports:
        try:
            root = ET.parse(report).getroot()
        except ET.ParseError as e:
            logger.warning("surefire_unparseable", path=str(report), error=str(e))
            continue

        for node in root.iter("testcase"):
            class_name = (node.get("classname") or "").rsplit(".", 1)[-1]
            method = node.get("name") or ""
            failure = node.find("failure")
            error = node.find("error")
            skipped = node.find("skipped")

            if failure is not None:
                status, detail_node = "failed", failure
            elif error is not None:
                status, detail_node = "error", error
            elif skipped is not None:
                status, detail_node = "skipped", skipped
            else:
                status, detail_node = "passed", None

            result.cases.append(CaseResult(
                case_id=mapping.get(f"{class_name}#{method}"),
                class_name=class_name,
                method=method,
                status=status,
                time=float(node.get("time") or 0.0),
                message=(detail_node.get("message") if detail_node is not None else None),
                detail=((detail_node.text or "").strip()[:2000] if detail_node is not None else None),
            ))

    result.total = len(result.cases)
    # On the fallback path (a project run outside the app) there is no wall clock,
    # so fall back to the sum of the per-case times Surefire recorded.
    if not result.duration:
        result.duration = sum(c.time for c in result.cases)
    result.passed = sum(1 for c in result.cases if c.status == "passed")
    result.failed = sum(1 for c in result.cases if c.status == "failed")
    result.errors = sum(1 for c in result.cases if c.status == "error")
    result.skipped = sum(1 for c in result.cases if c.status == "skipped")
    return result
