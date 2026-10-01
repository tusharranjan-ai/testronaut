"""
Phase 2, step 1: approved test cases -> a runnable Maven + TestNG + REST Assured project.

The canonical JSON (LLD §3) is deliberately free of any framework reference, so
this module is the only place that knows about Java. A different target language
is a sibling of this file, not a change to the data model.

What the operator still has to supply is the part no spec contains: the base URL
and any credentials. Those live in `testronaut.properties`, not in the generated
source, so the project can be committed and the secrets cannot.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import structlog
from sqlmodel import Session, select

from .assertions import compile_all
from .db import ArtifactStatus, GenerationRun, Requirement, TestCase
from .spec_parser import op_token as make_op_token

logger = structlog.get_logger()

GROUP_ID = "com.testronaut"
PACKAGE = "com.testronaut.generated"

JAVA_KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char", "class",
    "const", "continue", "default", "do", "double", "else", "enum", "extends", "final",
    "finally", "float", "for", "goto", "if", "implements", "import", "instanceof", "int",
    "interface", "long", "native", "new", "package", "private", "protected", "public",
    "return", "short", "static", "strictfp", "super", "switch", "synchronized", "this",
    "throw", "throws", "transient", "try", "void", "volatile", "while",
}

# `${petId}` in any string value is resolved from testronaut.properties at runtime.
PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}")


@dataclass
class GeneratedProject:
    root: Path
    files: list[str] = field(default_factory=list)
    test_count: int = 0
    endpoint_count: int = 0
    compiled_assertions: int = 0
    skipped_assertions: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    method_map: dict[str, str] = field(default_factory=dict)   # Class#method -> case_id

    def as_dict(self) -> dict:
        return {"root": str(self.root), "files": self.files, "test_count": self.test_count,
                "endpoint_count": self.endpoint_count,
                "compiled_assertions": self.compiled_assertions,
                "skipped_assertions": self.skipped_assertions,
                "placeholders": sorted(set(self.placeholders))}


# ---------- naming ----------

def _pascal(text: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", text) if p]
    return "".join(p[:1].upper() + p[1:].lower() for p in parts) or "Endpoint"


def _camel(text: str) -> str:
    name = _pascal(text)
    name = name[:1].lower() + name[1:]
    if name in JAVA_KEYWORDS or name[:1].isdigit():
        name = f"_{name}"
    return name


def class_name(endpoint_key: str, operation_id: Optional[str]) -> str:
    """One test class per endpoint, named for the operation."""
    method, _, path = endpoint_key.partition(" ")
    base = operation_id or make_op_token(method, path, None)
    return f"{_pascal(base)}Test"


def method_name(case_id: str, taken: set[str]) -> str:
    name = _camel(case_id)
    candidate, n = name, 2
    while candidate in taken:            # case_ids are unique per run, but be safe
        candidate, n = f"{name}_{n}", n + 1
    taken.add(candidate)
    return candidate


# ---------- Java literals ----------

def _jstr(value: Any) -> str:
    return json.dumps("" if value is None else str(value))


def _collect_placeholders(value: Any, found: list[str]) -> None:
    if isinstance(value, str):
        found.extend(PLACEHOLDER.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            _collect_placeholders(v, found)
    elif isinstance(value, list):
        for v in value:
            _collect_placeholders(v, found)


def _resolved(value: Any) -> str:
    """A Java expression for a string that may carry ${...} placeholders."""
    text = "" if value is None else str(value)
    if PLACEHOLDER.search(text):
        return f"Config.resolve({_jstr(text)})"
    return _jstr(text)


# ---------- rendering ----------

def _render_case(case: TestCase, requirements: dict[str, Requirement],
                 taken: set[str], project: GeneratedProject,
                 class_name_: str = "") -> str:
    lines: list[str] = []

    traced = [requirements[r].title for r in case.requirement_ids if r in requirements]
    lines.append("    /**")
    lines.append(f"     * {case.case_id} — {case.title}")
    if case.description:
        lines.append(f"     * {case.description}")
    for req_id, title in zip(case.requirement_ids, traced):
        lines.append(f"     * Traces to {req_id}: {title}")
    for pre in case.preconditions:
        lines.append(f"     * Precondition: {pre}")
    if case.test_data_notes:
        lines.append(f"     * Test data: {case.test_data_notes}")
    lines.append("     */")

    groups = json.dumps([case.category, case.priority])[1:-1]
    method = method_name(case.case_id, taken)
    project.method_map[f"{class_name_}#{method}"] = case.case_id
    lines.append(f'    @Test(groups = {{{groups}}}, description = {_jstr(case.title)})')
    lines.append(f"    public void {method}() {{")
    lines.append("        given()")
    lines.append("            .spec(Config.request())")

    for key, value in (case.headers or {}).items():
        if key.lower() == "content-type":
            continue                     # set once on the shared spec
        lines.append(f"            .header({_jstr(key)}, {_resolved(value)})")
    for key, value in (case.path_params or {}).items():
        lines.append(f"            .pathParam({_jstr(key)}, {_resolved(value)})")
    for key, value in (case.query_params or {}).items():
        lines.append(f"            .queryParam({_jstr(key)}, {_resolved(value)})")

    if case.body is not None:
        body = json.dumps(case.body)
        expr = f"Config.resolve({_jstr(body)})" if PLACEHOLDER.search(body) else _jstr(body)
        lines.append(f"            .body({expr})")

    verb = case.method.lower()
    lines.append("        .when()")
    lines.append(f"            .{verb}({_jstr(case.path)})")
    lines.append("        .then()")
    lines.append(f"            .statusCode({case.expected_status})")

    compiled, skipped = compile_all(case.expected_body or [])
    project.compiled_assertions += len(compiled)
    project.skipped_assertions.extend(skipped)
    for fragment in compiled:
        lines.append(f"            {fragment}")
    lines.append("        ;")

    for raw in skipped:
        # Not silently dropped: an assertion nobody can compile is still something
        # a human agreed to, so it lands where they will see it.
        lines.append(f"        // TODO assert manually: {raw}")

    lines.append("    }")
    return "\n".join(lines)


def _render_class(endpoint_key: str, cases: list[TestCase],
                  requirements: dict[str, Requirement], project: GeneratedProject) -> tuple[str, str]:
    name = class_name(endpoint_key, cases[0].operation_id)
    taken: set[str] = set()
    body = "\n\n".join(_render_case(c, requirements, taken, project, name) for c in cases)

    source = f"""package {PACKAGE};

