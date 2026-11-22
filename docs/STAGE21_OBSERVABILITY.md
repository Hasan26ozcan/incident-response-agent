# Stage 21 — Observability & Cost

*Roadmap output:* a dashboard + a cost/latency report → `python -m incident_agent.observe report|demo` writes
`observability.html` (self-contained dashboard), `observability.md` (report) and `observability.json`.

```
python -m incident_agent.observe demo                       # simulated, offline, no API calls
python -m incident_agent.observe report --runs <runs dir>   # traces derived from durable journals
python -m incident_agent.observe report --trace spans.jsonl # traces written by TracedClient/Tracer
python -m incident_agent.observe prices                     # the verified price table
python -m incident_agent.observe wandb                      # log gate metrics as a W&B experiment (offline)
python -m incident_agent.agenteval run                      # whole gate, `observability` section included
```

## Trace vs. experiment (the roadmap's distinction)

| | Answers | Tool here |
|---|---|---|
| **Trace** (runtime observability) | what happened in *this* run, in order, how long, how many tokens, what it cost | local JSONL spans (`Tracer`, `TracedClient`), spans derived from the durable journal; optional LangSmith or Arize Phoenix (OpenTelemetry) export |
| **Experiment tracking** | is variant A better than B across *many* runs | W&B (`log_wandb`, offline by default, `wandb sync` later); the Stage 19 gate/baseline is the local equivalent |

Everything is **local-first**: the defaults need no account, no network and no extra package. Exporters are opt-in
adapters that import lazily and say what is missing. Telemetry is best-effort: a failing sink is counted
(`tracer.dropped`) and never raises into the agent.

## What was built (`src/incident_agent/observe/`)

| Module | Purpose |
|---|---|
| `trace` | `Span`, `Tracer` (context-var nesting, error status), `MemorySink`, `JsonlSink`, `read_spans` (torn lines skipped) |
| `redact` | credential-shaped strings (sk-, gsk_, xox, ghp_, AKIA, Bearer, Slack webhooks, JWTs, `key=value`) are removed from every attribute and error before it is stored or exported; values are bounded (2000 chars, 50 items) |
| `llm` | `TracedClient`: one `llm` span per call (model, tokens, cached tokens, cost, structured?, prefix hash). **Message content is not recorded** unless `capture_content=True` |
| `pricing` + `pricing.json` | USD per 1M tokens, **only verified prices**; an unlisted model costs `None` ("unknown"), never 0 or a guess |
| `cascade` | `CascadeClient(cheap, strong, accept)`: keep the cheap answer when the quality gate accepts it; escalate on rejection or `LLMUnavailable`; a config error (bad key) is never swallowed |
| `window` | `SlidingWindow` / `WindowedClient`: keep system + task + newest turns within a token budget; a tool call and its replies are one unit (no orphan tool message); a marker says turns were trimmed |
| `prefix` | prompt-cache analyser: prefix reuse between calls, volatile timestamps/uuids in the cacheable prefix, tool lists that change, prompts below the cacheable minimum |
| `journal_traces` | a run span + one span per step derived from `journal.jsonl` (retries, crashes, unfinished steps, waits) — no instrumentation needed |
| `exporters` | `OtelSink` (Phoenix/any OTLP), `LangSmithSink` (key from `LANGSMITH_API_KEY` only), `log_wandb` |
| `report` | percentile latency, tokens, cache-hit ratio, cost by model, cascade escalation rate, step latency; markdown + dashboard |
| `selfcheck` | the gate section (below) |

One change to existing code: `OpenAICompatClient.chat` adds `usage["cached_tokens"]` **only** when the provider reports
a positive integer (`prompt_tokens_details.cached_tokens`); otherwise `usage` is exactly as before.

## Cost model — what is and is not known

Verified on 2026-11-21: Mistral Large $0.50 in / $1.50 out; Groq `openai/gpt-oss-120b` $0.15 / $0.60 (cached input
$0.075); `openai/gpt-oss-20b` $0.075 / $0.30 (cached $0.0375). Groq llama models are listed as Enterprise-only and
Cerebras prices were not found, so they are **unpriced** and shown as such. Free-tier usage is rate-limited, not billed:
the figures are what the same tokens would cost on a paid plan, which is what matters when choosing a cascade.

## Prompt caching (Groq)

Automatic and only for `openai/gpt-oss-20b`, `120b` and `safeguard-20b`; needs an **identical prefix** (minimum
roughly 128–1024 tokens), reports `cached_tokens`, bills them at 50 %, and expires after ~2 h. Changing `tools` or
`tool_choice` invalidates the cache. So: put the stable part (system prompt, tool list, task) first and the volatile
part (timestamps, ids, new observations) last — `prefix.analyze` checks exactly that.

## The gate (`observability` section, dev split, offline, deterministic)

| Check | Proves |
|---|---|
| `redaction` | a credential in attributes, nested values or an exception message never reaches the trace file |
| `trace_shape` | one trace per run, parent links resolve, errors marked, a crashing sink never breaks the run |
| `journal_traces` | a real engine + gate run yields exactly one run span and one span per started step |
| `cascade` | accepted → cheap, rejected → strong, unavailable → strong, config error → raised; priced saving vs always-strong |
| `window` | head kept, budget respected, newest turn kept, no orphan tool reply |
| `prefix` | volatile prefix and changed tools are flagged; a stable prefix is not |

Negative controls (an eval that cannot fail proves nothing): a tracer **without** redaction, a window that cuts by
message count, and a cascade whose gate accepts everything must each be caught. CI fails if a check fails, a control
is not caught, a check is removed, or the cascade saving falls more than 0.02 below the baseline.

## Honest limits

* The gate's cascade saving (25 %) is a **mechanics test** with scripted models, not a measurement of real Groq/Mistral
  quality. The real saving depends on how often the cheap model passes your gate on real incidents.
* The cascade quality gate is only as good as its `accept` function; the default checks emptiness and schema validity,
  not correctness.
* LangSmith, Phoenix and W&B adapters are tested against fakes, not against live services (no accounts used here).
  Verify them once with your own account.
* Nothing is wired into the agents or CLI yet. `TracedClient`, `CascadeClient` and `WindowedClient` are wrappers you
  compose around any `LLMClient`; wiring them in is a decision for the owner.
* Latency in the demo is a simulated tick clock. Real latencies appear once you run against a provider.
* Redaction is pattern-based like the injection scanner: an unusual secret format can slip through. Content is off by
  default for that reason.
