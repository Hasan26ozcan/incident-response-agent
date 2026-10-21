---
id: rb-post-maintenance-regression
type: runbook
title: Runbook: problems that start after maintenance has finished
class: post_maintenance_regression
---
Use when a change window is marked complete but errors begin or persist after it: pods restarted, a configuration refresh was part of the change, and now a resource such as the connection pool behaves differently.
Checks: compare effective configuration before and after the window (refreshed values may differ from the previous ones); look at the first error time relative to the end of the window; check whether the change record mentioned configuration reloads.
Decision: this is not maintenance noise, because the maintenance caused a real regression. Treat it as an incident: roll back the configuration change or restore the previous values through the pipeline, then verify. Do not close the incident because the window was approved.