import org.testng.annotations.Test;

import static io.restassured.RestAssured.given;
import static org.hamcrest.Matchers.*;

/**
 * {endpoint_key}
 *
 * Generated by Testronaut from approved test cases. Edits here are overwritten
 * on the next generation — change the test case and regenerate instead.
 */
public class {name} {{

{body}
}}
"""
    return name, source


CONFIG_JAVA = f"""package {PACKAGE};

import io.restassured.builder.RequestSpecBuilder;
import io.restassured.http.ContentType;
import io.restassured.specification.RequestSpecification;

import java.io.IOException;
import java.io.InputStream;
import java.util.Properties;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Base URL, credentials and test data — everything the spec cannot tell us.
 *
 * Values come from testronaut.properties, overridable by -D system properties so
 * CI can inject secrets without writing them to a file.
 */
public final class Config {{

    private static final Properties PROPS = load();
    private static final Pattern PLACEHOLDER = Pattern.compile("\\\\$\\\\{{([A-Za-z_][A-Za-z0-9_.]*)\\\\}}");

    private Config() {{ }}

    private static Properties load() {{
        Properties props = new Properties();
        try (InputStream in = Config.class.getClassLoader()
                .getResourceAsStream("testronaut.properties")) {{
            if (in != null) {{
                props.load(in);
            }}
        }} catch (IOException e) {{
            throw new IllegalStateException("Could not read testronaut.properties", e);
        }}
        return props;
    }}

