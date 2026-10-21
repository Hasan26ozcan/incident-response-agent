---
id: pm-db-pool-exhaustion
type: postmortem
title: Post-mortem: inventory API starved of connections by a slow report query
class: db_pool_exhaustion
---
Summary: inventory API requests failed with timeouts for twenty-five minutes. Timeline: an analytics export started running against the primary database, held connections for minutes each, and the shared pool emptied. Detection by the pool-waiters alert. Impact: stock lookups failed during a sale. Resolution: the export was killed and moved to a replica. Action items: separate pools per workload, statement timeouts on reporting roles.
