"""
Agent SDK path: one submit tool per stage, plus a PreToolUse hook.

The Agent SDK earns its place here through deterministic control, not billing.
Three layers, strongest first (PLAN 9):

1. **Capability restriction.** Each stage is given exactly one tool. No Read, no
   Write, no Bash, no WebFetch. An uploaded OpenAPI spec is untrusted input
   reaching a model prompt — a `description` field carrying an injection payload
   cannot read the filesystem or exfiltrate anything when the tools do not exist
   in the session.
2. **Schema-validated tool.** Arguments arrive through a JSON schema the SDK
   enforces, replacing "prompt for JSON, strip fences, retry once" on this path.
3. **PreToolUse hook.** Returns a deny decision with a reason the model sees and
   self-corrects against, in-loop.

`check_tool_use` is pure and runs the same validators as the raw-API path, so
the two transports cannot drift. It is unit-tested without the SDK installed.
"""

from dataclasses import dataclass, field
import structlog

from .generator import ValidationError, validate_case, validate_requirement

logger = structlog.get_logger()

SUBMIT_REQUIREMENTS = "submit_requirements"
SUBMIT_TEST_CASES = "submit_test_cases"
SUBMIT_REVIEW = "submit_review"

# One tool per stage. The list is the security boundary, so it stays explicit.
STAGE_TOOLS = {
    "requirements_authoring": [f"mcp__testronaut__{SUBMIT_REQUIREMENTS}"],
    "test_design": [f"mcp__testronaut__{SUBMIT_TEST_CASES}"],
    "requirements_review": [f"mcp__testronaut__{SUBMIT_REVIEW}"],
    "test_case_review": [f"mcp__testronaut__{SUBMIT_REVIEW}"],
}

STAGE_SKILLS = {
    "requirements_authoring": "requirements-authoring",
    "test_design": "test-design",
    "requirements_review": "requirements-review",
    "test_case_review": "test-case-review",
}

_REQUIREMENT_ITEM = {
    "type": "object",
    "properties": {
        "req_id": {"type": "string", "pattern": "^FR-[A-Z0-9_]+-[0-9]{3,}$"},
        "title": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "spec_source": {"type": "string"},
        "priority": {"enum": ["P1", "P2", "P3"]},
    },
    "required": ["req_id", "title", "description", "acceptance_criteria"],
}

_CASE_ITEM = {
    "type": "object",
    "properties": {
        "case_id": {"type": "string", "pattern": "^TC-[A-Z0-9_]+-[0-9]{3,}$"},
        "requirement_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "category": {"enum": ["happy_path", "negative", "boundary", "auth", "security"]},
        "title": {"type": "string", "minLength": 1},
        "description": {"type": "string"},
        "priority": {"enum": ["P1", "P2", "P3"]},
        "preconditions": {"type": "array", "items": {"type": "string"}},
        "path_params": {"type": "object"},
        "query_params": {"type": "object"},
        "headers": {"type": "object"},
        "body": {},
        "expected_status": {"type": "integer"},
        "expected_body": {"type": "array", "items": {"type": "string"}},
        "test_data_notes": {"type": "string"},
    },
    "required": ["case_id", "requirement_ids", "category", "title",
                 "description", "expected_status"],
}

_FINDING_ITEM = {
    "type": "object",
    "properties": {
        "severity": {"enum": ["high", "medium", "low"]},
        "target_id": {"type": "string"},
        "field": {"type": "string"},
        "issue": {"type": "string", "minLength": 1},
        "suggestion": {},
        "resolution": {"enum": ["applied", "raised"]},
    },
    "required": ["severity", "target_id", "issue", "resolution"],
}

TOOL_SCHEMAS = {
    SUBMIT_REQUIREMENTS: {
        "name": SUBMIT_REQUIREMENTS,
        "description": "Submit every functional requirement for the current endpoint, in one call.",
        "input_schema": {
            "type": "object",
            "properties": {"requirements": {"type": "array", "items": _REQUIREMENT_ITEM, "minItems": 1}},
            "required": ["requirements"],
        },
    },
    SUBMIT_TEST_CASES: {
        "name": SUBMIT_TEST_CASES,
        "description": "Submit every test case for the current endpoint, in one call.",
        "input_schema": {
            "type": "object",
            "properties": {"test_cases": {"type": "array", "items": _CASE_ITEM, "minItems": 1}},
            "required": ["test_cases"],
        },
    },
    SUBMIT_REVIEW: {
        "name": SUBMIT_REVIEW,
        "description": "Submit review findings for the current endpoint. An empty array means no issues.",
        "input_schema": {
            "type": "object",
            "properties": {"findings": {"type": "array", "items": _FINDING_ITEM}},
            "required": ["findings"],
        },
    },
}


