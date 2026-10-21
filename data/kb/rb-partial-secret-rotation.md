---
id: rb-partial-secret-rotation
type: runbook
title: Runbook: only some replicas fail after a credential rotation
class: partial_secret_rotation
---
Symptoms: authentication errors from the downstream service that affect a subset of pods of the same workload; other replicas keep working; the rotation reported success. Failures are identifiable by pod name.
First checks: group errors by pod and compare the credential version each pod holds; check whether the rollout of the new secret to all pods finished; look for pods started before or during the rotation that never reloaded.
Remediation: restart only the stale pods so they mount the new credential (a restart is a high-risk action, do it pod by pod); do not rotate again. Verify the error rate drops to zero and add a check that all replicas converge on the new version.
