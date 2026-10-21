---
id: rb-false-alarm
type: runbook
title: Runbook: confirming an alert is a false alarm
class: false_alarm
---
Use when an alert fired but nothing appears to be wrong: metrics are within their normal range, logs contain only routine entries, no deploys or changes are recorded and customers are not affected.
Checks: compare the alerting metric with its own baseline and with a second independent signal; look at whether the threshold is too tight or flapping; confirm there is no error, restart or deploy in the window; confirm no customer reports.
Decision: if every check is clean, classify as a false alarm and do not take any remediation; a restart or rollback on a healthy service creates risk for no benefit. Record why the alert fired (threshold, noisy metric, transient spike), tune or silence the rule with the owning team, and close the incident with the evidence.
