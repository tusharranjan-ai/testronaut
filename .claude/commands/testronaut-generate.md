# testronaut-generate

## Description
Generate requirements and test cases from an OpenAPI spec.

## Usage
```
/testronaut-generate <spec-path-or-url> [options]
```

## Options
- `--provider <ollama|anthropic|openai>` — LLM provider (default: ollama)
- `--model <model-id>` — Model to use (default: provider's default)
- `--categories <csv>` — Coverage categories (default: happy_path,negative,boundary,auth,security)
- `--endpoints <csv>` — Specific endpoints as "METHOD PATH" (default: all)
- `--output <json|xlsx>` — Export format (default: json)

## Behavior
1. Upload/parse the spec if not already in DB
2. Create a generation_run with selected options
3. Run the pipeline:
   - Stage 1: Generate requirements per endpoint
   - Stage 2: Review requirements (auto-revise mechanical issues)
   - Gate 1: Present requirements for human approval
   - Stage 3: Generate test cases from approved requirements
   - Stage 4: Review test cases (auto-revise mechanical issues)
   - Traceability check
   - Gate 2: Present test cases for human approval
4. Export approved artifacts

## Agent SDK Integration
Each stage spawns an Agent SDK session with restricted tools:
- Stage 1: `write_requirement` only
- Stage 2: `propose_requirement_change` only
- Stage 3: `write_test_case` only
- Stage 4: `propose_test_case_change`, `check_traceability` only

Hooks enforce ID formats, traceability, and spec compliance.
