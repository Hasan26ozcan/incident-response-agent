---
id: rb-autoscaling-failure
type: runbook
title: Runbook: the autoscaler is not adding capacity under load
class: autoscaling_failure
---
Symptoms: load and queue depth grow while the replica count stays flat, autoscaler events show evaluation problems or no decision, and per-instance utilisation is far above target.
First checks: inspect the autoscaler object for conditions and recent events; verify the metrics pipeline it depends on is serving data; check whether the maximum replica count, a quota or a lack of schedulable nodes is capping scale-out; confirm the target metric is the one that reflects the real bottleneck.
Remediation: scale manually as an immediate mitigation (changing replica counts is a medium risk action), then repair the metrics source or limits. Verify the autoscaler reacts to a controlled load increase.
