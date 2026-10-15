#!/usr/bin/env python3
"""
Stage 1 — Synthetic Incident Dataset Generator.

Generates, for each of 20 synthetic incident scenarios:
  data/incidents/<id>/meta.json     — non-spoiler metadata agents are allowed to read
  data/incidents/<id>/logs.log      — synthetic log lines (the "log store")
  data/incidents/<id>/metrics.json  — synthetic metric time series (the "metrics store")
  data/incidents/<id>/deploys.json  — synthetic deploy history (the "deploy-history store")

...and, kept OUT of data/incidents/ so agents never see it by accident:
  eval/gold/<id>.json               — gold root cause, evidence, rubric-relevant metadata

Why the split: from Stage 6 onward, the Log/Metrics/Deploy-History agents in
the agent responsibility matrix read exactly these three stores. Keeping the
gold answer in a separate eval/ tree (never under data/incidents/) prevents
eval leakage regardless of how broadly a future agent is allowed to `ls` or
grep the data directory.

Stdlib only, deliberately — Stage 2 hasn't set up dependency management yet,
and this script needs to be runnable standing on its own.

Usage:
    python3 generate_dataset.py
"""

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INCIDENTS_DIR = ROOT / "incidents"
GOLD_DIR = ROOT.parent / "eval" / "gold"
BASE_TIME = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Log message template library, by category
# ---------------------------------------------------------------------------
TEMPLATES = {
    "cpu_exhaustion": {
        "normal": [
            "health check ok",
            "request processed in {lat}ms",
            "cache hit ratio 92%",
            "scheduled job completed successfully",
        ],
        "anomaly": [
            "thread pool queue depth exceeded threshold (depth={q})",
            "handler execution time {lat}ms exceeds SLA (200ms)",
            "possible unbounded loop detected in retry handler",
            "CPU throttling triggered by container runtime (cfs_quota exceeded)",
        ],
    },
    "memory_leak": {
        "normal": ["health check ok", "request processed in {lat}ms", "heap usage {pct}% of limit"],
        "anomaly": [
            "GC pause exceeded 2000ms (full GC)",
            "OutOfMemoryError: Java heap space",
            "container OOMKilled by kubelet (exit code 137)",
            "heap usage {pct}% of limit, approaching hard cap",
        ],
    },
    "cascading_failure": {
        "normal": ["health check ok", "request processed in {lat}ms", "downstream call succeeded"],
        "anomaly": [
            "upstream service payment-service timeout after 5000ms",
            "circuit breaker OPEN for payment-service",
            "fallback response served due to upstream failure",
            "downstream timeout cascading from payment-service to checkout-api",
        ],
    },
    "bad_deploy": {
        "normal": ["health check ok", "request processed in {lat}ms", "order validated successfully"],
        "anomaly": [
            "NullPointerException in OrderValidator.validate",
            "500 Internal Server Error rate increased sharply",
            "unhandled exception in build v{ver}",
            "request failed: validation pipeline threw uncaught exception",
        ],
    },
    "db_pool_exhaustion": {
        "normal": ["health check ok", "query completed in {lat}ms", "connection acquired from pool"],
        "anomaly": [
            "HikariPool-1 - Connection is not available, request timed out after 30000ms",
            "db connection pool exhausted: active=50 idle=0 waiting={q}",
            "slow query detected: {lat}ms (threshold 500ms)",
        ],
    },
    "disk_full": {
        "normal": ["health check ok", "log rotation completed", "request processed in {lat}ms"],
        "anomaly": [
            "write failed: No space left on device",
            "log rotation failed: disk full",
            "/var/log partition at {pct}% capacity",
        ],
    },
    "network_partition": {
        "normal": ["health check ok", "request processed in {lat}ms", "sidecar proxy healthy"],
        "anomaly": [
            "connection reset by peer",
            "packet loss detected {pct}% on eth0",
            "TCP retransmission rate elevated",
            "service mesh sidecar reporting upstream unreachable",
        ],
    },
    "third_party_outage": {
        "normal": ["health check ok", "request processed in {lat}ms", "external call succeeded"],
        "anomaly": [
            "payment-gateway API returned 503 Service Unavailable",
            "external API timeout after 10000ms",
            "circuit breaker OPEN for external payment-gateway",
        ],
    },
    "dns_misconfig": {
        "normal": ["health check ok", "request processed in {lat}ms", "DNS resolution ok (cached)"],
        "anomaly": [
            "DNS resolution failed for internal-service.svc.cluster.local",
            "getaddrinfo ENOTFOUND internal-service.svc.cluster.local",
            "upstream DNS server not responding, retrying",
        ],
    },
    "tls_cert_expiry": {
        "normal": ["health check ok", "request processed in {lat}ms", "TLS handshake ok"],
        "anomaly": [
            "TLS handshake failed: certificate has expired",
            "SSLError: certificate verify failed: certificate has expired",
            "clients rejecting connection due to expired certificate",
        ],
    },
    "mq_backlog": {
        "normal": ["health check ok", "message processed in {lat}ms", "consumer heartbeat ok"],
        "anomaly": [
            "consumer lag increased to {q} messages",
            "queue depth exceeds 100000",
            "consumer group rebalance triggered repeatedly",
        ],
    },
    "k8s_crashloop": {
        "normal": ["health check ok", "request processed in {lat}ms", "pod ready"],
        "anomaly": [
            "readiness probe failed: HTTP probe returned status 500",
            "pod entered CrashLoopBackOff",
            "container restarted {q} times in 5 minutes",
        ],
    },
    "autoscaling_failure": {
        "normal": ["health check ok", "request processed in {lat}ms", "replica count stable at 4"],
        "anomaly": [
            "HPA target CPU 250% but no scale-up event triggered",
            "autoscaler evaluation error: metrics API unavailable",
            "request queue depth growing unbounded, replica count unchanged",
        ],
    },
    "secret_rotation_failure": {
        "normal": ["health check ok", "request processed in {lat}ms", "token refreshed successfully"],
        "anomaly": [
            "authentication failed: invalid credentials",
            "401 Unauthorized from downstream auth-service",
            "secret volume mount stale, using expired token",
        ],
    },
    "config_drift": {
        "normal": ["health check ok", "request processed in {lat}ms", "config loaded from environment"],
        "anomaly": [
            "feature flag mismatch between staging and prod",
            "config value MAX_RETRIES=10 in prod, expected 3",
            "unexpected retry storm traced to environment-specific override",
        ],
    },
    "lb_misconfig": {
        "normal": ["health check ok", "request processed in {lat}ms", "traffic evenly distributed"],
        "anomaly": [
            "uneven traffic distribution: node-3 receiving 80% of requests",
            "health check misconfigured, routing to unhealthy node",
            "502 Bad Gateway rate increased on node-3",
        ],
    },
    "gc_pause_storm": {
        "normal": ["health check ok", "request processed in {lat}ms", "JVM heap stable"],
        "anomaly": [
            "JVM GC pause STW duration {lat}ms",
            "full GC triggered {q} times in 10 minutes",
            "request latency spike correlates with GC cycle",
        ],
        "distractor": [
            "connection reset by peer",
            "upstream timeout after 5000ms",
        ],
    },
    "n_plus_one_query": {
        "normal": ["health check ok", "request processed in {lat}ms", "query completed in {lat}ms"],
        "anomaly": [
            "query pattern detected: {q} SELECT statements issued for one request",
            "ORM lazy-loading triggering repeated per-row queries",
            "db CPU utilization {pct}% under moderate load",
        ],
        "distractor": [
            "HikariPool-1 connection wait time elevated",
            "db connection pool utilization at 90%",
        ],
    },
    # ---- dataset v2 (INC-021..028): hard false alarms and real incidents that look harmless ----
    "false_alarm_maintenance": {
        "normal": ["health check ok", "request processed in {lat}ms", "cache hit ratio 92%"],
        "anomaly": [],
    },
    "false_alarm_batch": {
        "normal": ["health check ok", "request processed in {lat}ms", "index segment merged"],
        "anomaly": [],
    },
    "false_alarm_exporter": {
        "normal": ["health check ok", "request processed in {lat}ms", "ledger entry committed"],
        "anomaly": [],
    },
    "false_alarm_loadtest": {
        "normal": ["health check ok", "request processed in {lat}ms", "catalog cache warm"],
        "anomaly": [],
    },
    "slow_memory_leak": {
        "normal": ["health check ok", "request processed in {lat}ms", "report generated"],
        "anomaly": [],  # quiet by design: no ERROR lines, only late WARN lines
    },
    "post_maintenance_regression": {
        "normal": ["health check ok", "request processed in {lat}ms", "order validated successfully"],
        "anomaly": [
            "db connection pool exhausted: active=2 idle=0 waiting={q}",
            "config value POOL_SIZE=2 loaded after refresh (previous value 50)",
            "request failed: timed out waiting for db connection after 5000ms",
        ],
    },
    "partial_secret_rotation": {
        "normal": ["health check ok", "request processed in {lat}ms", "token validated"],
        "anomaly": [
            "401 Unauthorized from downstream billing-api (pod auth-proxy-2): credential rejected",
            "authentication failed: invalid credentials presented by pod auth-proxy-2",
        ],
    },
    "consumer_stall_during_batch": {
        "normal": ["health check ok", "message processed in {lat}ms", "consumer heartbeat ok"],
        "anomaly": [],
    },
    "false_alarm": {
        "normal": ["health check ok", "request processed in {lat}ms", "metrics within normal range"],
        "anomaly": [],  # deliberately empty: no real anomaly signal exists
    },
}


