---
id: pm-false-alarm
type: postmortem
title: Post-mortem: CPU alert on the recommendations service with no underlying problem
class: false_alarm
---
Summary: an on-call page for CPU on the recommendations service was investigated for twenty minutes before being closed. Timeline: a short sample spike crossed a fixed threshold; latency, errors and traffic were all normal and the logs showed healthy status lines. No action was needed. Impact: none for customers, wasted engineering time. Resolution: alert rule changed to require a sustained breach. Action items: review alert thresholds against baseline quarterly.
