---
id: rb-k8s-crashloop
type: runbook
title: Runbook: pods restart repeatedly
class: k8s_crashloop
---
Symptoms: pods in CrashLoopBackOff, climbing restart counts, readiness probes failing, a service running with fewer healthy replicas than desired.
First checks: describe the pod for events and the last termination reason and exit code; read the logs of the previous container instance; check for missing configuration, a secret or volume that does not mount, an out-of-memory kill, or probes that are too aggressive for the start-up time; check what changed in the manifest or image.
Remediation: fix the cause (config, resources, probe thresholds) or roll back the manifest or image; deleting pods only restarts the loop (restarting production workloads is high risk). Verify pods reach ready and stay ready for several probe periods.