def gen_metric_series(rng, total_minutes, step_min, baseline, noise, anomaly=None, spike_at=None, gap=None):
    """anomaly: {'start_offset_min', 'target', 'ramp_min', 'duration_min'} or None.
    spike_at: single-point transient blip (used for the false-alarm scenario)."""
    points = []
    n = total_minutes // step_min
    for i in range(n):
        minute = i * step_min
        t = BASE_TIME + timedelta(minutes=minute)
        val = baseline + rng.gauss(0, noise)
        if anomaly:
            start = anomaly["start_offset_min"]
            ramp = anomaly.get("ramp_min", 8)
            dur = anomaly.get("duration_min", total_minutes - start)
            target = anomaly["target"]
            if start <= minute < start + dur:
                progress = min(1.0, (minute - start) / max(1, ramp))
                val = baseline + (target - baseline) * progress + rng.gauss(0, noise)
            elif minute >= start + dur:
                if anomaly.get("recovers"):
                    val = baseline + rng.gauss(0, noise)  # transient: back to normal on its own
                else:
                    val = target + rng.gauss(0, noise)  # stays elevated (unresolved at snapshot end)
        if spike_at is not None and minute == spike_at:
            val = baseline + noise * 6  # transient blip, single point
        if gap is not None and gap["start"] <= minute < gap["end"]:
            val = gap["value"]  # exporter artefact: a fixed sentinel value, no extra RNG draw
        points.append({"ts": t.isoformat(), "value": max(0.0, round(val, 2))})
    return points


def gen_logs(
    rng,
    total_minutes,
    service,
    cat_templates,
    anomaly_start_offset_min,
    normal_rate_per_min=2,
    anomaly_rate_per_min=4,
    distractor_start_offset_min=None,
    distractor_rate_per_min=2,
    events=None,
    warn_window=None,
    normal_until_min=None,
):
    lines = []
    normal = cat_templates["normal"]
    anomaly = cat_templates.get("anomaly", [])
    distractor = cat_templates.get("distractor", [])
    for minute in range(total_minutes):
        t = BASE_TIME + timedelta(minutes=minute)
        for _ in range(
            rng.randint(0, normal_rate_per_min) if normal_until_min is None or minute < normal_until_min else 0
        ):
            tmpl = rng.choice(normal)
            msg = tmpl.format(lat=rng.randint(20, 90), pct=rng.randint(10, 40), q=rng.randint(0, 5))
            ts = t + timedelta(seconds=rng.randint(0, 59))
            lines.append(f'{ts.isoformat()} service={service} level=INFO msg="{msg}"')
        if anomaly and minute >= anomaly_start_offset_min:
            for _ in range(rng.randint(1, anomaly_rate_per_min)):
                tmpl = rng.choice(anomaly)
                msg = tmpl.format(
                    lat=rng.randint(1500, 5000),
                    pct=rng.randint(85, 100),
                    q=rng.randint(80, 400),
                    ver=rng.choice(["2.4.0"]),
                )
                ts = t + timedelta(seconds=rng.randint(0, 59))
                lines.append(f'{ts.isoformat()} service={service} level=ERROR msg="{msg}"')
        if distractor and distractor_start_offset_min is not None and minute >= distractor_start_offset_min:
            for _ in range(rng.randint(0, distractor_rate_per_min)):
                tmpl = rng.choice(distractor)
                msg = tmpl.format(lat=rng.randint(500, 2000), pct=rng.randint(80, 95), q=rng.randint(50, 200))
                ts = t + timedelta(seconds=rng.randint(0, 59))
                lines.append(f'{ts.isoformat()} service={service} level=WARN msg="{msg}"')
        if warn_window and warn_window["start"] <= minute < warn_window["end"]:  # v2 only: benign/quiet WARN lines
            span = max(1, warn_window["end"] - warn_window["start"])
            lo, hi = warn_window.get("pct", (0, 0))
            lat_lo, lat_hi = warn_window.get("lat", (20, 90))
            for _ in range(rng.randint(0, warn_window.get("rate", 1))):
                msg = rng.choice(warn_window["templates"]).format(
                    pct=round(lo + (hi - lo) * (minute - warn_window["start"]) / span),
                    lat=rng.randint(lat_lo, lat_hi),
                )
                ts = t + timedelta(seconds=rng.randint(0, 59))
                lines.append(f'{ts.isoformat()} service={service} level=WARN msg="{msg}"')
    for ev in events or []:  # v2 only: fixed one-off log events (maintenance start/end, job start/end, ...)
        ts = BASE_TIME + timedelta(minutes=ev["minute"], seconds=rng.randint(0, 59))
        lines.append(f'{ts.isoformat()} service={service} level={ev["level"]} msg="{ev["msg"]}"')
    lines.sort()
    return lines


def gen_deploys(service, deploy_specs):
    deploys = []
    for i, d in enumerate(deploy_specs):
        t = BASE_TIME + timedelta(minutes=d["offset_min"])
        deploys.append(
            {
                "deploy_id": f"deploy-{i + 1:03d}",
                "timestamp": t.isoformat(),
                "service": d.get("service", service),
                "version": d["version"],
                "description": d["description"],
            }
        )
    return deploys


