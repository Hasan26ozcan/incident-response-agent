"""LLM Tree-of-Thought + Plan-and-Solve (Stage 9).

Roadmap: parallel hypothesis branching on complex scenarios, most likely scenario selected, measurable accuracy
improvement on the gold set. Pipeline (``TreeOfThought.run``), applied to an existing diagnosis:

1. hypothesize  - one LLM call lists the key observations (verbatim quotes) and up to N-1 *different* hypotheses,
                  each with a short plan. The current diagnosis is branch H0 and cannot be dropped by the model.
                  Near-duplicate hypotheses are discarded by code.
2. branches     - every branch runs in parallel as Plan-and-Solve: an investigator executes the plan with read-only
                  tools, then writes a structured report (findings, per-step results, observations explained or
                  contradicted). Everything counts only when its quote is found in the evidence or in that
                  branch's own tool output.
3. scoring      - deterministic, code only (``score_branch``): grounded support, observation coverage, grounded
                  refutations, contradictions, plan results. The model never scores itself.
4. selection    - highest score wins, but a challenger must beat H0 by ``switch_margin``; ties and near-ties keep
                  the existing diagnosis (status-quo bias against noise).
5. solve        - if a challenger wins, one more LLM call writes the full diagnosis from its verified findings;
                  it must pass the same acceptance policy as the debate (``accept_revision``): half the quotes
                  grounded, and dismissing a real incident needs two grounded quotes and all domains normal.
6. calibration  - a small margin between winner and runner-up caps confidence.

A branch, the hypothesize call or the solve call may fail: the existing diagnosis is kept and the reason recorded.
``LLMConfigError`` is never swallowed. No remediation is executed.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from incident_agent.agents.grounding import is_grounded, tokens
from incident_agent.agents.llm_commander import RevisedDiagnosis, Signals, claims_false_alarm, compute_signals
from incident_agent.agents.llm_debate import accept_revision, evidence_from_outcomes
from incident_agent.agents.llm_orchestrator import LLMOrchestrator, WorkerOutcome
from incident_agent.agents.risk_guard import apply_risk_guard
from incident_agent.agents.tool_loop import ToolLoop
from incident_agent.llm import (
    LLMClient,
    LLMConfigError,
    LLMUnavailable,
    StructuredOutputError,
    generate_structured,
)
from incident_agent.prompts import tot_prompts as P
from incident_agent.schemas.agent_output import EvidenceItem, EvidenceType, ReasoningStep
from incident_agent.schemas.diagnosis import Diagnosis
from incident_agent.tools.toolbox import TOOL_NAMES, IncidentToolbox, specs_for

SWITCH_MARGIN = 0.15
LOW_MARGIN_CAP = 0.6
DUPLICATE_JACCARD = 0.6
UNGROUNDED_PENALTY = 0.15
H0 = "H0"
BASE_PLAN = [
    "Check that the observed symptoms follow the claimed cause in time",
    "Look for logs that directly show the claimed cause",
    "Look for a deploy or configuration change that explains it",
]


# --------------------------------------------------------------------------- LLM-facing schemas
class Observation(BaseModel):
    text: str = Field(min_length=3)
    quote: str = Field(min_length=3)


class HypothesisSpec(BaseModel):
    root_cause: str = Field(min_length=10)
    mechanism: str = Field(default="", max_length=500)
    category: str = Field(default="unknown", min_length=1)
    is_false_alarm: bool = False
    plan: list[str] = Field(min_length=1, max_length=4)


class Hypotheses(BaseModel):
    observations: list[Observation] = Field(default_factory=list, max_length=6)
    hypotheses: list[HypothesisSpec] = Field(default_factory=list, max_length=4)


class Finding(BaseModel):
    kind: Literal["support", "refute"]
    claim: str = Field(min_length=3)
    quote: str = Field(min_length=3)


class PlanResult(BaseModel):
    step: int = Field(ge=0)
    outcome: Literal["supported", "refuted", "unknown"]
    quote: str = ""


class BranchReport(BaseModel):
    findings: list[Finding] = Field(default_factory=list, max_length=8)
    plan_results: list[PlanResult] = Field(default_factory=list, max_length=4)
    explains: list[int] = Field(default_factory=list, max_length=6)
    contradicted: list[int] = Field(default_factory=list, max_length=6)


# --------------------------------------------------------------------------- records
@dataclass
class Branch:
    id: str
    root_cause: str
    mechanism: str
    category: str
    is_false_alarm: bool
    plan: list[str]
    report: BranchReport | None = None
    transcript: list[str] = field(default_factory=list)
    score: float | None = None
    parts: dict[str, float] = field(default_factory=dict)
    grounded_support: list[str] = field(default_factory=list)
    grounded_refute: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "root_cause": self.root_cause,
            "is_false_alarm": self.is_false_alarm,
            "plan": self.plan,
            "score": self.score,
            "parts": self.parts,
            "grounded_support": self.grounded_support,
            "grounded_refute": self.grounded_refute,
            "error": self.error,
            "transcript": self.transcript,
            "report": self.report.model_dump() if self.report else None,
        }


@dataclass
class ToTResult:
    diagnosis: Diagnosis
    original: Diagnosis
    verdict: str  # kept | switched | switch_rejected | skipped | gated_off | error
    winner: str = H0
    margin: float | None = None  # winner score minus runner-up score
    observations: list[Observation] = field(default_factory=list)
    branches: list[Branch] = field(default_factory=list)
    discarded_hypotheses: int = 0
    before_false_alarm: bool = False
    after_false_alarm: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.verdict == "switched"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "winner": self.winner,
            "margin": self.margin,
            "changed": self.changed,
            "before_false_alarm": self.before_false_alarm,
            "after_false_alarm": self.after_false_alarm,
            "observations": [o.model_dump() for o in self.observations],
            "branches": [b.summary() for b in self.branches],
            "discarded_hypotheses": self.discarded_hypotheses,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- deterministic scoring
def score_branch(report: BranchReport, n_plan: int, n_obs: int, seen: set[str]) -> tuple[float, dict[str, Any]]:
    """Score a branch from grounded evidence only. Returns (score, components)."""
    support = [f.quote for f in report.findings if f.kind == "support" and is_grounded(f.quote, seen)]
    refute = [f.quote for f in report.findings if f.kind == "refute" and is_grounded(f.quote, seen)]

    def valid(idx: list[int]) -> set[int]:
        return {i for i in idx if 0 <= i < n_obs}

    explains = valid(report.explains)
    contradicted = valid(report.contradicted) - explains  # explaining and contradicting one item cancels out
    supported_steps = {
        r.step
        for r in report.plan_results
        if r.outcome == "supported" and 0 <= r.step < n_plan and is_grounded(r.quote, seen)
    }
    parts = {
        "support": min(len(support), 4) / 4,
        "coverage": len(explains) / n_obs if n_obs else 0.0,
        "refutation": min(len(refute), 3) / 3,
        "contradiction": len(contradicted) / n_obs if n_obs else 0.0,
        "plan": len(supported_steps) / n_plan if n_plan else 0.0,
    }
    score = parts["support"] + 1.5 * parts["coverage"] - 1.5 * parts["refutation"] - parts["contradiction"]
    score += 0.5 * parts["plan"]
    return round(score, 4), {"parts": parts, "support": support, "refute": refute}


def _jaccard(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta | tb else 0.0


class TreeOfThought:
    agent_type = "llm_tot"

    def __init__(
        self,
        client: LLMClient,
        *,
        n_branches: int = 3,
        parallel: bool = True,
        temperature: float = 0.0,
        max_repairs: int = 2,
        branch_max_steps: int = 4,
        branch_max_tool_calls: int = 5,
        evidence_limit: int = 7000,
        switch_margin: float = SWITCH_MARGIN,
        confidence_margin: float = SWITCH_MARGIN,
        skip_if_confidence_at_least: float | None = None,
        risk_guard: bool = False,
        fail_tools: set[str] | None = None,
    ) -> None:
        self.client = client
        self.n_branches = max(2, n_branches)
        self.parallel = parallel
        self.temperature = temperature
        self.max_repairs = max_repairs
        self.branch_max_steps = branch_max_steps
        self.branch_max_tool_calls = branch_max_tool_calls
        self.evidence_limit = evidence_limit
        self.switch_margin = switch_margin
        self.confidence_margin = confidence_margin  # winner-vs-runner-up gap below which confidence is capped
        self.skip_if_confidence_at_least = skip_if_confidence_at_least
        self.risk_guard = risk_guard
        self.fail_tools = fail_tools or set()

    # -- helpers ---------------------------------------------------------------
    def _structured(self, messages: list[dict[str, Any]], schema: type[Any]) -> Any:
        return generate_structured(
            self.client, messages, schema, max_repairs=self.max_repairs, temperature=self.temperature
        )

    @staticmethod
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

    def _with_step(self, d: Diagnosis, text: str, **update: Any) -> Diagnosis:
        steps = [*d.reasoning_steps, ReasoningStep(step_number=len(d.reasoning_steps) + 1, description=text)]
        return d.model_copy(update={"agent_type": self.agent_type, "reasoning_steps": steps, **update})

    def _dedupe(self, base: Diagnosis, specs: list[HypothesisSpec]) -> tuple[list[HypothesisSpec], int]:
        kept: list[HypothesisSpec] = []
        dropped = 0
        for spec in specs:
            clash = _jaccard(spec.root_cause, base.root_cause) >= DUPLICATE_JACCARD or any(
                _jaccard(spec.root_cause, k.root_cause) >= DUPLICATE_JACCARD for k in kept
            )
            if clash or len(kept) >= self.n_branches - 1:
                dropped += 1
            else:
                kept.append(spec)
        return kept, dropped

    # -- one branch (Plan-and-Solve) -------------------------------------------
    def _run_branch(
        self,
        incident_id: str,
        meta: str,
        branch: Branch,
        observations: list[Observation],
        evidence: str,
        shared: set[str],
    ) -> Branch:
        try:
            plan_text = "\n".join(f"{i}. {step}" for i, step in enumerate(branch.plan))
            obs_text = "\n".join(f"{i}. {o.text}" for i, o in enumerate(observations)) or "(none listed)"
            user = P.BRANCH_USER.format(
                incident_id=incident_id,
                meta=meta,
                root_cause=branch.root_cause,
                mechanism=branch.mechanism or "(not given)",
                plan=plan_text,
                observations=obs_text,
                evidence=evidence,
            )
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": P.BRANCH_SYSTEM},
                {"role": "user", "content": user},
            ]
            try:
                ToolLoop(
                    self.client,
                    IncidentToolbox(incident_id, fail_tools=self.fail_tools),
                    specs_for(set(TOOL_NAMES)),
                    transcript=branch.transcript,
                    unavailable=set(),
                    temperature=self.temperature,
                    max_steps=self.branch_max_steps,
                    max_tool_calls=self.branch_max_tool_calls,
                ).run(messages)
            except LLMUnavailable as exc:
                branch.transcript.append(f"[investigation interrupted: {str(exc)[:100]}]")
            joined = "\n\n".join(branch.transcript) or "(no tool output)"
            report_msgs = [
                {"role": "system", "content": P.BRANCH_SYSTEM},
                {"role": "user", "content": user + "\n\n" + P.BRANCH_REPORT_USER.format(transcript=joined[-5000:])},
            ]
            branch.report = self._structured(report_msgs, BranchReport)
            seen = shared | tokens("\n".join(branch.transcript))
            branch.score, info = score_branch(branch.report, len(branch.plan), len(observations), seen)
            branch.parts = info["parts"]
            branch.grounded_support = info["support"]
            branch.grounded_refute = info["refute"]
        except LLMConfigError:
            raise
        except (LLMUnavailable, StructuredOutputError) as exc:
            branch.error = f"{type(exc).__name__}: {str(exc)[:160]}"
            branch.score = None
        return branch

    # -- main ------------------------------------------------------------------
    def run(
        self,
        incident_id: str,
        meta: str,
        diagnosis: Diagnosis,
        evidence_text: str,
        *,
        signals: Signals | None = None,
        degraded: bool = False,
    ) -> ToTResult:
        before_fa = claims_false_alarm(diagnosis.root_cause)
        res = ToTResult(
            diagnosis=diagnosis,
            original=diagnosis,
            verdict="skipped",
            before_false_alarm=before_fa,
            after_false_alarm=before_fa,
        )
        if degraded:
            res.notes.append("Tree of thought skipped: the base diagnosis was degraded.")
            return res
        gate = self.skip_if_confidence_at_least
        if gate is not None and diagnosis.confidence >= gate:
            res.verdict = "gated_off"
            res.notes.append(f"Tree of thought not needed: confidence {diagnosis.confidence:.2f} >= {gate:.2f}.")
            return res

        diag = self._diag_json(diagnosis)
        evidence = evidence_text[: self.evidence_limit] or "(no evidence collected)"
        shared = tokens(evidence_text)

        # 1. hypothesize
        try:
            gen: Hypotheses = self._structured(
                [
                    {"role": "system", "content": P.HYPOTHESIZE_SYSTEM},
                    {
                        "role": "user",
                        "content": P.HYPOTHESIZE_USER.format(
                            incident_id=incident_id,
                            meta=meta,
                            diagnosis=diag,
                            evidence=evidence,
                            n=self.n_branches - 1,
                        ),
                    },
                ],
                Hypotheses,
            )
        except (LLMUnavailable, StructuredOutputError) as exc:
            res.verdict = "error"
            res.notes.append(f"hypothesize failed ({type(exc).__name__}: {str(exc)[:120]}); original diagnosis kept.")
            return res
        res.observations = [o for o in gen.observations if is_grounded(o.quote, shared)]
        dropped_obs = len(gen.observations) - len(res.observations)
        if dropped_obs:
            res.notes.append(f"{dropped_obs} observation(s) dropped: quote not found in the evidence.")
        specs, res.discarded_hypotheses = self._dedupe(diagnosis, gen.hypotheses)
        if res.discarded_hypotheses:
            res.notes.append(f"{res.discarded_hypotheses} hypothesis(es) discarded as duplicates or over budget.")
        if not specs:
            res.verdict = "kept"
            res.notes.append("No genuinely different hypothesis was proposed; diagnosis kept.")
            res.diagnosis = self._with_step(
                diagnosis, "Tree of thought: no distinct alternative hypothesis; diagnosis stands."
            )
            return res

        branches = [Branch(H0, diagnosis.root_cause, "", diagnosis.category, before_fa, list(BASE_PLAN))]
        for i, sp in enumerate(specs, 1):
            branches.append(Branch(f"H{i}", sp.root_cause, sp.mechanism, sp.category, sp.is_false_alarm, sp.plan))
        res.branches = branches

        # 2. parallel plan-and-solve branches
        args = [(incident_id, meta, b, res.observations, evidence, shared) for b in branches]
        if self.parallel:
            with ThreadPoolExecutor(max_workers=len(branches)) as pool:
                list(pool.map(lambda a: self._run_branch(*a), args))  # LLMConfigError is re-raised by map
        else:
            for a in args:
                self._run_branch(*a)
        for b in branches:
            if b.error:
                res.notes.append(f"Branch {b.id} failed: {b.error}")

        # 3-4. score and select
        base_branch = branches[0]
        scored = [b for b in branches if b.score is not None]
        challengers = [b for b in scored if b.id != H0]
        if base_branch.score is None or not challengers:
            res.verdict = "error"
            res.notes.append("Tree of thought inconclusive (base branch or every challenger failed); diagnosis kept.")
            return res
        ranked = sorted(scored, key=lambda b: -(b.score or 0.0))
        best = max(challengers, key=lambda b: b.score or 0.0)
        winner = best if (best.score or 0.0) - (base_branch.score or 0.0) >= self.switch_margin else base_branch
        runner_up = next(b for b in ranked if b.id != winner.id)
        res.winner = winner.id
        res.margin = round((winner.score or 0.0) - (runner_up.score or 0.0), 4)
        scores = ", ".join(f"{b.id}={b.score}" for b in scored)

        if winner.id == H0:
            conf = diagnosis.confidence
            if res.margin < self.confidence_margin:
                conf = min(conf, LOW_MARGIN_CAP)
            res.verdict = "kept"
            text = (
                f"Tree of thought: {len(branches)} branches scored {scores}; the diagnosis stands "
                f"(margin {res.margin:+.2f}), confidence {diagnosis.confidence:.2f} -> {conf:.2f}."
            )
            res.diagnosis = self._with_step(diagnosis, text, confidence=conf)
            return res

        # 5. solve for the winning challenger under the shared acceptance policy
        findings = json.dumps(
            [{"kind": "support", "quote": q} for q in winner.grounded_support]
            + [{"kind": "refute", "quote": q} for q in winner.grounded_refute],
            indent=1,
        )
        try:
            rev: RevisedDiagnosis = self._structured(
                [
                    {"role": "system", "content": P.SOLVE_SYSTEM},
                    {
                        "role": "user",
                        "content": P.SOLVE_USER.format(
                            incident_id=incident_id,
                            diagnosis=diag,
                            root_cause=winner.root_cause,
                            mechanism=winner.mechanism or "(not given)",
                            findings=findings,
                        ),
                    },
                ],
                RevisedDiagnosis,
            )
        except (LLMUnavailable, StructuredOutputError) as exc:
            res.verdict = "error"
            res.notes.append(f"solve failed ({type(exc).__name__}: {str(exc)[:120]}); original diagnosis kept.")
            return res
        seen = shared | tokens("\n".join(winner.transcript))
        ok, grounded, why = accept_revision(rev, diagnosis, seen, signals)
        if not ok:
            res.verdict = "switch_rejected"
            res.notes.append(f"Switch to {winner.id} rejected: {why}.")
            res.diagnosis = self._with_step(
                diagnosis, f"Tree of thought: {winner.id} scored higher but was rejected ({why}); diagnosis kept."
            )
            return res
        res.diagnosis = self._build_switched(diagnosis, winner, rev, grounded, res, scores)
        res.verdict = "switched"
        res.after_false_alarm = claims_false_alarm(res.diagnosis.root_cause)
        return res

    def _build_switched(
        self,
        original: Diagnosis,
        winner: Branch,
        rev: RevisedDiagnosis,
        grounded: list[bool],
        res: ToTResult,
        scores: str,
    ) -> Diagnosis:
        root = rev.root_cause
        if rev.is_false_alarm and not claims_false_alarm(root):
            root = f"False alarm: no real incident. {root}"
        quotes = [
            EvidenceItem(
                source_type=EvidenceType.META,
                source=f"tot:{winner.id}",
                detail=q,
                confidence_weight=1.0 if ok else 0.3,
            )
            for q, ok in zip(rev.supporting_quotes, grounded, strict=True)
        ]
        conf = round(max(0.0, rev.confidence - UNGROUNDED_PENALTY * (len(grounded) - sum(grounded))), 2)
        if res.margin is not None and res.margin < self.confidence_margin:
            conf = min(conf, LOW_MARGIN_CAP)
        d = Diagnosis(
            agent_type=self.agent_type,
            incident_id=original.incident_id,
            root_cause=root,
            confidence=conf,
            evidence=[*original.evidence, *quotes],
            affected_service=original.affected_service,
            category=rev.category,
            reasoning_steps=[
                *original.reasoning_steps,
                ReasoningStep(
                    step_number=len(original.reasoning_steps) + 1,
                    description=f"Tree of thought switched to {winner.id} (scores {scores}; margin {res.margin:+.2f}).",
                ),
            ],
            recommendation=rev.recommendation,
            risk_tier=rev.risk_tier,
        )
        return apply_risk_guard(d)[0] if self.risk_guard else d


class ToTOrchestrator:
    """Stage 6 orchestrator followed by Stage 9 tree of thought."""

    agent_type = "llm_tot"

    def __init__(
        self,
        client: LLMClient,
        *,
        temperature: float = 0.0,
        risk_guard: bool = False,
        tot_kwargs: dict[str, Any] | None = None,
        **orchestrator_kwargs: Any,
    ) -> None:
        self.client = client
        self.orchestrator = LLMOrchestrator(
            client, temperature=temperature, risk_guard=risk_guard, **orchestrator_kwargs
        )
        self.tot = TreeOfThought(client, temperature=temperature, risk_guard=risk_guard, **(tot_kwargs or {}))
        self.record: ToTResult | None = None

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
        self.record = self.tot.run(
            incident_id,
            meta,
            base,
            evidence_from_outcomes(outcomes),
            signals=compute_signals(outcomes),
            degraded=self.orchestrator.degraded is not None,
        )
        return self.record.diagnosis
