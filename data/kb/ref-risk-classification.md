---
id: ref-risk-classification
type: reference
title: Reference: risk tiers for remediation actions
---
Low risk: read-only inspection, collecting diagnostics, adding a silence. Medium risk: reversible configuration changes, notifications, scaling by small amounts. High risk: restarting or scaling production services, rollbacks, purging data, changing certificates, and anything touching secrets or access control. High-risk actions need human approval and a recorded rationale. Recommending an action is never the same as performing it.
