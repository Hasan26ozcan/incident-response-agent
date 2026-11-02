"""Slack notification (Stage 15): message builder, a real webhook sender and an offline outbox.

The message is built only from Diagnosis fields, escaped for Slack (no ``<!channel>``, no injected links) and cut to
Slack's block limits. A mention is added only when the caller passes one that matches a strict pattern. The webhook
URL is a secret: it is read from the environment by the caller, validated here, and never printed or put in errors.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from incident_agent.reporting.http import HttpClient, HttpError, check_url
from incident_agent.reporting.postmortem import effective_tier
from incident_agent.reporting.sanitize import one_line, slack_escape, slack_text
from incident_agent.schemas.diagnosis import Diagnosis

SLACK_HOSTS = frozenset({"hooks.slack.com"})
_MENTION = re.compile(r"<!(?:here|channel)>|<@[UW][A-Z0-9]{2,20}>|<!subteam\^S[A-Z0-9]{2,20}>")
_SAFE_URL = re.compile(r"(?:https://[^\s<>|]+|file:///[^\s<>|]+|http://(?:localhost|127\.0\.0\.1)[^\s<>|]*)")
HEADER_MAX = 150  # Slack header limit; every other text is bounded by one_line (600 characters) well below 3000


class ConfigError(ValueError):
    """Missing or invalid configuration. The message names the variable, never its value."""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    detail: str
    status: int | None = None
    attempts: int = 1


class Sender(Protocol):
    name: str

    def send(self, payload: Mapping[str, Any]) -> SendResult: ...


def validate_mention(mention: str) -> str:
    if not _MENTION.fullmatch(mention):
        raise ValueError("mention must be <!here>, <!channel>, <@Uxxxx> or <!subteam^Sxxxx>")
    return mention


def build_slack_message(
    diagnosis: Diagnosis, page_url: str | None = None, *, mention: str | None = None
) -> dict[str, Any]:
    d = diagnosis
    if mention is not None:
        validate_mention(mention)
    tier = effective_tier(d)[0].value
    fallback = (
        f"{d.incident_id} diagnosed: {slack_text(d.root_cause, 140)} (confidence {d.confidence:.0%}, risk {tier})"
    )
    lead = f"{mention} " if mention else ""
    if page_url is not None and _SAFE_URL.fullmatch(page_url):
        link = f"<{page_url}|Post-mortem draft>"
    else:
        link = "Post-mortem page was not published"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": one_line(f"{d.incident_id} diagnosed", HEADER_MAX)}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"{lead}*Root cause*\n{slack_text(d.root_cause, 600)}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Service*\n{slack_text(d.affected_service, 80)}"},
                {"type": "mrkdwn", "text": f"*Category*\n{slack_text(d.category, 80)}"},
                {"type": "mrkdwn", "text": f"*Confidence*\n{d.confidence:.0%}"},
                {"type": "mrkdwn", "text": f"*Risk tier*\n{slack_escape(str(tier))}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Recommendation*\n{slack_text(d.recommendation, 600)}"},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": link}},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": "Automated draft, not reviewed by a human. No remediation was executed."}
            ],
        },
    ]
    return {"text": fallback, "blocks": blocks}


class WebhookSender:
    """Real Slack incoming webhook. Only used when the caller explicitly asked for live delivery."""

    name = "slack-webhook"

    def __init__(self, url: str, *, http: HttpClient | None = None) -> None:
        try:
            check_url(url, allowed_hosts=SLACK_HOSTS)
        except ValueError as exc:
            raise ConfigError(f"SLACK_WEBHOOK_URL is not usable: {exc}") from None  # message has no URL
        self._url = url
        self._http = http or HttpClient()

    def send(self, payload: Mapping[str, Any]) -> SendResult:
        try:
            resp = self._http.request(
                "POST",
                self._url,
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload).encode(),
            )
        except HttpError as exc:
            return SendResult(False, str(exc))
        text = one_line(resp.body.decode("utf-8", "replace"), 120)
        if resp.status == 200:
            return SendResult(True, "delivered", resp.status, resp.attempts)
        return SendResult(False, f"Slack answered HTTP {resp.status}: {text}", resp.status, resp.attempts)


class OutboxSender:
    """Offline stand-in: appends each message to a JSONL file instead of posting it."""

    name = "slack-outbox"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def send(self, payload: Mapping[str, Any]) -> SendResult:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            return SendResult(False, f"cannot write outbox: {type(exc).__name__}")
        return SendResult(True, "written to outbox (not sent to Slack)")
