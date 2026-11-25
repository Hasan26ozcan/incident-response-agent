# Proposal: Stage 21f — Service layer

- **Status:** Implemented
- **Depends on:** 21c (pipeline), 21e (store chain), 18 (approvals), 20 (kill-switch)

## Decisions
1. stdlib HTTP, one process, one worker; shadow mode default; assist mode opt-in.
2. Scoped, digest-only bearer tokens; approvers keep their own roster tokens.
3. Loopback by default; no TLS in-process (reverse proxy).
4. Health/ready/metrics endpoints; static dashboard without secrets.
5. Slack via the existing sender, webhook only from the environment.

## Out of scope
TLS, SSO, multi-tenancy, horizontal scale, persistent job queue.
