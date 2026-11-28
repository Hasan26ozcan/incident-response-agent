# Proposal: Stage 21h — Security and quality audit

- **Status:** Implemented
## Decisions
1. mypy strict on three platforms; ruff S/B/PLE/SIM enabled.
2. Security-relevant asserts become explicit fail-closed checks.
3. Project semgrep rules, detect-secrets baseline, pip-audit, radon gate, Trivy/hadolint/Sonar in CI.
4. Findings and debt are documented, including what could not be run in the sandbox.
## Out of scope
Refactoring the remaining D/E complexity functions; digest-pinning base images.
