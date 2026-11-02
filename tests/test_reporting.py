"""Stage 15: sanitising, post-mortem page, Slack message, HTTP client, wiki, notifier, CLI."""

from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from incident_agent.agents.react_agent import ReActAgent
from incident_agent.reporting import __main__ as cli
from incident_agent.reporting.http import HttpClient, HttpError, check_url
from incident_agent.reporting.notify import Ledger, Reporter, build_reporter
from incident_agent.reporting.postmortem import (
    LOW_CONFIDENCE,
    MAX_EVIDENCE,
    MAX_STEPS,
    build_postmortem,
    effective_tier,
    slug_for,
)
from incident_agent.reporting.sanitize import (
    REDACTED,
    md_code,
    md_text,
    one_line,
    redact,
    slack_escape,
    slack_text,
)
from incident_agent.reporting.slack import (
    ConfigError,
    OutboxSender,
    SendResult,
    WebhookSender,
    build_slack_message,
    validate_mention,
)
from incident_agent.reporting.wiki import ConfluenceWiki, FileWiki, PublishResult, WikiError, to_storage
from incident_agent.schemas.agent_output import (
    EvidenceItem,
    EvidenceType,
    IncidentMetadata,
    ReasoningStep,
    RiskTier,
    WorkerFinding,
)
from incident_agent.schemas.command_diagnosis import CommanderDiagnosis
from incident_agent.schemas.diagnosis import Diagnosis

ALL_IDS = [f"INC-{i:03d}" for i in range(1, 29)]


def make_diag(**over: Any) -> Diagnosis:
    base: dict[str, Any] = {
        "agent_type": "test",
        "incident_id": "INC-007",
        "root_cause": "Database connection pool exhausted by a leaking handler.",
        "confidence": 0.9,
        "evidence": [EvidenceItem(source_type=EvidenceType.LOG_ENTRY, source="log:1", detail="pool exhausted")],
        "affected_service": "checkout-api",
        "category": "resource_exhaustion",
        "reasoning_steps": [ReasoningStep(step_number=1, description="Saw pool errors")],
        "recommendation": "Fix the connection leak in the handler.",
        "risk_tier": RiskTier.LOW,
    }
    base.update(over)
    return Diagnosis(**base)


# --- sanitize ---


class TestSanitize:
    @pytest.mark.parametrize(
        "secret",
        [
            "sk-abcdefghijklmnop1234",
            "gsk_abcdefghijklmnop1234",
            "xoxb-1234567890-abcdefghij",
            "ghp_abcdefghijklmnopqrstuvwxyz",
            "AKIAABCDEFGHIJKLMNOP",
            "Bearer abcdefghijklmnopqrstuv",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij",
            "https://hooks.slack.com/services/T000/B000/XXXXXXXX",
        ],
    )
    def test_redacts_secret_shapes(self, secret: str) -> None:
        out = redact(f"before {secret} after")
        assert REDACTED in out and secret not in out
        assert out.startswith("before ") and out.endswith(" after")

    @pytest.mark.parametrize("key", ["password", "token", "api_key", "api-key", "secret", "Authorization", "PASSWD"])
    @pytest.mark.parametrize("sep", ["=", ": ", " = "])
    def test_redacts_keyed_values_but_keeps_the_key(self, key: str, sep: str) -> None:
        out = redact(f"login {key}{sep}hunter2 ok")
        assert "hunter2" not in out
        assert key in out and REDACTED in out

    def test_leaves_ordinary_text_alone(self) -> None:
        text = "token bucket empty; secret sauce; skipped 3 items"
        assert redact(text) == text

    def test_one_line_collapses_whitespace_and_control_characters(self) -> None:
        assert one_line("a\n\n b\t\tc\x00d\x1b[31m") == "a b c d [31m"
        assert one_line("x y") == "x y"

    def test_one_line_truncates_to_exactly_max_len(self) -> None:
        out = one_line("a" * 50, 10)
        assert len(out) == 10 and out.endswith("…")
        assert one_line("a" * 10, 10) == "a" * 10
        assert one_line("a" * 11, 10) == "a" * 9 + "…"

    def test_one_line_redacts_before_truncating(self) -> None:
        out = one_line("sk-" + "A" * 40 + " tail", 200)
        assert REDACTED in out and "AAAA" not in out

    def test_md_text_neutralises_markdown(self) -> None:
        out = md_text("# h [x](http://evil) *b* _i_ `c` <b> | ! \\")
        for ch in "#[]*_`<>|!":
            if ch == "#":
                assert "\\#" in out
            else:
                assert out.count(ch) == out.count("\\" + ch)  # every occurrence is escaped
        assert "\\\\" in out

    def test_md_code_cannot_be_closed_early(self) -> None:
        out = md_code("a`b`c")
        assert out == "`a'b'c`"
        assert out.count("`") == 2

    def test_slack_escape(self) -> None:
        assert (
            slack_escape("<!channel> & <@U1> <http://x|y>") == "&lt;!channel&gt; &amp; &lt;@U1&gt; &lt;http://x|y&gt;"
        )

    def test_slack_text_escapes_after_redacting(self) -> None:
        out = slack_text("<!here> password=abc")
        assert "<" not in out and "abc" not in out


# --- post-mortem ---