    public static String get(String key) {{
        String value = System.getProperty(key, PROPS.getProperty(key));
        if (value == null || value.isBlank()) {{
            throw new IllegalStateException(
                "Missing test data '" + key + "'. Set it in testronaut.properties or pass -D"
                + key + "=value.");
        }}
        return value;
    }}

    public static String get(String key, String fallback) {{
        String value = System.getProperty(key, PROPS.getProperty(key));
        return (value == null || value.isBlank()) ? fallback : value;
    }}

    /** Substitute every ${{key}} in a string with its configured value. */
    public static String resolve(String template) {{
        Matcher m = PLACEHOLDER.matcher(template);
        StringBuilder out = new StringBuilder();
        while (m.find()) {{
            m.appendReplacement(out, Matcher.quoteReplacement(get(m.group(1))));
        }}
        m.appendTail(out);
        return out.toString();
    }}

    /** Shared request spec: base URI, content type, and auth if configured. */
    public static RequestSpecification request() {{
        RequestSpecBuilder builder = new RequestSpecBuilder()
                .setBaseUri(get("baseUrl"))
                .setContentType(ContentType.JSON);

        String header = get("authHeader", "");
        String value = get("authValue", "");
        if (!header.isBlank() && !value.isBlank()) {{
            builder.addHeader(header, value);
        }}
        return builder.build();
    }}
}}
"""


def _pom(artifact_id: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0
                             http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>

  <groupId>{GROUP_ID}</groupId>
  <artifactId>{artifact_id}</artifactId>
  <version>1.0.0</version>
  <packaging>jar</packaging>

  <properties>
    <maven.compiler.release>17</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>

  <dependencies>
    <dependency>
      <groupId>org.testng</groupId>
      <artifactId>testng</artifactId>
      <version>7.10.2</version>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>io.rest-assured</groupId>
      <artifactId>rest-assured</artifactId>
      <version>5.5.0</version>
      <scope>test</scope>
    </dependency>
    <dependency>
      <groupId>org.hamcrest</groupId>
      <artifactId>hamcrest</artifactId>
      <version>2.2</version>
      <scope>test</scope>
    </dependency>
  </dependencies>

  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.2.5</version>
        <configuration>
          <suiteXmlFiles>
            <suiteXmlFile>testng.xml</suiteXmlFile>
          </suiteXmlFiles>
          <!-- A failing assertion is the point of this project, not a build error. -->
          <testFailureIgnore>true</testFailureIgnore>
        </configuration>
      </plugin>
    </plugins>
  </build>
</project>
"""


def _testng_xml(class_names: list[str]) -> str:
    classes = "\n".join(f'      <class name="{PACKAGE}.{c}"/>' for c in class_names)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE suite SYSTEM "https://testng.org/testng-1.0.dtd">
<suite name="Testronaut" verbose="1">
  <test name="Generated API tests">
    <classes>
{classes}
    </classes>
  </test>
</suite>
"""


def _properties(base_url: str, placeholders: list[str], test_data: dict) -> str:
    lines = [
        "# Everything the OpenAPI spec cannot tell us.",
        "# Override any value at run time with -Dkey=value.",
        "",
        f"baseUrl={base_url}",
        "",
        "# Optional auth applied to every request.",
        f"authHeader={test_data.get('authHeader', '')}",
        f"authValue={test_data.get('authValue', '')}",
    ]
    extras = sorted(set(placeholders) - {"baseUrl", "authHeader", "authValue"})
    if extras:
        lines += ["", "# Referenced as ${...} by the generated tests."]
        lines += [f"{key}={test_data.get(key, '')}" for key in extras]
    for key, value in sorted(test_data.items()):
        if key not in extras and key not in {"authHeader", "authValue"}:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def _readme(run: GenerationRun, project: GeneratedProject) -> str:
    missing = "\n".join(f"- `{p}`" for p in sorted(set(project.placeholders))) or "- none"
    return f"""# Generated API tests — run #{run.id}

{project.test_count} test methods across {project.endpoint_count} endpoints, generated by
Testronaut from approved test cases.