@dataclass
class RunContext:
    """Everything the hook needs to judge a submission. Built per endpoint."""
    endpoint: dict
    categories: list[str]
    taken_req_ids: set[str] = field(default_factory=set)
    taken_case_ids: set[str] = field(default_factory=set)
    approved_req_ids: list[str] = field(default_factory=list)
    valid_target_ids: set[str] = field(default_factory=set)


def deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def allow() -> dict:
    return {}


def check_tool_use(tool_name: str, tool_input: dict, ctx: RunContext) -> dict:
    """
    PreToolUse decision. Returns {} to allow, or a deny decision whose reason the
    model reads and corrects against without a round trip through the parser.

    Runs the same validators as the raw-API path (generator.validate_*), so the
    rules in LLD 9 are enforced identically on both transports.
    """
    name = tool_name.rsplit("__", 1)[-1]

    if name == SUBMIT_REQUIREMENTS:
        items = tool_input.get("requirements")
        if not isinstance(items, list) or not items:
            return deny("requirements must be a non-empty array.")
        taken = set(ctx.taken_req_ids)
        for i, item in enumerate(items, start=1):
            try:
                values = validate_requirement(item, ctx.endpoint, i, taken)
            except ValidationError as e:
                return deny(f"requirement #{i}: {e}")
            taken.add(values["req_id"])
        return allow()

    if name == SUBMIT_TEST_CASES:
        items = tool_input.get("test_cases")
        if not isinstance(items, list) or not items:
            return deny("test_cases must be a non-empty array.")
        taken = set(ctx.taken_case_ids)
        for i, item in enumerate(items, start=1):
            try:
                values = validate_case(item, ctx.endpoint, i, taken,
                                       ctx.categories, ctx.approved_req_ids)
            except ValidationError as e:
                return deny(f"test case #{i}: {e}")
            taken.add(values["case_id"])
        return allow()

    if name == SUBMIT_REVIEW:
        findings = tool_input.get("findings")
        if not isinstance(findings, list):
            return deny("findings must be an array (use [] when nothing is wrong).")
        for i, finding in enumerate(findings, start=1):
            target = (finding or {}).get("target_id")
            if target not in ctx.valid_target_ids:
                return deny(
                    f"finding #{i} targets {target!r}, which this run never produced. "
                    f"Valid targets: {sorted(ctx.valid_target_ids)}")
            if finding.get("resolution") == "applied" and finding.get("suggestion") in (None, ""):
                return deny(f"finding #{i} is marked applied but supplies no suggestion.")
        return allow()

    # Layer 1: nothing else is reachable. Belt and braces if allowed_tools is misconfigured.
    return deny(f"Tool {tool_name!r} is not available in this session.")


def log_tool_use(tool_name: str, tool_input: dict) -> None:
    """PostToolUse audit trail: what the model emitted, per stage."""
    counts = {k: len(v) for k, v in (tool_input or {}).items() if isinstance(v, list)}
    logger.info("agent_tool_use", tool=tool_name, **counts)


def sdk_options(stage: str, cwd: str) -> dict:
    """
    Options for a claude_agent_sdk session for one stage.

    setting_sources is ["project"] deliberately, never "user": a pipeline run
    must not pull in the operator's personal ~/.claude config, or the same spec
    would generate differently on two machines.
    """
    if stage not in STAGE_TOOLS:
        raise ValueError(f"Unknown stage: {stage}")
    return {
        "allowed_tools": list(STAGE_TOOLS[stage]),
        "disallowed_tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch", "WebSearch"],
        "setting_sources": ["project"],
        "cwd": cwd,
        "permission_mode": "default",
    }


def require_sdk():
    """Import claude_agent_sdk, with an actionable message when it is absent."""
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "The claude_agent_sdk provider needs `pip install claude-agent-sdk` and a "
            "real ANTHROPIC_API_KEY. A Claude subscription is not a sanctioned auth "
            "path for SDK-built agents (PLAN 3); use the `anthropic` provider with an "
            "API key, or `ollama` for a free local run."
        ) from e
    return claude_agent_sdk