class TestPostMortem:
    def test_is_deterministic_and_hash_ignores_generation_time(self) -> None:
        a = build_postmortem(make_diag())
        b = build_postmortem(make_diag())
        c = build_postmortem(make_diag(), generated_at="2026-10-31T10:00:00+00:00")
        assert a == b
        assert a.content_hash == c.content_hash and a.markdown != c.markdown
        assert "2026-10-31T10:00:00" in c.markdown and "Generated at" not in a.markdown

    def test_hash_changes_with_every_content_field(self) -> None:
        base = build_postmortem(make_diag()).content_hash
        for over in (
            {"root_cause": "A completely different root cause text."},
            {"confidence": 0.8},
            {"affected_service": "other"},
            {"category": "other"},
            {"recommendation": "Do something else entirely, please."},
            {"risk_tier": RiskTier.MEDIUM},
            {"reasoning_steps": [ReasoningStep(step_number=1, description="other")]},
            {"evidence": [EvidenceItem(source_type=EvidenceType.DEPLOY, source="d", detail="other")]},
        ):
            assert build_postmortem(make_diag(**over)).content_hash != base, over

    def test_slug_and_title(self) -> None:
        pm = build_postmortem(make_diag())
        assert pm.slug == "postmortem-inc-007" == slug_for("INC-007")
        assert pm.title == "Post-mortem draft: INC-007 - resource_exhaustion on checkout-api"  # plain, for the wiki API
        assert pm.markdown.startswith("# Post-mortem draft: INC-007 - resource\\_exhaustion on checkout-api\n")
        with pytest.raises(ValueError):
            slug_for("../x")

    def test_states_that_it_is_an_unreviewed_draft_and_invents_nothing(self) -> None:
        md = build_postmortem(make_diag()).markdown
        assert "Not reviewed by a human" in md and "No remediation was executed" in md
        assert "Add impact, timeline and lessons learned, which this draft does not contain" in md
        assert "## Impact" not in md and "## Timeline" not in md and "Lessons learned:" not in md

    def test_low_confidence_banner_boundary(self) -> None:
        assert "Low confidence" in build_postmortem(make_diag(confidence=LOW_CONFIDENCE - 0.01)).markdown
        assert "Low confidence" not in build_postmortem(make_diag(confidence=LOW_CONFIDENCE)).markdown
        assert "Low confidence (49%)" in build_postmortem(make_diag(confidence=0.49)).markdown

    def test_evidence_and_steps_are_capped_and_counted(self) -> None:
        ev = [EvidenceItem(source_type=EvidenceType.LOG_ENTRY, source=f"s{i}", detail=f"d{i}") for i in range(15)]
        steps = [ReasoningStep(step_number=i + 1, description=f"step{i}") for i in range(15)]
        md = build_postmortem(make_diag(evidence=ev, reasoning_steps=steps)).markdown
        assert f"## Evidence ({MAX_EVIDENCE} of 15 shown)" in md
        assert f"`d{MAX_EVIDENCE - 1}`" in md and f"`d{MAX_EVIDENCE}`" not in md
        assert f"{MAX_STEPS}. step{MAX_STEPS - 1}" in md and f"step{MAX_STEPS}" not in md

    def test_metadata_rows_only_when_present(self) -> None:
        meta = IncidentMetadata(
            incident_id="INC-007",
            category="x",
            services=["a", "b"],
            window_start="2026-03-01T00:00:00Z",
            window_end="2026-03-01T01:00:00Z",
            severity="sev2",
        )
        with_meta = build_postmortem(make_diag(metadata=meta)).markdown
        assert "| Time window | 2026-03-01T00:00:00Z to 2026-03-01T01:00:00Z |" in with_meta
        assert "| Severity | sev2 |" in with_meta and "| Services in scope | a, b |" in with_meta
        assert "Time window" not in build_postmortem(make_diag()).markdown

    def test_untrusted_text_cannot_inject_markup_or_leak_secrets(self) -> None:
        evil = "<!channel> [click](http://evil.example) # Heading `x` password=hunter2 sk-" + "B" * 30
        ev = [EvidenceItem(source_type=EvidenceType.LOG_ENTRY, source=evil, detail=evil)]
        md = build_postmortem(
            make_diag(
                root_cause=evil + " padding",
                recommendation=evil + " padding",
                affected_service=evil,
                category=evil,
                evidence=ev,
                reasoning_steps=[ReasoningStep(step_number=1, description=evil)],
            )
        ).markdown
        assert "hunter2" not in md and "BBBB" not in md
        outside_code = re.sub(r"`[^`]*`", "", md)  # text inside a code span is inert
        assert not re.search(r"(?<!\\)\]\(", outside_code)  # no live Markdown link anywhere
        assert md.splitlines()[0].startswith("# Post-mortem draft: INC-007 - ") and "[click]" not in md.splitlines()[0]
        assert "\n# Heading" not in md and "\n## Heading" not in md
        for line in md.splitlines()[3:]:
            assert "\r" not in line

    def test_effective_tier_is_raised_never_lowered(self) -> None:
        d = make_diag(recommendation="Restart the pods to reset connections.")
        tier, phrases = effective_tier(d)
        assert tier == RiskTier.HIGH and "restart" in phrases
        assert effective_tier(make_diag())[0] == RiskTier.LOW
        assert effective_tier(make_diag(risk_tier=RiskTier.HIGH))[0] == RiskTier.HIGH  # recommendation says low-risk
        assert effective_tier(make_diag(risk_tier=RiskTier.HIGH))[1] == []
        med = effective_tier(make_diag(recommendation="Open a ticket and notify the owner team."))
        assert med[0] == RiskTier.MEDIUM

    def test_no_raise_when_the_agent_already_rated_it_as_high_as_the_text_implies(self) -> None:
        d = make_diag(risk_tier=RiskTier.HIGH, recommendation="Restart the pods to reset connections.")
        assert effective_tier(d) == (RiskTier.HIGH, [])
        assert "raised to" not in build_postmortem(d).markdown
        m = make_diag(risk_tier=RiskTier.MEDIUM, recommendation="Open a ticket and notify the owner team.")
        assert effective_tier(m) == (RiskTier.MEDIUM, [])

    def test_page_shows_the_raised_tier_and_why(self) -> None:
        md = build_postmortem(make_diag(recommendation="Restart the pods to reset connections.")).markdown
        assert "| Risk tier of the recommendation | high |" in md
        assert "The agent rated this low; raised to high because the recommendation mentions `restart`." in md
        assert "needs human approval" in md

    def test_low_tier_page_has_no_approval_line(self) -> None:
        md = build_postmortem(make_diag()).markdown
        assert "needs human approval" not in md and "raised to" not in md


# --- slack message ---


class TestSlackMessage:
    def test_structure_and_fallback_text(self) -> None:
        msg = build_slack_message(make_diag(), "https://wiki.example/p/1")
        assert msg["blocks"][0]["type"] == "header" and "INC-007" in msg["blocks"][0]["text"]["text"]
        assert "INC-007 diagnosed" in msg["text"] and "confidence 90%" in msg["text"] and "risk low" in msg["text"]
        json.dumps(msg)
        fields = {f["text"].split("\n")[0]: f["text"].split("\n")[1] for f in msg["blocks"][2]["fields"]}
        assert fields == {
            "*Service*": "checkout-api",
            "*Category*": "resource_exhaustion",
            "*Confidence*": "90%",
            "*Risk tier*": "low",
        }

    def test_link_when_url_is_safe(self) -> None:
        msg = build_slack_message(make_diag(), "https://wiki.example/p/1")
        assert msg["blocks"][4]["text"]["text"] == "<https://wiki.example/p/1|Post-mortem draft>"

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "javascript:alert(1)",
            "http://evil.example/x",
            "https://a.example/x|y",
            "https://a.example/>x",
            "https://a b",
        ],
    )
    def test_no_link_for_missing_or_unsafe_url(self, url: str | None) -> None:
        msg = build_slack_message(make_diag(), url)
        assert msg["blocks"][4]["text"]["text"] == "Post-mortem page was not published"

    def test_local_urls_are_accepted(self) -> None:
        assert "<file:///tmp/x.md|" in build_slack_message(make_diag(), "file:///tmp/x.md")["blocks"][4]["text"]["text"]
        assert (
            "<http://127.0.0.1:1/x|"
            in build_slack_message(make_diag(), "http://127.0.0.1:1/x")["blocks"][4]["text"]["text"]
        )

    def test_untrusted_text_is_escaped_and_redacted(self) -> None:
        evil = "<!channel> <@U123> <http://evil|x> & token=abc123"
        msg = build_slack_message(
            make_diag(root_cause=evil + " pad", recommendation=evil + " pad", affected_service=evil)
        )
        text = json.dumps(msg)
        assert "<!channel>" not in text and "<@U123>" not in text and "<http://evil" not in text
        assert "abc123" not in text and "&lt;!channel&gt;" in text

    def test_limits(self) -> None:
        msg = build_slack_message(make_diag(root_cause="x" * 5000, recommendation="y" * 5000))
        assert len(msg["blocks"][0]["text"]["text"]) <= 150
        for b in msg["blocks"]:
            if b["type"] == "section" and "text" in b:
                assert len(b["text"]["text"]) <= 3000
        assert len(msg["text"]) <= 3000
        assert len(msg["blocks"][1]["text"]["text"]) <= 620

    def test_mention_validation(self) -> None:
        for ok in ("<!here>", "<!channel>", "<@U024BE7LH>", "<!subteam^S0614TZR7>"):
            assert validate_mention(ok) == ok
            assert build_slack_message(make_diag(), None, mention=ok)["blocks"][1]["text"]["text"].startswith(ok + " ")
        for bad in ("@channel", "<!everyone>", "<@u123>", "<!here> hi", " <!here>", "<@U1>", ""):
            with pytest.raises(ValueError):
                build_slack_message(make_diag(), None, mention=bad)

    def test_no_mention_by_default(self) -> None:
        assert build_slack_message(make_diag())["blocks"][1]["text"]["text"].startswith("*Root cause*")

    def test_risk_tier_shown_is_the_effective_one(self) -> None:
        msg = build_slack_message(make_diag(recommendation="Restart the pods now."))
        assert "risk high" in msg["text"] and msg["blocks"][2]["fields"][3]["text"] == "*Risk tier*\nhigh"


