"""
Provider probe + dispatch.

One code path for all raw-API providers (Ollama, OpenAI, Anthropic); the
per-provider difference is a URL, a header dict and two payload keys, so this is
a pair of if/elif branches rather than a class hierarchy.

Also owns "get JSON out of a language model", which is prompt + parse + one
retry (PLAN 5): local models wrap output in ``` fences no matter what the
prompt says.
"""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger()

PROVIDERS = ["ollama", "openai", "anthropic", "claude_agent_sdk", "omniroute"]

DEFAULT_MODELS = {
    "ollama": "qwen2.5:14b",
    "openai": "gpt-4o",
    "anthropic": "claude-opus-5",
    "claude_agent_sdk": "claude-opus-5",
    # auto/chat is the one OmniRoute route confirmed to work with zero setup;
    # reasoning/coding-tier auto/* routes need a provider connected in its own
    # dashboard first (Home → Provider Topology), or they fail outright.
    "omniroute": "auto/chat",
}

# Ollama's own default context is far below what the model supports, but the
# model's maximum is not free either: the KV cache for it has to fit in RAM
# alongside the weights. A 14b model at 32k needs more than an 18 GB machine has,
# and Ollama's failure mode is an empty HTTP body rather than an error, so the
# default here is a size that fits comfortably. Raise it if you have the memory.
def ollama_num_ctx() -> int:
    try:
        return max(2048, int(os.getenv("OLLAMA_NUM_CTX", "16384")))
    except ValueError:
        return 16384

DEFAULT_TIMEOUT = 300.0  # a 14b model on CPU is slow; this is per request


class LLMError(Exception):
    """Transport, HTTP, or unparseable-output failure from a provider."""


@dataclass
class ProviderInfo:
    name: str
    available: bool
    models: list[str] = field(default_factory=list)
    default_model: Optional[str] = None
    error: Optional[str] = None


# ---------- shared HTTP client ----------

_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")


def omniroute_base_url() -> str:
    return os.getenv("OMNIROUTE_BASE_URL", "http://localhost:20128/v1").rstrip("/")


def _api_key(provider: str) -> str:
    env = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
           "claude_agent_sdk": "ANTHROPIC_API_KEY"}[provider]
    key = os.getenv(env)
    if not key:
        raise LLMError(f"{env} is not set")
    return key


# ---------- completion ----------

