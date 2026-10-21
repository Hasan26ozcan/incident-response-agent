---
id: pm-false-alarm-maintenance
type: postmortem
title: Post-mortem: page during a node-pool drain that was already announced
class: false_alarm_maintenance
---
Summary: on-call was paged for latency on the inventory service in the middle of a scheduled node-pool upgrade. Timeline: pods were evicted gracefully and rescheduled; probes were slow for a few minutes; the change record listed the window. Everything recovered when the nodes were uncordoned. Impact: none. Resolution: the alert was suppressed for announced windows. Action items: link the change calendar to alert routing.
