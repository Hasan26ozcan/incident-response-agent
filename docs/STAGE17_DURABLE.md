# Stage 17 — Durable workflow (resume after interruption)

```
python -m incident_agent.durable demo                          # kill the worker mid-run, resume, approve, nothing repeats
python -m incident_agent.durable start INC-002 --run-id r1 --no-wait
python -m incident_agent.durable status r1
python -m incident_agent.durable signal r1 approval --by alice # add --reject to refuse
python -m incident_agent.durable resume r1 --no-wait           # after a crash; --retry-failed retries a failed run
python -m incident_agent.durable cancel r1
python -m incident_agent.durable list
```
Runs live under `INCIDENT_RUNS_DIR` (default `.cache/runs`): one `journal.jsonl`, `signals.jsonl`, diagnostic
`lock.json`, and `sandbox-audit.jsonl` per run. Windows uses a named OS mutex keyed to the run directory.
`--backend namespace|docker|process` picks the sandbox (mutating commands need
`namespace` or `docker`; with `process` they are refused and the report says so). `--crash-after EVENT:STEP` abruptly terminates the worker after that journal event (SIGKILL on POSIX; forced exit on
Windows), e.g. `step_completed:diagnose`.

## Guarantees
| Property | How |
|---|---|
| No lost progress | every event is fsynced before it counts; state is a fold of the journal |
| No repeated work | finished steps are read from the journal |
| `execute` at most once | `max_attempts=1`, attempts counted across crashes, deterministic approval nonce |
| Waits survive restarts | the deadline is stored in the journal |
| One worker per run | OS file lock, dropped automatically when the process dies |
| Safe resume | workflow name, version and steps are checked against the journal |

Steps are at-least-once: a step that was started but not finished when the worker died runs again
(`interrupted_before=True` in the journal) and must be idempotent.

## Temporal (optional)
```
pip install "temporalio>=1.7"
temporal server start-dev                                      # separate terminal (Temporal CLI)
python -m incident_agent.durable.temporal worker
python -m incident_agent.durable.temporal start INC-002        # prints the workflow id
python -m incident_agent.durable.temporal signal <workflow-id> --by alice
python -m incident_agent.durable.temporal status <workflow-id>
```
The server-backed tests in `tests/test_temporal_adapter.py` skip when no server can be started; run them where the
Temporal CLI is installed.
