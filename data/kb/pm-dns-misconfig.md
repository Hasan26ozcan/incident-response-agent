---
id: pm-dns-misconfig
type: postmortem
title: Post-mortem: internal service name stopped resolving after a record cleanup
class: dns_misconfig
---
Summary: callers of an internal service received not-found errors on its hostname for about thirty minutes. Timeline: a housekeeping change removed what looked like an unused record; cached answers expired gradually, so failures grew over time rather than all at once. Impact: calls to that service failed. Resolution: the record was restored and caches cleared. Action items: peer review for DNS deletions, query-based usage check before removing any record.
