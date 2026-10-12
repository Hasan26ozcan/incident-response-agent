"""Deterministic risk-tier floor derived from openspec/risk-classification.md.

The model proposes a remediation and a risk tier. This guard can only RAISE the tier ("when in doubt,
classify up"), never lower it, when the recommendation text names an action the spec puts in a higher tier:

* high   - restart/reboot, scale, roll back/redeploy, drain/cordon/evict, purge/delete data,
           anything touching credentials, secrets, certificates or access control
* medium - reversible persistent change: revert/config change, ticket, notification, post-mortem

A mention that is negated nearby ("no restart needed", "avoid rolling back") does not count.
It is a post-processing step, so toggling it re-uses cached LLM responses at zero API cost.
"""

from __future__ import annotations

import re

from incident_agent.schemas.agent_output import ReasoningStep, RiskTier
from incident_agent.schemas.diagnosis import Diagnosis

_HIGH = re.compile(
    r"\b(restart(?:s|ed|ing)?|reboot(?:s|ed|ing)?|roll(?:ed|ing|s)?[ -]?back|rollbacks?|redeploy(?:s|ed|ing)?"
    r"|re-deploy(?:s|ed|ing)?|scal(?:e|es|ed|ing)(?: (?:out|up|down|in))?|drain(?:s|ed|ing)?|cordon(?:s|ed|ing)?"
    r"|evict(?:s|ed|ing)?|kill(?:s|ed|ing)?|purg(?:e|es|ed|ing)|truncat(?:e|es|ed|ing)"
    r"|delet(?:e|es|ed|ing) (?:the )?data"
    r"|rotat(?:e|es|ed|ing)|secrets?|credentials?|certificates?|certs?|passwords?|api[ -]?keys?|access[ -]control"
    r"|iam|permissions?|fail[ -]?over)\b",
    re.IGNORECASE,
)
_MEDIUM = re.compile(
    r"\b(revert(?:s|ed|ing)?|tickets?|notify|notif(?:y|ies|ication|ications)|slack|post-?mortems?|wiki"
    r"|feature[ -]flags?|temporar(?:y|ily) (?:raise|increase|enable|disable|change|lower))\b",
    re.IGNORECASE,
)
_NEGATION = re.compile(
    r"\b(no|not|without|avoid|never|don'?t|nothing|neither|nor|unnecessary)\b[^.;]{0,40}$", re.IGNORECASE
)
_ORDER = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}


def _hits(pattern: re.Pattern[str], text: str) -> list[str]:
    out: list[str] = []
    for m in pattern.finditer(text):
        if _NEGATION.search(text[max(0, m.start() - 48) : m.start()]):
            continue
        out.append(m.group(0).lower())
    return out


def risk_floor(recommendation: str) -> tuple[RiskTier, list[str]]:
    """Lowest tier the recommendation text justifies, plus the matched phrases."""
    high = _hits(_HIGH, recommendation)
    if high:
        return RiskTier.HIGH, high
    medium = _hits(_MEDIUM, recommendation)
    if medium:
        return RiskTier.MEDIUM, medium
    return RiskTier.LOW, []


def apply_risk_guard(diagnosis: Diagnosis) -> tuple[Diagnosis, bool]:
    """Return (diagnosis, raised). Only ever raises the tier."""
    floor, matched = risk_floor(diagnosis.recommendation)
    if _ORDER[floor] <= _ORDER[diagnosis.risk_tier]:
        return diagnosis, False
    steps = [
        *diagnosis.reasoning_steps,
        ReasoningStep(
            step_number=len(diagnosis.reasoning_steps) + 1,
            description=f"Risk guard raised the tier {diagnosis.risk_tier.value} -> {floor.value}: "
            f"recommendation mentions {', '.join(sorted(set(matched)))}.",
        ),
    ]
    guarded = Diagnosis(**{**diagnosis.model_dump(), "risk_tier": floor, "reasoning_steps": steps})
    return guarded, True
