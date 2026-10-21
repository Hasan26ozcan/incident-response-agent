---
id: rb-secret-rotation-failure
type: runbook
title: Runbook: authentication errors after a credential or secret rotation
class: secret_rotation_failure
---
Symptoms: unauthorized or invalid-credential errors from a downstream service that began at the time of a rotation, a mounted secret that is stale, or a token that cannot be refreshed; the downstream service itself is healthy.
First checks: compare the time of the first failures with the rotation record; confirm which credential version the caller is actually using; check that the new secret reached the volume or environment and that processes reloaded it.
Remediation: roll the affected workloads so they pick up the new secret (restarting production pods is high risk) or re-sync the secret; as a last resort re-issue credentials. Touching secrets and access is a high-risk category; follow the approval path. Verify authentication succeeds and add rotation checks.
