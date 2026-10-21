---
id: rb-false-alarm-loadtest
type: runbook
title: Runbook: alerts during approved synthetic load tests
class: false_alarm_loadtest
---
Use when traffic and resource usage jump while a performance or load test is running: request rate several times normal, a rate limiter engaging for a known test client, CPU and latency rising proportionally to the extra requests.
Checks: find the test announcement or change record and the test client identifier; confirm the traffic originates from that client and that the rate limiter is behaving as designed; verify real customer traffic is unaffected and that everything returns to normal when the test ends.
Decision: expected test traffic is a false alarm; do not scale or restart in response. If the test exceeds its approved limits or customers are affected, stop the test through its owners.
