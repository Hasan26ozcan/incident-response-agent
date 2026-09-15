# Sandbox execution environment for agent-proposed code/shell actions.
#
# Scope at Stage 2: this establishes the hardened BASE IMAGE only —
# non-root user, minimal surface, no network unless explicitly opted into
# at `docker run` time. It does not yet enforce a command allow-list;
# that's Stage 16 (see ROADMAP.md Phase E, "Sandbox Kod/Shell Çalıştırma
# Güvenliği" / Sandbox Code-Shell Execution Safety), which will add an
# allow-listing layer on top of this image rather than replacing it.
#
# Every risky action an agent proposes is classified per
# openspec/risk-classification.md before it ever reaches this container —
# this image is the last line of isolation, not the only one.

FROM python:3.11-slim

# Non-root user. Never run agent-proposed code as root, even inside an
# already-isolated container — defense in depth.
RUN groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash sandbox

WORKDIR /home/sandbox

# Minimal, predictable base — no extra packages installed here. Later
# stages that need specific tooling inside the sandbox (e.g. a log-parsing
# library for an MCP server call made from within it) should extend this
# image explicitly and document why, rather than this base image growing
# an unbounded package list over time.

USER sandbox

# No CMD/ENTRYPOINT that does anything on its own — this image is meant to
# be invoked per-command (`docker run ... <command>`), not as a long-lived
# service. `docker run --rm --network none --read-only <image> <cmd>` is
# the expected invocation shape; see docker-compose.yml and Makefile for a
# working example, and docker/sandbox/README.md for the full rationale.
CMD ["/bin/bash"]
