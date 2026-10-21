---
id: rb-lb-misconfig
type: runbook
title: Runbook: uneven load balancing or bad health checks
class: lb_misconfig
---
Symptoms: one node or backend receives a disproportionate share of requests, gateway errors concentrate on a single instance, traffic is still routed to unhealthy backends, and fleet-wide capacity looks fine while one member is overloaded.
First checks: compare request distribution per backend; inspect the health-check path, port, expected status and thresholds; check weights, stickiness and recent changes to the balancer or ingress; confirm the unhealthy backend is actually failing the check.
Remediation: correct the health check or weights and drain the unhealthy backend (balancer changes affect all traffic: medium to high risk, use a staged change). Verify distribution evens out and gateway errors disappear.