# ---------------------------------------------------------------------------
# Scenario definitions — 20 scenarios covering the categories called out in
# ruff: noqa: C408  # dict() keyword-arg calls are intentional for readability
# ruff: noqa: E501  # long string literals in scenario definitions
# nosec: B311       # random.Random with seed is for deterministic generation, not crypto
# exhaustion/etc.") plus enough additional categories, distractor pairs,
# a false alarm, and a recurrence pair to exercise every later stage.
# ---------------------------------------------------------------------------
SCENARIOS = [
    dict(
        id="INC-001",
        slug="cpu-runaway-retry-loop",
        category="cpu_exhaustion",
        difficulty="easy",
        services=["checkout-api"],
        total_minutes=90,
        incident_start_min=40,
        metrics={
            "cpu_percent": dict(baseline=22, noise=3, anomaly=dict(start_offset_min=40, target=97, ramp_min=6)),
            "latency_p99_ms": dict(baseline=110, noise=15, anomaly=dict(start_offset_min=41, target=4200, ramp_min=8)),
        },
        deploys=[dict(offset_min=-200, version="1.9.0", description="Routine dependency bump, no logic change")],
        gold=dict(
            root_cause="An unbounded retry loop in checkout-api's payment-confirmation handler pins a worker thread at 100% CPU per affected request, starving the thread pool.",
            evidence=[
                "metric:cpu_percent ramps 22%→97% starting minute 40",
                "metric:latency_p99_ms ramps in lockstep from minute 41",
                "log: repeated 'possible unbounded loop detected in retry handler' from checkout-api starting minute 40",
            ],
            recommended_remediation="Restart the affected checkout-api pods to shed the stuck threads, then patch the retry handler to cap retry attempts.",
            remediation_risk_tier="high",
            risk_rationale="Restarting a production service pod is an explicit high-risk action per the risk classification.",
        ),
        primary_stage_use=["3", "6", "19"],
    ),
    dict(
        id="INC-002",
        slug="memory-leak-oom-checkout",
        category="memory_leak",
        difficulty="medium",
        services=["checkout-api"],
        total_minutes=120,
        incident_start_min=20,
        metrics={
            "memory_percent": dict(baseline=35, noise=2, anomaly=dict(start_offset_min=20, target=98, ramp_min=90)),
            "gc_pause_ms": dict(baseline=40, noise=10, anomaly=dict(start_offset_min=70, target=2500, ramp_min=20)),
        },
        deploys=[dict(offset_min=-30, version="2.1.0", description="Added in-memory response cache for cart lookups")],
        gold=dict(
            root_cause="The new in-memory response cache added in v2.1.0 has no eviction policy, causing unbounded heap growth that culminates in OOMKilled restarts.",
            evidence=[
                "metric:memory_percent climbs steadily from minute 20, reaching 98% by minute ~110",
                "metric:gc_pause_ms spikes from minute 70 as GC struggles to reclaim heap",
                "log: 'container OOMKilled by kubelet' entries beginning near minute 110",
                "deploy: v2.1.0 (30 minutes before baseline start) introduced the unbounded cache",
            ],
            recommended_remediation="Roll back to v2.0.x, add a bounded/TTL eviction policy to the cache before re-deploying.",
            remediation_risk_tier="high",
            risk_rationale="Rollback of a deploy is explicitly listed as a high-risk action.",
        ),
        primary_stage_use=["3", "6", "9", "12", "19"],
    ),
    dict(
        id="INC-003",
        slug="cascading-payment-timeout",
        category="cascading_failure",
        difficulty="hard",
        services=["checkout-api", "payment-service"],
        total_minutes=90,
        incident_start_min=25,
        metrics={
            "error_rate_pct": dict(baseline=0.5, noise=0.3, anomaly=dict(start_offset_min=25, target=45, ramp_min=5)),
            "latency_p99_ms": dict(baseline=130, noise=20, anomaly=dict(start_offset_min=26, target=5200, ramp_min=6)),
        },
        deploys=[
            dict(
                offset_min=-10,
                version="4.0.1",
                description="payment-service: switched TLS provider for card network integration",
            )
        ],
        gold=dict(
            root_cause="payment-service's v4.0.1 TLS provider change introduced a slow handshake path; payment-service latency exceeds checkout-api's timeout, and checkout-api's own retries amplify the load, cascading into checkout-api errors.",
            evidence=[
                "log: 'upstream service payment-service timeout after 5000ms' from checkout-api starting minute 25",
                "log: 'circuit breaker OPEN for payment-service' shortly after",
                "metric:error_rate_pct and latency_p99_ms both spike at minute 25-26, on checkout-api not payment-service directly",
                "deploy: payment-service v4.0.1 (10 minutes before window start) is the only recent change",
            ],
            recommended_remediation="Roll back payment-service to the prior TLS provider configuration; verify checkout-api circuit breaker recovers.",
            remediation_risk_tier="high",
            risk_rationale="Rollback of a production service's config is high-risk.",
        ),
        primary_stage_use=["6", "7", "9", "19"],
    ),
    dict(
        id="INC-004",
        slug="bad-deploy-order-validator-npe",
        category="bad_deploy",
        difficulty="easy",
        services=["checkout-api"],
        total_minutes=60,
        incident_start_min=15,
        metrics={
            "error_rate_pct": dict(baseline=0.4, noise=0.2, anomaly=dict(start_offset_min=15, target=22, ramp_min=2))
        },
        deploys=[
            dict(
                offset_min=-2,
                version="2.4.0",
                description="checkout-api: refactor OrderValidator to support gift-card orders",
            )
        ],
        gold=dict(
            root_cause="v2.4.0's OrderValidator refactor fails to null-check the new gift-card field, throwing a NullPointerException on any order without a gift card.",
            evidence=[
                "log: 'NullPointerException in OrderValidator.validate' starting minute 15",
                "metric:error_rate_pct steps sharply from 0.4% to ~22% at minute 15",
                "deploy: checkout-api v2.4.0 deployed 2 minutes before the window starts, matching the error onset",
            ],
            recommended_remediation="Roll back checkout-api to v2.3.x immediately; fix the null-check before re-attempting v2.4.0.",
            remediation_risk_tier="high",
            risk_rationale="Deploy rollback is explicitly high-risk.",
        ),
        primary_stage_use=["3", "6", "19"],
    ),
    dict(
        id="INC-005",
        slug="db-pool-exhaustion-checkout",
        category="db_pool_exhaustion",
        difficulty="medium",
        services=["checkout-api", "orders-db"],
        total_minutes=90,
        incident_start_min=35,
        metrics={
            "db_pool_active": dict(baseline=12, noise=2, anomaly=dict(start_offset_min=35, target=50, ramp_min=10)),
            "db_pool_waiting": dict(baseline=0, noise=0.5, anomaly=dict(start_offset_min=38, target=120, ramp_min=8)),
        },
        deploys=[
            dict(
                offset_min=-15,
                version="1.4.2",
                service="marketing-site",
                description="marketing-site: unrelated homepage banner update",
            )
        ],
        gold=dict(
            root_cause="A traffic surge from a flash-sale campaign exhausts orders-db's connection pool (max 50), causing checkout-api requests to queue and time out.",
            evidence=[
                "metric:db_pool_active saturates at the configured max (50) from minute ~45",
                "metric:db_pool_waiting climbs sharply from minute 38",
                "log: 'db connection pool exhausted: active=50 idle=0 waiting=...' from checkout-api",
                "deploy: the only recent deploy (marketing-site banner) is unrelated and should NOT be blamed — a common false-correlation trap",
            ],
            recommended_remediation="Temporarily raise the connection pool max and add read-replica routing for read-heavy queries; longer term, add pool-saturation alerting ahead of known campaigns.",
            remediation_risk_tier="medium",
            risk_rationale="A pool-size config bump via existing config management, without a full service redeploy, is treated as medium risk here — but escalate to high if it requires a restart.",
        ),
        primary_stage_use=["6", "8", "19"],
        distractor_note="The marketing-site deploy is a same-timeframe red herring; correct diagnosis must not attribute the incident to it.",
    ),
    dict(
        id="INC-006",
        slug="disk-full-log-partition",
        category="disk_full",
        difficulty="easy",
        services=["notifications-service"],
        total_minutes=180,
        incident_start_min=100,
        metrics={
            "disk_used_pct": dict(baseline=55, noise=1, anomaly=dict(start_offset_min=100, target=100, ramp_min=60))
        },
        deploys=[],
        gold=dict(
            root_cause="A misconfigured debug-log verbosity setting left on since the last incident causes notifications-service to fill its /var/log partition.",
            evidence=[
                "metric:disk_used_pct climbs steadily from minute 100, saturating at 100% by minute ~160",
                "log: '/var/log partition at ...% capacity' warnings escalating in frequency",
                "log: 'write failed: No space left on device' errors once disk hits 100%",
            ],
            recommended_remediation="Rotate/purge old logs to free space immediately; revert debug verbosity to normal and add disk-usage alerting at 80%.",
            remediation_risk_tier="high",
            risk_rationale="Executing a cleanup/remediation script on a production host is high-risk per the taxonomy, even though the individual commands seem low-stakes.",
        ),
        primary_stage_use=["3", "6", "19"],
    ),
    dict(
        id="INC-007",
        slug="network-partition-eth0-packet-loss",
        category="network_partition",
        difficulty="hard",
        services=["checkout-api", "inventory-service"],
        total_minutes=90,
        incident_start_min=30,
        metrics={
            "latency_p99_ms": dict(baseline=140, noise=20, anomaly=dict(start_offset_min=30, target=3800, ramp_min=4)),
            "tcp_retransmit_rate": dict(
                baseline=0.2, noise=0.1, anomaly=dict(start_offset_min=30, target=14, ramp_min=4)
            ),
        },
        deploys=[],
        gold=dict(
            root_cause="A top-of-rack switch fault on the node hosting inventory-service causes elevated packet loss between checkout-api and inventory-service.",
            evidence=[
                "metric:tcp_retransmit_rate jumps from 0.2% to 14% at minute 30, sharply, not gradually",
                "metric:latency_p99_ms spikes in lockstep",
                "log: 'packet loss detected ...% on eth0' and 'connection reset by peer' from both services simultaneously",
                "no deploys in the window — ruling out a code-change root cause",
            ],
            recommended_remediation="Cordon and drain the affected node; file an infrastructure ticket with the platform team for the switch fault.",
            remediation_risk_tier="high",
            risk_rationale="Cordoning/draining a production node is an infrastructure-level high-risk action.",
        ),
        primary_stage_use=["6", "8", "9", "19"],
        distractor_note="Symptom profile (latency + resets) is easy to mis-attribute to an application-level GC issue (compare INC-017); correct diagnosis requires reading the retransmit-rate metric, not just latency.",
    ),
    dict(
        id="INC-008",
        slug="third-party-payment-gateway-outage",
        category="third_party_outage",
        difficulty="medium",
        services=["payment-service"],
        total_minutes=60,
        incident_start_min=10,
        metrics={
            "error_rate_pct": dict(baseline=0.3, noise=0.2, anomaly=dict(start_offset_min=10, target=38, ramp_min=2))
        },
        deploys=[],
        gold=dict(
            root_cause="The external card-network payment gateway is down (confirmed by their public status page), returning 503s to all outbound calls.",
            evidence=[
                "log: 'payment-gateway API returned 503 Service Unavailable' starting minute 10",
                "log: 'circuit breaker OPEN for external payment-gateway' shortly after",
                "metric:error_rate_pct steps sharply and stays flat at the elevated level — consistent with a binary external outage, not a gradual internal degradation",
                "no deploys in the window on payment-service",
            ],
            recommended_remediation="No internal fix available; enable the fallback payment provider if configured, and monitor the vendor's status page for resolution.",
            remediation_risk_tier="medium",
            risk_rationale="Switching to a fallback provider is a visible, persistent routing change but does not touch production code paths — classified medium, escalate to high if it also requires a redeploy.",
        ),
        primary_stage_use=["6", "19"],
    ),
    dict(
        id="INC-009",
        slug="dns-resolution-failure-internal-svc",
        category="dns_misconfig",
        difficulty="medium",
        services=["checkout-api"],
        total_minutes=60,
        incident_start_min=20,
        metrics={
            "error_rate_pct": dict(baseline=0.3, noise=0.2, anomaly=dict(start_offset_min=20, target=30, ramp_min=2))
        },
        deploys=[
            dict(
                offset_min=-8,
                version="n/a",
                service="platform-dns",
                description="platform: internal DNS CoreDNS config map updated (cache TTL change)",
            )
        ],
        gold=dict(
            root_cause="A CoreDNS config map change dropped the internal search-domain entry, so lookups for internal-service.svc.cluster.local fail intermittently.",
            evidence=[
                "log: 'DNS resolution failed for internal-service.svc.cluster.local' repeated from checkout-api",
                "log: 'getaddrinfo ENOTFOUND' errors matching the same hostname",
                "deploy: CoreDNS config map change 8 minutes before window start correlates with onset",
            ],
            recommended_remediation="Revert the CoreDNS config map change; validate internal service discovery recovers across all namespaces, not just checkout-api.",
            remediation_risk_tier="high",
            risk_rationale="Cluster-wide DNS config change is high-risk (affects many services beyond the one showing symptoms).",
        ),
        primary_stage_use=["6", "19"],
    ),
    dict(
        id="INC-010",
        slug="tls-cert-expiry-checkout-ingress",
        category="tls_cert_expiry",
        difficulty="easy",
        services=["checkout-api"],
        total_minutes=45,
        incident_start_min=5,
        metrics={
            "error_rate_pct": dict(baseline=0.2, noise=0.1, anomaly=dict(start_offset_min=5, target=95, ramp_min=1))
        },
        deploys=[],
        gold=dict(
            root_cause="The ingress TLS certificate for checkout-api expired; automatic renewal failed silently two weeks prior.",
            evidence=[
                "log: 'TLS handshake failed: certificate has expired' from essentially all clients starting minute 5",
                "metric:error_rate_pct jumps almost immediately (within 1 minute) to ~95% — a signature of a hard cutover, not a gradual degradation",
                "no deploys in window — this is an operational/certificate lifecycle issue, not a code change",
            ],
            recommended_remediation="Issue and deploy a new certificate immediately; fix the renewal automation and add expiry alerting at 14/7/1 days out.",
            remediation_risk_tier="high",
            risk_rationale="Rotating a production ingress certificate is high-risk (customer-facing, affects all traffic).",
        ),
        primary_stage_use=["3", "6", "19"],
    ),
    dict(
        id="INC-011",
        slug="mq-consumer-lag-notifications",
        category="mq_backlog",
        difficulty="medium",
        services=["notifications-service"],
        total_minutes=90,
        incident_start_min=15,
        metrics={
            "consumer_lag_msgs": dict(
                baseline=200, noise=50, anomaly=dict(start_offset_min=15, target=95000, ramp_min=60)
            )
        },
        deploys=[
            dict(
                offset_min=-5,
                version="1.8.0",
                description="notifications-service: added synchronous audit-log write per message",
            )
        ],
        gold=dict(
            root_cause="v1.8.0 added a synchronous per-message audit-log write, roughly 8x-ing per-message processing time and causing consumers to fall behind the publish rate.",
            evidence=[
                "metric:consumer_lag_msgs climbs from ~200 baseline to ~95000 over the window",
                "log: 'consumer lag increased to ... messages' escalating warnings",
                "log: 'consumer group rebalance triggered repeatedly' — a symptom of consumers being killed for missing heartbeats while blocked on the slow audit write",
                "deploy: v1.8.0 5 minutes before window start matches onset timing",
            ],
            recommended_remediation="Roll back v1.8.0 or make the audit-log write asynchronous; scale out consumer replicas temporarily to drain the backlog.",
            remediation_risk_tier="high",
            risk_rationale="Rollback plus scaling replicas are both explicitly high-risk actions.",
        ),
        primary_stage_use=["6", "19"],
    ),
    dict(
        id="INC-012",
        slug="k8s-crashloop-bad-readiness-probe",
        category="k8s_crashloop",
        difficulty="medium",
        services=["inventory-service"],
        total_minutes=45,
        incident_start_min=5,
        metrics={
            "pod_restart_count": dict(baseline=0, noise=0.1, anomaly=dict(start_offset_min=5, target=14, ramp_min=15))
        },
        deploys=[
            dict(
                offset_min=-3,
                version="3.1.0",
                description="inventory-service: readiness probe path changed from /healthz to /health",
            )
        ],
        gold=dict(
            root_cause="v3.1.0 changed the readiness probe path to /health, but the route was never added to the service, so every pod fails readiness and gets recycled.",
            evidence=[
                "log: 'readiness probe failed: HTTP probe returned status 500' immediately after rollout",
                "log: 'pod entered CrashLoopBackOff' shortly after",
                "metric:pod_restart_count climbs from 0 to double digits within 15 minutes",
                "deploy: v3.1.0 probe path change 3 minutes before window start is the exact trigger",
            ],
            recommended_remediation="Roll back the probe path change (or add the missing /health route) and redeploy.",
            remediation_risk_tier="high",
            risk_rationale="Redeploying a production service is explicitly high-risk.",
        ),
        primary_stage_use=["3", "6", "19"],
    ),
    dict(
        id="INC-013",
        slug="autoscaling-hpa-not-triggering",
        category="autoscaling_failure",
        difficulty="hard",
        services=["checkout-api"],
        total_minutes=90,
        incident_start_min=20,
        metrics={
            "cpu_percent": dict(baseline=40, noise=4, anomaly=dict(start_offset_min=20, target=95, ramp_min=10)),
            "replica_count": dict(baseline=4, noise=0.05, anomaly=dict(start_offset_min=20, target=4, ramp_min=1)),
        },
        deploys=[
            dict(
                offset_min=-40,
                version="n/a",
                service="platform-metrics-server",
                description="platform: metrics-server upgraded to v0.7.2",
            )
        ],
        gold=dict(
            root_cause="The metrics-server upgrade broke the custom-metrics API path the HPA depends on; CPU climbs under real load but the HPA silently fails to evaluate, so replica_count never increases.",
            evidence=[
                "metric:cpu_percent ramps to 95% from minute 20, sustained",
                "metric:replica_count stays flat at 4 throughout — the key anomaly is an ABSENCE of the expected scale-up",
                "log: 'autoscaler evaluation error: metrics API unavailable' correlating with the metrics-server upgrade window",
                "deploy: metrics-server v0.7.2 upgrade 40 minutes before window start",
            ],
            recommended_remediation="Roll back metrics-server to the prior version to restore HPA evaluation; manually scale replicas in the interim.",
            remediation_risk_tier="high",
            risk_rationale="Manual scaling and a platform-component rollback are both high-risk, cluster-wide-impact actions.",
        ),
        primary_stage_use=["6", "9", "19"],
        distractor_note="The 'flat replica_count' signal is easy to overlook if an agent only checks whether CPU is high, rather than checking whether the expected corrective action occurred.",
    ),
    dict(
        id="INC-014",
        slug="secret-rotation-auth-failures",
        category="secret_rotation_failure",
        difficulty="medium",
        services=["checkout-api", "auth-service"],
        total_minutes=45,
        incident_start_min=8,
        metrics={
            "error_rate_pct": dict(baseline=0.3, noise=0.2, anomaly=dict(start_offset_min=8, target=40, ramp_min=2))
        },
        deploys=[
            dict(
                offset_min=-6,
                version="n/a",
                service="auth-service",
                description="platform: auth-service client secret rotated via vault",
            )
        ],
        gold=dict(
            root_cause="auth-service's client secret was rotated in Vault, but checkout-api's cached secret volume wasn't refreshed, so it keeps presenting the old (now invalid) credential.",
            evidence=[
                "log: '401 Unauthorized from downstream auth-service' starting minute 8",
                "log: 'secret volume mount stale, using expired token'",
                "deploy: secret rotation 6 minutes before window start matches onset exactly",
            ],
            recommended_remediation="Force a secret-volume remount / pod restart on checkout-api to pick up the new credential; add a post-rotation health check to the rotation runbook.",
            remediation_risk_tier="high",
            risk_rationale="This touches credentials and requires a service restart — both explicitly high-risk.",
        ),
        primary_stage_use=["6", "19"],
    ),
    dict(
        id="INC-015",
        slug="config-drift-max-retries-prod",
        category="config_drift",
        difficulty="hard",
        services=["checkout-api", "payment-service"],
        total_minutes=90,
        incident_start_min=30,
        metrics={
            "request_rate_multiplier": dict(
                baseline=1.0, noise=0.05, anomaly=dict(start_offset_min=30, target=3.2, ramp_min=15)
            ),
            "latency_p99_ms": dict(baseline=150, noise=15, anomaly=dict(start_offset_min=32, target=2600, ramp_min=15)),
        },
        deploys=[
            dict(
                offset_min=-500,
                version="n/a",
                description="config: MAX_RETRIES override for prod set to 10 during a since-resolved past incident, never reverted",
            )
        ],
        gold=dict(
            root_cause="A MAX_RETRIES=10 override left over from an unrelated past incident (never reverted) causes checkout-api to retry aggressively once payment-service latency ticks up even slightly, amplifying load 3x and creating a retry storm.",
            evidence=[
                "metric:request_rate_multiplier climbs to 3.2x baseline starting minute 30 with no corresponding increase in real user traffic",
                "metric:latency_p99_ms follows shortly after — an effect of the retry storm, not its cause",
                "log: 'config value MAX_RETRIES=10 in prod, expected 3' and 'unexpected retry storm traced to environment-specific override'",
                "deploy: the stale config override predates this window by ~8 hours — the trigger was a minor, otherwise-unremarkable payment-service latency blip, not a new deploy",
            ],
            recommended_remediation="Revert MAX_RETRIES to 3 in prod config; audit for other stale incident-response overrides that were never cleaned up.",
            remediation_risk_tier="medium",
            risk_rationale="A config value correction via existing config management, without a service redeploy, is treated as medium — escalate to high if applying it requires a restart.",
        ),
        primary_stage_use=["6", "9", "19"],
        distractor_note="Both the recent deploy list and the immediate trigger (a minor latency blip) are red herrings; the real root cause is 8 hours stale and requires checking config state, not recent changes.",
    ),
    dict(
        id="INC-016",
        slug="lb-misconfig-uneven-traffic-node3",
        category="lb_misconfig",
        difficulty="medium",
        services=["checkout-api"],
        total_minutes=60,
        incident_start_min=10,
        metrics={
            "error_rate_pct": dict(baseline=0.4, noise=0.2, anomaly=dict(start_offset_min=10, target=18, ramp_min=3))
        },
        deploys=[
            dict(
                offset_min=-12,
                version="n/a",
                service="platform-load-balancer",
                description="platform: load balancer health-check path updated for checkout-api node pool",
            )
        ],
        gold=dict(
            root_cause="The updated load-balancer health-check path doesn't match node-3's actual health endpoint, so the LB routes traffic to node-3 despite it being unable to serve those requests correctly.",
            evidence=[
                "log: 'uneven traffic distribution: node-3 receiving 80% of requests'",
                "log: 'health check misconfigured, routing to unhealthy node'",
                "log: '502 Bad Gateway rate increased on node-3' specifically (not the other nodes)",
                "deploy: LB health-check path update 12 minutes before window start",
            ],
            recommended_remediation="Revert the health-check path to the previous value, or fix node-3's endpoint to match; verify traffic rebalances evenly.",
            remediation_risk_tier="medium",
            risk_rationale="Load-balancer config revert is a persistent infra change but doesn't touch application code or require an app redeploy — medium.",
        ),
        primary_stage_use=["6", "19"],
    ),
    dict(
        id="INC-017",
        slug="gc-pause-storm-order-service",
        category="gc_pause_storm",
        difficulty="hard",
        services=["orders-service"],
        total_minutes=90,
        incident_start_min=25,
        metrics={
            "latency_p99_ms": dict(baseline=140, noise=15, anomaly=dict(start_offset_min=25, target=3400, ramp_min=8)),
            "gc_pause_ms": dict(baseline=30, noise=8, anomaly=dict(start_offset_min=24, target=3200, ramp_min=6)),
        },
        deploys=[
            dict(offset_min=-4, version="5.2.0", description="orders-service: increased in-memory order cache size 4x")
        ],
        gold=dict(
            root_cause="v5.2.0 quadrupled the in-memory order cache size without increasing heap allocation, pushing the JVM into frequent stop-the-world full GC cycles that manifest as request latency spikes.",
            evidence=[
                "metric:gc_pause_ms spikes first, one minute before latency_p99_ms — the ordering matters for correct attribution",
                "log: 'JVM GC pause STW duration ...ms' and 'full GC triggered ... times in 10 minutes'",
                "deploy: v5.2.0 cache-size increase 4 minutes before window start",
            ],
            recommended_remediation="Roll back the cache size increase, or increase JVM heap allocation to match; add GC-pause alerting.",
            remediation_risk_tier="high",
            risk_rationale="Rollback/config change requiring redeploy is high-risk.",
        ),
        primary_stage_use=["6", "8", "9", "19"],
        distractor_note="Superficially resembles INC-007's network-partition symptom profile (latency spikes + connection resets, since GC pauses also cause transient connection resets). Distinguishing signal is gc_pause_ms leading latency_p99_ms by ~1 minute, versus INC-007's simultaneous spike in tcp_retransmit_rate. Designed as a debate-mechanism (Stage 8) test pair with INC-007.",
    ),
    dict(
        id="INC-018",
        slug="n-plus-one-query-cart-service",
        category="n_plus_one_query",
        difficulty="medium",
        services=["cart-service", "orders-db"],
        total_minutes=90,
        incident_start_min=30,
        metrics={
            "db_cpu_pct": dict(baseline=25, noise=3, anomaly=dict(start_offset_min=30, target=94, ramp_min=20)),
            "latency_p99_ms": dict(baseline=150, noise=20, anomaly=dict(start_offset_min=32, target=2100, ramp_min=20)),
        },
        deploys=[
            dict(
                offset_min=-10,
                version="1.6.0",
                description="cart-service: added related-items suggestions to cart view via ORM relationship",
            )
        ],
        gold=dict(
            root_cause="v1.6.0's related-items suggestion feature uses ORM lazy-loading, issuing one query per cart item instead of a single batched query — an N+1 pattern that saturates db CPU under moderate load.",
            evidence=[
                "log: 'query pattern detected: ... SELECT statements issued for one request'",
                "log: 'ORM lazy-loading triggering repeated per-row queries'",
                "metric:db_cpu_pct climbs to 94% while metric:latency_p99_ms lags slightly behind it (query volume drives CPU, which then drives latency)",
                "deploy: v1.6.0 related-items feature 10 minutes before window start",
            ],
            recommended_remediation="Roll back the related-items feature or patch it to eager-load/batch the query; add a query-count-per-request guard to catch future N+1 regressions.",
            remediation_risk_tier="high",
            risk_rationale="Rollback/code fix requiring redeploy is high-risk.",
        ),
        primary_stage_use=["6", "8", "19"],
        distractor_note="Superficially resembles INC-005's DB-pool-exhaustion symptom profile (both show DB slowness affecting an API service). Distinguishing signal is db_cpu_pct saturating (compute-bound) here versus db_pool_waiting saturating (connection-bound) in INC-005. Designed as a debate-mechanism (Stage 8) test pair with INC-005.",
    ),
    dict(
        id="INC-019",
        slug="false-alarm-noisy-cpu-threshold",
        category="false_alarm",
        difficulty="easy",
        services=["search-service"],
        total_minutes=60,
        incident_start_min=None,
        metrics={"cpu_percent": dict(baseline=45, noise=4)},  # no anomaly at all
        metrics_spike={"cpu_percent": 30},  # single transient blip minute
        deploys=[],
        gold=dict(
            root_cause="No real incident. A single transient CPU sampling blip (one data point) crossed the alerting threshold; all other signals — logs, latency, error rate — remain nominal throughout.",
            evidence=[
                "metric:cpu_percent has exactly one elevated sample at minute 30, immediately returning to baseline",
                "log stream contains only INFO-level 'health check ok' / 'metrics within normal range' entries for the entire window — no ERROR or WARN entries at all",
                "no deploys in the window",
            ],
            recommended_remediation="No remediation needed. Close the alert as a false positive; consider adding a 2-consecutive-sample requirement to the alert rule to reduce noise.",
            remediation_risk_tier="low",
            risk_rationale="No system-touching action is taken; only the alert rule's own config may be tuned, which is a low-stakes, non-production-facing change.",
        ),
        is_false_alarm=True,
        primary_stage_use=["6", "8", "19", "20"],
        distractor_note="This scenario exists specifically to test false-positive handling (referenced by risk-classification.md and the eval rubric's false-positive-rate metric) and to check agents don't hallucinate a root cause when none exists.",
    ),
    dict(
        id="INC-020",
        slug="memory-leak-oom-notifications-recurrence",
        category="memory_leak",
        difficulty="medium",
        services=["notifications-service"],
        total_minutes=120,
        incident_start_min=30,
        metrics={
            "memory_percent": dict(baseline=30, noise=2, anomaly=dict(start_offset_min=30, target=97, ramp_min=80)),
            "gc_pause_ms": dict(baseline=35, noise=8, anomaly=dict(start_offset_min=75, target=2200, ramp_min=20)),
        },
        deploys=[
            dict(
                offset_min=-45,
                version="1.9.0",
                description="notifications-service: added per-recipient delivery-status cache",
            )
        ],
        gold=dict(
            root_cause="Same underlying pattern as INC-002: v1.9.0 added an in-memory delivery-status cache with no eviction policy, causing unbounded heap growth — a different service, same class of bug.",
            evidence=[
                "metric:memory_percent climbs steadily from minute 30, mirroring INC-002's shape",
                "metric:gc_pause_ms spikes from minute 75, mirroring INC-002's timing pattern relative to the memory ramp",
                "log: 'container OOMKilled by kubelet' entries late in the window",
                "deploy: v1.9.0 added an unbounded in-memory cache 45 minutes before window start — same causal pattern as INC-002's v2.1.0 cache",
            ],
            recommended_remediation="Roll back to v1.8.x, add a bounded/TTL eviction policy to the new cache before re-deploying — identical remediation shape to INC-002.",
            remediation_risk_tier="high",
            risk_rationale="Rollback of a deploy is explicitly high-risk.",
        ),
        recurrence_of="INC-002",
        primary_stage_use=["12", "19"],
        distractor_note="Designed to test Stage 12 episodic memory: an agent that retrieved and generalized from INC-002's post-mortem should diagnose this faster/more accurately than one seeing it cold, despite the different service name and cache field names.",
    ),
    # ------------------------------------------------------------------
    # Dataset v2 (INC-021..028). Added before any measurement; INC-001..020 are untouched.
    # Four HARD false alarms (a benign explanation exists in the data) and four REAL incidents that
    # superficially look benign. Stage 8 needs both: debate must remove false positives without
    # learning to dismiss everything.
    # ------------------------------------------------------------------
    dict(
        id="INC-021",
        slug="false-alarm-planned-node-drain",
        category="false_alarm_maintenance",
        difficulty="medium",
        services=["inventory-service"],
        total_minutes=75,
        incident_start_min=None,
        metrics={
            "cpu_percent": dict(
                baseline=35,
                noise=3,
                transient=True,
                anomaly=dict(start_offset_min=20, target=78, ramp_min=3, duration_min=12, recovers=True),
            ),
            "latency_p99_ms": dict(
                baseline=90,
                noise=10,
                transient=True,
                anomaly=dict(start_offset_min=20, target=210, ramp_min=3, duration_min=12, recovers=True),
            ),
        },
        deploys=[
            dict(
                offset_min=-120,
                version="maint-CHG-4821",
                description="PLANNED MAINTENANCE CHG-4821 (approved window 00:18-00:32): rolling node drain of node pool b for kernel patching; no application change",
            )
        ],
        log_events=[
            dict(minute=18, level="INFO", msg="maintenance window CHG-4821 started: draining node pool b"),
            dict(
                minute=32, level="INFO", msg="maintenance window CHG-4821 complete: pods rescheduled, nodes uncordoned"
            ),
        ],
        warn_window=dict(
            start=19,
            end=31,
            rate=2,
            templates=[
                "pod evicted by node drain (graceful, replacement scheduled)",
                "readiness probe slow during rescheduling (expected during drain)",
            ],
        ),
        gold=dict(
            benign_warnings=True,
            root_cause="No real incident. The alert was caused by the pre-announced rolling node drain CHG-4821 (00:18-00:32): CPU and latency rose briefly while pods were rescheduled and returned to baseline on their own before the maintenance window closed.",
            evidence=[
                "deploy: change record CHG-4821 filed before the window: planned node drain, approved window 00:18-00:32, no application change",
                "log: maintenance window CHG-4821 started at minute 18 and completed at minute 32; WARN lines are graceful pod evictions only",
                "metric:cpu_percent and metric:latency_p99_ms rise inside the window and recover to baseline right after it",
            ],
            recommended_remediation="No remediation needed. Close the alert as expected maintenance and add a schedule-aware silence for approved maintenance windows.",
            remediation_risk_tier="low",
            risk_rationale="No system-touching action; only the alert rule's own configuration may be tuned.",
        ),
        is_false_alarm=True,
        primary_stage_use=["8", "19"],
        distractor_note="Hard false alarm: WARN lines and a visible CPU/latency excursion exist, but a deploy entry and log events document a planned maintenance window and everything recovers.",
    ),
    dict(
        id="INC-022",
        slug="false-alarm-nightly-batch-reindex",
        category="false_alarm_batch",
        difficulty="medium",
        services=["search-indexer"],
        total_minutes=90,
        incident_start_min=None,
        metrics={
            "memory_percent": dict(
                baseline=40,
                noise=2,
                transient=True,
                anomaly=dict(start_offset_min=30, target=72, ramp_min=8, duration_min=25, recovers=True),
            ),
            "cpu_percent": dict(
                baseline=28,
                noise=3,
                transient=True,
                anomaly=dict(start_offset_min=30, target=70, ramp_min=5, duration_min=25, recovers=True),
            ),
        },
        deploys=[],
        log_events=[
            dict(minute=30, level="INFO", msg="nightly reindex job started (scheduled, cron 30 0 * * *)"),
            dict(minute=55, level="INFO", msg="nightly reindex job finished: 1.2M documents indexed in 24m"),
        ],
        warn_window=dict(
            start=31,
            end=54,
            rate=1,
            lat=(500, 900),
            templates=["slow query detected: {lat}ms (batch workload, expected during reindex)"],
        ),
        gold=dict(
            benign_warnings=True,
            root_cause="No real incident. The nightly scheduled reindex job (00:30-00:55) caused the expected CPU and memory increase and some slow batch queries; resource usage returned to baseline when the job finished.",
            evidence=[
                "log: nightly reindex job started at minute 30 (scheduled) and finished at minute 55",
                "metric:memory_percent and metric:cpu_percent rise only while the job runs and return to baseline afterwards",
                "log: the slow-query WARN lines are labelled as expected batch workload; there are no ERROR lines",
            ],
            recommended_remediation="No remediation needed. Add a schedule-aware silence for the nightly reindex window to the alert rule.",
            remediation_risk_tier="low",
            risk_rationale="No system-touching action; only the alert rule's own configuration may be tuned.",
        ),
        is_false_alarm=True,
        primary_stage_use=["8", "19"],
        distractor_note="Hard false alarm: a sizeable, sustained resource excursion plus WARN lines, fully explained by a scheduled job visible in the logs. Twin of INC-028, where a batch job masks a real consumer stall.",
    ),
    dict(
        id="INC-023",
        slug="false-alarm-metrics-exporter-sentinel",
        category="false_alarm_exporter",
        difficulty="hard",
        services=["ledger-service"],
        total_minutes=60,
        incident_start_min=None,
        metrics={
            "latency_p99_ms": dict(baseline=110, noise=8, gap=dict(start=25, end=31, value=5000.0)),
            "requests_per_sec": dict(baseline=320, noise=15),
        },
        deploys=[
            dict(
                offset_min=-60,
                service="metrics-exporter",
                version="0.18.3",
                description="Scheduled sidecar upgrade of the metrics exporter (0.18.2 -> 0.18.3), planned for 00:25-00:31; ledger-service itself is unchanged",
            )
        ],
        log_events=[
            dict(minute=25, level="INFO", msg="metrics exporter restarted (scheduled upgrade 0.18.2 -> 0.18.3)"),
            dict(minute=31, level="INFO", msg="metrics exporter scrape resumed"),
        ],
        gold=dict(
            root_cause="No real incident. The p99 latency spike to 5000 ms is a metrics artefact: the metrics exporter was restarted during a scheduled upgrade (00:25-00:31) and reported its probe-timeout sentinel, while the application logs show requests processed at normal latency throughout.",
            evidence=[
                "metric:latency_p99_ms reads exactly 5000 for minutes 25-30 then returns to baseline; requests_per_sec never changes",
                "log: metrics exporter restarted at minute 25 (scheduled upgrade) and resumed scraping at minute 31",
                "log: ledger-service keeps logging 'request processed' at normal latency during the spike; no ERROR or WARN lines",
            ],
            recommended_remediation="No remediation needed. Make the latency alert ignore exporter sentinel values and missing scrapes.",
            remediation_risk_tier="low",
            risk_rationale="No system-touching action; only the alert rule's own configuration may be tuned.",
        ),
        is_false_alarm=True,
        primary_stage_use=["8", "19", "20"],
        distractor_note="Hard false alarm: the metric alone looks like a severe latency incident; only the logs and the exporter upgrade show it is an artefact.",
    ),
    dict(
        id="INC-024",
        slug="false-alarm-approved-load-test",
        category="false_alarm_loadtest",
        difficulty="medium",
        services=["catalog-api"],
        total_minutes=60,
        incident_start_min=None,
        metrics={
            "requests_per_sec": dict(
                baseline=200,
                noise=15,
                transient=True,
                anomaly=dict(start_offset_min=20, target=1400, ramp_min=2, duration_min=15, recovers=True),
            ),
            "cpu_percent": dict(
                baseline=30,
                noise=3,
                transient=True,
                anomaly=dict(start_offset_min=20, target=68, ramp_min=3, duration_min=15, recovers=True),
            ),
            "latency_p99_ms": dict(
                baseline=80,
                noise=8,
                transient=True,
                anomaly=dict(start_offset_min=21, target=180, ramp_min=3, duration_min=14, recovers=True),
            ),
        },
        deploys=[],
        log_events=[
            dict(
                minute=20,
                level="INFO",
                msg="synthetic load test lt-0301 started by perf-team (approved, change CHG-4830)",
            ),
            dict(minute=35, level="INFO", msg="synthetic load test lt-0301 finished"),
        ],
        warn_window=dict(
            start=21, end=35, rate=1, templates=["rate limiter engaged for client lt-runner (expected, test traffic)"]
        ),
        gold=dict(
            benign_warnings=True,
            root_cause="No real incident. An approved synthetic load test (lt-0301, CHG-4830, 00:20-00:35) multiplied request volume 7x; CPU and latency rose moderately, the rate limiter engaged for the test client as designed, and everything returned to baseline when the test ended.",
            evidence=[
                "log: synthetic load test lt-0301 started by perf-team at minute 20 (approved) and finished at minute 35",
                "metric:requests_per_sec jumps from about 200 to 1400 between minute 20 and 35 then returns; CPU and latency follow and recover",
                "log: the only WARN lines say the rate limiter engaged for the test client (expected); no ERROR lines",
            ],
            recommended_remediation="No remediation needed. Add a schedule-aware silence for approved load tests to the alert rule.",
            remediation_risk_tier="low",
            risk_rationale="No system-touching action; only the alert rule's own configuration may be tuned.",
        ),
        is_false_alarm=True,
        primary_stage_use=["8", "19"],
        distractor_note="Hard false alarm: a large traffic surge with a latency rise looks like an overload incident; the logs show it was a documented, approved test.",
    ),
    dict(
        id="INC-025",
        slug="slow-burn-memory-leak-no-errors",
        category="slow_memory_leak",
        difficulty="hard",
        services=["reporting-service"],
        total_minutes=120,
        incident_start_min=10,
        metrics={
            "memory_percent": dict(baseline=38, noise=1.5, anomaly=dict(start_offset_min=10, target=83, ramp_min=110)),
            "gc_pause_ms": dict(baseline=30, noise=5, anomaly=dict(start_offset_min=70, target=95, ramp_min=40)),
            "cpu_percent": dict(baseline=25, noise=3),
        },
        deploys=[
            dict(
                offset_min=-30,
                version="3.2.0",
                description="reporting-service: added in-process memoization of dashboard query results (no size bound)",
            )
        ],
        warn_window=dict(
            start=60, end=120, rate=1, pct=(62, 82), templates=["heap usage {pct}% of limit, approaching hard cap"]
        ),
        gold=dict(
            root_cause="A real, slow memory leak. Release 3.2.0 added unbounded in-process memoization of dashboard query results, so heap usage climbs steadily from 38% to over 80% across the window; there are no ERROR lines yet, but the service will hit its limit and be OOM-killed if nothing is done.",
            evidence=[
                "metric:memory_percent climbs steadily from minute 10 to about 83% by the end of the window and never recovers",
                "metric:gc_pause_ms starts rising around minute 70",
                "deploy: v3.2.0 added unbounded memoization 30 minutes before the window",
                "log: only late WARN lines 'heap usage N% of limit, approaching hard cap' with N increasing; no ERROR lines",
            ],
            recommended_remediation="Roll back to the previous release (3.1.x) and bound the memoization cache with a size limit and TTL before redeploying.",
            remediation_risk_tier="high",
            risk_rationale="Rolling back a production deploy is an explicit high-risk action.",
        ),
        primary_stage_use=["8", "19"],
        distractor_note="False-negative trap: no ERROR lines, CPU is normal and the service is still serving. A debate that treats 'no errors' as 'no incident' dismisses a real, worsening leak.",
    ),
    dict(
        id="INC-026",
        slug="post-maintenance-config-regression",
        category="post_maintenance_regression",
        difficulty="hard",
        services=["orders-api"],
        total_minutes=90,
        incident_start_min=31,
        metrics={
            "cpu_percent": dict(
                baseline=30,
                noise=3,
                anomaly=dict(start_offset_min=20, target=60, ramp_min=2, duration_min=10, recovers=True),
            ),
            "latency_p99_ms": dict(baseline=100, noise=10, anomaly=dict(start_offset_min=31, target=3600, ramp_min=6)),
            "error_rate_percent": dict(
                baseline=0.5, noise=0.2, anomaly=dict(start_offset_min=31, target=24, ramp_min=5)
            ),
        },
        deploys=[
            dict(
                offset_min=-180,
                version="maint-CHG-5012",
                description="PLANNED MAINTENANCE CHG-5012 (approved window 00:20-00:30): rolling restart with configuration refresh",
            )
        ],
        log_events=[
            dict(
                minute=20, level="INFO", msg="maintenance window CHG-5012 started: rolling restart with config refresh"
            ),
            dict(minute=30, level="INFO", msg="maintenance window CHG-5012 complete"),
        ],
        gold=dict(
            root_cause="A real incident that follows a planned maintenance but is not part of it. The configuration refresh in CHG-5012 loaded POOL_SIZE=2 instead of 50, so the db connection pool is exhausted from minute 31 on; latency and error rate climb after the window closed and do not recover.",
            evidence=[
                "log: 'config value POOL_SIZE=2 loaded after refresh (previous value 50)' and 'db connection pool exhausted: active=2 idle=0' starting minute 31",
                "metric:error_rate_percent and metric:latency_p99_ms rise from minute 31, after the maintenance window ended at minute 30, and stay elevated",
                "deploy: change record CHG-5012, planned maintenance with configuration refresh, approved window 00:20-00:30",
            ],
            recommended_remediation="Revert POOL_SIZE to 50 in the refreshed configuration and reload the service configuration.",
            remediation_risk_tier="medium",
            risk_rationale="Reverting a configuration value is a reversible persistent change (medium); it is not a restart or rollback of a deploy.",
        ),
        primary_stage_use=["8", "19"],
        distractor_note="False-negative trap and twin of INC-021: a documented maintenance window and a recovered CPU blip invite 'expected maintenance', but errors start after the window closed and never recover.",
    ),
    dict(
        id="INC-027",
        slug="partial-secret-rotation-low-error-rate",
        category="partial_secret_rotation",
        difficulty="hard",
        services=["auth-proxy"],
        total_minutes=90,
        incident_start_min=25,
        anomaly_rate=1,
        metrics={
            "error_rate_percent": dict(
                baseline=0.3, noise=0.1, anomaly=dict(start_offset_min=25, target=3.5, ramp_min=4)
            ),
            "latency_p99_ms": dict(baseline=70, noise=6),
        },
        deploys=[
            dict(
                offset_min=-5,
                service="secrets-manager",
                version="rotation-2026-03-01",
                description="Scheduled rotation of the billing-api credential; rollout to auth-proxy pods completed on 2 of 3 pods",
            )
        ],
        gold=dict(
            root_cause="A real, partial failure after a credential rotation: one of three auth-proxy pods (auth-proxy-2) still presents the old, now-revoked billing-api credential, so about 3% of requests fail with 401 while the other pods are healthy.",
            evidence=[
                "metric:error_rate_percent steps from about 0.3% to about 3.5% at minute 25, shortly after the rotation, and stays there; latency is normal",
                "log: repeated '401 Unauthorized from downstream billing-api (pod auth-proxy-2): credential rejected'",
                "deploy: credential rotation recorded just before the window as rolled out to only 2 of 3 pods",
            ],
            recommended_remediation="Re-sync the rotated credential to the stale pod auth-proxy-2 and restart it so it mounts the new secret.",
            remediation_risk_tier="high",
            risk_rationale="Touching credentials/secrets and restarting a production pod are explicit high-risk actions.",
        ),
        primary_stage_use=["8", "19"],
        distractor_note="False-negative trap: a 3% error rate, normal latency and a sparse error log look like noise, but the failures are persistent, localized to one pod and tied to the rotation.",
    ),
    dict(
        id="INC-028",
        slug="consumer-stall-hidden-by-batch-job",
        category="consumer_stall_during_batch",
        difficulty="hard",
        services=["email-worker"],
        total_minutes=90,
        incident_start_min=15,
        normal_until_min=41,
        metrics={
            "queue_depth": dict(baseline=150, noise=20, anomaly=dict(start_offset_min=15, target=52000, ramp_min=3)),
            "messages_consumed_per_min": dict(
                baseline=900, noise=40, anomaly=dict(start_offset_min=41, target=0, ramp_min=1)
            ),
        },
        deploys=[],
        log_events=[
            dict(minute=15, level="INFO", msg="nightly digest job enqueued 50000 messages (scheduled)"),
            dict(minute=41, level="ERROR", msg="consumer thread died: OutOfMemoryError in template renderer"),
            dict(minute=45, level="WARN", msg="consumer heartbeat missed for 4 minutes"),
            dict(minute=52, level="WARN", msg="consumer heartbeat missed for 11 minutes"),
            dict(minute=60, level="WARN", msg="consumer heartbeat missed for 19 minutes"),
            dict(minute=75, level="WARN", msg="consumer heartbeat missed for 34 minutes"),
        ],
        gold=dict(
            root_cause="A real incident hidden behind an expected batch: the scheduled nightly digest job legitimately enqueued 50000 messages at minute 15, but the consumer then died at minute 41 (OutOfMemoryError in the template renderer) and stopped consuming, so the queue never drains.",
            evidence=[
                "log: 'consumer thread died: OutOfMemoryError in template renderer' at minute 41, after which no messages are processed",
                "metric:messages_consumed_per_min drops from about 900 to 0 at minute 41 and stays there; queue_depth stays at about 52000",
                "log: consumer heartbeat missed warnings growing from 4 to 34 minutes",
                "log: nightly digest job enqueued 50000 messages at minute 15 (the expected part of the spike)",
            ],
            recommended_remediation="Restart the email-worker consumer pods to resume consumption, and cap the digest batch size or increase the template renderer memory limit.",
            remediation_risk_tier="high",
            risk_rationale="Restarting production pods is an explicit high-risk action.",
        ),
        primary_stage_use=["8", "19"],
        distractor_note="False-negative trap and twin of INC-022: the queue growth is explained by a scheduled batch job, which invites a false-alarm verdict, but consumption stopped and the queue never drains.",
    ),
]


