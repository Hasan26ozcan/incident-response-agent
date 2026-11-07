"""SandboxRunner (Stage 16): policy -> approval -> isolation check -> audit -> execution in a backend.

Order matters and every refusal happens *before* anything is executed:

1. the incident id must look like ``INC-123`` (it is used to find files, so nothing else gets through);
2. the policy decides allow / needs_approval / deny (no shell, allow-list, typed arguments);
3. a high-risk command needs an approval bound to its fingerprint, valid, unexpired and unused;
4. the backend must *claim* every isolation guarantee the command requires; a weak backend is refused even with a
   valid approval (fail closed);
5. the decision is written to the audit log (an unwritable log stops the run) and the approval is consumed;
6. only then the command source runs in the backend, receiving the incident data on stdin, never a file path.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from incident_agent.sandbox import commands
from incident_agent.sandbox.approval import Approval, check_approval
from incident_agent.sandbox.audit import AuditError, AuditLog
from incident_agent.sandbox.backends import Backend, ExecResult, ExecSpec, Limits
from incident_agent.sandbox.policy import Decision, evaluate

INCIDENT_RE = re.compile(r"INC-\d{3}")
MAX_PAYLOAD = 1_000_000
COMMANDS_SOURCE = Path(commands.__file__).read_text(encoding="utf-8")


@dataclass(frozen=True)
class RunResult:
    status: str  # ok | failed | denied | needs_approval | refused
    message: str
    decision: Decision | None = None
    output: str = ""
    changed: bool = False
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    backend: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def load_incident_data(incident_id: str) -> dict[str, Any]:
    from incident_agent import tools

    return {
        "meta": tools.read_meta(incident_id),
        "logs": [entry._asdict() for entry in tools.read_logs(incident_id)],
        "metrics": tools.read_metrics(incident_id),
        "deploys": tools.read_deploys(incident_id),
    }


class SandboxRunner:
    def __init__(
        self,
        backend: Backend,
        audit: AuditLog | None = None,
        *,
        clock: Callable[[], float] = time.time,
        data_loader: Callable[[str], dict[str, Any]] = load_incident_data,
        limits: Limits | None = None,
    ) -> None:
        self.backend = backend
        self.audit = audit if audit is not None else AuditLog(None, clock)
        self._clock = clock
        self._load = data_loader
        self.limits = limits or Limits()

    def plan(self, line: str, incident_id: str) -> Decision:
        """Classify a command without running it (what the agent or a human sees before approving)."""
        if not isinstance(incident_id, str) or not INCIDENT_RE.fullmatch(incident_id):
            return Decision("deny", "incident id must look like INC-001")
        return evaluate(line, incident_id)

    def run(self, line: str, incident_id: str, approval: Approval | None = None) -> RunResult:
        decision = self.plan(line, incident_id)
        base = {
            "incident": str(incident_id)[:20],
            "command_line": line if isinstance(line, str) else "",
            "backend": self.backend.name,
        }
        if decision.verdict == "deny":
            return self._stop("denied", decision, base, decision.reason)
        high = decision.verdict == "needs_approval"
        apply = False
        if high:
            if approval is None:
                msg = f"{decision.reason}; fingerprint {decision.fingerprint}"
                return self._stop("needs_approval", decision, base, msg)
            assert decision.fingerprint is not None
            why = check_approval(approval, decision.fingerprint, now=self._clock(), is_used=self.audit.nonce_used)
            if why:
                return self._stop("refused", decision, base, why)
            apply = True
        missing = sorted(decision.requires - self.backend.guarantees)
        if missing:
            msg = f"backend {self.backend.name!r} does not provide: {', '.join(missing)}"
            return self._stop("refused", decision, base, msg)
        try:
            payload = self._payload(decision, incident_id, apply)
        except (OSError, ValueError, KeyError) as exc:
            return self._stop("failed", decision, base, f"cannot load incident data ({type(exc).__name__})")
        if len(payload) > MAX_PAYLOAD:
            return self._stop("failed", decision, base, "incident data is too large for the sandbox")
        try:
            if high:
                assert approval is not None
                self.audit.record("approval_consumed", nonce=approval.nonce, approved_by=approval.approved_by, **base)
            self.audit.record("execute", fingerprint=decision.fingerprint, tier=decision.tier, apply=apply, **base)
        except AuditError as exc:
            return RunResult("refused", f"not run: {exc}", decision, backend=self.backend.name)
        result = self.backend.run(ExecSpec(COMMANDS_SOURCE, payload, self.limits))
        return self._finish(decision, base, result)

    def _payload(self, decision: Decision, incident_id: str, apply: bool) -> bytes:
        body = {
            "incident_id": incident_id,
            "command": decision.command,
            "args": decision.args,
            "apply": apply,
            "data": self._load(incident_id),
        }
        return json.dumps(body).encode()

    def _stop(self, status: str, decision: Decision, base: dict[str, Any], message: str) -> RunResult:
        self._safe_record(status, reason=message, fingerprint=decision.fingerprint, **base)
        return RunResult(status, message, decision, backend=self.backend.name)

    def _finish(self, decision: Decision, base: dict[str, Any], result: ExecResult) -> RunResult:
        if result.timed_out or result.killed:
            return self._done("failed", f"sandbox stopped the command ({result.killed})", decision, base)
        if result.exit_code != 0:
            return self._done("failed", f"sandbox exited with code {result.exit_code}", decision, base)
        try:
            out = json.loads(result.stdout.decode("utf-8", "replace"))
        except ValueError:
            return self._done("failed", "sandbox returned something that is not JSON", decision, base)
        if not isinstance(out, dict):
            return self._done("failed", "sandbox returned an unexpected result", decision, base)
        if not out.get("ok"):
            return self._done("failed", str(out.get("error", "unknown error"))[:300], decision, base)
        before = out.get("before") if isinstance(out.get("before"), dict) else None
        after = out.get("after") if isinstance(out.get("after"), dict) else None
        text = str(out.get("output", ""))[: commands.MAX_OUTPUT]
        res = RunResult("ok", "completed", decision, text, out.get("changed") is True, before, after, self.backend.name)
        self._safe_record("result", status="ok", changed=res.changed, **base)
        return res

    def _done(self, status: str, msg: str, decision: Decision, base: dict[str, Any]) -> RunResult:
        self._safe_record("result", status=status, reason=msg, **base)
        return RunResult(status, msg, decision, backend=self.backend.name)

    def _safe_record(self, event: str, **fields: Any) -> None:
        try:
            self.audit.record(event, **fields)
        except AuditError:
            pass  # nothing more can be refused at this point; the pre-run record is what gates execution
