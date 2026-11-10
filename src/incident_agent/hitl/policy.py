"""Escalation policy (Stage 18): who is asked, for how long, and how many approvals an action needs.

The rules come from ``openspec/risk-classification.md`` (high risk): block until an explicit human approval arrives,
escalate on timeout, and never auto-approve on timeout. The policy therefore has no "approve" outcome of its own: when
the last level runs out the request *expires*, and an expired request is a refusal.

A policy is a short list of levels. Level 0 is asked first; when its timeout passes the request moves to the next level
(its roles are asked as well, and everybody asked earlier may still answer). ``required`` says how many distinct
approvers a risk tier needs (never fewer than one).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,23}")
MAX_LEVELS = 5
MAX_TIMEOUT = 7 * 24 * 3600.0
MAX_REQUIRED = 3  # approved_by (Stage 16) is at most 80 characters: three 24-character names still fit


class PolicyError(ValueError):
    """The policy file or object is not usable."""


@dataclass(frozen=True)
class Level:
    name: str
    roles: tuple[str, ...]
    timeout: float  # seconds this level is given before the request moves on
    mention: str | None = None  # optional chat mention for this level (validated by the notifier)

    def __post_init__(self) -> None:
        if not NAME_RE.fullmatch(self.name):
            raise PolicyError(f"level name {self.name!r} must be 1-24 characters of a-z, 0-9, '.', '_' or '-'")
        if not self.roles or any(not NAME_RE.fullmatch(r) for r in self.roles):
            raise PolicyError(f"level {self.name!r} needs one or more valid roles")
        if not isinstance(self.timeout, int | float) or isinstance(self.timeout, bool):
            raise PolicyError(f"level {self.name!r}: timeout must be a number of seconds")
        if not 0 < self.timeout <= MAX_TIMEOUT:
            raise PolicyError(f"level {self.name!r}: timeout must be between 0 and {MAX_TIMEOUT:g} seconds")


@dataclass(frozen=True)
class EscalationPolicy:
    levels: tuple[Level, ...]
    required: dict[str, int] = field(default_factory=lambda: {"high": 1})
    default_required: int = 1

    def __post_init__(self) -> None:
        if not 1 <= len(self.levels) <= MAX_LEVELS:
            raise PolicyError(f"a policy needs 1 to {MAX_LEVELS} levels")
        names = [lv.name for lv in self.levels]
        if len(set(names)) != len(names):
            raise PolicyError("level names must be unique")
        for tier, n in {**self.required, "<default>": self.default_required}.items():
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= MAX_REQUIRED:
                raise PolicyError(f"required approvals for {tier!r} must be between 1 and {MAX_REQUIRED}")

    @property
    def total_timeout(self) -> float:
        return float(sum(lv.timeout for lv in self.levels))

    def required_for(self, tier: str | None) -> int:
        return self.required.get(str(tier), self.default_required)

    def deadlines(self, opened_at: float, hard_deadline: float | None = None) -> list[float]:
        """When each level ends, counted from ``opened_at`` and never later than ``hard_deadline``."""
        out, t = [], opened_at
        for lv in self.levels:
            t += lv.timeout
            out.append(min(t, hard_deadline) if hard_deadline is not None else t)
        return out

    def roles_up_to(self, level: int) -> frozenset[str]:
        return frozenset(r for lv in self.levels[: level + 1] for r in lv.roles)

    def to_json(self) -> dict[str, Any]:
        return {
            "levels": [
                {"name": lv.name, "roles": list(lv.roles), "timeout": lv.timeout, "mention": lv.mention}
                for lv in self.levels
            ],
            "required": dict(self.required),
            "default_required": self.default_required,
        }

    @classmethod
    def from_json(cls, data: Any) -> EscalationPolicy:
        if not isinstance(data, dict) or not isinstance(data.get("levels"), list):
            raise PolicyError("policy must be an object with a 'levels' list")
        levels = []
        for raw in data["levels"]:
            if not isinstance(raw, dict) or not isinstance(raw.get("roles"), list):
                raise PolicyError("every level needs a name, a roles list and a timeout")
            levels.append(
                Level(str(raw.get("name")), tuple(map(str, raw["roles"])), raw.get("timeout", 0), raw.get("mention"))
            )
        required = data.get("required", {"high": 1})
        if not isinstance(required, dict):
            raise PolicyError("'required' must map a risk tier to a number")
        return cls(tuple(levels), {str(k): v for k, v in required.items()}, data.get("default_required", 1))


DEFAULT_POLICY = EscalationPolicy(
    (
        Level("primary", ("oncall",), 300.0),
        Level("secondary", ("lead",), 300.0),
        Level("management", ("manager",), 600.0),
    )
)


def load_policy(path: str | Path | None) -> EscalationPolicy:
    if path is None:
        return DEFAULT_POLICY
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PolicyError(f"policy file {Path(path).name} does not exist") from None
    except (OSError, ValueError) as exc:
        raise PolicyError(f"policy file {Path(path).name} cannot be read: {type(exc).__name__}") from None
    return EscalationPolicy.from_json(data)