def build_scenario(rng, spec):
    total_minutes = spec["total_minutes"]
    primary_service = spec["services"][0]
    cat_templates = TEMPLATES[spec["category"]]

    metrics_out = {}
    for name, m in spec["metrics"].items():
        anomaly = None
        if "anomaly" in m and (spec.get("incident_start_min") is not None or m.get("transient")):
            anomaly = m["anomaly"]  # "transient": a self-recovering excursion in a scenario with no real incident
        spike_at = None
        if "metrics_spike" in spec and name in spec["metrics_spike"]:
            spike_at = spec["metrics_spike"][name]
        metrics_out[name] = gen_metric_series(
            rng,
            total_minutes,
            step_min=1,
            baseline=m["baseline"],
            noise=m["noise"],
            anomaly=anomaly,
            spike_at=spike_at,
            gap=m.get("gap"),
        )

    anomaly_start = spec.get("incident_start_min")
    distractor_start = None
    if "distractor" in cat_templates and anomaly_start is not None:
        distractor_start = anomaly_start  # distractor noise co-occurs with the real anomaly

    logs = gen_logs(
        rng,
        total_minutes,
        primary_service,
        cat_templates,
        anomaly_start_offset_min=anomaly_start if anomaly_start is not None else total_minutes + 1,
        distractor_start_offset_min=distractor_start,
        events=spec.get("log_events"),
        warn_window=spec.get("warn_window"),
        normal_until_min=spec.get("normal_until_min"),
        **({"anomaly_rate_per_min": spec["anomaly_rate"]} if "anomaly_rate" in spec else {}),
    )

    deploys = gen_deploys(primary_service, spec["deploys"])

    meta = {
        "incident_id": spec["id"],
        "alert_name": f"Alert: anomalous behavior detected — {', '.join(spec['services'])}",
        "services": spec["services"],
        "window_start": BASE_TIME.isoformat(),
        "window_end": (BASE_TIME + timedelta(minutes=total_minutes)).isoformat(),
        "available_metrics": sorted(metrics_out.keys()),
        "note": "This metadata is intentionally non-spoiler: it names the services and window but not the cause.",
    }

    gold = dict(spec["gold"])
    gold.update(
        {
            "incident_id": spec["id"],
            "title": spec["slug"].replace("-", " "),
            "category": spec["category"],
            "difficulty": spec["difficulty"],
            "is_false_alarm": spec.get("is_false_alarm", False),
            "recurrence_of": spec.get("recurrence_of"),
            "distractor_note": spec.get("distractor_note"),
            "primary_stage_use": spec["primary_stage_use"],
        }
    )

    return meta, logs, metrics_out, deploys, gold


