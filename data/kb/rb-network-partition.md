---
id: rb-network-partition
type: runbook
title: Runbook: packet loss and unreachable peers between services
class: network_partition
---
Symptoms: connection resets and timeouts between specific services or zones, elevated retransmission, mesh sidecars reporting upstream unreachable, while each service looks healthy locally. Failures are correlated with network paths, not with a particular piece of code.
First checks: determine whether affected pairs share a node, availability zone or network segment; check node and cloud network events, CNI or overlay health, MTU or security-group changes; run connectivity tests in both directions.
Remediation: take the impaired node out of rotation (cordoning it and moving its pods) or shift traffic away from the impaired zone (traffic shifting is medium to high risk); engage the network or cloud provider; do not restart healthy services. Confirm retransmission rate and cross-service latency return to baseline.
