# Contributing

## Workflow

This project follows spec-driven development via OpenSpec (see
[`openspec/README.md`](./openspec/README.md)): non-trivial changes start as
a proposal under `openspec/changes/NNN-name/proposal.md` before any code
is written. Each roadmap stage in [`ROADMAP.md`](./ROADMAP.md) gets its own
proposal.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
make install
```

## Before you commit

```bash
make ci
```

This runs exactly what CI runs (`.github/workflows/ci.yml`): ruff lint,
ruff format check, bandit security scan, mypy, pytest with coverage, and
the Stage 1 dataset validator. If `make ci` is green locally, CI will be
green too.

Optionally, install the pre-commit hooks so fast checks (ruff) run on
every commit automatically:

```bash
pip install pre-commit
pre-commit install
```

## Common tasks

| Task | Command |
|---|---|
| Install with dev deps | `make install` |
| Lint | `make lint` |
| Auto-format | `make format` |
| Type check | `make typecheck` |
| Security scan (bandit) | `make security` |
| Run tests | `make test` |
| Regenerate the Stage 1 dataset | `make regen-data` |
| Validate dataset integrity | `make validate-data` |
| Full local CI | `make ci` |
| Scan sandbox for vulns (trivy) | `make trivy-scan` |
| Scan filesystem for vulns (trivy) | `make trivy-fs` |
| Build the sandbox image | `make sandbox-build` |
| Interactive sandbox shell | `make sandbox-shell` |

> **Note:** `make trivy-scan` and `make trivy-fs` require [trivy](https://aquasecurity.github.io/trivy/) (`v0.59+`) on your PATH. Install it with `pip install trivy` or download the binary from the [GitHub releases](https://github.com/aquasecurity/trivy/releases).

## Security scanning with Trivy

The project includes Trivy-based vulnerability scanning for Docker images
and the project filesystem. These are part of the CI pipeline and available
locally via `make trivy-scan` and `make trivy-fs`.

- `make trivy-scan`: builds the sandbox image and scans it for HIGH/CRITICAL
  vulnerabilities using `trivy image`.
- `make trivy-fs`: scans the `docker/` directory for vulnerable dependencies
  using `trivy fs`.

In CI, both scans run as part of the `lint-typecheck-test` job (filesystem scan)
and the `sandbox-image` job (image scan). Both are configured to fail on
HIGH/CRITICAL findings.

## Code style

Enforced by `ruff` (see `[tool.ruff]` in `pyproject.toml`) — 100-char
lines, isort-style import sorting, pyupgrade, bugbear, and a subset of
security checks. One deliberate exception: `data/generate_dataset.py` is
exempted from line-length and a couple of style rules because it's a
large declarative data table with long prose strings, where wrapping
would hurt readability rather than help — see the comment in
`pyproject.toml` for the rationale.

## Adding a new roadmap stage

1. Open an OpenSpec proposal under `openspec/changes/NNN-stage-name/`.
2. Implement against the proposal's acceptance criteria.
3. Add or update tests under `tests/` (or alongside the relevant
   subpackage once stages introduce more structure).
4. `make ci` must pass before opening a PR.
