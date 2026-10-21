---
id: ref-feature-flags
type: reference
title: Reference: feature flag hygiene
---
Every flag has an owner and an expiry date. New flags default to off in production, are evaluated consistently across environments and are removed once fully rolled out. Flag changes are logged and are treated as production changes for review purposes.
