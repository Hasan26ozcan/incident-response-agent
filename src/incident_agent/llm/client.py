"""Provider-agnostic LLM client (Stage 3-5 foundation).

One narrow interface (``LLMClient.chat``) so agents never import a vendor SDK.
Groq, Cerebras, Mistral and Gemini all expose OpenAI-compatible endpoints, so a single
``OpenAICompatClient`` covers them; ``FallbackClient`` rolls over to the next
provider when one is rate-limited, which matters on free tiers.

Note (Groq docs): structured outputs and tool use cannot be combined in one
request. Agents therefore use tools during investigation and a separate
schema-constrained call for the final answer; ``chat`` enforces this.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from incident_agent.llm.schema import flatten_schema

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
PROVIDER_NAMES = ("groq", "cerebras", "mistral")
MISTRAL_DEFAULT_INTERVAL = 1.1  # free tier allows 1 request per second


class LLMUnavailable(RuntimeError):
    """Provider unreachable or rate-limited even after retries (transient: retrying later may work)."""


class LLMConfigError(RuntimeError):
    """Permanent request failure: bad/expired key, unknown model, or a request the provider rejects.

    Deliberately NOT an ``LLMUnavailable``: agents degrade gracefully on outages, but a config error
    must fail loudly so a wrong key never produces a run full of silent zero scores.
    """


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "content": self.content,
                "tool_calls": [{"id": t.id, "name": t.name, "arguments": t.arguments} for t in self.tool_calls],
                "usage": self.usage,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> LLMResponse:
        d = json.loads(raw)
        return cls(
            content=d["content"],
            tool_calls=[ToolCall(**t) for t in d["tool_calls"]],
            usage=d.get("usage", {}),
        )


class LLMClient(Protocol):
    model: str

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse: ...


class OpenAICompatClient:
    """Chat client for any OpenAI-compatible endpoint (Groq, Cerebras, ...)."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        max_retries: int = 5,
        http_client: Any = None,
        min_interval: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        from openai import OpenAI

        self.model = model
        self.min_interval = min_interval  # client-side pacing for providers with a requests-per-second cap
        self._sleep = sleep
        self._clock = clock
        self._pace_lock = threading.Lock()
        self._next_slot = 0.0
        self._json_object_mode = False  # flips to True once the provider rejects json_schema
        self._client = OpenAI(api_key=api_key, base_url=base_url, max_retries=max_retries, http_client=http_client)

    def _throttle(self) -> None:
        """Space requests at least ``min_interval`` apart; safe across threads (parallel workers)."""
        with self._pace_lock:
            now = self._clock()
            start = max(now, self._next_slot)
            self._next_slot = start + self.min_interval
        if start > now:
            self._sleep(start - now)

    def _create(self, kwargs: dict[str, Any]) -> Any:
        """One provider call with permanent/transient errors mapped to our exception types."""
        import openai

        self._throttle()
        try:
            return self._client.chat.completions.create(**kwargs)
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as exc:
            raise LLMUnavailable(f"{self.model}: {exc}") from exc
        except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
            raise LLMConfigError(f"{self.model}: API key rejected or not permitted ({exc})") from exc
        except openai.NotFoundError as exc:
            raise LLMConfigError(f"{self.model}: model or endpoint not found ({exc})") from exc

    def _schema_kwargs(self, kwargs: dict[str, Any], schema: type[BaseModel]) -> dict[str, Any]:
        flat = flatten_schema(schema.model_json_schema())
        if self._json_object_mode:
            note = "Reply with ONE JSON object matching this JSON schema, and nothing else:\n" + json.dumps(flat)
            msgs = [*kwargs["messages"]]
            if msgs and msgs[0].get("role") == "system":
                msgs[0] = {**msgs[0], "content": f"{msgs[0]['content']}\n\n{note}"}
            else:
                msgs.insert(0, {"role": "system", "content": note})
            return {**kwargs, "messages": msgs, "response_format": {"type": "json_object"}}
        return {
            **kwargs,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": flat,
                    "strict": False,  # best-effort; we validate with Pydantic and repair
                },
            },
        }

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: type[BaseModel] | None = None,
        temperature: float = 0.0,
    ) -> LLMResponse:
        import openai

        if tools and response_schema:
            raise ValueError("tools and response_schema cannot be combined in one request")
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_schema:
            try:
                resp = self._create(self._schema_kwargs(kwargs, response_schema))
            except LLMConfigError:
                raise
            except openai.BadRequestError:
                if self._json_object_mode:
                    raise
                resp = self._json_object_retry(kwargs, response_schema)
        else:
            try:
                resp = self._create(kwargs)
            except openai.BadRequestError as exc:
                raise LLMConfigError(f"{self.model}: request rejected by provider ({exc})") from exc
        if not getattr(resp, "choices", None):
            raise LLMUnavailable(f"{self.model}: provider returned an empty response (no choices)")
        msg = resp.choices[0].message
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"_raw": raw}
            if not isinstance(args, dict):
                args = {"_raw": raw}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        usage = {}
        if resp.usage:
            usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
        return LLMResponse(content=msg.content, tool_calls=calls, usage=usage)

    def _json_object_retry(self, kwargs: dict[str, Any], schema: type[BaseModel]) -> Any:
        """Provider rejected json_schema: remember that and retry in json_object mode."""
        import openai

        self._json_object_mode = True
        try:
            return self._create(self._schema_kwargs(kwargs, schema))
        except openai.BadRequestError as exc:
            raise LLMConfigError(f"{self.model}: provider rejects both json_schema and json_object ({exc})") from exc


