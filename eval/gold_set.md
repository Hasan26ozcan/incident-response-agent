# Gold Set — Summary Table

Human-readable index over `eval/gold/*.json`, generated from the same data `data/generate_dataset.py` produces — regenerate this file (`python3 eval/generate_gold_set_md.py`) instead of hand-editing it if the dataset changes.

| ID | Title | Category | Difficulty | Services | False Alarm | Recurrence Of | Risk Tier | Stage Use |
|---|---|---|---|---|---|---|---|---|
| INC-001 | cpu runaway retry loop | cpu_exhaustion | easy | checkout-api | — | — | high | 3, 6, 19 |
| INC-002 | memory leak oom checkout | memory_leak | medium | checkout-api | — | — | high | 3, 6, 9, 12, 19 |
| INC-003 | cascading payment timeout | cascading_failure | hard | checkout-api, payment-service | — | — | high | 6, 7, 9, 19 |
| INC-004 | bad deploy order validator npe | bad_deploy | easy | checkout-api | — | — | high | 3, 6, 19 |
| INC-005 | db pool exhaustion checkout | db_pool_exhaustion | medium | checkout-api, orders-db | — | — | medium | 6, 8, 19 |
| INC-006 | disk full log partition | disk_full | easy | notifications-service | — | — | high | 3, 6, 19 |
| INC-007 | network partition eth0 packet loss | network_partition | hard | checkout-api, inventory-service | — | — | high | 6, 8, 9, 19 |
| INC-008 | third party payment gateway outage | third_party_outage | medium | payment-service | — | — | medium | 6, 19 |
| INC-009 | dns resolution failure internal svc | dns_misconfig | medium | checkout-api | — | — | high | 6, 19 |
| INC-010 | tls cert expiry checkout ingress | tls_cert_expiry | easy | checkout-api | — | — | high | 3, 6, 19 |
| INC-011 | mq consumer lag notifications | mq_backlog | medium | notifications-service | — | — | high | 6, 19 |
| INC-012 | k8s crashloop bad readiness probe | k8s_crashloop | medium | inventory-service | — | — | high | 3, 6, 19 |
| INC-013 | autoscaling hpa not triggering | autoscaling_failure | hard | checkout-api | — | — | high | 6, 9, 19 |
| INC-014 | secret rotation auth failures | secret_rotation_failure | medium | checkout-api, auth-service | — | — | high | 6, 19 |
| INC-015 | config drift max retries prod | config_drift | hard | checkout-api, payment-service | — | — | medium | 6, 9, 19 |
| INC-016 | lb misconfig uneven traffic node3 | lb_misconfig | medium | checkout-api | — | — | medium | 6, 19 |
| INC-017 | gc pause storm order service | gc_pause_storm | hard | orders-service | — | — | high | 6, 8, 9, 19 |
| INC-018 | n plus one query cart service | n_plus_one_query | medium | cart-service, orders-db | — | — | high | 6, 8, 19 |
| INC-019 | false alarm noisy cpu threshold | false_alarm | easy | search-service | yes | — | low | 6, 8, 19, 20 |
| INC-020 | memory leak oom notifications recurrence | memory_leak | medium | notifications-service | — | INC-002 | high | 12, 19 |

## Distractor / debate-test pairs

| Pair | Shared symptom | Distinguishing signal |
|---|---|---|
| INC-005 ↔ INC-018 | Both present as DB-driven slowness on checkout/cart flows | INC-005: `db_pool_waiting` saturates (connection-bound). INC-018: `db_cpu_pct` saturates (compute-bound, N+1 queries) |
| INC-007 ↔ INC-017 | Both present as latency spikes with connection resets | INC-007: `tcp_retransmit_rate` spikes simultaneously with latency (network). INC-017: `gc_pause_ms` leads latency by ~1 minute (JVM GC) |

## Recurrence pair (episodic memory test, Stage 12)

| First encounter | Recurrence | What should transfer |
|---|---|---|
| INC-002 (checkout-api memory leak) | INC-020 (notifications-service memory leak) | Same causal pattern (unbounded in-memory cache, no eviction) on a different service — tests generalization, not exact-match recall |

## False alarm (Stage 8/20 test)

INC-019 has no real anomaly — a single transient metric blip with an otherwise clean log stream. See `eval/rubric.md` §5 for the false-positive-handling hard gate this exists to test.
