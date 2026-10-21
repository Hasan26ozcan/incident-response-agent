---
id: pm-k8s-crashloop
type: postmortem
title: Post-mortem: inventory pods looped on start-up after a config change
class: k8s_crashloop
---
Summary: inventory-service ran at reduced capacity for twenty minutes. Timeline: a manifest change referenced a configuration key that did not exist in the namespace; each container exited immediately on start, and the readiness probe never passed. Impact: reduced throughput, some timeouts. Resolution: restored the key and redeployed. Action items: manifest validation in CI, failing fast with a clear message on missing config.
