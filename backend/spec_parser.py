"""
OpenAPI 3.x parsing -> normalized endpoint list.

Also the single home of the deterministic ID helpers (op_token / make_req_id /
make_case_id): they are pure string functions with no ORM dependency, and every
other module imports them from here so prompts, validation, export and import
can never disagree about an ID.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

import yaml
from openapi_spec_validator import validate

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


@dataclass
class SpecMeta:
    name: str
    version: str


class SpecParseError(Exception):
    """Raised when spec parsing fails, with a human-readable message."""


class UnresolvableRefError(SpecParseError):
    """Raised when a $ref cannot be resolved."""


# ---------- Deterministic identifiers ----------

def op_token(method: str, path: str, operation_id: Optional[str]) -> str:
    """
    Stable operation identifier. Prefers operationId; falls back to
    <METHOD>_<PATHSLUG>_<HASH4> because operationId is optional in OpenAPI and
    path slugs alone collide (/pet/{id} and /pet-id both slug to PETID).
    """
    if operation_id:
        token = re.sub(r"[^A-Za-z0-9]", "", operation_id).upper()[:24]
        if token:
            return token
    slug = re.sub(r"[^A-Za-z0-9]", "", path).upper()[:20]
    digest = hashlib.sha1(f"{method.upper()} {path}".encode()).hexdigest()[:4].upper()
    return f"{method.upper()}_{slug}_{digest}"


def make_req_id(method: str, path: str, operation_id: Optional[str], index: int) -> str:
    """FR-<OPTOKEN>-<NNN>"""
    return f"FR-{op_token(method, path, operation_id)}-{index:03d}"


def make_case_id(method: str, path: str, operation_id: Optional[str], index: int) -> str:
    """TC-<OPTOKEN>-<NNN>"""
    return f"TC-{op_token(method, path, operation_id)}-{index:03d}"


def endpoint_key(method: str, path: str) -> str:
    """"METHOD PATH" — the run-selection and review key."""
    return f"{method.upper()} {path}"


# ---------- Schema flattening ----------

def flatten_schema(obj: Any, components: dict, max_depth: int = 4) -> Any:
    """
    Resolve $ref and inline nested schemas up to max_depth.

    `components` is the spec's components object and must be passed in: the
    fragments this is called on (a requestBody, a responses map) never contain
    it themselves.

    Beyond max_depth, or on a reference cycle, emits {"type": ..., "truncated": true}
    instead of recursing. Real specs contain circular schemas (Pet.category.pets[]);
    without this the parser dies with RecursionError.
    """

    def _flatten(node: Any, depth: int, seen: frozenset) -> Any:
        if isinstance(node, dict):
            ref_path = node.get("$ref")
            if ref_path is not None:
                if not isinstance(ref_path, str) or not ref_path.startswith("#/components/"):
                    raise UnresolvableRefError(
                        f"Only local #/components/... refs are supported, got: {ref_path!r}"
                    )
                if depth >= max_depth or ref_path in seen:
                    return {"type": "object", "truncated": True}

                target: Any = components
                for part in ref_path.split("/")[2:]:
                    if not isinstance(target, dict) or part not in target:
                        raise UnresolvableRefError(f"Component not found: {ref_path}")
                    target = target[part]
                return _flatten(target, depth + 1, seen | {ref_path})

            return {k: _flatten(v, depth, seen) for k, v in node.items()}
        if isinstance(node, list):
            return [_flatten(item, depth, seen) for item in node]
        return node

    return _flatten(obj, 0, frozenset())


def _merge_params(path_params: list, op_params: list, components: dict) -> list[dict]:
    """
    Merge path-level and operation-level parameters. Per OpenAPI an
    operation-level parameter overrides a path-level one with the same name+in,
    so this de-duplicates rather than concatenating.
    """
    merged: dict[tuple, dict] = {}
    for param in list(path_params) + list(op_params):
        resolved = flatten_schema(param, components)
        merged[(resolved.get("name"), resolved.get("in"))] = resolved
    return list(merged.values())


def declared_statuses(responses: dict) -> set[int]:
    """Numeric status codes an operation declares. Used to validate expected_status."""
    out = set()
    for code in (responses or {}):
        try:
            out.add(int(code))
        except (TypeError, ValueError):
            continue  # "default", "2XX"
    return out


def parse_spec(raw: str, format: str) -> tuple[SpecMeta, list[dict]]:
    """
    Parse an OpenAPI 3.x document (JSON or YAML) into (SpecMeta, endpoints).

    Each endpoint dict: method, path, operation_id, summary, description,
    parameters, request_body, responses, security, op_token, endpoint_key.

    Raises SpecParseError with a human-readable message.
    """
    try:
        if format == "json":
            spec = json.loads(raw)
        elif format == "yaml":
            spec = yaml.safe_load(raw)
        else:
            raise SpecParseError(f"Unsupported format: {format!r} (expected 'json' or 'yaml')")
    except json.JSONDecodeError as e:
        raise SpecParseError(f"Invalid JSON: {e}") from e
    except yaml.YAMLError as e:
        raise SpecParseError(f"Invalid YAML: {e}") from e

    if not isinstance(spec, dict):
        raise SpecParseError("Spec must be a JSON/YAML object at the top level.")

    if str(spec.get("swagger", "")).startswith("2."):
        raise SpecParseError(
            "Swagger 2.0 is not supported in v1. Convert to OpenAPI 3.x first "
            "(e.g. with swagger2openapi)."
        )

    try:
        validate(spec)
    except UnresolvableRefError:
        raise
    except Exception as e:
        raise SpecParseError(f"Invalid OpenAPI spec: {e}") from e

    info = spec.get("info") or {}
    meta = SpecMeta(
        name=info.get("title") or "Untitled API",
        version=str(info.get("version") or "0.0.0"),
    )

    components = spec.get("components") or {}
    global_security = spec.get("security", [])
    endpoints: list[dict] = []

    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        path_params = path_item.get("parameters", [])

        for method, operation in path_item.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue

            operation_id = operation.get("operationId")
            request_body = operation.get("requestBody")
            responses = operation.get("responses") or {}

            endpoints.append({
                "method": method.upper(),
                "path": path,
                "operation_id": operation_id,
                "summary": operation.get("summary"),
                "description": operation.get("description"),
                "parameters": _merge_params(path_params, operation.get("parameters", []), components),
                "request_body": flatten_schema(request_body, components) if request_body else None,
                "responses": flatten_schema(responses, components),
                "security": operation.get("security", global_security),
                "op_token": op_token(method, path, operation_id),
                "endpoint_key": endpoint_key(method, path),
            })

    if not endpoints:
        raise SpecParseError("Spec contains no operations under `paths`.")

    return meta, endpoints
