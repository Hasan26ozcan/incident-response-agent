# Real-world runbook — the five things only your machine can prove

PowerShell, project root, `.venv` active. Do them **in this order**: each step is cheap to undo and the next one trusts the one before.
Rules for all of them: keys, webhooks and tokens live only in the local `.env` (gitignored); never paste them in chat or a commit; anything that ever leaked is revoked and recreated.

Before anything: `python -m incident_agent.acceptance` must print `ACCEPTANCE PASS`. If it does not, stop and fix that first.

## 1. Docker Compose (the service + the fault lab)  ~15 min
1. Start Docker Desktop and wait until it says "running". Check: `docker version` shows a *Server* section.
2. Create `.env` from `.env.example` (only names are listed there). For now only `GROQ_API_KEY` matters.
3. Service in shadow mode:
   ```
   docker compose -f deploy\docker-compose.yml up --build -d
   docker compose -f deploy\docker-compose.yml exec incident-agent python -m incident_agent.service token add me --scopes read,submit
   ```
   Copy the token it prints (shown once). Open http://127.0.0.1:8080/ , paste the token, press Load, submit `LAB-002`.
   Healthy looks like: `curl http://127.0.0.1:8080/readyz` -> `"status":"ready"`, and the run ends `completed` with a plan that **needs approval** (nothing applied).
4. Fault lab with real containers: `docker compose -f src\incident_agent\lab\docker-compose.yml up --build -d`, then
   `python -m incident_agent.lab.live capture ...` (see `docs/STAGE21B_FAULT_LAB.md`), then `python -m incident_agent.lab.live reset`. Stop everything with `docker compose ... down`.
5. Sandbox escape tests on your real Docker: `python -m incident_agent.sandbox selftest --backend docker`. Expected: all pass, none "would escape".
If a build fails, send the **last 30 lines** of the output (they contain no secrets).

## 2. Real model quality (Stage 21d)  ~1-2 h of free-tier time
1. `.env`: `GROQ_API_KEY=...` (and optionally `MISTRAL_API_KEY=...`). Check: `python -m incident_agent.eval.preflight` — 3 tiny calls, must be OK.
2. `python -m incident_agent.measure plan cascade` (spends nothing) shows the call count.
3. First real number, 5 incidents: `python -m incident_agent.measure run smoke --sets lab --pace 2`
4. Full: `python -m incident_agent.measure run cascade --sets dev,lab --pace 2`; when it stops on a rate limit, run the **same command again with `--resume`**.
5. `python -m incident_agent.measure report cascade` -> `docs/MEASUREMENTS.md`. Read it with the caveats in `docs/STAGE21D_MEASUREMENT.md` (n=19, crude F1).
Good looks like: LLM F1 clearly above the rule baseline on lab (which scores 0 of 5), cascade saving > 0 **without** a quality drop. Anything else is a finding, not a failure — send me `docs/MEASUREMENTS.md`.

## 3. Slack delivery  ~10 min
1. In Slack: create an app -> Incoming Webhooks -> add to a **test channel** -> copy the URL.
2. Put it in `.env` as `SLACK_WEBHOOK_URL=...` (nowhere else).
3. Post-mortem message: `python -m incident_agent.reporting INC-005 --live` (run twice: the second says "skipped (already announced)").
4. Service notices: restart the service (compose reads `.env`), submit a lab incident; one line per finished run appears in the channel.
5. If the webhook was ever pasted anywhere, delete it in Slack and make a new one.
Good looks like: one message per incident, no secrets in it, no duplicate on re-run.

## 4. Temporal server  ~20 min
1. Install the Temporal CLI (`winget install Temporal.TemporalCLI`, or download from temporal.io) and `pip install "temporalio>=1.7"`.
2. Terminal A: `temporal server start-dev`. Terminal B: `python -m incident_agent.durable.temporal worker`.
3. Terminal C: `python -m incident_agent.durable.temporal start INC-002` (prints the workflow id), then `status <id>` (running, waiting for approval), `signal <id> --by alice`, `status <id>` (completed).
4. The adapter's own tests: `python -m pytest tests\test_temporal_adapter.py -q` (they skip when no server is found; with the server up they run).
5. Kill terminal B in the middle of a run, start it again: the workflow must continue, not restart. Web UI: http://localhost:8233.
Note: the service (Stage 21f) uses the file-based durable engine, not Temporal. Temporal is the optional backend for the same workflow.

## 5. TLS (only if the service must be reachable beyond your own PC)
Locally you do not need it: the service listens on `127.0.0.1` only. To expose it, put a reverse proxy in front and never publish port 8080 itself.
Simplest on Windows: Caddy. `Caddyfile`:
```
agent.example.com {
    reverse_proxy 127.0.0.1:8080
}
```
`caddy run` (automatic Let's Encrypt for a real domain; for a test on your own machine use `localhost { tls internal ... }`). Then change nothing in the service except adding `--allow-remote` if the proxy is on another host. Check: `curl https://agent.example.com/healthz`; `http://` must redirect; a request without a token must be 401. Keep **shadow mode** on anything reachable from outside.
This path has not been run by me; treat the first run as a test and keep the kill-switch handy: `python -m incident_agent.security killswitch engage --dir .cache\service\killswitch --reason "drill"` (releasing it needs the operator token in the `INCIDENT_OPERATOR_SECRET` environment variable; in Docker the directory is `/state/killswitch`).

## Order and stop rules
1 -> 2 -> 3 -> 4 -> 5. After each one: `python -m incident_agent.acceptance --quick`, then `git add -A; git commit -m "real-world: <step>"`.
Stop and tell me if: a run applies something in shadow mode, a secret shows up in any output, the acceptance chain turns red, or a measured quality number is far below the lab baseline.
