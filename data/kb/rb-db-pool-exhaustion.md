---
id: rb-db-pool-exhaustion
type: runbook
title: Runbook: application waits on database connections
class: db_pool_exhaustion
---
Symptoms: requests time out waiting to acquire a connection, the pool shows all connections busy with a growing number of waiters, and database-side load may be modest or high. Application errors mention the pool, not the database engine.
First checks: compare pool size, active and waiting counts; list long-running or idle-in-transaction sessions on the database; look for connection leaks (code paths that do not release) and for slow queries that hold connections; check whether a configuration change shrank the pool or traffic increased.
Remediation: terminate runaway sessions (production database action, high risk); raise the pool size only within what the database can serve; fix the leak or the slow query. Verify waiters drop to zero and latency recovers.
