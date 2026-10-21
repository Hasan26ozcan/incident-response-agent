---
id: pm-secret-rotation-failure
type: postmortem
title: Post-mortem: service-to-service calls rejected after a scheduled secret rotation
class: secret_rotation_failure
---
Summary: checkout calls to the auth service returned unauthorized for twenty minutes. Timeline: the rotation completed in the vault, but the consumer kept reading a stale mounted copy until restarted. Impact: failed logins during the window. Resolution: pods were restarted to remount the secret. Action items: rotation runbook with a reload step, canary consumer that validates the new credential.
