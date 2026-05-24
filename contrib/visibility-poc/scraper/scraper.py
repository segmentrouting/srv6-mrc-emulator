"""
srv6-scraper — Prometheus exporter for the SRv6 MRC lab.

DESIGN-ONLY SKELETON. NONE OF THIS RUNS YET. See
`docs/design-visibility.md` for what each piece will do.

The next agent implementing this should:

1. Fill in `parse_ip_link_stats()` — currently returns empty.
2. Fill in `parse_snmp6()` — currently returns empty.
3. Fill in `parse_mrc_snapshot()` — currently returns empty;
   remember the wrapper envelope (.ev_state.tenants["<tenant>"]
   is a LIST, not a dict).
4. Fill in `discover_targets()` to walk the topo.yaml.
5. Wire `scrape_once()` into a `ThreadPoolExecutor` (~16 workers).
6. Bring up `prometheus_client.start_http_server(9100)` in
   `main()`.

The data-source signatures, metric definitions, and retry helper
are all in their final shape and should NOT be redesigned without
re-reading `docs/design-visibility.md`.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable

# Real implementation must `pip install prometheus_client pyyaml`.
# Imports are guarded so this file parses on a laptop without the
# dependencies installed (skeleton-only).
try:
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        start_http_server,
    )
except ImportError:  # skeleton parses without the dep
    CollectorRegistry = Counter = Gauge = start_http_server = None  # type: ignore

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


log = logging.getLogger("srv6_scraper")


# ---------------------------------------------------------------------------
# Constants — match docs/design-visibility.md §6 (metric naming).
# ---------------------------------------------------------------------------

SCRAPE_INTERVAL_SECONDS_FAST = 1.0   # iface + snmp6
SCRAPE_INTERVAL_SECONDS_SLOW = 5.0   # mrc snapshots
EXEC_TIMEOUT_SECONDS = 5.0
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_MS = 100
HTTP_PORT = 9100
WORKER_THREADS = 16

# EV state enum used for srv6_mrc_ev_state gauge. Must stay in sync
# with srv6_mrc/mrc/ev_state.py.  Keep the comment so the next agent
# checks this on each lab bump.
EV_STATE_VALUES = {
    "unknown": 0,
    "good": 1,
    "suspect": 2,
    "assumed_bad": 3,
}


# ---------------------------------------------------------------------------
# Dataclasses — typed metric payloads from each data source.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IfaceTarget:
    """One fabric NIC to scrape via `ip -stats link show`."""
    node: str          # e.g. "p0-leaf03"
    iface: str         # e.g. "Ethernet0"
    plane: int         # derived from node name or NIC ordinal
    tier: str          # "spine" | "leaf" | "host"


@dataclass(frozen=True)
class IfaceCounters:
    rx_packets: int
    rx_bytes: int
    tx_packets: int
    tx_bytes: int


@dataclass(frozen=True)
class HostSnmp6Target:
    host: str          # e.g. "yellow-host00"
    tenant: str        # "green" | "yellow"


@dataclass(frozen=True)
class HostSnmp6Counters:
    ip6_in_delivers: int
    ip6_in_discards: int
    udp6_in_datagrams: int
    udp6_rcvbuf_errors: int


@dataclass(frozen=True)
class MrcSnapshotTarget:
    host: str          # the src_host
    tenant: str        # tenant the daemon is publishing for
    dst_id: int        # peer leaf id (encoded in filename as 2-digit)

    @property
    def snapshot_path(self) -> str:
        return f"/dev/shm/srv6-mrc/{self.host}/{self.tenant}_{self.dst_id:02d}.json"


@dataclass
class EvRecord:
    """One row from .ev_state.tenants[tenant] — list element."""
    plane: int
    path: int
    state: str
    weight: float
    rtt_p50_ns: int | None
    consecutive_probe_successes: int
    consecutive_probe_timeouts: int


@dataclass
class MrcSnapshot:
    captured_ns: int
    src_host: str
    tenant: str
    dst_id: int
    ev_records: list[EvRecord] = field(default_factory=list)
    active_evs: int = 0


# ---------------------------------------------------------------------------
# Prometheus metric registry — names from docs/design-visibility.md §6.
#
# Skeleton only: real init runs in main() once prometheus_client is
# importable. Keep these as class-level so tests can reset by
# constructing a fresh CollectorRegistry.
# ---------------------------------------------------------------------------


class Metrics:
    """Holder for all srv6_* metrics. Constructed once in main()."""

    def __init__(self, registry: "CollectorRegistry"):
        # --- fabric NIC counters --------------------------------------
        self.iface_tx_packets = Counter(
            "srv6_iface_tx_packets_total",
            "Total TX packets per fabric NIC.",
            ["plane", "tier", "node", "iface"],
            registry=registry,
        )
        self.iface_tx_bytes = Counter(
            "srv6_iface_tx_bytes_total",
            "Total TX bytes per fabric NIC.",
            ["plane", "tier", "node", "iface"],
            registry=registry,
        )
        self.iface_rx_packets = Counter(
            "srv6_iface_rx_packets_total",
            "Total RX packets per fabric NIC.",
            ["plane", "tier", "node", "iface"],
            registry=registry,
        )
        self.iface_rx_bytes = Counter(
            "srv6_iface_rx_bytes_total",
            "Total RX bytes per fabric NIC.",
            ["plane", "tier", "node", "iface"],
            registry=registry,
        )

        # --- host kernel counters -------------------------------------
        self.host_ip6_in_delivers = Counter(
            "srv6_host_ip6_in_delivers_total",
            "/proc/net/snmp6 Ip6InDelivers.",
            ["host", "tenant"],
            registry=registry,
        )
        self.host_ip6_in_discards = Counter(
            "srv6_host_ip6_in_discards_total",
            "/proc/net/snmp6 Ip6InDiscards.",
            ["host", "tenant"],
            registry=registry,
        )
        self.host_udp6_in_datagrams = Counter(
            "srv6_host_udp6_in_datagrams_total",
            "/proc/net/snmp6 Udp6InDatagrams.",
            ["host", "tenant"],
            registry=registry,
        )
        self.host_udp6_rcvbuf_errors = Counter(
            "srv6_host_udp6_rcvbuf_errors_total",
            "/proc/net/snmp6 Udp6RcvbufErrors.",
            ["host", "tenant"],
            registry=registry,
        )

        # --- MRC snapshot ---------------------------------------------
        ev_labels = ["host", "tenant", "dst_id", "plane", "path"]
        flow_labels = ["host", "tenant", "dst_id"]

        self.mrc_ev_state = Gauge(
            "srv6_mrc_ev_state",
            "EV state enum (0=unknown 1=good 2=suspect 3=assumed_bad).",
            ev_labels, registry=registry,
        )
        self.mrc_ev_weight = Gauge(
            "srv6_mrc_ev_weight",
            "EV weight from health_aware_mrc policy.",
            ev_labels, registry=registry,
        )
        self.mrc_ev_rtt_p50_seconds = Gauge(
            "srv6_mrc_ev_rtt_p50_seconds",
            "EV probe RTT p50.",
            ev_labels, registry=registry,
        )
        self.mrc_ev_cps = Gauge(
            "srv6_mrc_ev_consecutive_probe_successes",
            "Consecutive probe successes for this EV.",
            ev_labels, registry=registry,
        )
        self.mrc_ev_cpt = Gauge(
            "srv6_mrc_ev_consecutive_probe_timeouts",
            "Consecutive probe timeouts for this EV.",
            ev_labels, registry=registry,
        )
        self.mrc_flow_active_evs = Gauge(
            "srv6_mrc_flow_active_evs",
            "Number of EVs in state in {good,suspect,unknown} for this flow.",
            flow_labels, registry=registry,
        )
        self.mrc_snapshot_captured_ns = Gauge(
            "srv6_mrc_snapshot_captured_ns",
            "Snapshot capture timestamp (ns since epoch); stale if << now.",
            flow_labels, registry=registry,
        )

        # --- scraper self-metrics -------------------------------------
        self.scrape_duration = Gauge(
            "srv6_scraper_scrape_duration_seconds",
            "Wall-clock duration of one scrape pass for this source.",
            ["source"], registry=registry,
        )
        self.exec_failures = Counter(
            "srv6_scraper_exec_failures_total",
            "Count of docker_exec_with_retry exhaustions.",
            ["source", "target"], registry=registry,
        )
        self.targets_scraped = Gauge(
            "srv6_scraper_targets_scraped",
            "Number of targets present at last scrape tick.",
            ["source"], registry=registry,
        )


# ---------------------------------------------------------------------------
# Retry helper — mandatory per AGENTS.md gotcha
# (`docker exec cat` rc=1 transient race during exec-session teardown).
# ---------------------------------------------------------------------------


class DockerExecError(RuntimeError):
    pass


def docker_exec_with_retry(
    container: str,
    argv: list[str],
    *,
    attempts: int = RETRY_ATTEMPTS,
    backoff_ms: int = RETRY_BACKOFF_MS,
    timeout_s: float = EXEC_TIMEOUT_SECONDS,
) -> str:
    """Run `docker exec <container> <argv>`. Retry on rc=1 only
    (the AGENTS.md gotcha; sustained rc=1 means the file truly
    isn't there). Other non-zero rc fails immediately. Returns
    stdout decoded as utf-8.

    SKELETON: returns "" without exec'ing. Real implementation
    must subprocess.run and inspect rc/stderr.
    """
    cmd = ["docker", "exec", container, *argv]
    last_stderr = ""
    for attempt in range(attempts):
        # TODO(implementor): actually run the command. Shape:
        #   proc = subprocess.run(cmd, capture_output=True,
        #                         timeout=timeout_s, check=False)
        #   if proc.returncode == 0:
        #       return proc.stdout.decode("utf-8", "replace")
        #   if proc.returncode != 1:
        #       raise DockerExecError(...)
        #   last_stderr = proc.stderr.decode(...)
        #   time.sleep(backoff_ms / 1000.0)
        del cmd, attempt  # silence linter on skeleton
        break
    raise DockerExecError(
        f"docker exec {container} {' '.join(argv)} failed after "
        f"{attempts} attempts: {last_stderr}"
    )


# ---------------------------------------------------------------------------
# Per-source: discovery + parse + populate metrics.
#
# Each `scrape_*` function takes the metrics registry and the list of
# targets it should hit, runs the docker exec, parses the output, and
# updates the gauges/counters. NO global state; rebuildable per tick.
# ---------------------------------------------------------------------------


def discover_targets(topo_yaml_path: str) -> tuple[
    list[IfaceTarget], list[HostSnmp6Target], list[MrcSnapshotTarget]
]:
    """Read topo.yaml and produce the per-source target lists.

    SKELETON: returns empty lists. Real implementation must:
      - parse topo.yaml (already done elsewhere; see
        `srv6_mrc/topo.py` for shape).
      - emit IfaceTarget for each (leaf, spine-facing NIC) and
        (host, ethN) pair. For leaves the plane is read from the
        node-name prefix `pN-leafNN`; for hosts the plane is the
        NIC ordinal (eth1=>0, eth2=>1, ...).
      - emit HostSnmp6Target per host (one per (host, tenant)).
      - emit MrcSnapshotTarget by discovering filenames under
        /dev/shm/srv6-mrc/<host>/ — this is the only source that
        needs a discovery exec per tick (cheap: one `ls` per host).
    """
    return [], [], []


def parse_ip_link_stats(raw: str) -> IfaceCounters | None:
    """Parse the RX:/TX: lines of `ip -stats link show <iface>`.

    Output sample on alpine iproute2:
        2: eth1: <BROADCAST,...> mtu 9000 ...
            link/ether ... brd ...
            RX:  bytes packets errors dropped overrun mcast
                 12345  678     0      0       0      0
            TX:  bytes packets errors dropped carrier collsns
                 23456  789     0      0       0       0

    SKELETON: returns None. Real impl must tokenize the two
    header+value pairs and emit IfaceCounters. Resilient to
    whitespace variation.
    """
    del raw
    return None


def parse_snmp6(raw: str) -> HostSnmp6Counters | None:
    """Parse the relevant lines from /proc/net/snmp6.

    Each line is `<MetricName> <integer>`. Tolerant of unknown
    lines. SKELETON: returns None.
    """
    del raw
    return None


def parse_mrc_snapshot(raw: str) -> MrcSnapshot | None:
    """Parse the daemon's snapshot envelope.

    Shape (verified 2026-05-23 lab snapshot — see AGENTS.md gotcha):

        {
          "captured_ns": ...,
          "src_host":   "yellow-host00",
          "src_id":     0,
          "dst_id":     7,
          "tenant":     "yellow",
          "transport_stats": {...},
          "ev_state": {
              "num_planes": 4,
              "num_paths":  8,
              "config":     {...},
              "tenants": {
                  "yellow": [
                      {"plane": 0, "path": 0, "state": "good",
                       "weight": 0.0625,
                       "rtt_p50_ns": 113000000,
                       "consecutive_probe_successes": 5,
                       "consecutive_probe_timeouts": 0,
                       ...},
                      ...
                  ]
              }
          }
        }

    Note `.ev_state.tenants["<tenant>"]` is a LIST, not a dict
    keyed by "P<p>:S<s>". A previous AGENTS.md generation said
    otherwise; that doc is stale.

    SKELETON: returns None. Real impl:
        env = json.loads(raw)
        ev_state = env["ev_state"]
        records = ev_state["tenants"][env["tenant"]]
        active = sum(1 for r in records
                     if r["state"] in ("good", "suspect", "unknown"))
        ...
    """
    del raw
    return None


def scrape_iface(metrics: Metrics, targets: Iterable[IfaceTarget]) -> None:
    """Run `ip -stats link show <iface>` per target, parse, populate.

    SKELETON: walks the target list but does not exec.
    """
    t0 = time.monotonic()
    count = 0
    for tgt in targets:
        try:
            raw = docker_exec_with_retry(
                tgt.node, ["ip", "-stats", "link", "show", tgt.iface]
            )
        except DockerExecError:
            metrics.exec_failures.labels(
                source="iface", target=f"{tgt.node}/{tgt.iface}"
            ).inc()
            continue
        ctrs = parse_ip_link_stats(raw)
        if ctrs is None:
            continue
        # prometheus_client Counter._value._value is private; the
        # correct path is to call .inc(delta) with a precomputed
        # delta from the previous sample, OR to use a Gauge and
        # have Prometheus do the rate() math. The design doc picks
        # Counter because the metric IS a counter and `rate()` is
        # cheap.  Implementation: keep a {target -> last_counters}
        # cache on Metrics and inc by (current - last).
        labels = dict(plane=str(tgt.plane), tier=tgt.tier,
                       node=tgt.node, iface=tgt.iface)
        # TODO(implementor): delta-then-inc; see comment above.
        del labels, ctrs
        count += 1
    metrics.scrape_duration.labels(source="iface").set(
        time.monotonic() - t0
    )
    metrics.targets_scraped.labels(source="iface").set(count)


def scrape_snmp6(metrics: Metrics, targets: Iterable[HostSnmp6Target]) -> None:
    """Run `cat /proc/net/snmp6` per host."""
    t0 = time.monotonic()
    count = 0
    for tgt in targets:
        try:
            raw = docker_exec_with_retry(tgt.host, ["cat", "/proc/net/snmp6"])
        except DockerExecError:
            metrics.exec_failures.labels(
                source="snmp6", target=tgt.host
            ).inc()
            continue
        ctrs = parse_snmp6(raw)
        if ctrs is None:
            continue
        # TODO(implementor): delta-then-inc per counter.
        del ctrs
        count += 1
    metrics.scrape_duration.labels(source="snmp6").set(
        time.monotonic() - t0
    )
    metrics.targets_scraped.labels(source="snmp6").set(count)


def scrape_mrc(metrics: Metrics, targets: Iterable[MrcSnapshotTarget]) -> None:
    """Read each MRC snapshot JSON; parse; populate per-EV gauges.

    Per AGENTS.md: snapshot files are several KiB; cat-via-exec is
    subject to BOTH the rc=1 race AND occasional trailing-byte
    truncation on large payloads. The retry helper handles rc=1.
    For truncation we rely on json.loads raising ValueError; on
    raise we drop this sample (do NOT poison the gauges with stale
    values) and Prometheus will see the metric not refreshed —
    `srv6_mrc_snapshot_captured_ns` going stale is the user-visible
    signal.
    """
    t0 = time.monotonic()
    count = 0
    for tgt in targets:
        try:
            raw = docker_exec_with_retry(tgt.host, ["cat", tgt.snapshot_path])
        except DockerExecError:
            metrics.exec_failures.labels(
                source="mrc",
                target=f"{tgt.host}/{tgt.tenant}/{tgt.dst_id}",
            ).inc()
            continue
        try:
            snap = parse_mrc_snapshot(raw)
        except (ValueError, KeyError, TypeError):
            # truncation or unexpected shape
            continue
        if snap is None:
            continue
        # TODO(implementor): set per-EV gauges + flow_active_evs +
        # snapshot_captured_ns.  See parse_mrc_snapshot() docstring
        # for shape.
        del snap
        count += 1
    metrics.scrape_duration.labels(source="mrc").set(
        time.monotonic() - t0
    )
    metrics.targets_scraped.labels(source="mrc").set(count)


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topo", required=True,
                        help="Path to topo.yaml (active topology).")
    parser.add_argument("--port", type=int, default=HTTP_PORT)
    parser.add_argument("--fast-interval", type=float,
                        default=SCRAPE_INTERVAL_SECONDS_FAST,
                        help="Iface + snmp6 cadence (seconds).")
    parser.add_argument("--slow-interval", type=float,
                        default=SCRAPE_INTERVAL_SECONDS_SLOW,
                        help="MRC snapshot cadence (seconds).")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if CollectorRegistry is None or yaml is None:
        log.error("prometheus_client and pyyaml must be installed "
                  "to run the scraper. This is the skeleton; "
                  "see contrib/visibility-poc/README.md.")
        return 2

    registry = CollectorRegistry()
    metrics = Metrics(registry)

    iface_targets, snmp6_targets, mrc_targets = discover_targets(args.topo)
    log.info("Discovered %d iface, %d host, %d mrc targets",
             len(iface_targets), len(snmp6_targets), len(mrc_targets))

    start_http_server(args.port, registry=registry)
    log.info("Listening on :%d/metrics", args.port)

    # Two-loop scheduler: fast (iface + snmp6) and slow (mrc).
    # TODO(implementor): use a ThreadPoolExecutor across the
    # targets per tick to parallelize docker exec calls.  See
    # design doc §4 (concurrency).
    last_slow = 0.0
    while True:
        t = time.monotonic()
        scrape_iface(metrics, iface_targets)
        scrape_snmp6(metrics, snmp6_targets)
        if t - last_slow >= args.slow_interval:
            scrape_mrc(metrics, mrc_targets)
            last_slow = t
        elapsed = time.monotonic() - t
        sleep_for = max(0.0, args.fast_interval - elapsed)
        time.sleep(sleep_for)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
