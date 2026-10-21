---
id: pm-slow-memory-leak
type: postmortem
title: Post-mortem: reporting service heap crept up for a week after memoisation was added
class: slow_memory_leak
---
Summary: reporting-service reached its heap ceiling on a Sunday. Timeline: a release added in-process memoisation of dashboard queries with no size cap; heap usage rose a little each day and warnings were treated as noise. Impact: out-of-memory restart during a scheduled report run. Resolution: cap and eviction added. Action items: forecast-based memory alert, review of every new cache for bounds.
