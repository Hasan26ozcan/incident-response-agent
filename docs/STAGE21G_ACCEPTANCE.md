# Stage 21g — Whole-system acceptance

Until now each layer was tested alone and a few together. This stage runs **everything in one chain**, offline, with a scripted model:

```
python -m incident_agent.acceptance            # about 30 s, all 10 steps
python -m incident_agent.acceptance --quick    # skips the live processes and the eval gate (about 10 s)
python -m incident_agent.acceptance --only service-assist --json acceptance.json
```
Exit code 0 only if every step passes. Run it after any change, and before any release.

| Step | What it proves |
|---|---|
| imports | every module imports (optional packages such as torch are reported, not required) |
| data | dataset, five lab captures and gold files are present |
| lab | real processes + injected fault -> a captured incident folder |
| pipeline | the five lab incidents go through the real engine/sandbox: memory leak, CPU and bad deploy propose a **gated** action; dependency and false alarm propose none; shadow mode changes nothing |
| security | the 33 attacks: 0 vulnerable, 0 open gaps; INJ-09/12/13 and APP-08 blocked |
| service-shadow | real HTTP server: auth, scopes, submit, status, metrics, error redaction, kill-switch |
| service-assist | human approval over HTTP, sealed, **applied exactly once**, **service restarts while the request is open and it still completes**, a forged store line is refused |
| measure | the real agent loop on lab data is scored, costed and reported |
| gate | the committed agentic eval baseline passes |
| no-leaks | a secret planted in the environment at the start is in **no** file, response or notification produced by the whole run |

## What it found
The first full run exposed a real bug no single-layer test had: after a service restart, a run that was waiting for approval could not complete, because the job table lived only in memory. The service now rebuilds it from disk at start-up; runs cut short by a crash are reported as `interrupted` instead of vanishing.

## Not covered (honest)
Real model quality (Stage 21d is run by you with keys), a real Docker Compose start (needs your daemon), Slack delivery to a real workspace, Temporal server, TLS.
