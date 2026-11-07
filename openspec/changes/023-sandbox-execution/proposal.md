# Proposal: Stage 16 — Sandbox Code/Shell Execution Safety

- **Status:** Implemented
- **Stage:** 16 of 23
- **Phase:** E — Tools & Integration
- **Depends on:** Stage 15 (reporting), `openspec/risk-classification.md`

## 1. Problem

The risk classification says executing a remediation script or shell command outside the read-only set is **high
risk** and needs human approval. Before the approval gate (Stage 18) can exist, something has to decide *what* may run
at all and *where* it runs, so that a wrong or hijacked agent cannot reach the host.

## 2. Scope

In scope: an allow-list of typed commands (no shell), a policy that classifies and fingerprints a command line, an
approval *interface* (verification only), an append-only audit log, three isolation backends with declared guarantees,
and a sandbox-escape test suite that assumes the attacker already runs arbitrary code inside the sandbox.

Out of scope: the human approval gate and its UX (Stage 18), real remediation against real infrastructure (needs
network and credentials, therefore approval-gated), wiring the runner into agents or MCP servers (needs a go-ahead).

## 3. Design decisions

1. **Allow-list first, isolation second.** Nothing outside nine registered commands can run; arguments are typed and
   bounded; shell syntax (`; | & > $( ) \``), newlines and expansion are refused, not interpreted.
2. **The sandbox gets no file-system access to the project.** Incident data is sent on stdin as JSON; the child is the
   standard-library-only `commands.py` source run with `python -I -S -B -c`; it re-validates everything it receives.
3. **Fail closed.** Every backend declares `guarantees`; a command that needs `network`, `filesystem` and `processes`
   isolation is refused on a backend that does not claim them, even with a valid approval.
4. **Approvals are bound and single use.** Fingerprint = sha256(incident, command, normalised arguments); short TTL;
   nonce consumed in the audit log before execution, so replay fails across restarts. An unwritable log blocks execution.
5. **Dry run is low risk.** `--dry-run` only describes the change; the parent sets `apply` only after a matching approval.
6. **Remediation is simulated** against a deterministic cluster derived from the incident data and is labelled as such.

## 4. Acceptance criteria (ROADMAP: "isolation verified via sandbox-escape attempt tests")

- Escape attempts run *through the backend* for: environment secrets, network (loopback, internet, DNS), host file read,
  symlink traversal, host file write, capabilities / no_new_privs / mount / chroot, detached daemons and child-process
  storms (survivor scan of `/proc`), memory bomb, CPU spin, stdout/stderr flood.
- A backend that claims a guarantee and leaks fails the suite (verified with a deliberately lying backend).
- On a backend that does not claim a guarantee the same attacks are reported as "would escape" (positive control).
- 36 hostile command lines, malformed incident ids and forged / expired / replayed approvals never reach a backend.

## 5. Known limits

Shared kernel (not a multi-tenant boundary). The namespace backend needs Linux with unprivileged user namespaces.
The Docker backend is unit-tested through its command line and a fake CLI; run `selftest --backend docker` on a machine
with Docker. On Windows only `process` and `docker` exist: use Docker Desktop or WSL2 for mutating commands.
A hard fork-bomb limit exists only in Docker (`--pids-limit`); other backends are bounded by timeout and a pid namespace.