def main():
    INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)
    GOLD_DIR.mkdir(parents=True, exist_ok=True)

    index = []
    for i, spec in enumerate(SCENARIOS):
        rng = random.Random(f"{spec['id']}-{spec['slug']}")  # deterministic per-scenario seed
        meta, logs, metrics_out, deploys, gold = build_scenario(rng, spec)

        inc_dir = INCIDENTS_DIR / spec["id"]
        inc_dir.mkdir(parents=True, exist_ok=True)
        (inc_dir / "meta.json").write_text(json.dumps(meta, indent=2), newline="\n")
        (inc_dir / "logs.log").write_text("\n".join(logs) + "\n", newline="\n")
        (inc_dir / "metrics.json").write_text(json.dumps(metrics_out, indent=2), newline="\n")
        (inc_dir / "deploys.json").write_text(json.dumps(deploys, indent=2), newline="\n")

        (GOLD_DIR / f"{spec['id']}.json").write_text(json.dumps(gold, indent=2), newline="\n")

        index.append(
            {
                "incident_id": spec["id"],
                "slug": spec["slug"],
                "category": spec["category"],
                "difficulty": spec["difficulty"],
                "services": spec["services"],
                "is_false_alarm": spec.get("is_false_alarm", False),
                "recurrence_of": spec.get("recurrence_of"),
            }
        )
        print(f"generated {spec['id']} ({spec['slug']})")

    (INCIDENTS_DIR / "_index.json").write_text(json.dumps(index, indent=2), newline="\n")
    print(f"\n{len(SCENARIOS)} scenarios generated under {INCIDENTS_DIR}")
    print(f"gold answers written under {GOLD_DIR} (kept out of data/incidents/ to prevent eval leakage)")


if __name__ == "__main__":
    main()
