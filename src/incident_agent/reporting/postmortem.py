"""Deterministic post-mortem page built from a validated ``Diagnosis`` (Stage 15).

No LLM writes this page: every sentence is a template filled with Diagnosis fields, so the page cannot claim more than
the diagnosis did. It is honest about its status (an unreviewed, automatically generated draft) and never invents a
timeline, an impact figure or "lessons learned" that the diagnosis does not contain. All free text passes through
``sanitize`` (secrets redacted, Markdown/Slack structure neutralised).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from incident_agent.agents.risk_guard import risk_floor
from incident_agent.reporting.sanitize import md_code, md_text, one_line
from incident_agent.schemas.agent_output import RiskTier
from incident_agent.schemas.diagnosis import Diagnosis

LOW_CONFIDENCE = 0.5
MAX_EVIDENCE = 10
MAX_STEPS = 10


@dataclass(frozen=True)
class PostMortem:
    incident_id: str
    slug: str
    title: str
    markdown: str
    content_hash: str  # sha256 of the body; the generation time is not part of it, so a re-run is detectable


_ORDER = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}


def effective_tier(diagnosis: Diagnosis) -> tuple[RiskTier, list[str]]:
    """The tier to show people: the diagnosis tier, raised (never lowered) to the floor the recommendation text implies.

    A published page or alert must not understate risk just because the agent ran without the risk guard.
    Returns the tier and the phrases that raised it (empty when the tier is unchanged).
    """
    floor, phrases = risk_floor(diagnosis.recommendation)
    if _ORDER[floor] > _ORDER[diagnosis.risk_tier]:
        return floor, phrases
    return diagnosis.risk_tier, []


def slug_for(incident_id: str) -> str:
    slug = f"postmortem-{incident_id.lower()}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", slug):
        raise ValueError("incident id cannot be turned into a page slug")
    return slug


def build_postmortem(diagnosis: Diagnosis, *, generated_at: str | None = None) -> PostMortem:
    d = diagnosis
    eff, raised_by = effective_tier(d)
    tier = eff.value
    title = f"Post-mortem draft: {d.incident_id} - {one_line(d.category, 60)} on {one_line(d.affected_service, 60)}"
    body: list[str] = []
    add = body.append

    if d.confidence < LOW_CONFIDENCE:
        add(f"> **Low confidence ({d.confidence:.0%}).** Treat the root cause below as a hypothesis, not a finding.")
        add("")
    add("## Summary")
    add("")
    add("| Field | Value |")
    add("|---|---|")
    add(f"| Incident | {md_text(d.incident_id)} |")
    add(f"| Affected service | {md_text(d.affected_service, 80)} |")
    add(f"| Category | {md_text(d.category, 80)} |")
    add(f"| Diagnosis confidence | {d.confidence:.0%} |")
    add(f"| Risk tier of the recommendation | {tier} |")
    if d.metadata is not None:
        m = d.metadata
        add(f"| Time window | {md_text(m.window_start, 40)} to {md_text(m.window_end, 40)} |")
        add(f"| Severity | {md_text(m.severity, 40)} |")
        add(f"| Services in scope | {md_text(', '.join(m.services), 120)} |")
    add("")
    add("## Root cause")
    add("")
    add(md_text(d.root_cause, 600))
    add("")
    add(f"## Evidence ({min(len(d.evidence), MAX_EVIDENCE)} of {len(d.evidence)} shown)")
    add("")
    for item in d.evidence[:MAX_EVIDENCE]:
        add(f"- {item.source_type.value} ({md_text(item.source, 60)}): {md_code(item.detail)}")
    add("")
    add("## Reasoning")
    add("")
    for n, step in enumerate(d.reasoning_steps[:MAX_STEPS], 1):
        add(f"{n}. {md_text(step.description, 300)}")
    add("")
    add("## Recommendation")
    add("")
    add(md_text(d.recommendation, 600))
    add("")
    if raised_by:
        phrases = ", ".join(md_code(p, 40) for p in dict.fromkeys(raised_by))
        add(
            f"The agent rated this {d.risk_tier.value}; raised to {tier} because the recommendation mentions {phrases}."
        )
        add("")
    if tier != "low":
        add(f"Risk tier {tier}: an action like this needs human approval before it is carried out.")
        add("")
    add("## Follow-up (to be completed by a human)")
    add("")
    add("- [ ] Review and correct this draft")
    add("- [ ] Confirm the resolved failure class (it feeds incident memory)")
    add("- [ ] Add impact, timeline and lessons learned, which this draft does not contain")
    add("")
    text = "\n".join(body)
    header = [
        f"# {md_text(title, 200)}",
        "",
        "*Generated automatically by the incident response agent. Not reviewed by a human. "
        "No remediation was executed.*",
    ]
    if generated_at:
        header.append(f"*Generated at {md_text(generated_at, 40)}.*")
    markdown = "\n".join([*header, "", text])
    return PostMortem(
        incident_id=d.incident_id,
        slug=slug_for(d.incident_id),
        title=title,
        markdown=markdown,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
    )
