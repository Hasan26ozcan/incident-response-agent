"""Hierarchical swarm (Stage 7): Incident Commander -> Orchestrator -> specialist workers.

``HierarchicalSwarm.run`` is the end-to-end diagnosis flow from the roadmap (no actions): the
orchestrator investigates and synthesises, then the commander reviews, assigns severity and decides
escalation. The scored diagnosis is ``result.diagnosis`` (possibly revised by the commander).
"""

from __future__ import annotations

from typing import Any

from incident_agent.agents.llm_commander import CommanderResult, LLMIncidentCommander, compute_signals
from incident_agent.agents.llm_debate import DebateResult, LLMDebate, evidence_from_outcomes
from incident_agent.agents.llm_orchestrator import LLMOrchestrator, WorkerOutcome
from incident_agent.agents.tree_of_thought import ToTResult, TreeOfThought
from incident_agent.llm import LLMClient
from incident_agent.schemas.command_diagnosis import CommanderDiagnosis
from incident_agent.tools.toolbox import IncidentToolbox


class HierarchicalSwarm:
    agent_type = "llm_swarm"

    def __init__(
        self,
        client: LLMClient,
        *,
        allow_revision: bool = True,
        temperature: float = 0.0,
        confidence_threshold: float = 0.7,
        risk_guard: bool = False,
        debate: bool = False,
        tot: bool = False,
        **orchestrator_kwargs: Any,
    ) -> None:
        self.client = client
        self.orchestrator = LLMOrchestrator(
            client, temperature=temperature, risk_guard=risk_guard, **orchestrator_kwargs
        )
        self.commander = LLMIncidentCommander(
            client,
            allow_revision=allow_revision,
            confidence_threshold=confidence_threshold,
            temperature=temperature,
            risk_guard=risk_guard,
        )
        self.debate = LLMDebate(client, temperature=temperature, risk_guard=risk_guard) if debate else None
        self.debate_record: DebateResult | None = None
        self.tot = TreeOfThought(client, temperature=temperature, risk_guard=risk_guard) if tot else None
        self.tot_record: ToTResult | None = None
        self.result: CommanderResult | None = None

    # -- attributes the eval harness reads for traces --------------------------------
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

    def run(self, incident_id: str) -> CommanderDiagnosis:
        self.result = None
        self.debate_record = None
        self.tot_record = None
        diagnosis = self.orchestrator.run(incident_id)
        meta = IncidentToolbox(incident_id).call("get_incident_meta", {})
        if self.tot is not None:  # Stage 9: branch and select before the debate and the commander see it
            outcomes = list(self.orchestrator.outcomes.values())
            self.tot_record = self.tot.run(
                incident_id,
                meta,
                diagnosis,
                evidence_from_outcomes(outcomes),
                signals=compute_signals(outcomes),
                degraded=self.orchestrator.degraded is not None,
            )
            diagnosis = self.tot_record.diagnosis
        if self.debate is not None:  # Stage 8: the commander reviews the debated diagnosis
            outcomes = list(self.orchestrator.outcomes.values())
            self.debate_record = self.debate.run(
                incident_id,
                meta,
                diagnosis,
                evidence_from_outcomes(outcomes),
                signals=compute_signals(outcomes),
                degraded=self.orchestrator.degraded is not None,
            )
            diagnosis = self.debate_record.diagnosis
        self.result = self.commander.run(
            incident_id,
            meta,
            diagnosis,
            list(self.orchestrator.outcomes.values()),
            degraded=self.orchestrator.degraded is not None,
        )
        return self.result.output
