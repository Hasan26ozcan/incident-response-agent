---
id: rb-false-alarm-exporter
type: runbook
title: Runbook: metric anomalies caused by the telemetry pipeline
class: false_alarm_exporter
---
Use when a dashboard shows a jump, dip or gap that the service itself does not feel: request logs look normal, business throughput is steady, and the odd metric coincides with a restart or upgrade of the metrics exporter or collector.
Checks: compare the suspicious metric with an independent signal such as application logs, success counters or traffic at the edge; look for exporter restarts, version changes or scrape failures at the same time; check whether the metric is a counter reset or a gap rather than a real change.
Decision: if only the telemetry changed and the service is healthy, treat it as a false alarm; take no action on the service. Note the artefact so that dashboards and alert rules tolerate exporter restarts.