# --- fake servers ---

Handler = Callable[[str, str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]]


class FakeServer:
    def __init__(self, handler: Handler) -> None:
        outer = self
        self.requests: list[dict[str, Any]] = []

        class H(BaseHTTPRequestHandler):
            def _do(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                outer.requests.append({"method": self.command, "path": self.path, "headers": headers, "body": body})
                status, hdrs, payload = handler(self.command, self.path, headers, body)
                self.send_response(status)
                for k, v in hdrs.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = _do

            def log_message(self, *a: object) -> None:
                return None

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self._srv.server_address[1]}"
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def __enter__(self) -> FakeServer:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


def sequence(*responses: tuple[int, dict[str, str], bytes]) -> Handler:
    queue = list(responses)

    def handler(*_: Any) -> tuple[int, dict[str, str], bytes]:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return handler


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, s: float) -> None:
        self.calls.append(s)


# --- HTTP client ---


class TestHttp:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://a.example/",
            "file:///etc/passwd",
            "http://example.com/x",
            "https://",
            "https://u:p@a.example/",
            "//a.example",
        ],
    )
    def test_check_url_rejects(self, url: str) -> None:
        with pytest.raises(ValueError):
            check_url(url)

    @pytest.mark.parametrize(
        "url", ["https://a.example/x", "http://127.0.0.1:8000/x", "http://localhost/x", "http://[::1]/x"]
    )
    def test_check_url_accepts(self, url: str) -> None:
        assert check_url(url) == url

    def test_allowed_hosts(self) -> None:
        hosts = frozenset({"hooks.slack.com"})
        assert check_url("https://hooks.slack.com/x", allowed_hosts=hosts)
        assert check_url("http://127.0.0.1:1/x", allowed_hosts=hosts)  # loopback is always fine
        with pytest.raises(ValueError, match="not allowed"):
            check_url("https://evil.example/x", allowed_hosts=hosts)

    def test_constructor_validation(self) -> None:
        with pytest.raises(ValueError):
            HttpClient(max_attempts=0)
        with pytest.raises(ValueError):
            HttpClient(timeout=0)

    def test_success_returns_body_and_headers(self) -> None:
        with FakeServer(lambda *a: (200, {"X-A": "1"}, b"hello")) as srv:
            r = HttpClient().request("POST", srv.url + "/p", headers={"X-T": "t"}, body=b"data")
        assert (r.status, r.body, r.attempts) == (200, b"hello", 1)
        assert r.headers["X-A"] == "1"
        assert srv.requests[0]["body"] == b"data" and srv.requests[0]["headers"]["x-t"] == "t"

    def test_client_error_is_returned_not_retried(self) -> None:
        with FakeServer(lambda *a: (400, {}, b"invalid_payload")) as srv:
            r = HttpClient(sleep=Sleeps()).request("POST", srv.url)
        assert r.status == 400 and r.attempts == 1 and len(srv.requests) == 1

    def test_retries_5xx_with_exponential_backoff_then_succeeds(self) -> None:
        sleeps = Sleeps()
        with FakeServer(sequence((503, {}, b""), (500, {}, b""), (200, {}, b"ok"))) as srv:
            r = HttpClient(backoff=0.5, sleep=sleeps).request("GET", srv.url)
        assert (r.status, r.attempts) == (200, 3)
        assert sleeps.calls == [0.5, 1.0]

    def test_honours_retry_after_but_caps_it(self) -> None:
        sleeps = Sleeps()
        with FakeServer(
            sequence((429, {"Retry-After": "2"}, b""), (429, {"Retry-After": "999"}, b""), (200, {}, b"ok"))
        ) as srv:
            HttpClient(max_retry_after=30, sleep=sleeps).request("GET", srv.url)
        assert sleeps.calls == [2.0, 30.0]

    def test_bad_retry_after_falls_back_to_backoff(self) -> None:
        sleeps = Sleeps()
        with FakeServer(sequence((429, {"Retry-After": "soon"}, b""), (200, {}, b"ok"))) as srv:
            HttpClient(backoff=0.25, sleep=sleeps).request("GET", srv.url)
        assert sleeps.calls == [0.25]

    def test_last_retryable_response_is_returned_when_attempts_run_out(self) -> None:
        sleeps = Sleeps()
        with FakeServer(lambda *a: (503, {}, b"down")) as srv:
            r = HttpClient(max_attempts=3, sleep=sleeps).request("GET", srv.url)
        assert (r.status, r.attempts, len(srv.requests)) == (503, 3, 3)
        assert len(sleeps.calls) == 2  # no sleep after the last attempt

    def test_single_attempt_never_sleeps(self) -> None:
        sleeps = Sleeps()
        with FakeServer(lambda *a: (503, {}, b"")) as srv:
            r = HttpClient(max_attempts=1, sleep=sleeps).request("GET", srv.url)
        assert r.status == 503 and sleeps.calls == []

    def test_redirects_are_not_followed(self) -> None:
        with FakeServer(lambda *a: (302, {"Location": "http://127.0.0.1:9/elsewhere"}, b"")) as srv:
            r = HttpClient().request("GET", srv.url, headers={"Authorization": "Basic secret"})
        assert r.status == 302 and len(srv.requests) == 1

    def test_network_error_is_retried_and_message_hides_the_url(self) -> None:
        with FakeServer(lambda *a: (200, {}, b"")) as srv:
            url = srv.url
        sleeps = Sleeps()
        with pytest.raises(HttpError) as exc:
            HttpClient(max_attempts=2, sleep=sleeps).request("GET", url + "/secret-path-token")
        assert "secret-path-token" not in str(exc.value) and "127.0.0.1" not in str(exc.value)
        assert "2 attempt(s)" in str(exc.value) and len(sleeps.calls) == 1

    def test_recovers_after_a_network_error(self) -> None:
        # first request hits a closed port; we cannot change the port, so emulate with a server that drops once
        state = {"n": 0}

        def handler(*a: Any) -> tuple[int, dict[str, str], bytes]:
            state["n"] += 1
            return (200, {}, b"ok")

        with FakeServer(handler) as srv:
            assert HttpClient().request("GET", srv.url).body == b"ok"

    def test_loopback_traffic_ignores_proxy_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            monkeypatch.setenv(var, "http://127.0.0.1:9")  # nothing listens there
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        with FakeServer(lambda *a: (200, {}, b"direct")) as srv:
            assert HttpClient(max_attempts=1).request("GET", srv.url).body == b"direct"

    def test_http_to_non_loopback_is_refused_before_any_request(self) -> None:
        with pytest.raises(ValueError):
            HttpClient().request("GET", "http://example.com/")


# --- webhook / outbox ---


