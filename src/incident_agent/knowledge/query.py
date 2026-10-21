"""Build retrieval queries from what an agent can observe about an incident (Stage 10).

The ``log`` style query is made only from observable data: the services and metric names in ``meta.json``, the
distinct WARN/ERROR message templates, the rare INFO events (a maintenance window opening, a job starting) and
the descriptions in the deploy history. It never reads the gold answer.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from incident_agent.knowledge.text import normalise_message

ROOT = Path(__file__).resolve().parents[3]
INCIDENTS_DIR = ROOT / "data" / "incidents"
_LINE = re.compile(r'^(?P<ts>\S+)\s+service=(?P<svc>\S+)\s+level=(?P<level>\w+)\s+msg="(?P<msg>.*)"\s*$')
_SEVERE = {"WARN", "WARNING", "ERROR", "CRITICAL", "FATAL"}


class QueryError(ValueError):
    pass


def build_log_query(
    incident_id: str,
    incidents_dir: Path | None = None,
    max_severe: int = 6,
    max_rare_info: int = 4,
    rare_threshold: int = 2,
) -> str:
    base = (incidents_dir or INCIDENTS_DIR) / incident_id
    meta_file = base / "meta.json"
    if not meta_file.is_file():
        raise QueryError(f"no incident data for {incident_id} (looked in {base})")
    meta = json.loads(meta_file.read_text())
    severe: Counter[str] = Counter()
    info: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    log_file = base / "logs.log"
    lines = log_file.read_text().splitlines() if log_file.is_file() else []
    for position, line in enumerate(lines):
        m = _LINE.match(line)
        if not m:
            continue
        template = normalise_message(m["msg"])
        if m["level"].upper() in _SEVERE:
            severe[template] += 1
        elif m["level"].upper() == "INFO":
            info[template] += 1
            first_seen.setdefault(template, position)
    parts: list[str] = [*meta.get("services", []), *meta.get("available_metrics", [])]
    parts += [t for t, _ in sorted(severe.items(), key=lambda kv: (-kv[1], kv[0]))[:max_severe]]
    rare = [t for t, n in info.items() if n <= rare_threshold]
    parts += sorted(rare, key=lambda t: first_seen[t])[:max_rare_info]
    deploys_file = base / "deploys.json"
    if deploys_file.is_file():
        parts += [str(d.get("description", "")).strip() for d in json.loads(deploys_file.read_text())[:3]]
    return "; ".join(p for p in parts if p)
