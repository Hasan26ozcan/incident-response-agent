---
id: rb-slow-memory-leak
type: runbook
title: Runbook: slow memory growth that has not caused failures yet
class: slow_memory_leak
---
Symptoms: memory or heap usage creeps up over many hours or days, warnings about approaching the limit appear, garbage-collection time increases gradually, but nothing has crashed and latency is still acceptable. The trend, not the current value, is the signal.
First checks: measure the growth slope and project the time to reach the limit; correlate the start of the climb with a release that added in-process caching or memoisation; compare heap snapshots over time.
Remediation: plan a controlled restart before the projected limit (restarting production is high risk, schedule it); fix the unbounded structure or add eviction. Do not dismiss low-severity warnings: the incident is the projected exhaustion.
