---
id: rb-mq-backlog
type: runbook
title: Runbook: message queue backlog and growing consumer lag
class: mq_backlog
---
Symptoms: queue depth and consumer lag rise steadily, consumer groups rebalance repeatedly, processing latency grows, and downstream effects arrive late rather than failing outright.
First checks: compare produce rate with consume rate; look at how many consumers are alive and how long each message takes; check for a poison message that makes consumers fail and reprocess; check for slow downstream calls inside the handler.
Remediation: scale consumers horizontally if the work is parallel, fix or quarantine the poison message, and tune batch size and timeouts; avoid purging the queue unless business owners agree (purging is a high-risk data action). Confirm lag falls and drains to baseline.
