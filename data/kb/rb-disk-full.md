---
id: rb-disk-full
type: runbook
title: Runbook: volume or partition is full
class: disk_full
---
Symptoms: writes fail with no-space errors, log rotation stops, services cannot create temporary files or write data, and some processes crash on startup. A usage percentage alert on the volume usually fires first.
First checks: identify which filesystem and which directory is growing (largest directories first); check for runaway logging, core dumps, unrotated archives or a stuck cleanup job; check inode exhaustion as well as bytes.
Remediation: free space by removing disposable files such as rotated logs and temporary data (deleting data is a high-risk action; confirm what is safe) or expand the volume; then fix the producer of the growth and the retention policy. Confirm writes succeed and alert thresholds are restored.