## Before you run

Set the base URL of the system under test in `src/test/resources/testronaut.properties`:

```properties
baseUrl=http://localhost:8080
```

Values the tests still need:

{missing}

## Run

```bash
mvn test
```

Results land in `target/surefire-reports/`.

Override any value without editing the file:

```bash
mvn test -DbaseUrl=https://staging.example.com -DauthValue=$TOKEN
```

`testronaut.properties` is gitignored, because it is where a real credential
ends up. Inject secrets with `-D` in CI rather than committing them.

## Regenerating

These files are generated. Edit the test case in Testronaut and regenerate — edits
made here are lost on the next run.
"""


# ---------- entry point ----------

def generate_project(run_id: int, session: Session, target_dir: Path,
                     base_url: str = "http://localhost:8080",
                     test_data: Optional[dict] = None,
                     only_approved: bool = True) -> GeneratedProject:
    """Write a complete Maven project for one run's cases. Returns what was written."""
    run = session.get(GenerationRun, run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")

    query = select(TestCase).where(TestCase.run_id == run_id)
    if only_approved:
        query = query.where(TestCase.status == ArtifactStatus.APPROVED.value)
    cases = list(session.exec(query.order_by(TestCase.order_index)).all())
    if not cases:
        raise ValueError(
            "No approved test cases in this run. Approve cases at gate 2 first, "
            "or generate with only_approved=false.")

    requirements = {r.req_id: r for r in session.exec(
        select(Requirement).where(Requirement.run_id == run_id)).all()}

    root = Path(target_dir)
    package_dir = root / "src" / "test" / "java" / Path(*PACKAGE.split("."))
    package_dir.mkdir(parents=True, exist_ok=True)
    (root / "src" / "test" / "resources").mkdir(parents=True, exist_ok=True)

    project = GeneratedProject(root=root)
    for case in cases:
        _collect_placeholders(
            [case.path_params, case.query_params, case.headers, case.body], project.placeholders)

    by_endpoint: dict[str, list[TestCase]] = {}
    for case in cases:
        by_endpoint.setdefault(case.endpoint_key or f"{case.method} {case.path}", []).append(case)

    class_names = []
    for endpoint_key, endpoint_cases in sorted(by_endpoint.items()):
        name, source = _render_class(endpoint_key, endpoint_cases, requirements, project)
        (package_dir / f"{name}.java").write_text(source, encoding="utf-8")
        class_names.append(name)
        project.files.append(f"src/test/java/{PACKAGE.replace('.', '/')}/{name}.java")

    (package_dir / "Config.java").write_text(CONFIG_JAVA, encoding="utf-8")
    (root / "pom.xml").write_text(_pom(f"testronaut-run-{run_id}"), encoding="utf-8")
    (root / "testng.xml").write_text(_testng_xml(class_names), encoding="utf-8")
    (root / "src" / "test" / "resources" / "testronaut.properties").write_text(
        _properties(base_url, project.placeholders, test_data or {}), encoding="utf-8")
    # The properties file is the one place a real credential lands. Ship the
    # ignore rule with it, so "commit the generated project" cannot leak it.
    (root / ".gitignore").write_text(
        "target/\nsrc/test/resources/testronaut.properties\n", encoding="utf-8")

    project.test_count = len(cases)
    project.endpoint_count = len(by_endpoint)
    project.files += [f"src/test/java/{PACKAGE.replace('.', '/')}/Config.java",
                      "pom.xml", "testng.xml", "src/test/resources/testronaut.properties",
                      ".gitignore", "README.md"]
    (root / "README.md").write_text(_readme(run, project), encoding="utf-8")
    (root / "testronaut-map.json").write_text(
        json.dumps({"run_id": run_id, "methods": project.method_map}, indent=2), encoding="utf-8")
    project.files.append("testronaut-map.json")

    logger.info("codegen_done", run_id=run_id, tests=project.test_count,
                endpoints=project.endpoint_count,
                compiled=project.compiled_assertions, skipped=len(project.skipped_assertions))
    return project
