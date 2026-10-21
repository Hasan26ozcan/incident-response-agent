---
id: rb-false-alarm-batch
type: runbook
title: Runbook: load from scheduled batch jobs and reindexing
class: false_alarm_batch
---
Use when CPU, memory or query latency rise on a schedule: nightly reindex, report generation, data exports, cron-driven maintenance.
Checks: look for the job start and finish entries in the logs; compare with the same time on previous nights; confirm the slow-query or resource warnings are tagged as expected for that workload and stop when the job finishes; verify user-facing traffic is unaffected.
Decision: expected and recovering load from a known schedule is a false alarm; do not restart or scale. If the job overruns its usual duration, or the symptoms continue after it finishes, escalate as a genuine problem. Consider routing batch load away from the serving path.
