---
id: pm-bad-deploy
type: postmortem
title: Post-mortem: validation exception introduced by an order-service release
class: bad_deploy
---
Summary: a Tuesday release of order-service caused a wave of server errors on order submission. Timeline: the new version added a validator that dereferenced an optional field; requests without it threw an uncaught exception. Error rate rose within two minutes of the rollout. Impact: roughly one in five submissions failed for eleven minutes. Resolution: rollback to the previous build, hotfix next day with a regression test. Action items: canary analysis on error rate, contract tests for optional fields.
