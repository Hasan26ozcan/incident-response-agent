---
id: rb-bad-deploy
type: runbook
title: Runbook: errors began right after a release
class: bad_deploy
---
Symptoms: error rate or exception volume rises sharply at the moment a new version reaches production; the same stack trace repeats; a canary or the first batch of upgraded instances fails before the rest.
First checks: line up the first error timestamp with the deployment record and version rollout; read the repeated exception to locate the new code path; compare error rates between old and new versions; look for a missing configuration key, a migration that did not run, or an unhandled null input.
Remediation: roll back to the previous version (rolling back production is high risk but is normally the fastest safe fix); if a feature flag guards the change, disable it instead. Keep the failing build for the post-mortem. Verify recovery by error rate, not by the pipeline reporting success.
