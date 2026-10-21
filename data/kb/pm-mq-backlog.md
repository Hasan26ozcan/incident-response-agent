---
id: pm-mq-backlog
type: postmortem
title: Post-mortem: notification delays caused by a flapping consumer group
class: mq_backlog
---
Summary: notifications were delivered up to two hours late. Timeline: a slow downstream call raised per-message time, consumers missed their session timeout, the group rebalanced over and over and made no progress. Impact: delayed emails and pushes. Resolution: timeout raised, downstream call given its own deadline, consumers scaled out. Action items: lag alert by age of oldest message, load test of handler time.
