---
id: pm-false-alarm-batch
type: postmortem
title: Post-mortem: nightly reindex tripped the CPU and memory alarms again
class: false_alarm_batch
---
Summary: the search indexer paged at the same time for the third night. Timeline: the scheduled reindex consumed most of the CPU and memory for forty minutes and produced slow-query warnings marked as expected; user searches were unaffected and the metrics settled on completion. Impact: none. Resolution: alert rules given a batch-window exemption. Action items: move the reindex to a dedicated node pool.
