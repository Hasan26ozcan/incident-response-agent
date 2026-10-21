---
id: rb-memory-leak
type: runbook
title: Runbook: memory grows steadily until containers are killed
class: memory_leak
---
Symptoms: resident memory or JVM heap rises monotonically and does not come back down after garbage collection; garbage-collection pauses get longer and more frequent; the orchestrator kills containers for exceeding their memory limit and the cycle starts again after each restart.
First checks: plot memory over several hours and across restarts (a sawtooth with a constant slope is a leak); take a heap dump or allocation profile and diff two snapshots; look for unbounded caches, listeners never removed, per-request objects kept in static collections; check what changed in the last release.
Remediation: a rolling restart restores service immediately but only resets the clock (restarting production pods is high risk). Raise limits only as a temporary measure. Fix the retention bug, then verify the growth slope is flat under the same load.
