---
id: ref-kubernetes-upgrade
type: reference
title: Reference: Kubernetes cluster upgrade runbook
---
Upgrade the control plane first, then node pools one at a time. Cordon and drain each node, respecting disruption budgets, wait for replacement pods to become ready, then uncordon. Watch readiness and error rates between steps and stop on any regression. Announce the window in advance.
