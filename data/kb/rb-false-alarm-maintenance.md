---
id: rb-false-alarm-maintenance
type: runbook
title: Runbook: alerts raised during planned maintenance
class: false_alarm_maintenance
---
Use when symptoms appear while a change window is open: node drains, rolling restarts, rescheduled pods, readiness probes that are slow while workloads move.
Checks: find the approved change record and its window; compare the start of the symptoms with the window; confirm the warnings are the expected kind (graceful eviction, rescheduling) and that metrics return to baseline when the window closes.
Decision: if the symptoms are fully explained by the window and recover, treat as a false alarm and do not intervene. If anything persists after the window ends, or the problem is not the kind the change would cause, it is not maintenance noise: investigate it as a real incident.
