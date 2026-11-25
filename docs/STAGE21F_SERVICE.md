# Stage 21f — Service layer

A small HTTP service around the pipeline: one process, stdlib only, **shadow mode by default** (it reads, diagnoses and proposes; it never changes anything).

## Run it (PowerShell, project root, `.venv` active)
```
python -m incident_agent.service token add ci --scopes read,submit     # prints the token ONCE; keep it in a password manager
python -m incident_agent.service serve                                  # http://127.0.0.1:8080  (needs GROQ_API_KEY in .env)
```
Open `http://127.0.0.1:8080/`, paste the token, press Load. Submit with `curl`:
```
$h = @{ Authorization = "Bearer <token>" }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8080/api/incidents -Headers $h -ContentType application/json -Body '{"incident_id":"LAB-002"}'
Invoke-RestMethod -Uri http://127.0.0.1:8080/api/runs -Headers $h
```
No model key? `serve --agent react_baseline` runs the rule-based baseline (no LLM). Slack: set `SLACK_WEBHOOK_URL` in `.env` (name only here; a webhook pasted in chat is compromised, revoke it).

## Endpoints
| Method / path | Auth | What |
|---|---|---|
| `GET /healthz` | none | liveness: `{"status":"ok"}` |
| `GET /readyz` | none | worker alive, kill-switch, queue depth (503 when not ready) |
| `GET /` | none | static dashboard (holds no secret; the token is typed into the page and lives only in that tab) |
| `POST /api/incidents` | `submit` | `{"incident_id":"LAB-002"}` -> 202 + run id; same incident already open -> 200 `duplicate` |
| `GET /api/runs`, `/api/runs/<id>` | `read` | status, diagnosis, plan, execution, cost, related knowledge |
| `GET /metrics` | `read` | Prometheus text: jobs by status, queue depth, kill-switch, counters |
| `GET /api/requests` | `read` | approval requests (assist mode only) |
| `POST /api/requests/<id>/approve` / `reject` | approver | headers `X-Approver`, `X-Approver-Token`, body `{"confirm":"<first 8 of fingerprint>"}` |

## Security properties (each one has a test)
* Loopback only; `--host 0.0.0.0` is refused without `--allow-remote` (put TLS in front).
* Tokens are stored as SHA-256 digests, shown once, scoped (`read`, `submit`). A service token can never approve: approvers use their own roster token (`python -m incident_agent.service roster add alice --roles oncall`).
* Failed logins are throttled (10/min per address, then 429); per-token submit rate limit; bounded queue; 8 KB JSON bodies only.
* Kill-switch engaged: no new work and no decisions (503); `/readyz` says so.
* Error text and Slack messages pass through the redactor; one failed investigation never stops the service.
* Responses carry `nosniff`, `no-store` and a strict Content-Security-Policy; the dashboard writes with `textContent` only.

## Assist mode (optional, off by default)
`serve --mode assist` leaves a mutating plan waiting for a human. Add approvers with `roster add`, approve through the API with the fingerprint prefix (the request store is HMAC-chained, Stage 21e), and only then does the sandboxed action run. Keep shadow mode until you trust the measured quality (Stage 21d).

## Docker
```
docker compose -f deploy\docker-compose.yml up --build
```
Published on `127.0.0.1:8080` only, read-only filesystem, no capabilities, state in a volume. Create tokens inside it: `docker compose -f deploy\docker-compose.yml exec incident-agent python -m incident_agent.service token add ci --scopes read,submit`.
The compose file was validated structurally but **not started in the build sandbox** (no Docker daemon there): this is your first real run.

## Limits
Single process, one investigation at a time (free-tier friendly). No TLS, no SSO, no multi-tenant isolation. Run records live on local disk. The dashboard is minimal by design.
