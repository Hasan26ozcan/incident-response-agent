---
id: pm-partial-secret-rotation
type: postmortem
title: Post-mortem: three of eight proxy pods kept using a revoked billing credential
class: partial_secret_rotation
---
Summary: about thirty-seven percent of billing calls from the auth proxy failed. Timeline: the scheduled rotation reached most pods; three pods that were restarting at that moment kept the previous credential, which the billing system then rejected. Impact: a third of requests failed for twenty minutes. Resolution: the stale pods were recycled. Action items: rotation job verifies every replica, alert on per-pod error rate.
