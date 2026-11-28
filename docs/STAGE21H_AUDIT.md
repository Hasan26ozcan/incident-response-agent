# Stage 21h — Security and quality audit

Tools run on the final tree, what each found, and what was done. "Not run here" means the sandbox could not run it; the CI workflow does.

| Tool | Result | Action |
|---|---|---|
| **mypy --strict** (src, 174 files) | 66 errors on first run, **0 now**, on linux, win32 and darwin | fixed; `strict = true` in `pyproject.toml`; CI runs all three platforms |
| **bandit** (project config) | 0 issues; unconfigured run listed only low items | 3 security-relevant `assert`s (sandbox approval binding) became explicit checks that fail closed (an `assert` disappears under `python -O`) |
| **ruff** + new rule sets `S` (security), `B` (bugbear), `PLE`, `SIM` | all clean | enabled in `pyproject.toml`; deliberate exceptions are per-file with a reason |
| **semgrep** | registry packs unreachable from the sandbox (not run here); 10 project rules written (`security/semgrep-rules.yml`) and run: **0 findings in src** | CI runs the project rules and `p/python`, `p/owasp-top-ten` |
| **pip-audit** (all declared dependencies) | **no known vulnerabilities** | CI repeats it weekly |
| **detect-secrets** + pattern grep over the repo | no real secrets; 118 reviewed fake fixtures recorded in `.secrets.baseline`; a planted fake key is detected | CI fails on any new finding |
| **radon** (complexity, maintainability) | average **A (3.8)**; one function was grade F (`gate`, 48) | `gate` split into six section checks (same output); CI blocks any new grade F |
| **vulture** (dead code) | only interface-mandated names (`log_message` override) and an unused parameter (`window_hours`) | noted below |
| **Trivy** | not obtainable in the sandbox (download blocked, no Docker daemon): **not run here** | CI already scans `docker/` and the sandbox image; now also the whole repo (vuln, secret, misconfig) and the service image |
| **hadolint** | not run here | CI lints both Dockerfiles; manual review: non-root user, no secrets, health check present, base image not digest-pinned |
| **SonarQube / SonarCloud** | needs your server or account: **not run here** | `sonar-project.properties` + CI job (runs when `SONAR_TOKEN` exists); the rules Sonar applies most (complexity, dead code, security hotspots, duplication) were covered by radon, vulture, bandit and ruff |

## Real bugs the audit found
1. `--agent swarm --report` passed the commander object where a `Diagnosis` is required: the post-mortem would have crashed (found by `mypy --strict`, now fixed and tested).
2. Three `assert`s guarding approval binding could vanish under `python -O` (fixed, fail closed).
3. A vacuous test assertion (`... or True`) now asserts something.
4. Auto-fixers proposed changes that would have broken `ResourceCache` membership; instead it now implements `__contains__` (tested).

## Known technical debt (tracked, not hidden)
Functions above grade D, in order: `TreeOfThought.run` (E 40), `eval.validate_kb.validate` (E 38), `observe.report.summarize` (E 32), then ten at D (21-25). They work and are tested; splitting them is behaviour-neutral refactoring for a later pass. Also: `get_recent_deploys(window_hours)` ignores its parameter; base images are tags, not digests (pin before a production release).

## Run it yourself
```
mypy --python-version 3.12 --platform win32
bandit -c bandit.yml -r src
ruff check . ; ruff format --check .
pip install semgrep pip-audit detect-secrets radon vulture
semgrep --config security/semgrep-rules.yml --metrics=off src
pip-audit
radon cc src -n E -s
trivy fs --scanners vuln,secret,misconfig .       # needs Trivy installed
```
