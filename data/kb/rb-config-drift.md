---
id: rb-config-drift
type: runbook
title: Runbook: production behaves differently from staging because of configuration drift
class: config_drift
---
Symptoms: a problem that never appeared in staging, an environment-specific override or flag value that differs from the expected one, unexpected retry or timeout behaviour, errors mentioning a setting that does not match what the code assumes.
First checks: diff the effective configuration between environments (not just the repository); look for manual edits, an override left over from an earlier incident, or a flag evaluated differently per environment; find when the value last changed.
Remediation: restore the intended value through the normal configuration pipeline and redeploy or reload (production configuration changes are medium risk); never hand-edit a running pod. Then add drift detection so the live configuration is compared against the source of truth.