@dataclass
class LLM:
    """A provider+model pair. Generation and review each hold one."""
    provider: str
    model: str

    async def complete(self, system: str, user: str, temperature: float = 0.2,
                       max_tokens: int = 4000, timeout: float = DEFAULT_TIMEOUT) -> str:
        provider = "anthropic" if self.provider == "claude_agent_sdk" else self.provider
        if provider not in ("ollama", "openai", "anthropic", "omniroute"):
            raise LLMError(f"Unknown provider: {self.provider}")

        if provider == "ollama":
            url = f"{ollama_base_url()}/api/chat"
            headers: dict[str, str] = {}
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens,
                            "num_ctx": ollama_num_ctx()},
            }
        elif provider == "openai":
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {_api_key('openai')}"}
            payload = {
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        elif provider == "omniroute":
            # OpenAI-compatible gateway (https://github.com/diegosouzapw/OmniRoute);
            # self-hosted, so no key is required for its zero-config "auto" model.
            url = f"{omniroute_base_url()}/chat/completions"
            headers = {}
            key = os.getenv("OMNIROUTE_API_KEY")
            if key:
                headers["Authorization"] = f"Bearer {key}"
            payload = {
                "model": self.model,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        else:
            url = "https://api.anthropic.com/v1/messages"
            headers = {"x-api-key": _api_key("anthropic"), "anthropic-version": "2023-06-01"}
            # No `temperature`: sampling parameters are rejected (400) on the
            # current Claude models, including the default claude-opus-5.
            payload = {
                "model": self.model,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "max_tokens": max_tokens,
            }

        try:
            resp = await _http().post(url, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            if not resp.content.strip():
                raise LLMError(
                    f"{provider} returned an empty body. For Ollama this is usually the "
                    f"context window failing to allocate — num_ctx is {ollama_num_ctx()} "
                    f"and its KV cache must fit in RAM alongside the model weights. "
                    f"Lower it with OLLAMA_NUM_CTX.")
            data = resp.json()
        except LLMError:
            raise
        except httpx.HTTPStatusError as e:
            body = e.response.text[:300]
            raise LLMError(f"{provider} returned HTTP {e.response.status_code}: {body}") from e
        except httpx.HTTPError as e:
            # str() on a connection error is frequently empty; the class name is
            # what tells you a socket died rather than the server saying no.
            detail = str(e).strip() or type(e).__name__
            if provider == "ollama" and isinstance(e, (httpx.ConnectError, httpx.ReadError,
                                                       httpx.RemoteProtocolError)):
                detail += (" — Ollama dropped the connection. It usually means the "
                           "model did not fit alongside the other in-flight requests; "
                           "lower TESTRONAUT_MAX_CONCURRENCY.")
            raise LLMError(f"{provider} request failed: {detail}") from e

        try:
            if provider == "ollama":
                content = data["message"]["content"]
                if not content.strip():
                    raise LLMError(
                        f"Ollama returned an empty response for {self.model}. This is "
                        f"usually the context window failing to allocate: num_ctx is "
                        f"{ollama_num_ctx()} and the KV cache must fit in RAM alongside "
                        f"the weights. Lower it with OLLAMA_NUM_CTX.")
                return content
            if provider in ("openai", "omniroute"):
                return data["choices"][0]["message"]["content"]
            return "".join(b["text"] for b in data["content"] if b.get("type") == "text")
        except LLMError:
            raise
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{provider} returned an unexpected response shape: {e}") from e

    async def complete_json(self, system: str, user: str, **kw) -> list[dict]:
        """
        Prompt -> parse -> one retry (PLAN 5). The retry feeds the parse error
        back so the model can repair its own output rather than re-rolling blind.
        """
        raw = await self.complete(system, user, **kw)
        try:
            return parse_items(raw)
        except LLMError as first:
            logger.warning("json_parse_retry", provider=self.provider, error=str(first))
            repair = (
                f"{user}\n\n---\nYour previous reply could not be parsed: {first}\n"
                "Reply again with ONLY the JSON array. No prose, no markdown fences.\n"
                "If the previous reply was cut off mid-value, it was too long: emit "
                "fewer items and keep every string short, but still return valid JSON."
            )
            raw = await self.complete(system, repair, **kw)
            try:
                return parse_items(raw)
            except LLMError as second:
                raise LLMError(f"Model did not return valid JSON after one retry: {second}") from second


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_items(raw: str) -> list[dict]:
    """
    Extract a JSON array of objects from model output, tolerating ``` fences and
    surrounding prose. Always returns a list, so callers never branch on shape.
    """
    if not raw or not raw.strip():
        raise LLMError("empty response")

    text = _FENCE.sub("", raw.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the outermost [...] or {...} in the text.
        for opener, closer in (("[", "]"), ("{", "}")):
            start, end = text.find(opener), text.rfind(closer)
            if start != -1 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                    break
                except json.JSONDecodeError:
                    continue
        else:
            raise LLMError(f"not valid JSON: {text[:200]}")

    if isinstance(data, dict):
        # A single object, or {"requirements": [...]} / {"test_cases": [...]}.
        for value in data.values():
            if isinstance(value, list) and all(isinstance(i, dict) for i in value):
                return value
        return [data]
    if isinstance(data, list):
        # An empty array is a legitimate answer, not a parse failure: the review
        # contract says `[]` means nothing is wrong. Treating it as an error made
        # every clean review burn a retry and then fail its endpoint.
        return [i for i in data if isinstance(i, dict)]
    raise LLMError(f"expected a JSON array or object, got {type(data).__name__}")


# ---------- probing ----------

async def probe_all_providers() -> list[ProviderInfo]:
    """Availability + model list per provider. Never raises."""
    return [await _probe(name) for name in PROVIDERS]


async def _probe(name: str) -> ProviderInfo:
    try:
        if name == "ollama":
            resp = await _http().get(f"{ollama_base_url()}/api/tags", timeout=10.0)
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
        elif name == "openai":
            resp = await _http().get("https://api.openai.com/v1/models",
                                     headers={"Authorization": f"Bearer {_api_key('openai')}"},
                                     timeout=15.0)
            resp.raise_for_status()
            models = sorted(m["id"] for m in resp.json().get("data", []))
        elif name == "omniroute":
            headers = {}
            key = os.getenv("OMNIROUTE_API_KEY")
            if key:
                headers["Authorization"] = f"Bearer {key}"
            resp = await _http().get(f"{omniroute_base_url()}/models", headers=headers, timeout=10.0)
            resp.raise_for_status()
            # OmniRoute's catalog is mostly raw per-vendor passthrough ids (350+,
            # including image models like aihorde/*) that mean nothing here; its
            # own "auto/*" routing aliases are the only ones worth offering.
            models = sorted(m["id"] for m in resp.json().get("data", [])
                            if m["id"].startswith("auto/"))
        else:
            # GET /v1/models is free; POST /v1/messages would bill every probe.
            resp = await _http().get("https://api.anthropic.com/v1/models",
                                     headers={"x-api-key": _api_key("anthropic"),
                                              "anthropic-version": "2023-06-01"},
                                     timeout=15.0)
            resp.raise_for_status()
            models = [m["id"] for m in resp.json().get("data", [])]
    except LLMError as e:                      # missing key: expected, not an error state
        return ProviderInfo(name=name, available=False, error=str(e))
    except Exception as e:
        logger.warning("provider_probe_failed", provider=name, error=str(e))
        return ProviderInfo(name=name, available=False, error=str(e))

    preferred = DEFAULT_MODELS.get(name)
    default = preferred if preferred in models else (models[0] if models else None)
    return ProviderInfo(name=name, available=bool(models), models=models, default_model=default)


def get_provider(provider: str, model: str) -> LLM:
    """Factory kept for call-site readability; validates the provider name."""
    if provider not in PROVIDERS:
        raise LLMError(f"Unknown provider: {provider}")
    if provider in ("openai", "anthropic", "claude_agent_sdk"):
        _api_key(provider)                     # fail fast, at run creation
    return LLM(provider=provider, model=model)
