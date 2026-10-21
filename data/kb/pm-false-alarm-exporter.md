---
id: pm-false-alarm-exporter
type: postmortem
title: Post-mortem: a latency graph spiked when the metrics sidecar was upgraded
class: false_alarm_exporter
---
Summary: a latency and throughput alert fired for the ledger service although customers saw nothing. Timeline: a scheduled upgrade restarted the metrics exporter; the new instance reported a different baseline and a short gap, which looked like a collapse in throughput. Ledger writes continued normally. Impact: none. Resolution: closed as telemetry artefact. Action items: alerts that require corroboration from a second source.
