# Sandbox Execution Environment

Isolated container for running agent-proposed code or shell commands.
Introduced at Stage 2 per ROADMAP.md ("Sandbox execution ortamı (Docker
izole container) hazırlanır") so every later stage that needs to execute
something on the agent's behalf has a hardened base to build on from day
one, instead of bolting isolation on after the fact.

## What this stage sets up

- `Dockerfile`: a minimal, non-root `python:3.11-slim`-based image.
- Nothing else runs as root inside it, and it ships with no extra
  packages — see the Dockerfile's own comments for why.

## What this stage deliberately does NOT set up yet

- **Command allow-listing.** Stage 16 ("Sandbox Kod/Shell Çalıştırma
  Güvenliği") adds the layer that decides *which* commands an agent may
  run inside this container. At Stage 2, the container is isolated but
  unopinionated about what runs in it — that's intentional; allow-listing
  needs the agent/tool architecture from Stages 3–15 to exist first.
- **Resource-limit enforcement in code.** The limits below are documented
  invocation patterns (flags to `docker run`), not yet wired into any
  Python orchestration code — that lands when an agent actually calls
  into the sandbox, starting around Stage 13 (MCP servers).

## Invocation pattern every later stage should follow

```bash
docker build -t incident-agent-sandbox:local -f docker/sandbox/Dockerfile docker/sandbox

docker run --rm \
  --network none \
  --read-only \
  --memory 256m \
  --cpus 0.5 \
  --user sandbox \
  incident-agent-sandbox:local \
  <command>
```

- `--network none`: no network access by default. If a specific future
  use case genuinely needs it (e.g. an MCP call made from inside the
  sandbox), that's an explicit, reviewed exception — not the default.
- `--read-only`: the container filesystem itself can't be modified;
  anything the sandboxed process needs to write goes to a mounted volume
  the caller controls, not the container's own layers.
- `--memory` / `--cpus`: hard resource ceilings, so a runaway or malicious
  agent-proposed command can't exhaust the host.
- `--user sandbox`: belt-and-suspenders on top of the Dockerfile's own
  non-root `USER sandbox` — never rely on the image default alone.

`docker-compose.yml` at the repo root codifies this exact invocation for
local development; `make sandbox-shell` runs it. CI (`.github/workflows/ci.yml`,
job `sandbox-image`) builds this image on every push and asserts the
non-root and no-network properties hold, so a future change that
accidentally weakens isolation fails CI immediately rather than being
discovered at Stage 16.

## Why this exists before there's anything to sandbox

Setting up the isolation boundary before Stage 3's agent exists (rather
than after) means no agent code is ever written against a permissive
execution environment that later needs retrofitting — the constraint
shapes the design from the start, per the "CI'ı en baştan kur" (set up CI
from the beginning, not bolted on later) principle in ROADMAP.md.
