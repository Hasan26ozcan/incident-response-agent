# Stage 16 — Sandbox execution safety

Run from the project root (virtualenv active):

```
python -m incident_agent.sandbox commands          # allow-listed commands and their risk tier
python -m incident_agent.sandbox backends          # which isolation backends work on this machine
python -m incident_agent.sandbox run INC-001 "logs-grep timeout --max 5"
python -m incident_agent.sandbox run INC-001 "restart-service checkout-api"          # -> needs approval + fingerprint
python -m incident_agent.sandbox run INC-001 "restart-service checkout-api --dry-run" # low risk, describes only
python -m incident_agent.sandbox selftest [--backend docker|namespace|process]       # sandbox-escape attempts
```

`--approve FINGERPRINT --approved-by NAME` is a local stand-in for the human gate (Stage 18). It is a demonstration,
not a security boundary against an agent that can run the CLI itself.

## Layers

| Layer | File | Job |
|---|---|---|
| Commands | `commands.py` | typed allow-list, strict parser, simulated remediation (stdlib only, also the child program) |
| Policy | `policy.py` | tokenise without a shell, refuse operators and expansion, classify, fingerprint |
| Approval | `approval.py` | verify a bound, expiring, single-use approval |
| Audit | `audit.py` | fsynced JSONL, redacted, remembers consumed nonces |
| Backends | `backends.py` | `process`, `namespace` (Linux), `docker`; each declares what it guarantees |
| Runner | `runner.py` | policy -> approval -> isolation check -> audit -> execution |
| Escape tests | `selftest.py` | hostile programs run through the backend; policy attacks; forged approvals |

## Backend guarantees

| Backend | env | time / output | memory | network | filesystem | processes | caps |
|---|---|---|---|---|---|---|---|
| process | yes | yes | POSIX only | no | no | no | no |
| namespace | yes | yes | yes | yes | yes | yes | yes |
| docker | yes | yes | yes | yes | yes | yes | yes |

Mutating commands need network + filesystem + processes isolation; `process` therefore runs read-only commands and dry
runs only.

## Measured in the build sandbox (Linux, root, Python 3.13)

- `selftest --backend namespace`: 16 passed, 0 failed, 0 skipped.
- `selftest --backend process`: 8 passed, 8 skipped; every skip says "would escape" (positive control).
- `tests/test_sandbox.py`: 124 tests, 99 % line coverage of `incident_agent.sandbox`; mutation check: 37 deliberately broken
  variants, 0 survivors.
- Windows local run: sandbox tests and the `process` self-test; memory isolation is not claimed and the memory-bomb
  attack is skipped rather than risk an unbounded allocation. Network/filesystem/process isolation requires Docker.
  The Docker backend was not tested against a real daemon.
- Not measured: macOS.
