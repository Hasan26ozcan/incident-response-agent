"""The notification component (Stage 15): publish the post-mortem, then tell Slack, exactly once per content.

Order: wiki first (so Slack can link to it), then Slack. The two are isolated: a wiki failure still sends the Slack
message (without a link); a Slack failure never loses the wiki page. Nothing here may break a diagnosis run, so errors
are returned in ``ReportResult`` instead of raised. A ledger remembers what was already announced, so re-running the
same incident does not spam the channel; because a lost alert is worse than a duplicate, an unreadable ledger line is
ignored rather than blocking delivery.

Live delivery (real Slack webhook, real Confluence) is opt-in: ``build_reporter(live=True)``. Without it everything is
written to local files, whatever the environment contains.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from incident_agent.reporting.postmortem import PostMortem, build_postmortem, effective_tier
from incident_agent.reporting.sanitize import one_line
from incident_agent.reporting.slack import (
    ConfigError,
    OutboxSender,
    Sender,
    WebhookSender,
    build_slack_message,
    validate_mention,
)
from incident_agent.reporting.wiki import ConfluenceWiki, FileWiki, PublishResult, Wiki
from incident_agent.schemas.agent_output import RiskTier
from incident_agent.schemas.diagnosis import Diagnosis

DEFAULT_DIR = ".cache/reports"


class Ledger:
    """Append-only JSONL of announcements: one line per (incident, content hash, with/without link)."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None

    @staticmethod
    def key(incident_id: str, content_hash: str, has_link: bool) -> str:
        return f"{incident_id}:{content_hash}:{'link' if has_link else 'nolink'}"

    def seen(self, key: str) -> bool:
        if self.path is None or not self.path.exists():
            return False
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for line in lines:
            try:
                if json.loads(line).get("key") == key:
                    return True
            except (ValueError, AttributeError):
                continue
        return False

    def add(self, key: str) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key}) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


@dataclass
class ReportResult:
    incident_id: str
    content_hash: str
    wiki: PublishResult | None = None
    wiki_error: str | None = None
    slack_sent: bool = False
    slack_skipped: str | None = None
    slack_error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.wiki_error is None and self.slack_error is None


class Reporter:
    def __init__(self, wiki: Wiki, sender: Sender, ledger: Ledger | None = None, *, mention_on_high: str | None = None):
        self.wiki = wiki
        self.sender = sender
        self.ledger = ledger or Ledger(None)
        self.mention_on_high = mention_on_high
        if mention_on_high is not None:  # fail at construction, not during an incident
            try:
                validate_mention(mention_on_high)
            except ValueError as exc:
                raise ConfigError(str(exc)) from None

    def describe(self) -> str:
        return f"wiki={self.wiki.name}, slack={self.sender.name}"

    def report(self, diagnosis: Diagnosis, *, generated_at: str | None = None) -> ReportResult:
        page: PostMortem = build_postmortem(diagnosis, generated_at=generated_at)
        result = ReportResult(diagnosis.incident_id, page.content_hash)
        try:
            result.wiki = self.wiki.publish(page)
        except Exception as exc:  # noqa: BLE001  isolation: notification must never break the caller
            result.wiki_error = f"{type(exc).__name__}: {one_line(str(exc), 200)}"
        url = result.wiki.url if result.wiki else None
        key = Ledger.key(page.incident_id, page.content_hash, url is not None)
        if self.ledger.seen(key):
            result.slack_skipped = "already announced"
            return result
        mention = self.mention_on_high if effective_tier(diagnosis)[0] == RiskTier.HIGH else None
        try:
            sent = self.sender.send(build_slack_message(diagnosis, url, mention=mention))
        except Exception as exc:  # noqa: BLE001
            result.slack_error = f"{type(exc).__name__}: {one_line(str(exc), 200)}"
            return result
        if not sent.ok:
            result.slack_error = one_line(sent.detail, 200)
            return result
        result.slack_sent = True
        try:
            self.ledger.add(key)
        except OSError as exc:
            result.notes.append(f"ledger not written ({type(exc).__name__}); a re-run may announce again")
        return result


_CONFLUENCE_VARS = ("CONFLUENCE_BASE_URL", "CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN", "CONFLUENCE_SPACE")


def build_reporter(
    *, live: bool = False, env: Mapping[str, str] | None = None, base_dir: str | Path | None = None
) -> Reporter:
    env = os.environ if env is None else env
    base = Path(base_dir or env.get("REPORT_DIR") or DEFAULT_DIR)
    mention = env.get("SLACK_MENTION_ON_HIGH") or None
    ledger = Ledger(base / "ledger.jsonl")
    file_wiki = FileWiki(base / "wiki")
    outbox = OutboxSender(base / "slack-outbox.jsonl")
    if not live:
        return Reporter(file_wiki, outbox, ledger, mention_on_high=mention)
    hook = env.get("SLACK_WEBHOOK_URL")
    present = [v for v in _CONFLUENCE_VARS if env.get(v)]
    if present and len(present) != len(_CONFLUENCE_VARS):
        missing = [v for v in _CONFLUENCE_VARS if not env.get(v)]
        raise ConfigError(f"Confluence is partly configured; missing: {', '.join(missing)}")
    if not hook and not present:
        raise ConfigError("--live needs SLACK_WEBHOOK_URL and/or the CONFLUENCE_* variables; see .env.example")
    sender: Sender = WebhookSender(hook) if hook else outbox
    wiki: Wiki = file_wiki
    if present:
        try:
            wiki = ConfluenceWiki(
                env["CONFLUENCE_BASE_URL"],
                env["CONFLUENCE_EMAIL"],
                env["CONFLUENCE_API_TOKEN"],
                env["CONFLUENCE_SPACE"],
                parent_id=env.get("CONFLUENCE_PARENT_ID") or None,
            )
        except ValueError as exc:
            raise ConfigError(f"Confluence settings are not usable: {exc}") from None
    return Reporter(wiki, sender, ledger, mention_on_high=mention)
