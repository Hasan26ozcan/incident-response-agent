---
id: pm-consumer-stall-during-batch
type: postmortem
title: Post-mortem: nightly digest stalled the email worker
class: consumer_stall_during_batch
---
Summary: email delivery paused for ninety minutes after the nightly digest job. Timeline: the job enqueued a very large number of messages; a worker thread rendering large templates ran out of memory and died while the process kept heartbeating intermittently; the queue grew with no consumption. Impact: delayed email. Resolution: worker restarted, digest split into smaller batches. Action items: alert when consumption drops to zero with non-empty queue.
