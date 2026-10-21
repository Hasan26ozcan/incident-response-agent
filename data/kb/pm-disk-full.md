---
id: pm-disk-full
type: postmortem
title: Post-mortem: notification service stopped writing after logs filled the node disk
class: disk_full
---
Summary: notifications stopped being sent after the service could not write to local disk. Timeline: debug logging had been enabled for a test and never reverted; the log partition reached full capacity overnight and rotation itself failed for lack of space. Impact: a few thousand delayed notifications. Resolution: old logs were purged, debug level reverted, partition enlarged. Action items: size-based log rotation, alert at eighty percent usage, expire temporary log levels.