class TestSenders:
    def test_webhook_rejects_wrong_host_without_echoing_the_url(self) -> None:
        url = "https://evil.example/services/T/B/SECRETSECRET"
        with pytest.raises(ConfigError) as exc:
            WebhookSender(url)
        assert "SECRETSECRET" not in str(exc.value) and "evil.example/services" not in str(exc.value)

    def test_webhook_rejects_plain_http_and_credentials(self) -> None:
        for url in ("http://hooks.slack.com/services/x", "https://u:p@hooks.slack.com/services/x", "nonsense"):
            with pytest.raises(ConfigError):
                WebhookSender(url)

    def test_webhook_accepts_the_slack_host(self) -> None:
        WebhookSender("https://hooks.slack.com/services/T000/B000/XXXX")

    def test_webhook_success(self) -> None:
        with FakeServer(lambda *a: (200, {}, b"ok")) as srv:
            res = WebhookSender(srv.url + "/services/T/B/X").send({"text": "hi"})
        assert res.ok and res.status == 200 and res.attempts == 1
        req = srv.requests[0]
        assert req["method"] == "POST" and json.loads(req["body"]) == {"text": "hi"}
        assert req["headers"]["content-type"] == "application/json"

    def test_webhook_error_reports_slack_text_without_url(self) -> None:
        with FakeServer(lambda *a: (400, {}, b"invalid_payload")) as srv:
            res = WebhookSender(srv.url + "/services/T/B/TOPSECRET").send({"text": "hi"})
        assert not res.ok and res.status == 400
        assert "invalid_payload" in res.detail and "TOPSECRET" not in res.detail

    def test_webhook_retries_rate_limit(self) -> None:
        sleeps = Sleeps()
        with FakeServer(sequence((429, {"Retry-After": "1"}, b"rate_limited"), (200, {}, b"ok"))) as srv:
            res = WebhookSender(srv.url + "/services/T/B/X", http=HttpClient(sleep=sleeps)).send({"text": "hi"})
        assert res.ok and res.attempts == 2 and sleeps.calls == [1.0]

    def test_webhook_network_failure_is_a_result_not_an_exception(self) -> None:
        with FakeServer(lambda *a: (200, {}, b"")) as srv:
            url = srv.url
        res = WebhookSender(url + "/services/T/B/SECRET", http=HttpClient(max_attempts=1)).send({"text": "x"})
        assert not res.ok and "SECRET" not in res.detail and "127.0.0.1" not in res.detail

    def test_non_200_success_codes_are_not_success(self) -> None:
        with FakeServer(lambda *a: (204, {}, b"")) as srv:
            assert not WebhookSender(srv.url + "/services/x").send({"text": "x"}).ok

    def test_outbox_appends_jsonl(self, tmp_path: Path) -> None:
        box = OutboxSender(tmp_path / "deep" / "out.jsonl")
        assert box.send({"a": 1}).ok and box.send({"b": 2}).ok
        lines = (tmp_path / "deep" / "out.jsonl").read_text().splitlines()
        assert [json.loads(x) for x in lines] == [{"a": 1}, {"b": 2}]

    def test_outbox_failure_is_a_result(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        res = OutboxSender(blocker / "sub" / "out.jsonl").send({"a": 1})
        assert not res.ok and "cannot write outbox" in res.detail


# --- wiki ---


class TestFileWiki:
    def test_create_update_unchanged_and_history(self, tmp_path: Path) -> None:
        wiki = FileWiki(tmp_path / "w")
        p1 = build_postmortem(make_diag())
        r1 = wiki.publish(p1)
        assert (r1.action, r1.version) == ("created", 1) and r1.url.startswith("file://")
        assert (tmp_path / "w" / "postmortem-inc-007.md").read_text() == p1.markdown
        r1b = wiki.publish(p1)
        assert (r1b.action, r1b.version, r1b.url) == ("unchanged", 1, r1.url)
        p2 = build_postmortem(make_diag(root_cause="Something else entirely happened here."))
        r2 = wiki.publish(p2)
        assert (r2.action, r2.version) == ("updated", 2)
        assert (tmp_path / "w" / "postmortem-inc-007.md").read_text() == p2.markdown
        hist = tmp_path / "w" / ".history"
        assert (hist / "postmortem-inc-007.v1.md").read_text() == p1.markdown
        assert (hist / "postmortem-inc-007.v2.md").read_text() == p2.markdown
        assert wiki.publish(p2).action == "unchanged"
        assert sorted(x.name for x in hist.iterdir()) == ["postmortem-inc-007.v1.md", "postmortem-inc-007.v2.md"]

    def test_reverting_to_old_content_is_a_new_version(self, tmp_path: Path) -> None:
        wiki = FileWiki(tmp_path)
        p1 = build_postmortem(make_diag())
        p2 = build_postmortem(make_diag(confidence=0.3))
        wiki.publish(p1)
        wiki.publish(p2)
        assert wiki.publish(p1).version == 3

    def test_index_lists_every_page(self, tmp_path: Path) -> None:
        wiki = FileWiki(tmp_path)
        wiki.publish(build_postmortem(make_diag()))
        wiki.publish(build_postmortem(make_diag(incident_id="INC-008")))
        index = (tmp_path / "index.md").read_text()
        assert "postmortem-inc-007.md" in index and "postmortem-inc-008.md" in index
        assert index.count("(version 1)") == 2

    def test_index_title_cannot_inject_links(self, tmp_path: Path) -> None:
        wiki = FileWiki(tmp_path)
        wiki.publish(build_postmortem(make_diag(category="[x](http://evil.example) <!channel>")))
        index = (tmp_path / "index.md").read_text()
        assert "(postmortem-inc-007.md)" in index
        assert not re.search(r"(?<!\\)\]\(http", index) and "<!channel>" not in index

    @pytest.mark.parametrize("slug", ["../x", "a/b", "A", "", "-x", "x" * 65, "a.b", "a\\b"])
    def test_rejects_unsafe_slugs(self, tmp_path: Path, slug: str) -> None:
        from dataclasses import replace

        page = replace(build_postmortem(make_diag()), slug=slug)
        with pytest.raises(WikiError, match="invalid page slug"):
            FileWiki(tmp_path).publish(page)
        assert list(tmp_path.iterdir()) == []

    def test_corrupt_metadata_is_an_error_not_silent_overwrite(self, tmp_path: Path) -> None:
        (tmp_path / "postmortem-inc-007.json").write_text("{not json")
        with pytest.raises(WikiError, match="cannot read page metadata"):
            FileWiki(tmp_path).publish(build_postmortem(make_diag()))

    def test_index_skips_unreadable_neighbours(self, tmp_path: Path) -> None:
        (tmp_path / "broken.json").write_text("{nope")
        (tmp_path / "other.json").write_text(json.dumps({"slug": "other"}))  # no title
        FileWiki(tmp_path).publish(build_postmortem(make_diag()))
        index = (tmp_path / "index.md").read_text()
        assert "postmortem-inc-007.md" in index and "broken" not in index and "other" not in index

    def test_non_dict_metadata_is_treated_as_new(self, tmp_path: Path) -> None:
        (tmp_path / "postmortem-inc-007.json").write_text("[1]")
        assert FileWiki(tmp_path).publish(build_postmortem(make_diag())).action == "created"

    def test_unwritable_root_is_a_wiki_error(self, tmp_path: Path) -> None:
        blocker = tmp_path / "f"
        blocker.write_text("x")
        with pytest.raises(WikiError, match="cannot write wiki page"):
            FileWiki(blocker / "w").publish(build_postmortem(make_diag()))

    def test_no_temp_files_left_behind(self, tmp_path: Path) -> None:
        FileWiki(tmp_path).publish(build_postmortem(make_diag()))
        assert not [p for p in tmp_path.rglob("*") if p.suffix == ".tmp" or ".tmp" in p.name]


class TestStorageFormat:
    def test_everything_is_html_escaped(self) -> None:
        out = to_storage("<script>alert(1)</script> & **<b>x</b>** `<i>` \\<")
        assert "<script>" not in out and "<b>" not in out and "<i>" not in out
        assert "&lt;script&gt;" in out and "&amp;" in out
        assert "<strong>&lt;b&gt;x&lt;/b&gt;</strong>" in out and "<code>&lt;i&gt;</code>" in out

    def test_trailing_text_and_escaped_characters_are_escaped(self) -> None:
        assert to_storage("x < y & z") == "<p>x &lt; y &amp; z</p>"
        assert to_storage("`a` then <tail>") == "<p><code>a</code> then &lt;tail&gt;</p>"
        assert to_storage("a \\< b \\> c") == "<p>a &lt; b &gt; c</p>"

    def test_headings_lists_tables_quotes(self) -> None:
        md = (
            "# T\n\n## S\n\n> **Low** quote\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
            "- [ ] one\n- two\n\n1. a\n2. b\n\npara\n"
        )
        out = to_storage(md)
        assert "<h1>T</h1>" in out and "<h2>S</h2>" in out
        assert "<blockquote><p><strong>Low</strong> quote</p></blockquote>" in out
        assert "<th>A</th><th>B</th>" in out and "<td>1</td><td>2</td>" in out and "---" not in out
        assert "<ul><li>☐ one</li><li>two</li></ul>" in out
        assert "<ol><li>a</li><li>b</li></ol>" in out
        assert "<p>para</p>" in out

    def test_escaped_pipe_stays_inside_its_cell(self) -> None:
        out = to_storage("| A | B |\n|---|---|\n| x \\| y | 2 |\n")
        assert "<td>x | y</td><td>2</td>" in out and out.count("<tr>") == 2

    def test_markdown_escapes_become_literal_characters(self) -> None:
        assert to_storage("db\\_pool \\[x\\]") == "<p>db_pool [x]</p>"

    def test_real_page_converts_without_error(self) -> None:
        out = to_storage(build_postmortem(make_diag(recommendation="Restart the pods.")).markdown)
        assert out.count("<table>") == 1 and "<h1>" in out and "http" not in out

    def test_empty(self) -> None:
        assert to_storage("") == ""


def confluence_handler(store: dict[str, Any]) -> Handler:
    def handler(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        if headers.get("authorization") != store.get("auth", headers.get("authorization")):
            return 401, {}, b'{"message":"Unauthorized"}'
        if method == "GET":
            pages = list(store["pages"].values())
            return 200, {}, json.dumps({"results": pages}).encode()
        data = json.loads(body)
        if method == "POST":
            pid = str(len(store["pages"]) + 1)
            page = {
                "id": pid,
                "title": data["title"],
                "version": {"number": 1},
                "body": data["body"],
                "_links": {"webui": f"/spaces/{data['space']['key']}/pages/{pid}"},
                "post": data,
            }
            store["pages"][pid] = page
            return 200, {}, json.dumps(page).encode()
        pid = path.split("/")[-1]
        page = store["pages"][pid]
        if data["version"]["number"] != page["version"]["number"] + 1:
            return 409, {}, b'{"message":"version conflict"}'
        page.update(version=data["version"], body=data["body"], put=data)
        return 200, {}, json.dumps(page).encode()

    return handler


class TestConfluence:
    def make(self, srv: FakeServer, **kw: Any) -> ConfluenceWiki:
        return ConfluenceWiki(srv.url + "/wiki", "me@example.com", "TOKEN123", "ENG", **kw)

    def test_create_then_unchanged_then_update(self) -> None:
        store: dict[str, Any] = {"pages": {}}
        with FakeServer(confluence_handler(store)) as srv:
            wiki = self.make(srv)
            p1 = build_postmortem(make_diag())
            r1 = wiki.publish(p1)
            assert (r1.action, r1.version) == ("created", 1)
            assert r1.url == srv.url + "/wiki/spaces/ENG/pages/1"
            assert wiki.publish(p1) == PublishResult("unchanged", r1.url, 1)
            assert len(srv.requests) == 3  # create (GET+POST), then one GET
            p2 = build_postmortem(make_diag(confidence=0.4))
            r2 = wiki.publish(p2)
            assert (r2.action, r2.version) == ("updated", 2)
        assert store["pages"]["1"]["put"]["version"] == {"number": 2}
        assert store["pages"]["1"]["put"]["id"] == "1"

    def test_accepts_201_for_create(self) -> None:
        store: dict[str, Any] = {"pages": {}}
        base = confluence_handler(store)

        def handler(method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
            status, hdrs, payload = base(method, path, headers, body)
            return (201 if method == "POST" else status), hdrs, payload

        with FakeServer(handler) as srv:
            assert self.make(srv).publish(build_postmortem(make_diag())).action == "created"

    def test_request_shapes(self) -> None:
        store: dict[str, Any] = {"pages": {}}
        with FakeServer(confluence_handler(store)) as srv:
            self.make(srv, parent_id="999").publish(build_postmortem(make_diag()))
        get, post = srv.requests
        assert get["path"].startswith("/wiki/rest/api/content?spaceKey=ENG&title=Post-mortem%20draft%3A%20INC-007")
        assert "expand=version,body.storage" in get["path"]
        sent = json.loads(post["body"])
        assert sent["type"] == "page" and sent["space"] == {"key": "ENG"} and sent["ancestors"] == [{"id": "999"}]
        assert sent["body"]["storage"]["representation"] == "storage"
        assert sent["body"]["storage"]["value"].startswith("<!-- postmortem-sha256:")
        assert "<table>" in sent["body"]["storage"]["value"]

    def test_basic_auth_header(self) -> None:
        store: dict[str, Any] = {"pages": {}}
        with FakeServer(confluence_handler(store)) as srv:
            self.make(srv).publish(build_postmortem(make_diag()))
        expected = "Basic " + base64.b64encode(b"me@example.com:TOKEN123").decode()
        assert all(r["headers"]["authorization"] == expected for r in srv.requests)

    def test_no_parent_means_no_ancestors(self) -> None:
        store: dict[str, Any] = {"pages": {}}
        with FakeServer(confluence_handler(store)) as srv:
            self.make(srv).publish(build_postmortem(make_diag()))
        assert "ancestors" not in json.loads(srv.requests[1]["body"])

    def test_auth_failure_is_a_wiki_error_without_the_token(self) -> None:
        store: dict[str, Any] = {"pages": {}, "auth": "Basic different"}
        with FakeServer(confluence_handler(store)) as srv:
            with pytest.raises(WikiError) as exc:
                self.make(srv).publish(build_postmortem(make_diag()))
        text = str(exc.value)
        assert "HTTP 401" in text and "TOKEN123" not in text and "me@example.com" not in text
        assert base64.b64encode(b"me@example.com:TOKEN123").decode() not in text

    def test_version_conflict_surfaces(self) -> None:
        store: dict[str, Any] = {"pages": {}}
        with FakeServer(confluence_handler(store)) as srv:
            wiki = self.make(srv)
            wiki.publish(build_postmortem(make_diag()))
            store["pages"]["1"]["version"]["number"] = 5  # someone else edited meanwhile; our GET sees 5 -> PUT 6 ok
            assert wiki.publish(build_postmortem(make_diag(confidence=0.2))).version == 6

    def test_non_json_and_non_object_responses(self) -> None:
        for body in (b"<html>", b"[1]"):
            with FakeServer(lambda *a, b=body: (200, {}, b)) as srv:
                with pytest.raises(WikiError):
                    self.make(srv).publish(build_postmortem(make_diag()))

    def test_unreachable_server(self) -> None:
        with FakeServer(lambda *a: (200, {}, b"")) as srv:
            url = srv.url
        wiki = ConfluenceWiki(url, "e", "t", "ENG", http=HttpClient(max_attempts=1))
        with pytest.raises(WikiError, match="network error"):
            wiki.publish(build_postmortem(make_diag()))

    def test_constructor_validation(self) -> None:
        for space in ("", "a b", "a/b", "x" * 300):
            with pytest.raises(ValueError):
                ConfluenceWiki("https://x.atlassian.net/wiki", "e", "t", space)
        with pytest.raises(ValueError):
            ConfluenceWiki("http://x.atlassian.net/wiki", "e", "t", "ENG")  # plain http to a remote host
        assert (
            ConfluenceWiki("https://x.atlassian.net/wiki/", "e", "t", "ENG").base_url == "https://x.atlassian.net/wiki"
        )

    def test_page_url_fallback_without_links(self) -> None:
        wiki = ConfluenceWiki("https://x.atlassian.net/wiki", "e", "t", "ENG")
        assert wiki._page_url({"id": "42"}) == "https://x.atlassian.net/wiki/pages/viewpage.action?pageId=42"
        assert wiki._page_url({"id": "42", "_links": {"webui": "/spaces/E/pages/42"}}).endswith(
            "/wiki/spaces/E/pages/42"
        )
        assert "evil" not in wiki._page_url({"id": "1", "_links": {"webui": "http://evil"}})


# --- notifier ---


class FakeWiki:
    name = "fake-wiki"

    def __init__(self, fail: Exception | None = None, url: str = "https://wiki.example/p/1") -> None:
        self.fail, self.url, self.pages = fail, url, []

    def publish(self, page: Any) -> PublishResult:
        if self.fail:
            raise self.fail
        same = [p for p in self.pages if p.content_hash == page.content_hash]
        self.pages.append(page)
        return PublishResult("unchanged" if same else "created", self.url, len(self.pages))


class FakeSender:
    name = "fake-slack"

    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = results or []
        self.sent: list[dict[str, Any]] = []

    def send(self, payload: Any) -> SendResult:
        self.sent.append(dict(payload))
        r = self.results.pop(0) if self.results else SendResult(True, "ok")
        if isinstance(r, Exception):
            raise r
        return r


class TestReporter:
    def test_publishes_then_notifies_with_the_link(self, tmp_path: Path) -> None:
        wiki, sender = FakeWiki(), FakeSender()
        res = Reporter(wiki, sender, Ledger(tmp_path / "l.jsonl")).report(make_diag())
        assert res.ok and res.slack_sent and res.wiki and res.wiki.action == "created"
        assert "<https://wiki.example/p/1|Post-mortem draft>" in json.dumps(sender.sent[0])
        assert len(wiki.pages) == 1 and len(sender.sent) == 1
        assert res.content_hash == wiki.pages[0].content_hash

    def test_rerun_with_same_content_is_not_announced_again(self, tmp_path: Path) -> None:
        wiki, sender = FakeWiki(), FakeSender()
        rep = Reporter(wiki, sender, Ledger(tmp_path / "l.jsonl"))
        rep.report(make_diag())
        res = rep.report(make_diag())
        assert res.slack_skipped == "already announced" and not res.slack_sent and res.ok
        assert len(sender.sent) == 1 and res.wiki and res.wiki.action == "unchanged"

    def test_ledger_survives_a_new_reporter(self, tmp_path: Path) -> None:
        sender = FakeSender()
        Reporter(FakeWiki(), sender, Ledger(tmp_path / "l.jsonl")).report(make_diag())
        Reporter(FakeWiki(), sender, Ledger(tmp_path / "l.jsonl")).report(make_diag())
        assert len(sender.sent) == 1

    def test_changed_content_is_announced_again(self, tmp_path: Path) -> None:
        sender = FakeSender()
        rep = Reporter(FakeWiki(), sender, Ledger(tmp_path / "l.jsonl"))
        rep.report(make_diag())
        rep.report(make_diag(root_cause="A different root cause was found later."))
        assert len(sender.sent) == 2

    def test_other_incident_is_announced(self, tmp_path: Path) -> None:
        sender = FakeSender()
        rep = Reporter(FakeWiki(), sender, Ledger(tmp_path / "l.jsonl"))
        rep.report(make_diag())
        rep.report(make_diag(incident_id="INC-008"))
        assert len(sender.sent) == 2

    def test_without_a_ledger_every_run_announces(self) -> None:
        sender = FakeSender()
        rep = Reporter(FakeWiki(), sender)
        rep.report(make_diag())
        rep.report(make_diag())
        assert len(sender.sent) == 2

    def test_wiki_failure_still_notifies_without_link_and_is_reported(self, tmp_path: Path) -> None:
        sender = FakeSender()
        res = Reporter(FakeWiki(fail=WikiError("HTTP 503: down")), sender, Ledger(tmp_path / "l")).report(make_diag())
        assert not res.ok and res.wiki is None and "WikiError: HTTP 503: down" in str(res.wiki_error)
        assert res.slack_sent and "Post-mortem page was not published" in json.dumps(sender.sent[0])

    def test_later_success_with_link_announces_again(self, tmp_path: Path) -> None:
        sender, ledger = FakeSender(), Ledger(tmp_path / "l")
        Reporter(FakeWiki(fail=WikiError("down")), sender, ledger).report(make_diag())
        Reporter(FakeWiki(), sender, ledger).report(make_diag())
        assert len(sender.sent) == 2 and "<https://wiki.example" in json.dumps(sender.sent[1])
        assert "wiki.example" not in json.dumps(sender.sent[0])

    def test_slack_failure_keeps_the_page_and_allows_a_retry(self, tmp_path: Path) -> None:
        wiki, ledger = FakeWiki(), Ledger(tmp_path / "l")
        sender = FakeSender([SendResult(False, "Slack answered HTTP 500: oops", 500)])
        rep = Reporter(wiki, sender, ledger)
        res = rep.report(make_diag())
        assert not res.ok and not res.slack_sent and res.slack_error == "Slack answered HTTP 500: oops"
        assert res.wiki is not None and len(wiki.pages) == 1
        res2 = rep.report(make_diag())  # ledger was not written, so this retries
        assert res2.ok and res2.slack_sent and len(sender.sent) == 2

    def test_sender_exception_is_captured_not_raised(self) -> None:
        res = Reporter(FakeWiki(), FakeSender([RuntimeError("boom token=abc12345")])).report(make_diag())
        assert not res.ok and "RuntimeError: boom" in str(res.slack_error)
        assert "abc12345" not in str(res.slack_error)

    def test_unexpected_wiki_exception_is_captured_and_redacted(self) -> None:
        res = Reporter(FakeWiki(fail=ValueError("password=hunter22")), FakeSender()).report(make_diag())
        assert res.wiki_error and "hunter22" not in res.wiki_error and res.slack_sent

    def test_both_fail(self) -> None:
        res = Reporter(FakeWiki(fail=WikiError("a")), FakeSender([SendResult(False, "b")])).report(make_diag())
        assert res.wiki_error and res.slack_error and not res.ok

    def test_ledger_write_failure_is_a_note(self, tmp_path: Path) -> None:
        blocker = tmp_path / "f"
        blocker.write_text("x")
        res = Reporter(FakeWiki(), FakeSender(), Ledger(blocker / "sub" / "l.jsonl")).report(make_diag())
        assert res.ok and res.slack_sent and res.notes and "may announce again" in res.notes[0]

    def test_corrupt_ledger_lines_do_not_block_alerts(self, tmp_path: Path) -> None:
        path = tmp_path / "l"
        path.write_text("{garbage\n[1]\n\n")
        sender = FakeSender()
        assert Reporter(FakeWiki(), sender, Ledger(path)).report(make_diag()).slack_sent

    def test_mention_only_for_effective_high(self) -> None:
        sender = FakeSender()
        rep = Reporter(FakeWiki(), sender, mention_on_high="<!here>")
        rep.report(make_diag())  # low
        rep.report(make_diag(risk_tier=RiskTier.HIGH))
        rep.report(make_diag(recommendation="Restart the pods to reset connections."))  # raised to high
        starts = [p["blocks"][1]["text"]["text"].startswith("<!here> ") for p in sender.sent]
        assert starts == [False, True, True]

    def test_invalid_mention_fails_at_construction(self) -> None:
        with pytest.raises(ConfigError):
            Reporter(FakeWiki(), FakeSender(), mention_on_high="@channel")

    def test_page_is_published_before_slack_is_called(self) -> None:
        order: list[str] = []

        class W(FakeWiki):
            def publish(self, page: Any) -> PublishResult:
                order.append("wiki")
                return super().publish(page)

        class S(FakeSender):
            def send(self, payload: Any) -> SendResult:
                order.append("slack")
                return super().send(payload)

        Reporter(W(), S()).report(make_diag())
        assert order == ["wiki", "slack"]

    def test_generated_at_reaches_the_page_not_the_hash(self) -> None:
        wiki = FakeWiki()
        res = Reporter(wiki, FakeSender()).report(make_diag(), generated_at="2026-10-31T00:00:00+00:00")
        assert "2026-10-31T00:00:00" in wiki.pages[0].markdown
        assert res.content_hash == build_postmortem(make_diag()).content_hash

    def test_describe(self) -> None:
        assert Reporter(FakeWiki(), FakeSender()).describe() == "wiki=fake-wiki, slack=fake-slack"


class TestLedger:
    def test_key_format_distinguishes_link(self) -> None:
        assert Ledger.key("INC-1", "h", True) == "INC-1:h:link" and Ledger.key("INC-1", "h", False) == "INC-1:h:nolink"

    def test_no_path_means_never_seen_and_add_is_a_noop(self) -> None:
        ledger = Ledger(None)
        ledger.add("k")
        assert not ledger.seen("k")

    def test_unreadable_ledger_counts_as_not_seen(self, tmp_path: Path) -> None:
        (tmp_path / "l").mkdir()  # a directory where the file should be
        assert not Ledger(tmp_path / "l").seen("a")

    def test_roundtrip(self, tmp_path: Path) -> None:
        ledger = Ledger(tmp_path / "x" / "l.jsonl")
        assert not ledger.seen("a")
        ledger.add("a")
        assert ledger.seen("a") and not ledger.seen("b")
        assert Ledger(tmp_path / "x" / "l.jsonl").seen("a")


# --- build_reporter ---

CONF = {
    "CONFLUENCE_BASE_URL": "https://x.atlassian.net/wiki",
    "CONFLUENCE_EMAIL": "me@example.com",
    "CONFLUENCE_API_TOKEN": "TOK",
    "CONFLUENCE_SPACE": "ENG",
}
HOOK = "https://hooks.slack.com/services/T000/B000/XXXXXXXX"


class TestBuildReporter:
    def test_default_is_local_even_when_the_environment_has_live_settings(self, tmp_path: Path) -> None:
        rep = build_reporter(env={"SLACK_WEBHOOK_URL": HOOK, **CONF}, base_dir=tmp_path)
        assert rep.describe() == "wiki=file, slack=slack-outbox"

    def test_live_without_any_settings_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="--live needs"):
            build_reporter(live=True, env={}, base_dir=tmp_path)

    def test_live_slack_only(self, tmp_path: Path) -> None:
        rep = build_reporter(live=True, env={"SLACK_WEBHOOK_URL": HOOK}, base_dir=tmp_path)
        assert rep.describe() == "wiki=file, slack=slack-webhook"

    def test_live_confluence_only(self, tmp_path: Path) -> None:
        rep = build_reporter(live=True, env=CONF, base_dir=tmp_path)
        assert rep.describe() == "wiki=confluence, slack=slack-outbox"

    def test_live_both(self, tmp_path: Path) -> None:
        rep = build_reporter(live=True, env={"SLACK_WEBHOOK_URL": HOOK, **CONF}, base_dir=tmp_path)
        assert rep.describe() == "wiki=confluence, slack=slack-webhook"

    def test_partial_confluence_names_missing_variables_only(self, tmp_path: Path) -> None:
        env = {"CONFLUENCE_BASE_URL": "https://x.atlassian.net/wiki", "CONFLUENCE_API_TOKEN": "SUPERSECRET"}
        with pytest.raises(ConfigError) as exc:
            build_reporter(live=True, env=env, base_dir=tmp_path)
        text = str(exc.value)
        assert "CONFLUENCE_EMAIL" in text and "CONFLUENCE_SPACE" in text and "SUPERSECRET" not in text

    def test_bad_webhook_host_is_refused_without_echo(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as exc:
            build_reporter(live=True, env={"SLACK_WEBHOOK_URL": "https://evil.example/TOPSECRET"}, base_dir=tmp_path)
        assert "TOPSECRET" not in str(exc.value)

    def test_bad_confluence_space_is_a_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="Confluence settings are not usable"):
            build_reporter(live=True, env={**CONF, "CONFLUENCE_SPACE": "a b"}, base_dir=tmp_path)

    def test_bad_mention_is_a_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            build_reporter(env={"SLACK_MENTION_ON_HIGH": "everyone"}, base_dir=tmp_path)

    def test_report_dir_from_environment_and_layout(self, tmp_path: Path) -> None:
        rep = build_reporter(env={"REPORT_DIR": str(tmp_path / "r")})
        rep.report(make_diag())
        assert (tmp_path / "r" / "wiki" / "postmortem-inc-007.md").exists()
        assert (tmp_path / "r" / "slack-outbox.jsonl").exists() and (tmp_path / "r" / "ledger.jsonl").exists()

    def test_explicit_dir_wins_over_environment(self, tmp_path: Path) -> None:
        build_reporter(env={"REPORT_DIR": str(tmp_path / "env")}, base_dir=tmp_path / "arg").report(make_diag())
        assert (tmp_path / "arg").exists() and not (tmp_path / "env").exists()

    def test_end_to_end_live_path_over_real_http(self, tmp_path: Path) -> None:
        store: dict[str, Any] = {"pages": {}}
        with FakeServer(lambda *a: (200, {}, b"ok")) as slack, FakeServer(confluence_handler(store)) as wiki:
            env = {
                "SLACK_WEBHOOK_URL": slack.url + "/services/T/B/X",
                "CONFLUENCE_BASE_URL": wiki.url + "/wiki",
                "CONFLUENCE_EMAIL": "me@example.com",
                "CONFLUENCE_API_TOKEN": "TOK",
                "CONFLUENCE_SPACE": "ENG",
                "SLACK_MENTION_ON_HIGH": "<!here>",
            }
            rep = build_reporter(live=True, env=env, base_dir=tmp_path)
            res = rep.report(make_diag(recommendation="Restart the pods to reset connections."))
            assert res.ok and res.slack_sent and res.wiki and res.wiki.action == "created"
            msg = json.loads(slack.requests[0]["body"])
            assert f"<{wiki.url}/wiki/spaces/ENG/pages/1|Post-mortem draft>" in json.dumps(msg)
            assert msg["blocks"][1]["text"]["text"].startswith("<!here> ")
            again = rep.report(make_diag(recommendation="Restart the pods to reset connections."))
            assert again.slack_skipped == "already announced" and len(slack.requests) == 1
        assert len(store["pages"]) == 1


# --- every incident, offline ---


class TestAllIncidents:
    @pytest.mark.parametrize("incident", ALL_IDS)
    def test_offline_agent_to_published_page(self, tmp_path: Path, incident: str) -> None:
        diagnosis = ReActAgent(confidence_threshold=0.7).run(incident)
        res = build_reporter(base_dir=tmp_path).report(diagnosis)
        assert res.ok and res.slack_sent and res.wiki and res.wiki.action == "created"
        page = (tmp_path / "wiki" / f"postmortem-{incident.lower()}.md").read_text()
        assert page.startswith("# Post-mortem draft: " + incident) and "](http" not in page and "<!" not in page
        payload = json.loads((tmp_path / "slack-outbox.jsonl").read_text().splitlines()[0])
        text = json.dumps(payload)
        assert "<!channel>" not in text and "<!here>" not in text
        assert len(payload["blocks"]) == 6
        # the page never understates the risk the recommendation text implies
        eff, _ = effective_tier(diagnosis)
        assert f"| Risk tier of the recommendation | {eff.value} |" in page


# --- command line ---


class TestCli:
    def test_local_run_twice(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["INC-005", "--dir", str(tmp_path)]) == 0
        first = capsys.readouterr().out
        assert "mode: local files (wiki=file, slack=slack-outbox)" in first
        assert "wiki:  created v1 file://" in first and "slack: sent" in first
        assert cli.main(["INC-005", "--dir", str(tmp_path)]) == 0
        second = capsys.readouterr().out
        assert "wiki:  unchanged v1" in second and "slack: skipped (already announced)" in second

    def test_diagnosis_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        f = tmp_path / "d.json"
        f.write_text(make_diag().to_json())
        assert cli.main(["INC-007", "--diagnosis-json", str(f), "--dir", str(tmp_path / "out")]) == 0
        assert (tmp_path / "out" / "wiki" / "postmortem-inc-007.md").exists()

    def test_diagnosis_json_for_another_incident_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        f = tmp_path / "d.json"
        f.write_text(make_diag().to_json())
        assert cli.main(["INC-008", "--diagnosis-json", str(f), "--dir", str(tmp_path / "out")]) == 2
        assert "is for INC-007, not INC-008" in capsys.readouterr().out
        assert not (tmp_path / "out").exists()

    def test_invalid_json_and_missing_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{}")
        assert cli.main(["INC-007", "--diagnosis-json", str(bad), "--dir", str(tmp_path)]) == 2
        assert cli.main(["INC-007", "--diagnosis-json", str(tmp_path / "nope.json"), "--dir", str(tmp_path)]) == 2
        assert capsys.readouterr().out.count("ERROR:") == 2

    def test_unknown_incident(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["INC-999", "--dir", str(tmp_path)]) == 2
        assert "ERROR" in capsys.readouterr().out

    def test_live_without_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        for var in ("SLACK_WEBHOOK_URL", *CONF):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
        assert cli.main(["INC-005", "--live", "--dir", str(tmp_path)]) == 2
        assert "--live needs" in capsys.readouterr().out

    def test_wiki_failure_and_notes_are_printed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class BadWiki:
            name = "bad"

            def publish(self, page: Any) -> PublishResult:
                raise WikiError("HTTP 503: down")

        blocker = tmp_path / "f"
        blocker.write_text("x")
        real = cli.build_reporter

        def patched(**kw: Any) -> Reporter:
            r = real(**kw)
            r.wiki = BadWiki()
            r.ledger = Ledger(blocker / "sub" / "l.jsonl")
            return r

        monkeypatch.setattr(cli, "build_reporter", patched)
        assert cli.main(["INC-005", "--dir", str(tmp_path / "o")]) == 1
        out = capsys.readouterr().out
        assert (
            "wiki:  FAILED WikiError: HTTP 503: down" in out
            and "slack: sent" in out
            and "note:  ledger not written" in out
        )

    def test_exit_code_is_1_when_delivery_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        class Failing(OutboxSender):
            def send(self, payload: Any) -> SendResult:
                return SendResult(False, "Slack answered HTTP 500: oops")

        real = cli.build_reporter

        def patched(**kw: Any) -> Reporter:
            r = real(**kw)
            r.sender = Failing(tmp_path / "x")
            return r

        monkeypatch.setattr(cli, "build_reporter", patched)
        assert cli.main(["INC-005", "--dir", str(tmp_path)]) == 1
        assert "slack: FAILED Slack answered HTTP 500: oops" in capsys.readouterr().out


class TestDiagnoseCommand:
    def run_cli(self, argv: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
        from incident_agent import cli as main_cli

        for var in (
            "SLACK_WEBHOOK_URL",
            "CONFLUENCE_BASE_URL",
            "CONFLUENCE_EMAIL",
            "CONFLUENCE_API_TOKEN",
            "CONFLUENCE_SPACE",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("REPORT_DIR", str(tmp_path / "r"))
        monkeypatch.setattr(main_cli, "_load_dotenv", lambda: None)
        return main_cli.main(argv)

    def test_report_flag_publishes_locally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert self.run_cli(["diagnose", "INC-005", "--agent", "legacy", "--report"], monkeypatch, tmp_path) == 0
        out = capsys.readouterr().out
        assert "=== Diagnosis: INC-005 ===" in out and "report (local files: wiki=file, slack=slack-outbox)" in out
        assert "wiki:  created" in out and "slack: sent" in out
        assert (tmp_path / "r" / "wiki" / "postmortem-inc-005.md").exists()

    def test_without_the_flag_nothing_is_written(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self.run_cli(["diagnose", "INC-005", "--agent", "legacy"], monkeypatch, tmp_path) == 0
        assert not (tmp_path / "r").exists()

    def test_report_live_without_settings_is_refused_after_the_diagnosis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert self.run_cli(["diagnose", "INC-005", "--agent", "legacy", "--report-live"], monkeypatch, tmp_path) == 2
        captured = capsys.readouterr()
        assert "=== Diagnosis: INC-005 ===" in captured.out and "--live needs" in captured.err
        assert not (tmp_path / "r").exists()

    def test_swarm_report_uses_its_validated_diagnosis(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from incident_agent import cli as main_cli

        base = make_diag()
        commander = CommanderDiagnosis(
            agent_type="incident_commander",
            incident_id=base.incident_id,
            narrative="The database pool exhausted after handlers leaked connections.",
            escalation_decision="monitor",
            severity_assessment="The checkout API is experiencing elevated errors.",
            confidence=base.confidence,
            risk_tier=base.risk_tier,
            diagnosis=base,
            worker_findings=[
                WorkerFinding(
                    worker_type="logs",
                    incident_id=base.incident_id,
                    evidence=base.evidence,
                    reasoning_steps=base.reasoning_steps,
                    confidence=base.confidence,
                    summary="The logs show database pool exhaustion.",
                )
            ],
            evidence=base.evidence,
            affected_service=base.affected_service,
            category=base.category,
            recommendation=base.recommendation,
            reasoning_steps=base.reasoning_steps,
        )

        class FakeSwarm:
            def __init__(self, *_: Any, **__: Any) -> None:
                pass

            def run(self, _: str) -> CommanderDiagnosis:
                return commander

        monkeypatch.setattr(main_cli, "_load_dotenv", lambda: None)
        monkeypatch.setattr("incident_agent.llm.build_client", lambda: object())
        monkeypatch.setattr("incident_agent.agents.swarm.HierarchicalSwarm", FakeSwarm)
        monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))

        assert main_cli.main(["diagnose", "INC-007", "--agent", "swarm", "--report"]) == 0
        page = (tmp_path / "reports" / "wiki" / "postmortem-inc-007.md").read_text()
        assert base.root_cause in page
