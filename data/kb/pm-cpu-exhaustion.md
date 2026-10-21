---
id: pm-cpu-exhaustion
type: postmortem
title: Post-mortem: pricing-service saturated its CPU quota after a retry change
class: cpu_exhaustion
---
Summary: pricing-service latency tripled for forty minutes. Timeline: a client library upgrade removed exponential backoff, so failed upstream calls were retried immediately in a tight loop and each worker thread burned a full core. Detection came from a latency alert, not a CPU alert. Impact: slow quotes, some timeouts. Resolution: pods were restarted to clear the stuck threads and the library was pinned to the previous version. Action items: alert on CPU throttling, add a lint rule requiring backoff on every retry, load-test retry paths.
