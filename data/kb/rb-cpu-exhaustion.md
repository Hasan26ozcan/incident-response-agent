---
id: rb-cpu-exhaustion
type: runbook
title: Runbook: a service is pinned at its CPU limit
class: cpu_exhaustion
---
Symptoms: CPU utilisation stays near the container limit, request latency climbs with it, work queues and thread pools back up, and the runtime reports CPU throttling. Traffic volume is usually normal, so the extra CPU is being burned on something other than useful work.
First checks: compare CPU against request rate (flat traffic with rising CPU points to a spinning code path); look at thread dumps or a quick profile for a hot loop, a retry handler without backoff, a catastrophic regular expression or a busy wait; check whether a release changed that code path.
Remediation: restarting the affected pods sheds stuck threads immediately but is a production restart (high risk, needs approval). The durable fix is to bound the loop, cap retries and add backoff. Scaling out only buys time and hides the defect. Confirm recovery by CPU and p99 latency returning to baseline.
