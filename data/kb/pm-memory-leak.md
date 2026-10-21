---
id: pm-memory-leak
type: postmortem
title: Post-mortem: cart-service out-of-memory kills traced to a session cache
class: memory_leak
---
Summary: cart-service pods were restarted by the platform several times a day for a week. Timeline: memory climbed by roughly one percent per hour; every restart hid the trend. A heap diff showed millions of session objects retained by a cache that had no eviction policy after a refactor. Impact: intermittent failed checkouts during restarts. Resolution: eviction and a size cap were added and rolled out gradually. Action items: memory-slope alert over six hours, a default size bound on every in-process cache.
