# Contributing

Issues and pull requests are welcome.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cd frontend && npm install
```

## Before opening a PR

```bash
.venv/bin/pytest                      # backend tests; no API key or Ollama needed
cd frontend && npm run build          # type-checks and builds the UI
```

CI runs the same two commands.

## Guidelines

- Keep PRs small and focused; explain the *why* in the description.
- Methodology prompts live in `.claude/skills/*/SKILL.md`. Changing how
  requirements or cases are written is a Markdown edit, not a code change.
- Never commit API keys, tokens or private specs. Report vulnerabilities via
  [SECURITY.md](SECURITY.md), not in a public issue.
