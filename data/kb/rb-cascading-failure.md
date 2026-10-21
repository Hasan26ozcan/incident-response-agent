---
id: rb-cascading-failure
type: runbook
title: Runbook: one failing dependency is dragging down its callers
class: cascading_failure
---
Symptoms: several services alarm at once; callers report timeouts to a downstream service, thread pools fill with blocked requests, circuit breakers open, and degraded fallback responses are served. The service that alarms loudest is often not the origin.
First checks: build the dependency order and find the earliest degraded component by timestamp; check the origin's own saturation (CPU, connections, queue depth) and recent changes; confirm callers' retries are not amplifying the load (a retry storm keeps the origin down).
Remediation: protect the origin first (shed load, disable non-essential callers, tighten retry budgets, keep circuit breakers open), then fix the origin. Do not restart the callers; they are victims and will fail again until the origin recovers. Close the incident only after breakers have closed and error rates are at baseline.
