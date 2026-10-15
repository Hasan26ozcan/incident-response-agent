"""Provider selection (Groq / Cerebras / Mistral), explicit LLM_PROVIDERS, and client-side request pacing."""

from __future__ import annotations

import threading

import pytest

from incident_agent.llm import CachedClient, FallbackClient, build_client
from incident_agent.llm import client as client_mod
from incident_agent.llm.client import LLMConfigError, LLMUnavailable, OpenAICompatClient

ENV_KEYS = (
    "GROQ_API_KEY",
    "GROQ_MODEL",
    "CEREBRAS_API_KEY",
    "CEREBRAS_MODEL",
    "MISTRAL_API_KEY",
    "MISTRAL_MODEL",
    "MISTRAL_MIN_INTERVAL",
    "LLM_PROVIDERS",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setattr(client_mod, "_load_dotenv", lambda path=None: None)
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _inner(tmp_path):
    built = build_client(str(tmp_path))
    assert isinstance(built, CachedClient)
    return built.inner


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _paced(interval: float, t: FakeTime) -> OpenAICompatClient:
    return OpenAICompatClient(
        "m", "k", "https://example.invalid/v1", min_interval=interval, sleep=t.sleep, clock=t.clock
    )


class TestPacing:
    def test_back_to_back_calls_are_spaced_by_the_interval(self):
        t = FakeTime()
        c = _paced(1.1, t)
        for _ in range(3):
            c._throttle()
        assert t.slept == pytest.approx([1.1, 1.1])

    def test_no_sleep_when_enough_time_has_passed(self):
        t = FakeTime()
        c = _paced(1.0, t)
        c._throttle()
        t.now += 5
        c._throttle()
        assert t.slept == []

    def test_partial_wait_is_only_the_remaining_time(self):
        t = FakeTime()
        c = _paced(1.0, t)
        c._throttle()
        t.now += 0.4
        c._throttle()
        assert t.slept == pytest.approx([0.6])

    def test_zero_interval_never_sleeps(self):
        t = FakeTime()
        c = _paced(0.0, t)
        for _ in range(5):
            c._throttle()
        assert t.slept == []

    def test_every_provider_call_is_paced(self):
        c = OpenAICompatClient("m", "k", "https://example.invalid/v1", max_retries=0)
        calls: list[str] = []
        c._throttle = lambda: calls.append("throttle")  # type: ignore[method-assign]

        class _Boom:
            def create(self, **kw):
                raise RuntimeError("stop here")

        c._client.chat.completions = _Boom()  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            c._create({"model": "m", "messages": []})
        assert calls == ["throttle"]

    def test_parallel_threads_get_distinct_slots(self):
        t = FakeTime()
        c = _paced(1.0, t)
        slots: list[float] = []
        lock = threading.Lock()

        def go() -> None:
            c._throttle()
            with lock:
                slots.append(c._next_slot)

        threads = [threading.Thread(target=go) for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert len(set(slots)) == 6  # no two threads shared a slot


class TestSelection:
    def test_mistral_alone_uses_the_mistral_endpoint_with_default_pacing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MISTRAL_API_KEY", "m_real")
        monkeypatch.setenv("MISTRAL_MODEL", "mistral-large-latest")
        inner = _inner(tmp_path)
        assert isinstance(inner, OpenAICompatClient)
        assert inner.model == "mistral-large-latest"
        assert "api.mistral.ai" in str(inner._client.base_url)
        assert inner.min_interval == pytest.approx(client_mod.MISTRAL_DEFAULT_INTERVAL)

    def test_pacing_interval_is_configurable_and_negative_means_off(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MISTRAL_API_KEY", "k")
        monkeypatch.setenv("MISTRAL_MODEL", "m")
        monkeypatch.setenv("MISTRAL_MIN_INTERVAL", "2.5")
        assert _inner(tmp_path).min_interval == 2.5  # type: ignore[union-attr]
        monkeypatch.setenv("MISTRAL_MIN_INTERVAL", "-3")
        assert _inner(tmp_path).min_interval == 0.0  # type: ignore[union-attr]

    def test_bad_interval_is_a_config_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MISTRAL_API_KEY", "k")
        monkeypatch.setenv("MISTRAL_MODEL", "m")
        monkeypatch.setenv("MISTRAL_MIN_INTERVAL", "fast")
        with pytest.raises(LLMConfigError, match="MISTRAL_MIN_INTERVAL"):
            build_client(str(tmp_path))

    def test_mistral_key_without_model_is_skipped_by_default(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GROQ_API_KEY", "g")
        monkeypatch.setenv("MISTRAL_API_KEY", "m")
        inner = _inner(tmp_path)
        assert isinstance(inner, OpenAICompatClient) and "groq" in str(inner._client.base_url)

    def test_default_order_is_groq_cerebras_mistral_with_fallback(self, monkeypatch, tmp_path):
        for k, v in {
            "GROQ_API_KEY": "g",
            "CEREBRAS_API_KEY": "c",
            "CEREBRAS_MODEL": "cm",
            "MISTRAL_API_KEY": "m",
            "MISTRAL_MODEL": "mm",
        }.items():
            monkeypatch.setenv(k, v)
        inner = _inner(tmp_path)
        assert isinstance(inner, FallbackClient)
        assert [c.model for c in inner.clients] == ["openai/gpt-oss-120b", "cm", "mm"]  # type: ignore[attr-defined]

    def test_llm_providers_selects_only_the_named_provider_even_if_groq_is_configured(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GROQ_API_KEY", "g")
        monkeypatch.setenv("MISTRAL_API_KEY", "m")
        monkeypatch.setenv("MISTRAL_MODEL", "mm")
        monkeypatch.setenv("LLM_PROVIDERS", "mistral")
        inner = _inner(tmp_path)
        assert isinstance(inner, OpenAICompatClient) and inner.model == "mm"

    def test_llm_providers_sets_the_order(self, monkeypatch, tmp_path):
        for k, v in {"GROQ_API_KEY": "g", "MISTRAL_API_KEY": "m", "MISTRAL_MODEL": "mm"}.items():
            monkeypatch.setenv(k, v)
        monkeypatch.setenv("LLM_PROVIDERS", " Mistral , groq ")
        inner = _inner(tmp_path)
        assert isinstance(inner, FallbackClient)
        assert [c.model for c in inner.clients] == ["mm", "openai/gpt-oss-120b"]  # type: ignore[attr-defined]

    def test_explicit_provider_with_missing_model_fails_loudly(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GROQ_API_KEY", "g")
        monkeypatch.setenv("MISTRAL_API_KEY", "m")
        monkeypatch.setenv("LLM_PROVIDERS", "mistral")
        with pytest.raises(LLMConfigError, match="MISTRAL_MODEL"):
            build_client(str(tmp_path))

    def test_explicit_provider_with_missing_key_fails_loudly(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_PROVIDERS", "mistral")
        with pytest.raises(LLMConfigError, match="MISTRAL_API_KEY"):
            build_client(str(tmp_path))

    def test_unknown_provider_is_rejected_with_the_valid_names(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_PROVIDERS", "mistrall")
        with pytest.raises(LLMConfigError, match="groq, cerebras, mistral"):
            build_client(str(tmp_path))

    def test_nothing_configured_names_every_option(self, tmp_path):
        with pytest.raises(LLMUnavailable, match="MISTRAL_API_KEY"):
            build_client(str(tmp_path))
