# Proposal: Stage 2 — Repo Skeleton & CI Foundation

- **Status:** Approved
- **Stage:** 2 / 22
- **Phase:** A — Foundations & Spec
- **Depends on:** Stage 0 (architecture, risk classification), Stage 1
  (dataset — now exercised by CI, see §3)

## 1. Problem / Motivation

ROADMAP.md is explicit that CI must be set up "en baştan" (from the
start), not bolted on after agent code already exists — because retrofit-
ting lint/type/security rules onto a working system is far more painful
than writing new code against rules that already apply to it, and issues
caught only late are issues that shaped design decisions upstream of
where they were caught. This stage produces an empty-but-CI-green
skeleton so Stage 3 is the first stage to ever write against a codebase
with tooling already enforcing quality, not the last stage to retrofit it.

## 2. Scope

- **Project structure**: `src/incident_agent/` package with placeholder
  subpackages (`agents/`, `tools/`, `memory/`, `workflows/`, `eval/`),
  each with a docstring naming the stage that populates it.
- **Dependency management**: `pyproject.toml` (hatchling build backend),
  currently zero runtime dependencies, `dev` extra with
  ruff/bandit/mypy/pytest/pytest-cov.
- **CI**: `.github/workflows/ci.yml`, two jobs:
  - `lint-typecheck-test`: ruff check, ruff format check, bandit, mypy,
    pytest+coverage, dataset regeneration-matches-committed-output check,
    and the Stage 1 `eval/validate_dataset.py` — matrixed across Python
    3.11 and 3.12.
  - `sandbox-image`: builds the Stage 2 sandbox Docker image and asserts
    its non-root and no-network-by-default properties on every push.
- **Sandbox execution environment**: `docker/sandbox/Dockerfile` — a
  minimal, non-root, `python:3.11-slim`-based isolated container; see
  `docker/sandbox/README.md` for what this stage does and does not set up
  (command allow-listing is explicitly deferred to Stage 16).
- **Local dev parity**: `Makefile` (`make ci` runs exactly what CI runs),
  `docker-compose.yml` for the sandbox, `.pre-commit-config.yaml` for fast
  pre-commit checks, `CONTRIBUTING.md` tying it together.
- **Smoke tests**: `tests/test_smoke.py` — proves the package installs,
  imports, and its console-script entry point (`incident-agent`) works,
  before any real agent logic exists.

Explicitly out of scope: any agent implementation (Stage 3+), any runtime
dependency beyond the dev tooling (added stage-by-stage as needed), and
sandbox command allow-listing (Stage 16).

## 3. Key decision: wire Stage 1's dataset into this stage's CI

Rather than treating Stage 1 and Stage 2 as fully independent, CI's
`lint-typecheck-test` job also re-runs `data/generate_dataset.py` and
`eval/generate_gold_set_md.py` and diffs the result against what's
committed, then runs `eval/validate_dataset.py`. This turns the Stage 1
dataset into a regression-tested asset from Stage 2 onward: if a future
change to the generator accidentally breaks determinism, introduces gold
leakage, or produces an inconsistent scenario, CI catches it on the same
push — rather than the breakage being discovered whenever some later
stage's agent first tries to read the corrupted data.

## 4. Key decision: relax line-length/style rules for one data file, explicitly

`data/generate_dataset.py` (Stage 1) is a large declarative table of 20
incident scenarios with long human-readable prose strings and
named-parameter `dict()` calls used for readability. Running the full
ruff rule set against it as originally configured produced ~290 lint
findings, almost all either line-length violations on prose strings or
style preferences (`dict()` vs `{}`) that don't reflect real problems.
Rather than mechanically reformatting the file into something harder to
read (or, worse, silently loosening the rules project-wide), `pyproject.toml`
carries a narrow, commented `per-file-ignores` exception for exactly this
file and exactly these three rules (`E501`, `C408`, `S311`). Every other
rule — including the ones that found two real issues below — still
applies to it.

Two genuine issues the first lint pass did catch and this proposal fixed:
- An unused loop variable (`for i, spec in enumerate(...)` where `i` was
  never used) — renamed away from the anti-pattern.
- A stale `datetime.timezone.utc` reference where `datetime.UTC` (the
  Python 3.11+ alias) is now preferred — auto-fixed by ruff.

This is the outcome CI-from-the-start is supposed to produce: real bugs
caught immediately, style noise triaged deliberately rather than either
ignored wholesale or fixed with churn that hurts readability.

## 5. Key decision: gitignore fix caught by this stage's own review

While wiring CI, a pre-existing bug in Stage 0's `.gitignore` was found:
a blanket `*.log` rule would have silently excluded every
`data/incidents/*/logs.log` file — the Stage 1 dataset's log stores —
from version control. This is exactly the kind of mistake CI-from-the-
start is meant to catch before it compounds: had Stage 3's agent code
been written and tested against a locally-present-but-never-committed
dataset, the breakage would only surface on a fresh clone, far from the
change that caused it. Fixed by replacing the blanket rule with a
narrower one and documenting why in the gitignore itself.

## 6. Acceptance criteria

- [x] `pyproject.toml` defines the package, dev dependencies, and
      ruff/mypy/bandit/pytest configuration
- [x] `src/incident_agent/` skeleton exists with all Stage 3+ subpackages
      stubbed and documented
- [x] `incident-agent` console script installs and runs
      (`python -m incident_agent.cli`)
- [x] `tests/test_smoke.py` passes with 100% coverage of the skeleton
- [x] `ruff check .`, `ruff format --check .`, `bandit -c pyproject.toml -r src`,
      `mypy`, and `pytest --cov=incident_agent` all pass locally with exit
      code 0 (verified before writing the CI workflow, not just assumed)
- [x] `python3 eval/validate_dataset.py` passes (Stage 1 regression)
- [x] `docker/sandbox/Dockerfile` defines a non-root, minimal sandbox
      image; `docker-compose.yml` and `Makefile` codify its intended
      resource-limited, network-isolated invocation
- [x] `.github/workflows/ci.yml` exists, matrixed across Python 3.11/3.12,
      and includes a dedicated job asserting the sandbox image's isolation
      properties (non-root, no-network-by-default) on every push
- [x] `make ci` reproduces the CI signal locally
- [x] Pre-existing `.gitignore` bug (blanket `*.log` excluding committed
      dataset content) found and fixed as part of this stage's review

## 7. Risks / open questions carried into later stages

- CI's Docker job assumes GitHub Actions' `ubuntu-latest` runners have
  Docker available, which they do today — if that ever changes, the job
  needs an explicit `docker/setup-buildx-action` step added.
- No runtime dependencies exist yet, so `pyproject.toml`'s `dependencies`
  list is empty; Stage 3 is expected to be the first stage to add one
  (pydantic), and should do so via its own proposal rather than silently
  expanding this stage's scope retroactively.
- The sandbox image has no allow-list yet — it is isolated but not yet
  policy-enforcing. Nothing in Stage 3–15 should assume otherwise; that
  guarantee only exists from Stage 16 onward.
