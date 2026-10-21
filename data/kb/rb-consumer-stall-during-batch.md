---
id: rb-consumer-stall-during-batch
type: runbook
title: Runbook: a queue consumer stopped during a large batch enqueue
class: consumer_stall_during_batch
---
Symptoms: queue depth rises and processed-messages-per-minute falls toward zero while a scheduled batch has just enqueued a large volume; heartbeats from a consumer are missed; the process may still be alive but a worker thread died, often from running out of memory while handling large messages.
First checks: look for a consumer thread or process failure in the logs near the start of the batch; compare the consume rate before and after; check memory use of the consumer for the message type being processed.
Remediation: restart the stalled consumer (a production restart, high risk) and let the backlog drain; reduce batch size or message size and raise the consumer's memory. Confirm throughput recovers and queue depth falls.
