---
id: pm-gc-pause-storm
type: postmortem
title: Post-mortem: order-service tail latency traced to full collections after a heap flag change
class: gc_pause_storm
---
Summary: p99 latency jumped from fifty milliseconds to two seconds in bursts. Timeline: a platform default shrank the heap; the working set no longer fit so the collector ran full collections every minute and paused all request threads. Impact: timeouts for some orders. Resolution: heap restored and collector flags reviewed. Action items: pause-time alert, heap sizing check in the deployment template.
