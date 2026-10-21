---
id: rb-dns-misconfig
type: runbook
title: Runbook: name resolution failures
class: dns_misconfig
---
Symptoms: services cannot resolve internal or external hostnames, errors name the host as not found or resolution failed, the resolver is slow or not responding, and failures hit many callers of one name at once. IP-based connections still work.
First checks: resolve the failing name from an affected pod and from a healthy host; check resolver pods and their configuration; review recent DNS record or zone changes and any search-domain or ndots modification; check for TTL and caching effects.
Remediation: revert the record or resolver configuration change (changing production DNS is high risk); restart resolver pods only if they are unhealthy; flush caches after the fix. Confirm resolution from several namespaces and that error rates drop.
