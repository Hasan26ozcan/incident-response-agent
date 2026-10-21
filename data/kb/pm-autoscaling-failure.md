---
id: pm-autoscaling-failure
type: postmortem
title: Post-mortem: traffic spike met a frozen autoscaler
class: autoscaling_failure
---
Summary: checkout slowed under a marketing campaign because capacity did not grow. Timeline: the metrics adapter had been unavailable since a cluster upgrade, so the autoscaler could not read its target and made no scaling decisions. Impact: ninety minutes of degraded latency. Resolution: manual scale-out, adapter reinstalled. Action items: alert on autoscaler error conditions, game-day for scale-out.
