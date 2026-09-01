"""
The assertion grammar from LLD §3, parsed once and consumed twice.

Assertions live in `test_case.expected_body` as strings — deliberately, since a
structured AST is speculative until codegen exists and strings survive editing in
Excel. This module is that codegen: it parses the six recognized forms and
compiles them to REST Assured matchers.

    $.path == value
    $.path != value
    $.path contains 'substring'
    $.path exists
    $.path matches /regex/
    $.array length == N

Anything that does not parse is not an error. Models write prose, humans write
prose, and a test case with an unparseable assertion is still worth generating —
it comes out as a TODO comment beside a status-code check rather than silently
becoming a test that asserts nothing.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

# `$.a.b[0].c` or bare `a.b` — the leading $. is conventional but optional.
_PATH = r"\$?\.?(?P<path>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\])*)"

_FORMS = [
    ("length", re.compile(rf"^\s*{_PATH}\s+length\s*==\s*(?P<value>\d+)\s*$", re.I)),
    ("exists", re.compile(rf"^\s*{_PATH}\s+exists\s*$", re.I)),
    ("matches", re.compile(rf"^\s*{_PATH}\s+matches\s*/(?P<value>.+)/\s*$", re.I)),
    ("contains", re.compile(rf"^\s*{_PATH}\s+contains\s+(?P<value>.+?)\s*$", re.I)),
    ("!=", re.compile(rf"^\s*{_PATH}\s*!=\s*(?P<value>.+?)\s*$")),
    ("==", re.compile(rf"^\s*{_PATH}\s*==\s*(?P<value>.+?)\s*$")),
]


# Both `$.name` and `response.name` mean "field `name` of the response body".
# REST Assured's GPath is already rooted there, so the prefix must come off or the
# matcher looks for a literal field called "response".
_ROOT_PREFIX = re.compile(r"^(?:response|body)\.", re.I)


@dataclass(frozen=True)
class Assertion:
    path: str
    op: str
    value: Any = None
    source: str = ""

    @property
    def rest_assured_path(self) -> str:
        """REST Assured uses Groovy GPath: the dotted form, rooted at the body."""
        return _ROOT_PREFIX.sub("", self.path)


def _literal(raw: str) -> Any:
    """A JSON scalar, or a single-quoted string, or bare text."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def parse(assertion: str) -> Optional[Assertion]:
    """Parse one assertion string, or return None if it is not in the grammar."""
    if not assertion or not assertion.strip():
        return None
    for op, pattern in _FORMS:
        match = pattern.match(assertion)
        if not match:
            continue
        raw = match.groupdict().get("value")
        if op == "length":
            value: Any = int(raw)
        elif op == "exists":
            value = None
        elif op == "matches":
            value = raw
        else:
            value = _literal(raw)
        return Assertion(path=match.group("path"), op=op, value=value, source=assertion)
    return None


def is_valid(assertion: str) -> bool:
    return parse(assertion) is not None


# ---------- compilation to REST Assured ----------

def _java_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))          # correct escaping for a Java string literal


def to_rest_assured(assertion: Assertion) -> str:
    """One `.body(...)` matcher chain fragment."""
    path = json.dumps(assertion.rest_assured_path)
    if assertion.op == "==":
        return f".body({path}, equalTo({_java_literal(assertion.value)}))"
    if assertion.op == "!=":
        return f".body({path}, not(equalTo({_java_literal(assertion.value)})))"
    if assertion.op == "contains":
        return f".body({path}, containsString({_java_literal(assertion.value)}))"
    if assertion.op == "exists":
        return f".body({path}, notNullValue())"
    if assertion.op == "matches":
        return f".body({path}, matchesPattern({_java_literal(assertion.value)}))"
    if assertion.op == "length":
        size = json.dumps(f"{assertion.rest_assured_path}.size()")
        return f".body({size}, equalTo({assertion.value}))"
    raise ValueError(f"Unknown assertion op: {assertion.op}")


def compile_all(assertions: list[str]) -> tuple[list[str], list[str]]:
    """
    Compile what parses. Returns (matcher fragments, unparseable originals) so the
    generator can emit the rest as TODO comments rather than dropping them.
    """
    compiled, skipped = [], []
    for raw in assertions:
        parsed = parse(raw)
        if parsed is None:
            skipped.append(raw)
        else:
            compiled.append(to_rest_assured(parsed))
    return compiled, skipped
