# Proposal: Stage 21g — Whole-system acceptance

- **Status:** Implemented
## Problem
Layers were proven separately; nothing proved the whole chain, restart safety, or that a secret never escapes.
## Decisions
1. One offline command, exit code gates releases; quick and full forms.
2. Real components everywhere except the model (scripted).
3. A planted secret is scanned for in every produced file and response.
4. Restart-while-waiting is a first-class step; the service recovers its job table from disk.
## Out of scope
Real providers, Docker daemon, live Slack.
