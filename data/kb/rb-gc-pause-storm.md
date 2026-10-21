---
id: rb-gc-pause-storm
type: runbook
title: Runbook: latency spikes caused by long garbage-collection pauses
class: gc_pause_storm
---
Symptoms: periodic latency spikes that line up with stop-the-world pauses, frequent full collections, request timeouts during pauses, and a heap that recovers after each collection (unlike a leak).
First checks: read the collector logs for pause duration and frequency; compare live-set size after collection to the heap limit; look for allocation spikes, large objects or a heap sized too small for the working set; check whether the collector or heap flags changed.
Remediation: tune heap size and collector settings, reduce allocation churn in hot paths; a rolling restart relieves it briefly (high risk, rarely needed). Verify pause percentiles and tail latency fall to baseline.
