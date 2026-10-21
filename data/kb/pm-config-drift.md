---
id: pm-config-drift
type: postmortem
title: Post-mortem: a retry limit override left in production amplified a small outage
class: config_drift
---
Summary: a minor dependency blip turned into a fivefold load increase. Timeline: a retry-count override added during an earlier incident remained in production only; when the dependency slowed, every caller retried many times. Impact: extended latency for an hour. Resolution: override removed and configuration redeployed. Action items: nightly configuration diff between environments, expiry dates on temporary overrides.
