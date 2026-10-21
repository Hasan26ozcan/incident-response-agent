---
id: ref-schema-migration
type: reference
title: Reference: database schema migration checklist
---
Prefer additive, backwards-compatible changes. Run the migration on a copy first, set a statement timeout, avoid long table locks by creating indexes concurrently, deploy code that tolerates both schemas, and only then remove old columns in a later release. Have a tested rollback script. Large backfills run in small batches outside peak hours.
