---
id: rb-third-party-outage
type: runbook
title: Runbook: an external vendor API is failing
class: third_party_outage
---
Symptoms: errors and timeouts concentrate on calls to one outside provider, the provider returns server errors or does not answer, our own components are otherwise healthy, and circuit breakers for that integration open.
First checks: check the vendor status page and your own synthetic probe; confirm no change on our side (credentials, endpoint, deploys); measure error rate by upstream endpoint.
Remediation: rely on the circuit breaker, serve degraded behaviour, queue work for later where the business allows, and communicate to stakeholders; there is nothing to restart or roll back internally (avoid changes that add load, such as aggressive retries). Resume normal operation gradually when the vendor recovers and drain the queue at a controlled rate.
