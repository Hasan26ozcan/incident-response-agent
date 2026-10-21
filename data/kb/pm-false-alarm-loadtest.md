---
id: pm-false-alarm-loadtest
type: postmortem
title: Post-mortem: catalog alerts triggered by the performance team's load run
class: false_alarm_loadtest
---
Summary: the catalog API alerted on request rate and CPU during a scheduled load test. Timeline: the performance team ran an approved test from a labelled client; the rate limiter engaged for that client as intended and normal traffic was unaffected. Impact: none. Resolution: closed after confirming the change record. Action items: tag test traffic so alerts can exclude it, announce tests in the on-call channel.
