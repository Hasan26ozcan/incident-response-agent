"""The incident workflow on the durable engine (Stage 17).

triage -> diagnose -> plan_action -> approval (only when the plan mutates) -> execute -> report

Every step is a plain function of its journalled inputs, so the same functions run under the dependency-free engine
(tested end to end here) and as the single Temporal activity in ``temporal.py``.

Safety properties:

* ``execute`` is **at most once**: ``max_attempts=1`` and attempts are counted across crashes, so a worker that dies
  inside it fails the run (a human decides) instead of running a mutating command twice;
* the approval nonce is derived from the run id, so a replay of ``execute`` cannot spend a second approval;
* no approval, a rejection, a timeout or an approval without a named approver only ever produces a ``--dry-run``;
* nothing here is wired into the agents; Stage 18 owns the real approval gate and the escalation policy.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from incident_agent.durable.engine import (
    NonRetryable,
    RetryPolicy,
    Step,
    StepContext,
    Wait,
    Workflow,
)
from incident_agent.sandbox import Approval, AuditLog, Backend, SandboxRunner, select_backend

STEP_ORDER = ("triage", "diagnose", "plan_action", "approval", "execute", "report")
SERVICE_RE = re.compile(r"[a-z][a-z0-9-]{0,62}")
APPROVAL_TTL = 300.0

# keyword -> (category, command template). The first match in message order of appearance wins ties by list order.
RULES: tuple[tuple[str, str, str], ...] = (
    ("out of memory", "memory", "restart-service {svc} --reason oom"),
    ("oom", "memory", "restart-service {svc} --reason oom"),
    ("connection pool", "saturation", "scale-service {svc} --replicas 4"),
    ("cpu throttling", "saturation", "scale-service {svc} --replicas 4"),
    ("hikaripool", "saturation", "scale-service {svc} --replicas 4"),
    ("saturat", "saturation", "scale-service {svc} --replicas 4"),
    ("cache", "cache", "flush-cache {svc}"),
    ("nullpointer", "bad_deploy", "rollback-deploy {svc}"),
    ("deploy", "bad_deploy", "rollback-deploy {svc}"),
)


@dataclass
class Deps:
    """Everything the steps need from outside, injectable for tests."""

    backend_factory: Callable[[], Backend] = field(default=lambda: select_backend())
    data_loader: Callable[[str], dict[str, Any]] | None = None
    retry: RetryPolicy = field(default_factory=lambda: RetryPolicy(max_attempts=3, initial_delay=0.2, max_delay=2.0))
    approval_timeout: float = 300.0
    signer: Any = None  # security.ApprovalSigner: the key the approval gate seals decisions with
    allow_unsigned: bool = False  # INSECURE, tests/demos only: accept an unsealed 'approval' signal


def default_root() -> Path:
    return Path(os.environ.get("INCIDENT_RUNS_DIR", ".cache/runs"))


def deterministic_nonce(run_id: str, step: str) -> str:
    return hashlib.sha256(f"{run_id}:{step}:approval".encode()).hexdigest()[:32]


def _load(deps: Deps, incident_id: str) -> dict[str, Any]:
    if deps.data_loader is not None:
        return deps.data_loader(incident_id)
    from incident_agent.sandbox.runner import load_incident_data

    return load_incident_data(incident_id)


def _text(entry: dict[str, Any]) -> str:
    return str(entry.get("message") or entry.get("msg") or "")


def make_triage(deps: Deps) -> Callable[[StepContext], dict[str, Any]]:
    def triage(ctx: StepContext) -> dict[str, Any]:
        incident = ctx.input.get("incident_id")
        if not isinstance(incident, str):
            raise NonRetryable("input needs an incident_id")
        try:
            data = _load(deps, incident)
        except (OSError, KeyError, ValueError) as exc:
            raise NonRetryable(f"cannot load incident data ({type(exc).__name__})") from exc
        logs = data.get("logs") or []
        levels = Counter(str(e.get("level", "")) for e in logs)
        errors = [e for e in logs if e.get("level") == "ERROR"]
        by_service = Counter(str(e.get("service", "")) for e in errors)
        meta = data.get("meta") or {}
        services = [s for s in meta.get("services", []) if isinstance(s, str)]
        suspect = by_service.most_common(1)[0][0] if by_service else (services[0] if services else "")
        return {
            "incident_id": incident,
            "services": services,
            "log_levels": dict(levels),
            "error_count": len(errors),
            "suspect_service": suspect,
            "deploys": len(data.get("deploys") or []),
        }

    return triage


def make_diagnose(deps: Deps) -> Callable[[StepContext], dict[str, Any]]:
    def diagnose(ctx: StepContext) -> dict[str, Any]:
        triage = ctx.results["triage"]
        data = _load(deps, triage["incident_id"])
        errors = [_text(e) for e in data.get("logs") or [] if e.get("level") == "ERROR"]
        category, evidence = "unknown", ""
        blob = " ".join(errors).lower()
        for keyword, cat, _ in RULES:
            if keyword in blob:
                category = cat
                evidence = next(m for m in errors if keyword in m.lower())[:200]
                break
        return {
            "category": category,
            "service": triage["suspect_service"],
            "evidence": evidence,
            "confidence": "rule-based" if category != "unknown" else "none",
        }

    return diagnose


def propose_command(diagnosis: dict[str, Any]) -> str:
    """Map a diagnosis to one allow-listed command line (a read-only look when nothing is understood)."""
    service = str(diagnosis.get("service", ""))
    if not SERVICE_RE.fullmatch(service):
        return "list-services"
    for _, category, template in RULES:
        if category == diagnosis.get("category"):
            return template.format(svc=service)
    return f"cluster-status {service}"


def plan_action(ctx: StepContext) -> dict[str, Any]:
    diagnosis = ctx.results["diagnose"]
    line = propose_command(diagnosis)
    incident = ctx.input["incident_id"]
    from incident_agent.sandbox import evaluate

    decision = evaluate(line, incident)
    return {
        "command_line": line,
        "verdict": decision.verdict,
        "tier": decision.tier,
        "fingerprint": decision.fingerprint,
        "reason": decision.reason,
    }


def needs_approval(results: dict[str, Any]) -> bool:
    return (results.get("plan_action") or {}).get("verdict") == "needs_approval"


def approval_check(deps: Deps, run_id: str, fingerprint: str, payload: dict[str, Any]) -> str:
    """Is this approval payload authentic? ``verified`` | ``unsigned_allowed`` | ``refused:<why>`` (fails closed)."""
    if deps.allow_unsigned:
        return "unsigned_allowed"
    if deps.signer is None:
        return "refused:no signer configured"
    if not isinstance(payload.get("sig"), str):
        return "refused:unsigned"
    return "verified" if deps.signer.verify(run_id, fingerprint, payload) else "refused:bad signature"


def make_execute(deps: Deps) -> Callable[[StepContext], dict[str, Any]]:
    def execute(ctx: StepContext) -> dict[str, Any]:
        plan = ctx.results["plan_action"]
        if plan["verdict"] == "deny":
            raise NonRetryable(f"the plan was denied by policy: {plan['reason']}")
        audit = AuditLog(ctx.run_dir / "sandbox-audit.jsonl")
        if deps.data_loader is not None:
            runner = SandboxRunner(deps.backend_factory(), audit, data_loader=deps.data_loader)
        else:
            runner = SandboxRunner(deps.backend_factory(), audit)
        line, approval, mode, check = plan["command_line"], None, "run", "not_required"
        if plan["verdict"] == "needs_approval":
            answer = ctx.results.get("approval") or {}
            payload = answer.get("payload") or {}
            approver = payload.get("by")
            check = approval_check(deps, ctx.run_id, plan["fingerprint"], payload)
            if (
                not answer.get("timed_out")
                and payload.get("approved") is True
                and isinstance(approver, str)
                and approver.strip()
                and check in ("verified", "unsigned_allowed")
            ):
                now = runner._clock()  # noqa: SLF001 - the runner's clock decides expiry
                approval = Approval(
                    plan["fingerprint"],
                    approver.strip(),
                    now,
                    now + APPROVAL_TTL,
                    deterministic_nonce(ctx.run_id, ctx.step),
                )
                mode = "applied"
            else:
                line, mode = f"{line} --dry-run", "dry_run"
        result = runner.run(line, ctx.input["incident_id"], approval)
        return {
            "mode": mode,
            "command_line": line,
            "status": result.status,
            "message": result.message,
            "output": result.output,
            "changed": result.changed,
            "backend": result.backend,
            "approved_by": approval.approved_by if approval else None,
            "approval_check": check,
        }

    return execute


def report(ctx: StepContext) -> dict[str, Any]:
    r = ctx.results
    ex = r.get("execute") or {}
    lines = [
        f"Incident {r['triage']['incident_id']}: {r['diagnose']['category']} "
        f"in {r['diagnose']['service'] or 'unknown'}",
        f"Evidence: {r['diagnose']['evidence'] or 'none'}",
        f"Proposed: {r['plan_action']['command_line']} ({r['plan_action']['tier']} risk)",
        f"Outcome: {ex.get('mode', 'n/a')} / {ex.get('status', 'n/a')} - {ex.get('message', '')}",
    ]
    return {"summary": "\n".join(lines), "changed": bool(ex.get("changed"))}


def incident_workflow(deps: Deps | None = None) -> Workflow:
    d = deps or Deps()
    return Workflow(
        "incident-response",
        "1",
        (
            Step("triage", make_triage(d), d.retry),
            Step("diagnose", make_diagnose(d), d.retry),
            Step("plan_action", plan_action, d.retry),
            Wait("approval", "approval", d.approval_timeout, when=needs_approval),
            Step("execute", make_execute(d), RetryPolicy(max_attempts=1)),
            Step("report", report, d.retry),
        ),
    )