class FallbackClient:
    """Try each client in order; move on when one is unavailable."""

    def __init__(self, clients: list[LLMClient]) -> None:
        if not clients:
            raise ValueError("FallbackClient needs at least one client")
        self.clients = clients
        self.model = clients[0].model

    def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        errors: list[Exception] = []
        for c in self.clients:
            try:
                return c.chat(messages, **kwargs)
            except (LLMUnavailable, LLMConfigError) as exc:
                errors.append(exc)
        detail = "; ".join(str(e) for e in errors)
        if any(isinstance(e, LLMUnavailable) for e in errors):
            raise LLMUnavailable(f"all providers unavailable: {detail}")
        raise LLMConfigError(f"all providers failed: {detail}")


_PLACEHOLDER = re.compile(r"your[_-]?(api[_-]?)?key|changeme|<.*>|^x{4,}$", re.IGNORECASE)


def _dotenv_candidates() -> list[Path]:
    """Find .env in the current directory or the repository root."""
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"]
    return list(dict.fromkeys(candidates))


def _load_dotenv(path: Path | None = None) -> None:
    """Load non-empty KEY=VALUE lines without overriding existing variables."""
    env_files = [path] if path is not None else _dotenv_candidates()
    for env_file in env_files:
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().removeprefix("export ").strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and value and not _PLACEHOLDER.search(value):
                os.environ.setdefault(key, value)


def _provider_client(name: str, explicit: bool) -> LLMClient | None:
    """Build one provider client from the environment; None when it is not configured.

    When the provider was requested explicitly (``LLM_PROVIDERS``) a missing key or model is an error, not a
    silent skip, so a typo can never make a run quietly use another provider.
    """
    env = os.environ
    key = env.get(f"{name.upper()}_API_KEY")
    model = env.get("GROQ_MODEL", "openai/gpt-oss-120b") if name == "groq" else env.get(f"{name.upper()}_MODEL")
    if not key or not model:
        if explicit:
            raise LLMConfigError(
                f"LLM_PROVIDERS names '{name}' but {name.upper()}_API_KEY or {name.upper()}_MODEL is not set"
            )
        return None
    if name == "groq":
        return OpenAICompatClient(model, key, GROQ_BASE_URL)
    if name == "cerebras":
        return OpenAICompatClient(model, key, CEREBRAS_BASE_URL)
    raw = env.get("MISTRAL_MIN_INTERVAL", str(MISTRAL_DEFAULT_INTERVAL))
    try:
        interval = float(raw)
    except ValueError as exc:
        raise LLMConfigError(f"MISTRAL_MIN_INTERVAL must be a number of seconds, got {raw!r}") from exc
    return OpenAICompatClient(model, key, MISTRAL_BASE_URL, min_interval=max(0.0, interval))


def build_client(cache_dir: str | None = None) -> LLMClient:
    """Build the default client from environment variables.

    Providers are tried in this order: Groq (GROQ_API_KEY, optional GROQ_MODEL), Cerebras (CEREBRAS_API_KEY +
    CEREBRAS_MODEL), Mistral (MISTRAL_API_KEY + MISTRAL_MODEL; paced to its 1 request/second free tier). More than
    one configured provider means automatic fallback. ``LLM_PROVIDERS`` (comma separated, e.g. ``mistral``)
    selects exactly those providers in that order; use it so an exhausted provider is not tried first. Model ids
    change often on free tiers, so verify them in each provider's console before relying on the defaults.
    """
    from incident_agent.llm.cache import CachedClient

    _load_dotenv()
    requested = [p.strip().lower() for p in os.environ.get("LLM_PROVIDERS", "").split(",") if p.strip()]
    unknown = [p for p in requested if p not in PROVIDER_NAMES]
    if unknown:
        raise LLMConfigError(f"LLM_PROVIDERS has unknown provider(s) {unknown}; valid: {', '.join(PROVIDER_NAMES)}")
    clients: list[LLMClient] = []
    for name in dict.fromkeys(requested or PROVIDER_NAMES):
        if (client := _provider_client(name, explicit=bool(requested))) is not None:
            clients.append(client)
    if not clients:
        searched = ", ".join(str(path) for path in _dotenv_candidates())
        raise LLMUnavailable(
            "GROQ_API_KEY not found (or CEREBRAS_API_KEY + CEREBRAS_MODEL, or MISTRAL_API_KEY + MISTRAL_MODEL). "
            "Add it to .env or set it in the environment. "
            f"Looked for .env in: {searched}. Check that the file is named exactly .env (not .env.txt) "
            "and the key value is not empty."
        )
    inner: LLMClient = clients[0] if len(clients) == 1 else FallbackClient(clients)
    return CachedClient(inner, cache_dir or os.environ.get("LLM_CACHE_DIR", ".llm_cache"))
