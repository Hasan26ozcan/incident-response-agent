---
id: pm-network-partition
type: postmortem
title: Post-mortem: intermittent errors between zones after a network maintenance
class: network_partition
---
Summary: calls between checkout and inventory failed intermittently for an hour. Timeline: a provider-side change degraded one zone's cross-zone links; retransmissions rose, connections were reset, sidecars marked upstreams unreachable. Services were healthy when tested locally. Impact: elevated checkout latency and retries. Resolution: traffic was pinned to the healthy zone until the provider restored the link. Action items: synthetic cross-zone probes, zone-aware routing.
