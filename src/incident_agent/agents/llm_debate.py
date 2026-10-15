"""LLM debate (Stage 8): a root-cause defender and a forensic examiner argue about a diagnosis.

Pipeline for one incident (``LLMDebate.run``):

1. defender  - structured case FOR the diagnosis, quotes copied from the evidence (no tools);
2. examiner  - read-only tools to look for NEW, disconfirming evidence, then a structured case AGAINST it;
3. code      - every challenge must quote the evidence or the examiner's own tool output, otherwise it is
               discarded (a challenge nobody can quote is not evidence);
4. rebuttal  - the defender answers each surviving challenge; unanswered or conceded ones *stand*;
5. arbiter   - rules ``confirmed`` or ``revised``; a revision needs verbatim quotes;
6. code      - accepts or rejects the ruling with an asymmetric, deterministic policy (below) and recalibrates
               confidence from the number of standing challenges.

Acceptance policy (the model never decides alone):

* confirmed: the diagnosis is kept; each standing challenge costs 0.1 confidence (max 0.3);
* revised, real -> false alarm: the costliest error. Accepted only if every quote is grounded, there are at
  least two, and (when worker signals exist) every domain was reported normal;
* revised, anything else: accepted if at least half of the quotes are grounded;
* no surviving challenge: confirmed without an arbiter call (nothing to rule on).

Any phase may fail (rate limit, invalid JSON): the debate then degrades to "keep the original diagnosis" and
records why. It never loses the diagnosis and never executes remediation. ``LLMConfigError`` is never
swallowed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field

from incident_agent.agents.grounding import is_grounded, tokens
from incident_agent.agents.llm_commander import RevisedDiagnosis, Signals, claims_false_alarm, compute_signals
from incident_agent.agents.llm_orchestrator import LLMOrchestrator, WorkerOutcome, reports_json
from incident_agent.agents.risk_guard import apply_risk_guard
from incident_agent.agents.tool_loop import ToolLoop
from incident_agent.llm import LLMClient, LLMUnavailable, StructuredOutputError, generate_structured
from incident_agent.prompts import debate_prompts as P
from incident_agent.schemas.agent_output import EvidenceItem, EvidenceType, ReasoningStep
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.tools.toolbox import TOOL_NAMES, IncidentToolbox, specs_for

T = TypeVar("T", bound=BaseModel)

ChallengeKind = Literal[
    "symptom_not_cause",
    "alternative_cause",
    "benign_explanation",
    "real_incident_missed",
    "timeline_conflict",
    "missing_evidence",
]
STANDING_PENALTY = 0.1
MAX_STANDING_PENALTY = 0.3
UNGROUNDED_PENALTY = 0.15


# --------------------------------------------------------------------------- LLM-facing schemas
class Claim(BaseModel):
    claim: str = Field(min_length=5)
    quote: str = Field(min_length=3)


class DefenceCase(BaseModel):
    claims: list[Claim] = Field(min_length=1, max_length=4)
    concessions: list[str] = Field(default_factory=list, max_length=4)


class Challenge(BaseModel):
    kind: ChallengeKind
    point: str = Field(min_length=5)
    quote: str = Field(min_length=3)
    severity: Literal["low", "medium", "high"] = "medium"
    alternative: str = ""


class ForensicCase(BaseModel):
    challenges: list[Challenge] = Field(default_factory=list, max_length=4)
    summary: str = Field(default="", max_length=600)


class Response(BaseModel):
    challenge: int = Field(ge=0)
    answer: str = Field(min_length=3)
    quote: str = ""
    conceded: bool = False


class Rebuttal(BaseModel):
    responses: list[Response] = Field(default_factory=list, max_length=4)


class ArbiterRuling(BaseModel):
    verdict: Literal["confirmed", "revised"]
    reasoning: str = Field(min_length=10, max_length=900)
    revised: RevisedDiagnosis | None = None


# --------------------------------------------------------------------------- result record
@dataclass
class DebateResult:
    diagnosis: Diagnosis
    original: Diagnosis
    verdict: str  # confirmed | revised | revision_rejected | no_challenges | skipped | error
    changed: bool = False  # the final root cause/false-alarm call differs from the original
    before_false_alarm: bool = False
    after_false_alarm: bool = False
    defence: DefenceCase | None = None
    challenges: list[Challenge] = field(default_factory=list)  # grounded ones only
    discarded_challenges: int = 0
    rebuttal: Rebuttal | None = None
    standing: list[int] = field(default_factory=list)
    ruling: ArbiterRuling | None = None
    examiner_transcript: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "changed": self.changed,
            "before_false_alarm": self.before_false_alarm,
            "after_false_alarm": self.after_false_alarm,
            "defence": self.defence.model_dump() if self.defence else None,
            "challenges": [c.model_dump() for c in self.challenges],
            "discarded_challenges": self.discarded_challenges,
            "rebuttal": self.rebuttal.model_dump() if self.rebuttal else None,
            "standing": self.standing,
            "ruling": self.ruling.model_dump() if self.ruling else None,
            "examiner_transcript": self.examiner_transcript,
            "notes": self.notes,
        }


def _diag_json(d: Diagnosis) -> str:
    return json.dumps(
        {
            "root_cause": d.root_cause,
            "category": d.category,
            "affected_service": d.affected_service,
            "confidence": d.confidence,
            "recommendation": d.recommendation,
            "risk_tier": d.risk_tier.value,
            "claims_false_alarm": claims_false_alarm(d.root_cause),
            "evidence": [e.detail for e in d.evidence][:6],
        },
        indent=1,
    )


class LLMDebate:
    agent_type = "llm_debate"

    def __init__(
        self,
        client: LLMClient,
        *,
        temperature: float = 0.0,
        max_repairs: int = 2,
        examiner_max_steps: int = 5,
        examiner_max_tool_calls: int = 6,
        evidence_limit: int = 7000,
        risk_guard: bool = False,
        fail_tools: set[str] | None = None,
    ) -> None:
        self.client = client
        self.temperature = temperature
        self.max_repairs = max_repairs
        self.examiner_max_steps = examiner_max_steps
        self.examiner_max_tool_calls = examiner_max_tool_calls
        self.evidence_limit = evidence_limit
        self.risk_guard = risk_guard
        self.fail_tools = fail_tools or set()

    # -- helpers -------------------------------------------------------------
    def _ask(self, messages: list[dict[str, Any]], schema: type[T], notes: list[str], phase: str) -> T | None:
        try:
            return generate_structured(
                self.client, messages, schema, max_repairs=self.max_repairs, temperature=self.temperature
            )
        except (LLMUnavailable, StructuredOutputError) as exc:  # LLMConfigError propagates on purpose
            notes.append(f"{phase} failed ({type(exc).__name__}: {str(exc)[:120]}).")
            return None

    def _examine(
        self, incident_id: str, meta: str, diag: str, evidence: str, notes: list[str]
    ) -> tuple[list[str], ForensicCase | None]:
        transcript: list[str] = []
        toolbox = IncidentToolbox(incident_id, fail_tools=self.fail_tools)
        user = P.EXAMINER_USER.format(incident_id=incident_id, meta=meta, diagnosis=diag, evidence=evidence)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": P.EXAMINER_SYSTEM},
            {"role": "user", "content": user},
        ]
        try:
            ToolLoop(
                self.client,
                toolbox,
                specs_for(set(TOOL_NAMES)),
                transcript=transcript,
                unavailable=set(),
                temperature=self.temperature,
                max_steps=self.examiner_max_steps,
                max_tool_calls=self.examiner_max_tool_calls,
            ).run(messages)
        except LLMUnavailable as exc:
            notes.append(f"examiner investigation interrupted ({str(exc)[:120]}); using what it found.")
        joined = "\n\n".join(transcript) or "(no tool output)"
        case_msgs = [
            {"role": "system", "content": P.EXAMINER_SYSTEM},
            {"role": "user", "content": user + "\n\n" + P.EXAMINER_CASE_USER.format(transcript=joined[-5000:])},
        ]
        return transcript, self._ask(case_msgs, ForensicCase, notes, "examiner case")

    @staticmethod
    def _standing(challenges: list[Challenge], rebuttal: Rebuttal | None, seen: set[str]) -> list[int]:
        """Challenges that were conceded, unanswered, or answered without a grounded quote."""
        answered: set[int] = set()
        if rebuttal is not None:
            answered = {r.challenge for r in rebuttal.responses if not r.conceded and is_grounded(r.quote, seen)}
        return [i for i in range(len(challenges)) if i not in answered]

    def _accept_revision(
        self, rev: RevisedDiagnosis, original: Diagnosis, seen: set[str], signals: Signals | None
    ) -> tuple[bool, list[bool], str]:
        grounded = [is_grounded(q, seen) for q in rev.supporting_quotes]
        share = sum(grounded) / len(grounded)
        dismissing_real = rev.is_false_alarm and not claims_false_alarm(original.root_cause)
        if dismissing_real:
            if not all(grounded) or len(grounded) < 2:
                return False, grounded, "dismissing a real incident needs at least two quotes, all grounded"
            if signals is not None and not signals.all_domains_normal:
                return False, grounded, "dismissing a real incident needs every domain reported normal"
            return True, grounded, ""
        if share < 0.5:
            return False, grounded, "supporting quotes are not found in the evidence"
        return True, grounded, ""

    @staticmethod
    def _with_step(d: Diagnosis, text: str, **update: Any) -> Diagnosis:
        steps = [*d.reasoning_steps, ReasoningStep(step_number=len(d.reasoning_steps) + 1, description=text)]
        return d.model_copy(update={"agent_type": LLMDebate.agent_type, "reasoning_steps": steps, **update})

    # -- main ----------------------------------------------------------------
    def run(
        self,
        incident_id: str,
        meta: str,
        diagnosis: Diagnosis,
        evidence_text: str,
        *,
        signals: Signals | None = None,
        degraded: bool = False,
    ) -> DebateResult:
        before_fa = claims_false_alarm(diagnosis.root_cause)
        res = DebateResult(
            diagnosis=diagnosis,
            original=diagnosis,
            verdict="skipped",
            before_false_alarm=before_fa,
            after_false_alarm=before_fa,
        )
        if degraded:
            res.notes.append("Debate skipped: the base diagnosis was degraded, there is nothing to debate.")
            return res

        diag = _diag_json(diagnosis)
        evidence = evidence_text[: self.evidence_limit] or "(no evidence collected)"

        # 1. defender
        res.defence = self._ask(
            [
                {"role": "system", "content": P.DEFENDER_SYSTEM},
                {
                    "role": "user",
                    "content": P.DEFENDER_USER.format(
                        incident_id=incident_id, meta=meta, diagnosis=diag, evidence=evidence
                    ),
                },
            ],
            DefenceCase,
            res.notes,
            "defender",
        )

        # 2. examiner (own tools -> may find evidence the workers never saw)
        res.examiner_transcript, case = self._examine(incident_id, meta, diag, evidence, res.notes)
        seen = tokens(evidence_text + "\n" + "\n".join(res.examiner_transcript))

        # 3. only quotable challenges survive
        raw = case.challenges if case else []
        res.challenges = [c for c in raw if is_grounded(c.quote, seen)]
        res.discarded_challenges = len(raw) - len(res.challenges)
        if res.discarded_challenges:
            res.notes.append(f"{res.discarded_challenges} challenge(s) discarded: quote not found in the evidence.")

        if not res.challenges:
            res.verdict = "no_challenges" if case is not None else "error"
            if case is not None:
                res.diagnosis = self._with_step(
                    diagnosis, "Debate: the forensic examiner raised no evidence-backed challenge; diagnosis stands."
                )
            else:
                res.notes.append("Debate inconclusive: the examiner produced no case; original diagnosis kept.")
            return res

        # 4. rebuttal
        listing = json.dumps([{"index": i, **c.model_dump()} for i, c in enumerate(res.challenges)], indent=1)
        res.rebuttal = self._ask(
            [
                {"role": "system", "content": P.REBUTTAL_SYSTEM},
                {
                    "role": "user",
                    "content": P.REBUTTAL_USER.format(
                        incident_id=incident_id, diagnosis=diag, challenges=listing, evidence=evidence
                    ),
                },
            ],
            Rebuttal,
            res.notes,
            "rebuttal",
        )
        res.standing = self._standing(res.challenges, res.rebuttal, seen)

        # 5. arbiter
        exchange = json.dumps(
            [
                {
                    "index": i,
                    **c.model_dump(),
                    "rebuttal": next(
                        (
                            {**r.model_dump(), "grounded": bool(r.quote) and is_grounded(r.quote, seen)}
                            for r in (res.rebuttal.responses if res.rebuttal else [])
                            if r.challenge == i
                        ),
                        None,
                    ),
                    "standing": i in res.standing,
                }
                for i, c in enumerate(res.challenges)
            ],
            indent=1,
        )
        res.ruling = self._ask(
            [
                {"role": "system", "content": P.ARBITER_SYSTEM},
                {
                    "role": "user",
                    "content": P.ARBITER_USER.format(
                        incident_id=incident_id,
                        diagnosis=diag,
                        defence=res.defence.model_dump_json(indent=1) if res.defence else "(defender unavailable)",
                        exchange=exchange,
                        signals=signals.describe() if signals else "n/a",
                        evidence=evidence,
                    ),
                },
            ],
            ArbiterRuling,
            res.notes,
            "arbiter",
        )
        if res.ruling is None:
            res.verdict = "error"
            res.notes.append("Debate inconclusive: no arbiter ruling; original diagnosis kept.")
            return res

        # 6. deterministic acceptance
        penalty = min(MAX_STANDING_PENALTY, STANDING_PENALTY * len(res.standing))
        if res.ruling.verdict == "revised" and res.ruling.revised is not None:
            rev = res.ruling.revised
            ok, grounded, why = self._accept_revision(rev, diagnosis, seen, signals)
            if ok:
                res.diagnosis = self._build_revised(diagnosis, res, rev, grounded)
                res.verdict = "revised"
            else:
                res.verdict = "revision_rejected"
                res.notes.append(f"Arbiter revision rejected: {why}.")
        else:
            res.verdict = "confirmed"
            if res.ruling.verdict == "revised":  # asked to revise but gave nothing to apply
                res.notes.append("Arbiter chose 'revised' without a revised diagnosis; treated as confirmed.")
        if res.verdict in ("confirmed", "revision_rejected"):
            conf = round(max(0.0, diagnosis.confidence - penalty), 2)
            text = (
                f"Debate: {len(res.challenges)} evidence-backed challenge(s), {len(res.standing)} standing; "
                f"diagnosis kept, confidence {diagnosis.confidence:.2f} -> {conf:.2f}."
            )
            res.diagnosis = self._with_step(diagnosis, text, confidence=conf)
        res.after_false_alarm = claims_false_alarm(res.diagnosis.root_cause)
        res.changed = res.verdict == "revised"
        return res

    def _build_revised(
        self, original: Diagnosis, res: DebateResult, rev: RevisedDiagnosis, grounded: list[bool]
    ) -> Diagnosis:
        root = rev.root_cause
        if rev.is_false_alarm and not claims_false_alarm(root):
            root = f"False alarm: no real incident. {root}"
        quotes = [
            EvidenceItem(
                source_type=EvidenceType.META,
                source="debate:quote",
                detail=q,
                confidence_weight=1.0 if ok else 0.3,
            )
            for q, ok in zip(rev.supporting_quotes, grounded, strict=True)
        ]
        confidence = round(max(0.0, rev.confidence - UNGROUNDED_PENALTY * (len(grounded) - sum(grounded))), 2)
        why = res.ruling.reasoning if res.ruling else "evidence contradicts it"
        revised = Diagnosis(
            agent_type=self.agent_type,
            incident_id=original.incident_id,
            root_cause=root,
            confidence=confidence,
            evidence=[*original.evidence, *quotes],
            affected_service=original.affected_service,
            category=rev.category,
            reasoning_steps=[
                *original.reasoning_steps,
                ReasoningStep(
                    step_number=len(original.reasoning_steps) + 1,
                    description=f"Debate revised the diagnosis ({len(res.challenges)} challenge(s), "
                    f"{len(res.standing)} standing): {why}",
                ),
            ],
            recommendation=rev.recommendation,
            risk_tier=rev.risk_tier,
        )
        return apply_risk_guard(revised)[0] if self.risk_guard else revised


def evidence_from_outcomes(outcomes: list[WorkerOutcome]) -> str:
    """The shared evidence pool: worker reports (with verification flags) followed by the raw tool output."""
    raw = "\n\n".join("\n\n".join(o.transcript) for o in outcomes)
    return f"WORKER REPORTS:\n{reports_json(outcomes, limit=3500)}\n\nTOOL OUTPUT:\n{raw}"


class DebatedOrchestrator:
    """Stage 6 orchestrator followed by the Stage 8 debate. Scored diagnosis = the debate's final diagnosis."""

    agent_type = "llm_debate"

    def __init__(
        self,
        client: LLMClient,
        *,
        temperature: float = 0.0,
        risk_guard: bool = False,
        debate_kwargs: dict[str, Any] | None = None,
        **orchestrator_kwargs: Any,
    ) -> None:
        self.client = client
        self.orchestrator = LLMOrchestrator(
            client, temperature=temperature, risk_guard=risk_guard, **orchestrator_kwargs
        )
        self.debate = LLMDebate(client, temperature=temperature, risk_guard=risk_guard, **(debate_kwargs or {}))
        self.record: DebateResult | None = None

    @property
    def degraded(self) -> str | None:
        return self.orchestrator.degraded

    @property
    def outcomes(self) -> dict[str, WorkerOutcome]:
        return self.orchestrator.outcomes

    @property
    def plan_history(self) -> list[Any]:
        return self.orchestrator.plan_history

    @property
    def transcript(self) -> list[str]:
        return self.orchestrator.transcript

    @property
    def unavailable(self) -> set[str]:
        return self.orchestrator.unavailable

    def run(self, incident_id: str) -> Diagnosis:
        self.record = None
        base = self.orchestrator.run(incident_id)
        outcomes = list(self.orchestrator.outcomes.values())
        meta = IncidentToolbox(incident_id).call("get_incident_meta", {})
        self.record = self.debate.run(
            incident_id,
            meta,
            base,
            evidence_from_outcomes(outcomes),
            signals=compute_signals(outcomes),
            degraded=self.orchestrator.degraded is not None,
        )
        return self.record.diagnosis
