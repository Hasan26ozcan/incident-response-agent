---
id: ref-postgres-backup-restore
type: reference
title: Reference: PostgreSQL backup and point-in-time restore procedure
---
Base backups run nightly and write-ahead logs are archived continuously. To restore, provision a new instance, fetch the most recent base backup before the target time, replay archived logs up to the recovery target, then validate row counts for the critical tables before switching traffic. Restores are rehearsed quarterly. Never restore over the primary; promote a verified replica instead.
