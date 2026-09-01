# Docker CLI binary only — the backend shells out to the host's Docker
# Desktop daemon to sandbox generated test runs (see runner.py); it never
# runs its own daemon.
FROM docker:27-cli AS dockercli

FROM python:3.12-slim
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

# claude_agent_sdk provider runs the Claude Code CLI as a subprocess.
RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
# generator.py reads its methodology prompts from here at runtime — it's not
# dev tooling, the app is broken without it.
COPY .claude/skills/ .claude/skills/

EXPOSE 8000
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
