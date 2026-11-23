# Stage 21b — Fault Lab (realistic data)

Until now every incident was synthetic text. The lab runs **real processes** and captures what they really did.

```
python -m incident_agent.lab list
python -m incident_agent.lab run LAB-003 --baseline 20 --fault 40      # real processes on loopback, ~1 minute
python -m incident_agent.lab run-all                                   # all five, sequentially
python -m incident_agent.lab eval --agent react_baseline               # score an agent on the captured incidents
python -m incident_agent.lab eval --agent llm_react                    # needs your local .env (see below)

# Docker Compose version (same service code, containerised, with a load generator):
docker compose -f lab/docker-compose.yml up --build -d
python -m incident_agent.lab.live capture LAB-003 --baseline 30 --fault 60
python -m incident_agent.lab.live reset
docker compose -f lab/docker-compose.yml down
```

## What runs

`checkout-api` (system under investigation) calls `payments-stub` (its dependency) over HTTP. A load generator drives
traffic. Faults are injected through `/admin`, the same way an operator or a chaos tool would; the service's logs never
announce the fault. `/metrics` reports what the process actually did (CPU time, Python heap via tracemalloc, request
latency p99, error rate, rps).

| Scenario | Fault (real effect) | Ground truth category |
|---|---|---|
| LAB-001 | busy loop on every request (CPU saturates, latency rises) | cpu_exhaustion |
| LAB-002 | 2 MB retained per request until allocations fail (memory grows, OutOfMemory, 5xx) | memory_leak |
| LAB-003 | payments-stub answers in 2.5 s; checkout-api times out at 1 s (504s, its own CPU/memory normal) | cascading_failure |
| LAB-004 | deploy 2.4.1 without a config key (KeyError on every request) | bad_deploy |
| LAB-005 | five-fold traffic from a load test; nothing breaks | false alarm |

Each capture is written in the existing incident-folder format (`data/lab/LAB-00N/{meta,logs,metrics,deploys}` +
`data/lab/gold/LAB-00N.json`), so every tool, agent and the Stage 3 scorer work unchanged. Incident ids `LAB-nnn` are now
accepted by the schemas, the data tools and the sandbox allow-list (path syntax is still refused). Five captured runs are
committed so evaluation works offline; re-run to refresh them.

## Finding (measured, not assumed)

The rule-based baseline agent scores **0 of 5 answered** on the live captures: it was tuned to the synthetic dataset's
thresholds and wording. This is the reality gap the lab exists to expose; the LLM agents are what should be measured here.

## Honest limits

* Real processes, but toy ones: one application, five fault types. It is a fault lab, not a production system.
* Metrics are sampled each second inside the service (a sidecar like Prometheus would be the production equivalent).
* `mem_mb` is the Python heap (tracemalloc), not RSS; CPU is process CPU time over wall time, capped at 100.
* Docker Compose files are validated (`docker compose config`) but were not started in the build environment (no Docker daemon
  there). The service code itself ran as real processes and is covered by tests. Run the Compose lab once on your machine.
* Timings (baseline and fault length) are short so a run takes about a minute; long slow-burn faults would need longer holds.
