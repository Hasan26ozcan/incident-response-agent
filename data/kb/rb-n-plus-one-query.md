---
id: rb-n-plus-one-query
type: runbook
title: Runbook: database load out of proportion to traffic (query amplification)
class: n_plus_one_query
---
Symptoms: database CPU is high although traffic is moderate, each request issues a large number of small queries, connection pool use is high and latency grows with the size of the result set; slow-query logs show no single slow statement.
First checks: count queries per request in a trace; inspect the ORM mapping for lazy loading in loops; compare the query pattern before and after a recent release; check cache hit rates.
Remediation: load related data in batches or with joins, add caching where safe, and deploy the fix through the normal pipeline; killing sessions only provides temporary relief (a production database action, high risk). Verify queries per request and database CPU drop.
