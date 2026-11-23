# Proposal: Stage 21b — Fault Lab

- **Status:** Implemented
- **Depends on:** Stages 3 (tools), 19 (eval harness)

## Problem
All incident data was synthetic. Nothing showed how the system behaves on logs and metrics produced by running software.

## Decisions
1. A tiny two-service system with real, injectable faults (stdlib only) plus Docker Compose packaging.
2. Capture into the existing incident-folder format so nothing downstream changes.
3. Ground truth is declared per scenario, never inferred; metadata stays non-spoiler.
4. `LAB-nnn` ids are allowed alongside `INC-nnn`; path-syntax protection is unchanged.

## Out of scope
Production monitoring integration (Prometheus/Loki readers), long-running soak faults, Kubernetes.

## Acceptance
`pytest tests/test_lab.py` passes; `python -m incident_agent.lab run LAB-001` produces a readable incident; the rule-based
baseline result on the captures is reported honestly.
