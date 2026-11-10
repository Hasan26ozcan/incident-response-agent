# Stage 18 — Human-in-the-loop approval and escalation

High-risk actions (every mutating command, `openspec/risk-classification.md`) need a human. The gate blocks until
someone approves, escalates when nobody answers, and **never** treats silence as consent.

```
request opened (level 1: oncall)
   ├─ enough valid approvals ............................ approved  -> run gets approved:true  -> command runs (once)
   ├─ one eligible rejection ............................ rejected  -> dry run
   ├─ level timeout ..... escalate: level 2 (lead) ... level 3 (management)
   ├─ last level times out .............................. expired   -> dry run
   └─ the run stops waiting (cancelled / timed out) ..... cancelled
```

## Quick start (PowerShell; Linux/macOS use `export` instead of `$env:`)

```powershell
python -m incident_agent.hitl demo                                  # both scenarios in ~8 seconds, no setup

python -m incident_agent.hitl roster add alice --role oncall        # prints alice's token ONCE
python -m incident_agent.hitl roster add bob   --role lead
python -m incident_agent.hitl start INC-002 --requested-by carol --backend namespace
python -m incident_agent.hitl serve                                 # keep running: opens requests, escalates, delivers
# in another terminal:
python -m incident_agent.hitl list --status pending
python -m incident_agent.hitl show inc-002-<id>.approval
$env:INCIDENT_APPROVER_TOKEN = "<alice's token>"
python -m incident_agent.hitl approve inc-002-<id>.approval --by alice --confirm <first 8 chars of the fingerprint>
```

`--live-slack` (global flag, before the command) also posts to the webhook in `SLACK_WEBHOOK_URL`; the local outbox
(`<root>/outbox.jsonl`) is always written. `--backend` picks the Stage 16 sandbox (mutating commands need `namespace`
or `docker`; with `process` they are refused and the report says so).

## Policy (`<root>/policy.json`, or `--policy FILE`)

```json
{
  "levels": [
    {"name": "primary",    "roles": ["oncall"],  "timeout": 300},
    {"name": "secondary",  "roles": ["lead"],    "timeout": 300, "mention": "<!subteam^S0123ABC>"},
    {"name": "management", "roles": ["manager"], "timeout": 600}
  ],
  "required": {"high": 2},
  "default_required": 1
}
```
Timeouts are seconds, 1-5 levels, 1-3 required approvals (never 0). The request deadline is capped by the durable
run's own `approval` wait. `start` sets that wait to the policy's total so they agree.

## Rules the gate enforces

| Rule | Where |
|---|---|
| Silence is a refusal (expired ⇒ `approved:false`) | `gate._advance_locked`, `bridge.approval_payload` |
| Late answer cannot revive an expired request | timeouts are applied before every decision |
| Token authentication, generic error | `roster.authenticate` |
| Only roles asked so far may answer | `RequestState.roles_asked` |
| Distinct approvers; requester cannot approve | `gate._decide` |
| Approver confirms the fingerprint | `--confirm` |
| One rejection from an eligible approver is final | `gate._decide` |
| Refused attempts are logged (capped at 50) | `attempt_denied` events |
| Notification failure never loses a request | journalled, retried ≤5 times per level |

Everything is appended to `<root>/requests/<id>/journal.jsonl`; nothing is edited.

## Limits (read these)

* Authentication is possession of a local token, not single sign-on. Do not hand tokens to agents or share them.
* Slack delivery is tested against fakes, not against a real workspace.
* Wall-clock time of the machine running `serve` drives escalation.
* Nothing is wired into the agents or MCP servers; Stage 19+ comes next.
