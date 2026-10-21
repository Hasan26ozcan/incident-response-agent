---
id: pm-third-party-outage
type: postmortem
title: Post-mortem: card authorisations failed while the payment gateway was down
class: third_party_outage
---
Summary: checkout could not authorise cards for fifty minutes. Timeline: the external gateway began returning service-unavailable responses; our client timed out and the circuit breaker opened as designed. No deploys or configuration changes had happened on our side. Impact: lost orders during the outage. Resolution: waited for the vendor, queued retries for pending payments. Action items: second gateway for failover, clearer customer messaging, vendor outage runbook.
