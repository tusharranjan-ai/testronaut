# Setting up a model provider

Testronaut needs one model provider. Pick one; you can switch per run in the UI,
and use a different one for the critic than for the generator.

| Provider | Cost | Key needed | Data leaves your machine |
|---|---|---|---|
| [Ollama](#ollama-default) | free | no | no |
| [OpenAI](#openai) | paid | `OPENAI_API_KEY` | yes |
| [Anthropic](#anthropic) | paid | `ANTHROPIC_API_KEY` | yes |
| [Claude Agent SDK](#claude-agent-sdk) | paid | `ANTHROPIC_API_KEY` | yes |
| [OmniRoute](#omniroute) | depends on routes | optional | depends on routes |

Keys go in `.env` (copy `.env.example`; `.env` is gitignored). Never commit a key,
paste it into an issue, or put it in a spec. After editing `.env`, restart:
`docker compose up -d` (Compose) or restart `uvicorn` (local).

## Ollama (default)

Runs models on your own machine: free, offline, nothing sent anywhere.

1. **Install** from <https://ollama.com/download> (macOS, Windows), or on Linux:
   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   ```
2. **Pull a model.** The default is `qwen2.5:14b` (roughly 9 GB; plan on 16 GB RAM
   or a GPU). A smaller model works but writes weaker requirements.
   ```bash
   ollama pull qwen2.5:14b
   ```
3. **Check it is running:** `curl http://localhost:11434/api/tags` should list the
   model. The desktop apps start the server for you; otherwise run `ollama serve`.
4. **Docker Compose on Linux only:** the backend container reaches Ollama through
   `host.docker.internal`, which cannot connect to a server bound to loopback
   (Ollama's default). Start it with `OLLAMA_HOST=0.0.0.0 ollama serve` (for the
   systemd service, `sudo systemctl edit ollama` and add
   `Environment="OLLAMA_HOST=0.0.0.0"`). That also exposes Ollama to your network,
   so firewall port 11434. Docker Desktop (macOS/Windows) and running the backend
   locally need none of this.

No `.env` change is needed. To use another host, set `OLLAMA_BASE_URL`.

## OpenAI

1. Create an account at <https://platform.openai.com> and add billing. The API is
   billed separately from a ChatGPT subscription.
2. Open <https://platform.openai.com/api-keys>, choose **Create new secret key**,
   and copy it now; it is shown once.
3. Add it to `.env`:
   ```
   OPENAI_API_KEY=sk-...
   ```
4. In the run configuration pick **OpenAI**. The default model is `gpt-4o`.

Set a monthly spend limit in the OpenAI billing settings before your first run.

## Anthropic

1. Create an account at <https://console.anthropic.com> and add credits. The API
   is billed separately from a Claude.ai subscription.
2. Go to **API keys**, choose **Create key**, and copy it; it is shown once.
3. Add it to `.env`:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
4. In the run configuration pick **Anthropic**. The default model is
   `claude-opus-5`; change it in the model field to use a cheaper one.

Set a spend limit in the Console before your first run.

## Claude Agent SDK

Same key as Anthropic (`ANTHROPIC_API_KEY`). The SDK runs the Claude Code CLI as a
subprocess, which the Docker image installs. Running locally you also need
`pip install claude-agent-sdk` (already in `backend/requirements.txt`) and the CLI
(`npm install -g @anthropic-ai/claude-code`). A Claude subscription login is not a
supported way to authenticate this provider; use an API key.

## OmniRoute

[OmniRoute](https://github.com/diegosouzapw/OmniRoute) is a third-party,
self-hosted gateway that exposes many model providers behind one OpenAI-compatible
endpoint. It is optional and not maintained by Testronaut.

1. **Start it** (Docker Compose):
   ```bash
   docker compose --profile omniroute up --build
   ```
   It listens on `http://localhost:20128` (localhost only).
2. **No key is needed** for its `auto/chat` route, which is Testronaut's default
   OmniRoute model. In the run configuration pick **OmniRoute**.
3. **Only if you secured your gateway with a key**, create that key in OmniRoute
   (see its documentation; the steps are its own and may change) and set:
   ```
   OMNIROUTE_API_KEY=...
   ```
4. Running OmniRoute somewhere else? Set
   `OMNIROUTE_BASE_URL=http://host:port/v1`.

Review OmniRoute's own docs before pointing it at your provider accounts: it will
hold those credentials, and your specs pass through it.

## Troubleshooting

- **Provider shows as unavailable in the UI:** the key is missing from `.env`, you
  did not restart after editing it, or (Ollama/OmniRoute) the server is not
  reachable from where the backend runs.
- **Ollama "connection refused" under Compose on Linux:** see step 4 above.
- **Slow or empty output on a local model:** lower `TESTRONAUT_MAX_CONCURRENCY` to
  `2`, or use a larger model.
