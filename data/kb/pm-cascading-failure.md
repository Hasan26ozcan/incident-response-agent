---
id: pm-cascading-failure
type: postmortem
title: Post-mortem: checkout outage that started in the payments dependency
class: cascading_failure
---
Summary: checkout showed errors and slow pages while the payments tier looked healthy on its dashboard. Timeline: a database lock held by a long migration slowed payment authorisations; checkout threads waited on them, its pool saturated, and its health checks failed. Because retries were unbounded the load doubled. Impact: forty minutes of degraded checkout. Resolution: the migration was cancelled and retry budgets were enforced. Action items: dependency-aware alert grouping, mandatory timeouts and budgets on every client call.
