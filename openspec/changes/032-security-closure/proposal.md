# Proposal: Stage 21e — Security closure

- **Status:** Implemented
- **Closes:** INJ-09 (hardening default), APP-08 (request-store tampering)

## Decisions
1. Hardening on by default; explicit opt-out for measurement only.
2. HMAC chain over the request journal, optional key (backward compatible), fail closed on tamper.
3. Both attacks report `vulnerable` on regression so CI fails.

## Out of scope
Same-user key theft, INJ-04 and INJ-07 (semantic), real Docker selftest (owner's machine).
