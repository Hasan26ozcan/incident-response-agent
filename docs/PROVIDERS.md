# LLM providers

All providers are OpenAI-compatible and need only environment variables (`.env`).

| Provider | Variables | Notes |
|----------|-----------|-------|
| Mistral | `MISTRAL_API_KEY`, `MISTRAL_MODEL`, optional `MISTRAL_MIN_INTERVAL` | Free Experiment plan, no card, phone verification; free-tier requests may be used by Mistral for training (the dataset is synthetic). Free limits at the time of writing: 1 request/s, 500k tokens/min, 1B tokens/month. The client paces itself to 1.1 s between requests. |
| Groq | `GROQ_API_KEY`, optional `GROQ_MODEL` | Free `openai/gpt-oss-120b` is capped at 200k tokens/day: one dev run uses roughly that. |
| Cerebras | `CEREBRAS_API_KEY`, `CEREBRAS_MODEL` | Generous free quota but may require a payment method. |

Without `LLM_PROVIDERS` every configured provider is used in the order Groq, Cerebras, Mistral, with automatic
fallback when one is rate-limited. With `LLM_PROVIDERS=mistral` (comma separated list, order respected) only the
named providers are used and a missing key or model is an error instead of a silent skip.

**Measurement hygiene.** Use one provider and model for every run of an ablation (base and variant), because the
response cache is keyed by model and results from different models are not comparable. A run with `degraded > 0`
or debate/ToT verdicts `error` is not valid evidence; rerun it under a new tag (cached calls cost nothing).
Limits and model ids change: confirm them in the provider console. List available models with
`Invoke-RestMethod https://api.mistral.ai/v1/models -Headers @{Authorization="Bearer $env:MISTRAL_API_KEY"}`.
