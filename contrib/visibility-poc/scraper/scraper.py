"""
srv6-scraper — Prometheus exporter for the SRv6 MRC lab.

Three data sources, all polled via `docker exec`:

  (a) `ip -stats link show <iface>` on fabric nodes  — per-NIC pps/bps
  (b) `cat /proc/net/snmp6` on hosts                  — kernel IPv6/UDP6
  (c) `/dev/shm/srv6-mrc/<host>/<tenant>_<dd>.json`   — MRC EV health

See `docs/design-visibility.md` for architecture and naming. This file
is intentionally self-contained (one import from srv6_mrc.topology to
read topo.yaml; otherwise stdlib + prometheus_client + pyyaml).

PR 1 scope: the scraper itself. Containerlab wiring, image build,
Grafana provisioning, and orchestrator-side fault annotations live in
PR 2+. See `docs/design-visibility.md` "PR 1 status" section.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:  # Hard deps, but the module must still import in test contexts.
    from prometheus_client import (
        CollectorRegistry,
        Gauge,
        start_http_server,
    )
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    CollectorRegistry = Gauge = start_http_server = None  # type: ignore
    _PROM_AVAILABLE = False


log = logging.getLogger("srv6_scraper")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRAPE_INTERVAL_SECONDS = 1.0
SLOW_SCRAPE_INTERVAL_SECONDS = 5.0   # MRC snapshots (larger payload)
EXEC_TIMEOUT_SECONDS = 5.0
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_S = 0.1
HTTP_PORT = 9100
WORKER_THREADS = 16

# EV state enum used for srv6_flow_ev_state gauge. Must stay in sync
# with srv6_mrc/mrc/ev_state.py. If the state machine grows a new
# state, add it here AND in docs/design-visibility.md §6.
EV_STATE_VALUES = {
    "unknown": 0,
    "good": 1,
    "suspect": 2,
    "assumed_bad": 3,
}

# /proc/net/snmp6 keys we promote to Prometheus gauges. Everything
# else in the file is parsed into the returned dict but not exposed —
# add a metric here AND in Metrics.__init__ to expose a new one.
SNMP6_EXPOSED_KEYS = (
    "Ip6InReceives", "Ip6InDelivers", "Ip6InDiscards",
    "Ip6OutRequests",
    "Udp6InDatagrams", "Udp6NoPorts", "Udp6InErrors",
    "Udp6OutDatagrams", "Udp6RcvbufErrors", "Udp6SndbufErrors",
    "Udp6InCsumErrors",
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IfaceTarget:
    """One fabric NIC to scrape via `ip -stats link show`."""
    node: str          # e.g. "p0-leaf03"  or  "green-host00"
    iface: str         # e.g. "Ethernet0"  or  "eth1"
    plane: int         # 0..planes-1
    tier: str          # "spine" | "leaf" | "host"


@dataclass(frozen=True)
class HostSnmp6Target:
    host: str          # e.g. "yellow-host00"
    tenant: str        # "green" | "yellow"


@dataclass(frozen=True)
class IfaceCounters:
    rx_bytes: int
    rx_packets: int
    rx_errors: int
    rx_dropped: int
    tx_bytes: int
    tx_packets: int
    tx_errors: int
    tx_dropped: int


@dataclass
class EvRecord:
    plane: int
    path: int
    state: str
    weight: float
    rtt_p50_ns: int | None
    consecutive_probe_successes: int
    consecutive_probe_timeouts: int
    demotes_suppressed_by_floor: int


@dataclass
class MrcSnapshotMetrics:
    """The slice of the snapshot envelope we actually emit as metrics."""
    captured_ns: int
    src_host: str
    tenant: str
    dst_id: int
    ev_records: list[EvRecord] = field(default_factory=list)
    active_evs: int = 0
    # daemon-wide counters (same value across every flow on a given host)
    dispatch_stats: dict[str, int] = field(default_factory=dict)
    # per-flow counters
    probe_reply_stats: dict[str, int] = field(default_factory=dict)
    reply_latency_buckets: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Metrics — all Gauges. Even monotonic counters use Gauge because the
# scraper does scrape-replace: we read the in-container counter and set
# the gauge to its current absolute value. Prometheus's `rate()` works
# on a gauge that happens to be monotonic just as well as on a Counter,
# and it sidesteps the prometheus_client Counter API (which only
# supports .inc(delta) and refuses to go backwards on container restart).
# ---------------------------------------------------------------------------


class Metrics:
    """Holder for all srv6_* metrics. Constructed once in main()."""

    def __init__(self, registry: "CollectorRegistry"):
        # --- fabric NIC counters --------------------------------------
        # All bytes/packets metrics are absolute (kernel counter values).
        # Use `rate()` in PromQL to derive pps/bps.
        iface_labels = ["plane", "tier", "node", "iface"]
        self.iface_rx_bytes = Gauge(
            "srv6_iface_rx_bytes",
            "Cumulative RX bytes from `ip -stats link show`.",
            iface_labels, registry=registry,
        )
        self.iface_rx_packets = Gauge(
            "srv6_iface_rx_packets",
            "Cumulative RX packets from `ip -stats link show`.",
            iface_labels, registry=registry,
        )
        self.iface_rx_errors = Gauge(
            "srv6_iface_rx_errors", "Cumulative RX errors.",
            iface_labels, registry=registry,
        )
        self.iface_rx_dropped = Gauge(
            "srv6_iface_rx_dropped", "Cumulative RX dropped.",
            iface_labels, registry=registry,
        )
        self.iface_tx_bytes = Gauge(
            "srv6_iface_tx_bytes",
            "Cumulative TX bytes from `ip -stats link show`.",
            iface_labels, registry=registry,
        )
        self.iface_tx_packets = Gauge(
            "srv6_iface_tx_packets",
            "Cumulative TX packets from `ip -stats link show`.",
            iface_labels, registry=registry,
        )
        self.iface_tx_errors = Gauge(
            "srv6_iface_tx_errors", "Cumulative TX errors.",
            iface_labels, registry=registry,
        )
        self.iface_tx_dropped = Gauge(
            "srv6_iface_tx_dropped", "Cumulative TX dropped.",
            iface_labels, registry=registry,
        )

        # --- host /proc/net/snmp6 -------------------------------------
        snmp6_labels = ["host", "tenant"]
        self.host_snmp6: dict[str, Gauge] = {
            key: Gauge(
                f"srv6_host_{_snmp6_metric_name(key)}",
                f"/proc/net/snmp6 {key}.",
                snmp6_labels, registry=registry,
            )
            for key in SNMP6_EXPOSED_KEYS
        }

        # --- MRC snapshot per-EV gauges -------------------------------
        ev_labels = ["host", "tenant", "dst_id", "plane", "path"]
        flow_labels = ["host", "tenant", "dst_id"]

        self.flow_ev_state = Gauge(
            "srv6_flow_ev_state",
            "EV state enum (0=unknown 1=good 2=suspect 3=assumed_bad).",
            ev_labels, registry=registry,
        )
        self.flow_ev_weight = Gauge(
            "srv6_flow_ev_weight",
            "EV weight from health_aware_mrc policy.",
            ev_labels, registry=registry,
        )
        self.flow_ev_rtt_p50_seconds = Gauge(
            "srv6_flow_ev_rtt_p50_seconds",
            "EV probe RTT p50 (seconds). Absent when no successful probe yet.",
            ev_labels, registry=registry,
        )
        self.flow_ev_cps = Gauge(
            "srv6_flow_ev_consecutive_probe_successes",
            "Consecutive probe successes for this EV.",
            ev_labels, registry=registry,
        )
        self.flow_ev_cpt = Gauge(
            "srv6_flow_ev_consecutive_probe_timeouts",
            "Consecutive probe timeouts for this EV.",
            ev_labels, registry=registry,
        )
        self.flow_ev_demotes_suppressed = Gauge(
            "srv6_flow_ev_demotes_suppressed_by_floor",
            "Cumulative demotes suppressed by min_active_evs floor.",
            ev_labels, registry=registry,
        )

        # --- per-flow aggregate gauges --------------------------------
        self.flow_active_evs = Gauge(
            "srv6_flow_active_evs",
            "EVs in state {good,suspect,unknown} for this flow.",
            flow_labels, registry=registry,
        )
        self.flow_snapshot_captured_ns = Gauge(
            "srv6_flow_snapshot_captured_ns",
            "Snapshot capture timestamp (ns since epoch); stale if << now.",
            flow_labels, registry=registry,
        )

        # --- per-flow probe_reply_stats (post 8fc8eae schema) ---------
        # received / decode_failed / no_match / matched. Track each as
        # its own gauge so PromQL `rate()` works without label gymnastics.
        self.flow_probe_reply_received = Gauge(
            "srv6_flow_probe_reply_received_total",
            "Probe replies the agent received.",
            flow_labels, registry=registry,
        )
        self.flow_probe_reply_decode_failed = Gauge(
            "srv6_flow_probe_reply_decode_failed_total",
            "Probe replies that failed to decode.",
            flow_labels, registry=registry,
        )
        self.flow_probe_reply_no_match = Gauge(
            "srv6_flow_probe_reply_no_match_total",
            "Probe replies with no matching outstanding probe.",
            flow_labels, registry=registry,
        )
        self.flow_probe_reply_matched = Gauge(
            "srv6_flow_probe_reply_matched_total",
            "Probe replies matched to outstanding probes.",
            flow_labels, registry=registry,
        )

        # --- per-flow reply_latency_buckets (commit 813abcf) ----------
        # Five buckets describing where probe replies land relative to
        # the sweep deadline. Captured as separate gauges (not a Prom
        # histogram) because the daemon already pre-bucketed them and
        # we just forward the counts.
        self.flow_reply_latency_buckets: dict[str, Gauge] = {
            bucket: Gauge(
                f"srv6_flow_reply_latency_{bucket}_total",
                f"Probe replies landing in the {bucket} latency bucket.",
                flow_labels, registry=registry,
            )
            for bucket in ("lt_50ms", "lt_200ms", "lt_1s", "lt_5s", "ge_5s")
        }

        # --- daemon-wide dispatch_stats (per host) --------------------
        # Note daemon-wide: only one snapshot file per host carries this,
        # but the daemon publishes the same numbers in every flow file.
        # We dedupe by only emitting the dispatch gauges once per host
        # per tick (whichever snapshot is read last wins; the values
        # don't differ across flows).
        dispatch_labels = ["host"]
        self.dispatch_replies_received = Gauge(
            "srv6_dispatch_replies_received_total",
            "MRC daemon: replies received on the shared dispatch socket.",
            dispatch_labels, registry=registry,
        )
        self.dispatch_replies_no_peer = Gauge(
            "srv6_dispatch_replies_no_peer_total",
            "MRC daemon: replies dropped because no peer agent matched src.",
            dispatch_labels, registry=registry,
        )
        self.dispatch_replies_unknown_magic = Gauge(
            "srv6_dispatch_replies_unknown_magic_total",
            "MRC daemon: replies dropped due to unknown magic byte.",
            dispatch_labels, registry=registry,
        )
        self.dispatch_replies_dispatched_probe = Gauge(
            "srv6_dispatch_replies_dispatched_probe_total",
            "MRC daemon: PROBE_REPLY dispatches to per-flow agent.",
            dispatch_labels, registry=registry,
        )
        self.dispatch_replies_dispatched_loss = Gauge(
            "srv6_dispatch_replies_dispatched_loss_total",
            "MRC daemon: LOSS_REPORT dispatches to per-flow agent.",
            dispatch_labels, registry=registry,
        )

        # --- scraper self-metrics -------------------------------------
        self.scrape_duration = Gauge(
            "srv6_scraper_scrape_duration_seconds",
            "Wall-clock duration of last scrape pass per source.",
            ["source"], registry=registry,
        )
        self.scrape_targets = Gauge(
            "srv6_scraper_targets",
            "Configured target count per source.",
            ["source"], registry=registry,
        )
        self.scrape_up = Gauge(
            "srv6_scrape_up",
            "1 if last scrape of this target succeeded, 0 otherwise.",
            ["source", "node"], registry=registry,
        )
        self.scrape_exec_failures = Gauge(
            "srv6_scraper_exec_failures_total",
            "Cumulative exec/parse failures per (source, node).",
            ["source", "node"], registry=registry,
        )
        # Internal: cumulative failure counter (we own the increment).
        self._exec_failures: dict[tuple[str, str], int] = {}


def _snmp6_metric_name(key: str) -> str:
    """Convert `Udp6RcvbufErrors` → `udp6_rcvbuf_errors`."""
    out: list[str] = []
    for i, ch in enumerate(key):
        if ch.isupper() and i and not key[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


# ---------------------------------------------------------------------------
# docker_exec_with_retry — the AGENTS.md gotcha pattern.
# ---------------------------------------------------------------------------


class DockerExecError(RuntimeError):
    """Raised when docker_exec_with_retry exhausts attempts."""


def docker_exec_with_retry(
    host: str,
    argv: list[str],
    *,
    attempts: int = RETRY_ATTEMPTS,
    backoff_s: float = RETRY_BACKOFF_S,
    timeout_s: float = EXEC_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    """Run `docker exec <host> <argv>`, retrying on rc=1 only.

    AGENTS.md "RESOLVED: SO_REUSEPORT cascade" gotcha:
    `docker exec cat` can transiently return rc=1 with no-such-file
    stderr in the microsecond window where a previous exec session is
    still tearing down. Retry with 100ms backoff covers it.

    rc=0    -> return CompletedProcess
    rc=1    -> sleep backoff_s and retry up to `attempts` times
    other   -> raise DockerExecError immediately (real failure)
    timeout -> raise DockerExecError immediately

    Returns the successful CompletedProcess on rc=0. Raises
    DockerExecError on exhaustion or non-retryable failure.
    """
    cmd = ["docker", "exec", host, *argv]
    last_stderr = ""
    last_rc = -1
    for attempt in range(attempts):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout_s, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DockerExecError(
                f"docker exec {host} {' '.join(argv)} timed out "
                f"after {timeout_s}s"
            ) from exc
        if proc.returncode == 0:
            return proc
        last_rc = proc.returncode
        last_stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        if proc.returncode != 1:
            # Non-retryable. Surface immediately.
            raise DockerExecError(
                f"docker exec {host} {' '.join(argv)} failed rc={last_rc}: "
                f"{last_stderr}"
            )
        if attempt < attempts - 1:
            time.sleep(backoff_s)
    raise DockerExecError(
        f"docker exec {host} {' '.join(argv)} failed after "
        f"{attempts} attempts (rc={last_rc}): {last_stderr}"
    )


# ---------------------------------------------------------------------------
# Parsers — pure functions, fully unit-testable.
# ---------------------------------------------------------------------------


def parse_ip_link_stats(output: str) -> IfaceCounters | None:
    """Parse `ip -stats link show <iface>` output.

    Alpine iproute2 emits something like:

        2: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9000 ...
            link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff
            RX:  bytes packets errors dropped overrun mcast
                 12345  678     0      0       0      0
            TX:  bytes packets errors dropped carrier collsns
                 23456  789     0      0       0       0

    Modern iproute2 (>=5.x) sometimes adds `missed` to the RX column
    set. We parse positionally: header row is the source of truth for
    column order; we only require `bytes`, `packets`, `errors`,
    `dropped`. Returns None if either RX or TX block is malformed.
    """
    if not output:
        return None
    lines = output.splitlines()
    rx_cols: dict[str, int] = {}
    tx_cols: dict[str, int] = {}
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("RX:"):
            cols = _parse_stat_block(lines, i)
            if cols is not None:
                rx_cols = cols
        elif stripped.startswith("TX:"):
            cols = _parse_stat_block(lines, i)
            if cols is not None:
                tx_cols = cols
        i += 1
    needed = ("bytes", "packets", "errors", "dropped")
    if not all(k in rx_cols for k in needed):
        return None
    if not all(k in tx_cols for k in needed):
        return None
    return IfaceCounters(
        rx_bytes=rx_cols["bytes"],
        rx_packets=rx_cols["packets"],
        rx_errors=rx_cols["errors"],
        rx_dropped=rx_cols["dropped"],
        tx_bytes=tx_cols["bytes"],
        tx_packets=tx_cols["packets"],
        tx_errors=tx_cols["errors"],
        tx_dropped=tx_cols["dropped"],
    )


def _parse_stat_block(lines: list[str], start: int) -> dict[str, int] | None:
    """Pair the column-header line at `lines[start]` with the value
    line at `lines[start+1]`. Returns mapping header->int or None."""
    if start + 1 >= len(lines):
        return None
    header = lines[start].strip()
    values = lines[start + 1].strip()
    # Strip the "RX:"/"TX:" prefix from the header.
    if header.startswith("RX:") or header.startswith("TX:"):
        header = header[3:].strip()
    headers = header.split()
    raw_vals = values.split()
    if len(headers) != len(raw_vals):
        return None
    out: dict[str, int] = {}
    for k, v in zip(headers, raw_vals):
        try:
            out[k] = int(v)
        except ValueError:
            return None
    return out


def parse_snmp6(output: str) -> dict[str, int]:
    """Parse /proc/net/snmp6.

    Each line is `<MetricName>\\s+<integer>`. Unknown / malformed lines
    are silently skipped. Returns a dict including all parseable
    entries; the scraper publishes only the subset in SNMP6_EXPOSED_KEYS.
    """
    out: dict[str, int] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        name, raw = parts
        try:
            out[name] = int(raw)
        except ValueError:
            continue
    return out


def parse_mrc_snapshot(payload: dict[str, Any]) -> MrcSnapshotMetrics | None:
    """Extract the metric-bearing slice of an MRC snapshot envelope.

    Input shape (locked in tests/test_mrc_daemon.py::test_snapshot_schema):

        {
          "captured_ns": int,
          "src_host":   "yellow-host00",
          "src_id":     int,
          "dst_id":     int,
          "tenant":     "yellow",
          "transport_stats":     {...},
          "dispatch_stats":      {"replies_received": int, ...},
          "probe_reply_stats":   {"received": int, ...},
          "reply_latency_buckets": {"lt_50ms": int, ..., "ge_5s": int},
          "ev_state": {
              "num_planes": int,
              "num_paths":  int,
              "config":     {...},
              "tenants":    {"<tenant>": [<per-EV dict>, ...]}
          }
        }

    Returns None if the envelope is missing required keys. The caller
    treats None as "drop this sample" — Prometheus will see the gauge
    not refreshed, and `srv6_flow_snapshot_captured_ns` going stale is
    the user-visible signal.
    """
    try:
        tenant = payload["tenant"]
        src_host = payload["src_host"]
        dst_id = int(payload["dst_id"])
        captured_ns = int(payload["captured_ns"])
        ev_state = payload["ev_state"]
        tenant_list = ev_state["tenants"][tenant]
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(tenant_list, list):
        return None

    ev_records: list[EvRecord] = []
    active = 0
    for rec in tenant_list:
        try:
            ev = EvRecord(
                plane=int(rec["plane"]),
                path=int(rec["path"]),
                state=str(rec["state"]),
                weight=float(rec.get("weight", 0.0)),
                rtt_p50_ns=(int(rec["rtt_p50_ns"])
                            if rec.get("rtt_p50_ns") is not None else None),
                consecutive_probe_successes=int(
                    rec.get("consecutive_probe_successes", 0)
                ),
                consecutive_probe_timeouts=int(
                    rec.get("consecutive_probe_timeouts", 0)
                ),
                demotes_suppressed_by_floor=int(
                    rec.get("demotes_suppressed_by_floor", 0)
                ),
            )
        except (KeyError, TypeError, ValueError):
            # Skip a malformed EV row rather than poisoning the snapshot.
            continue
        ev_records.append(ev)
        if ev.state in ("good", "suspect", "unknown"):
            active += 1

    def _intdict(key: str) -> dict[str, int]:
        raw = payload.get(key) or {}
        out: dict[str, int] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    out[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        return out

    return MrcSnapshotMetrics(
        captured_ns=captured_ns,
        src_host=str(src_host),
        tenant=str(tenant),
        dst_id=dst_id,
        ev_records=ev_records,
        active_evs=active,
        dispatch_stats=_intdict("dispatch_stats"),
        probe_reply_stats=_intdict("probe_reply_stats"),
        reply_latency_buckets=_intdict("reply_latency_buckets"),
    )


# ---------------------------------------------------------------------------
# Discovery — from topo.yaml via srv6_mrc.topology.Topology
# ---------------------------------------------------------------------------


def discover_targets(topo_yaml_path: str | os.PathLike) -> tuple[
    list[IfaceTarget], list[HostSnmp6Target]
]:
    """Read a topo.yaml and produce per-source target lists.

    Returns (iface_targets, host_targets). MRC snapshot targets are
    NOT pre-computed here because they are discovered dynamically per
    tick from `ls /dev/shm/srv6-mrc/<host>/`.

    Naming follows `generators/fabric.py`:
      - Spines: p<P>-spine<NN>            zero-padded
      - Leaves: p<P>-leaf<NN>             zero-padded
      - Hosts:  <tenant>-host<NN>         zero-padded

    Iface targets:
      - For every leaf, plane-NIC `Ethernet0` (the spine-facing uplink
        on plane P). This is the leaf side of "per-plane TX pps", the
        headline panel.
      - For every host, `eth1..eth<planes>` (one per plane).

    We do NOT scrape spine NICs in PR 1 — adding 4*spines*leaves
    targets (typically the largest source) would push 4p-8x16 past the
    1s cadence ceiling. Re-enable in PR 2 if needed; the metric labels
    already include tier="spine" as a valid value.
    """
    from srv6_mrc.topology import Topology  # local import: only when running
    topo = Topology.from_yaml(topo_yaml_path)

    iface_targets: list[IfaceTarget] = []
    host_targets: list[HostSnmp6Target] = []

    for plane in range(topo.planes):
        for leaf in range(topo.leaves_per_plane):
            iface_targets.append(IfaceTarget(
                node=f"p{plane}-leaf{leaf:02d}",
                iface="Ethernet0",
                plane=plane,
                tier="leaf",
            ))

    for tenant in topo.tenants:
        for host_id in range(topo.leaves_per_plane):
            host = f"{tenant}-host{host_id:02d}"
            host_targets.append(HostSnmp6Target(host=host, tenant=tenant))
            for plane in range(topo.planes):
                iface_targets.append(IfaceTarget(
                    node=host,
                    iface=f"eth{plane + 1}",
                    plane=plane,
                    tier="host",
                ))

    return iface_targets, host_targets


def list_mrc_snapshot_files(host: str) -> list[str]:
    """`ls /dev/shm/srv6-mrc/<host>/` via docker exec.

    Returns an empty list on any error (host not yet running, dir
    not yet created, race during teardown). The daemon creates the
    directory on startup; until then there are no flows to scrape.
    """
    try:
        proc = docker_exec_with_retry(
            host, ["ls", f"/dev/shm/srv6-mrc/{host}/"],
            attempts=2,  # cheaper than the file-cat path
        )
    except DockerExecError:
        return []
    files: list[str] = []
    for name in (proc.stdout or b"").decode("utf-8", "replace").split():
        name = name.strip()
        if name.endswith(".json"):
            files.append(name)
    return files


# ---------------------------------------------------------------------------
# Per-target scrape functions.
# Each returns (success: bool, exec_failed: bool).
# ---------------------------------------------------------------------------


def _bump_exec_failure(metrics: Metrics, source: str, node: str) -> None:
    key = (source, node)
    metrics._exec_failures[key] = metrics._exec_failures.get(key, 0) + 1
    metrics.scrape_exec_failures.labels(source=source, node=node).set(
        metrics._exec_failures[key]
    )


def scrape_one_iface(metrics: Metrics, tgt: IfaceTarget) -> bool:
    """Scrape a single iface target. Returns True on success."""
    try:
        proc = docker_exec_with_retry(
            tgt.node, ["ip", "-s", "link", "show", tgt.iface],
        )
    except DockerExecError as exc:
        log.warning("iface exec failed for %s/%s: %s",
                    tgt.node, tgt.iface, exc)
        _bump_exec_failure(metrics, "iface", tgt.node)
        metrics.scrape_up.labels(source="iface", node=tgt.node).set(0)
        return False
    out = (proc.stdout or b"").decode("utf-8", "replace")
    ctrs = parse_ip_link_stats(out)
    if ctrs is None:
        log.warning("iface parse failed for %s/%s", tgt.node, tgt.iface)
        _bump_exec_failure(metrics, "iface", tgt.node)
        metrics.scrape_up.labels(source="iface", node=tgt.node).set(0)
        return False
    labels = dict(plane=str(tgt.plane), tier=tgt.tier,
                  node=tgt.node, iface=tgt.iface)
    metrics.iface_rx_bytes.labels(**labels).set(ctrs.rx_bytes)
    metrics.iface_rx_packets.labels(**labels).set(ctrs.rx_packets)
    metrics.iface_rx_errors.labels(**labels).set(ctrs.rx_errors)
    metrics.iface_rx_dropped.labels(**labels).set(ctrs.rx_dropped)
    metrics.iface_tx_bytes.labels(**labels).set(ctrs.tx_bytes)
    metrics.iface_tx_packets.labels(**labels).set(ctrs.tx_packets)
    metrics.iface_tx_errors.labels(**labels).set(ctrs.tx_errors)
    metrics.iface_tx_dropped.labels(**labels).set(ctrs.tx_dropped)
    metrics.scrape_up.labels(source="iface", node=tgt.node).set(1)
    return True


def scrape_one_snmp6(metrics: Metrics, tgt: HostSnmp6Target) -> bool:
    try:
        proc = docker_exec_with_retry(tgt.host, ["cat", "/proc/net/snmp6"])
    except DockerExecError as exc:
        log.warning("snmp6 exec failed for %s: %s", tgt.host, exc)
        _bump_exec_failure(metrics, "snmp6", tgt.host)
        metrics.scrape_up.labels(source="snmp6", node=tgt.host).set(0)
        return False
    out = (proc.stdout or b"").decode("utf-8", "replace")
    counters = parse_snmp6(out)
    if not counters:
        log.warning("snmp6 parse empty for %s", tgt.host)
        _bump_exec_failure(metrics, "snmp6", tgt.host)
        metrics.scrape_up.labels(source="snmp6", node=tgt.host).set(0)
        return False
    for key in SNMP6_EXPOSED_KEYS:
        if key in counters:
            metrics.host_snmp6[key].labels(
                host=tgt.host, tenant=tgt.tenant,
            ).set(counters[key])
    metrics.scrape_up.labels(source="snmp6", node=tgt.host).set(1)
    return True


def scrape_one_mrc_snapshot(
    metrics: Metrics, host: str, tenant: str, dst_id: int,
) -> bool:
    """Read one snapshot file via docker exec cat; populate per-EV gauges."""
    snap_path = f"/dev/shm/srv6-mrc/{host}/{tenant}_{dst_id:02d}.json"
    target = f"{host}/{tenant}_{dst_id:02d}"
    try:
        proc = docker_exec_with_retry(host, ["cat", snap_path])
    except DockerExecError as exc:
        log.warning("mrc exec failed for %s: %s", target, exc)
        _bump_exec_failure(metrics, "mrc", target)
        metrics.scrape_up.labels(source="mrc", node=target).set(0)
        return False
    raw = (proc.stdout or b"").decode("utf-8", "replace")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        # AGENTS.md gotcha: docker exec stdout can lose trailing bytes
        # on large payloads. Drop the sample and let the gauge go stale.
        log.warning("mrc json parse failed for %s: %s", target, exc)
        _bump_exec_failure(metrics, "mrc", target)
        metrics.scrape_up.labels(source="mrc", node=target).set(0)
        return False
    snap = parse_mrc_snapshot(payload)
    if snap is None:
        log.warning("mrc snapshot shape invalid for %s", target)
        _bump_exec_failure(metrics, "mrc", target)
        metrics.scrape_up.labels(source="mrc", node=target).set(0)
        return False
    _emit_mrc_snapshot(metrics, snap)
    metrics.scrape_up.labels(source="mrc", node=target).set(1)
    return True


def _emit_mrc_snapshot(metrics: Metrics, snap: MrcSnapshotMetrics) -> None:
    flow_labels = dict(host=snap.src_host, tenant=snap.tenant,
                       dst_id=str(snap.dst_id))
    metrics.flow_snapshot_captured_ns.labels(**flow_labels).set(snap.captured_ns)
    metrics.flow_active_evs.labels(**flow_labels).set(snap.active_evs)

    for ev in snap.ev_records:
        ev_labels = dict(host=snap.src_host, tenant=snap.tenant,
                         dst_id=str(snap.dst_id),
                         plane=str(ev.plane), path=str(ev.path))
        metrics.flow_ev_state.labels(**ev_labels).set(
            EV_STATE_VALUES.get(ev.state, 0)
        )
        metrics.flow_ev_weight.labels(**ev_labels).set(ev.weight)
        if ev.rtt_p50_ns is not None:
            metrics.flow_ev_rtt_p50_seconds.labels(**ev_labels).set(
                ev.rtt_p50_ns / 1e9
            )
        metrics.flow_ev_cps.labels(**ev_labels).set(
            ev.consecutive_probe_successes
        )
        metrics.flow_ev_cpt.labels(**ev_labels).set(
            ev.consecutive_probe_timeouts
        )
        metrics.flow_ev_demotes_suppressed.labels(**ev_labels).set(
            ev.demotes_suppressed_by_floor
        )

    # probe_reply_stats — per flow
    prs = snap.probe_reply_stats
    metrics.flow_probe_reply_received.labels(**flow_labels).set(
        prs.get("received", 0)
    )
    metrics.flow_probe_reply_decode_failed.labels(**flow_labels).set(
        prs.get("decode_failed", 0)
    )
    metrics.flow_probe_reply_no_match.labels(**flow_labels).set(
        prs.get("no_match", 0)
    )
    metrics.flow_probe_reply_matched.labels(**flow_labels).set(
        prs.get("matched", 0)
    )

    # reply_latency_buckets — per flow
    for bucket, gauge in metrics.flow_reply_latency_buckets.items():
        gauge.labels(**flow_labels).set(
            snap.reply_latency_buckets.get(bucket, 0)
        )

    # dispatch_stats — daemon-wide (same value across every flow on
    # this host; last write wins but they all carry the same numbers).
    ds = snap.dispatch_stats
    metrics.dispatch_replies_received.labels(host=snap.src_host).set(
        ds.get("replies_received", 0)
    )
    metrics.dispatch_replies_no_peer.labels(host=snap.src_host).set(
        ds.get("replies_no_peer", 0)
    )
    metrics.dispatch_replies_unknown_magic.labels(host=snap.src_host).set(
        ds.get("replies_unknown_magic", 0)
    )
    metrics.dispatch_replies_dispatched_probe.labels(host=snap.src_host).set(
        ds.get("replies_dispatched_probe", 0)
    )
    metrics.dispatch_replies_dispatched_loss.labels(host=snap.src_host).set(
        ds.get("replies_dispatched_loss", 0)
    )


# ---------------------------------------------------------------------------
# Scrape-pass drivers (parallel via ThreadPoolExecutor).
# ---------------------------------------------------------------------------


def scrape_iface_pass(
    metrics: Metrics, targets: Iterable[IfaceTarget],
    executor: ThreadPoolExecutor,
) -> None:
    t0 = time.monotonic()
    targets = list(targets)
    metrics.scrape_targets.labels(source="iface").set(len(targets))
    futures = [executor.submit(scrape_one_iface, metrics, tgt)
               for tgt in targets]
    for f in as_completed(futures):
        try:
            f.result()
        except Exception:  # pragma: no cover
            log.exception("iface scrape future raised")
    metrics.scrape_duration.labels(source="iface").set(
        time.monotonic() - t0
    )


def scrape_snmp6_pass(
    metrics: Metrics, targets: Iterable[HostSnmp6Target],
    executor: ThreadPoolExecutor,
) -> None:
    t0 = time.monotonic()
    targets = list(targets)
    metrics.scrape_targets.labels(source="snmp6").set(len(targets))
    futures = [executor.submit(scrape_one_snmp6, metrics, tgt)
               for tgt in targets]
    for f in as_completed(futures):
        try:
            f.result()
        except Exception:  # pragma: no cover
            log.exception("snmp6 scrape future raised")
    metrics.scrape_duration.labels(source="snmp6").set(
        time.monotonic() - t0
    )


def scrape_mrc_pass(
    metrics: Metrics, hosts: Iterable[HostSnmp6Target],
    executor: ThreadPoolExecutor,
) -> None:
    """For each host, discover snapshot files then scrape each."""
    t0 = time.monotonic()
    hosts = list(hosts)
    # Step 1: list files per host (cheap exec). Done sequentially since
    # the executor is fully booked downstream and the list call is
    # ~one exec per host (16 hosts at 4p-4x8).
    flow_tasks: list[tuple[str, str, int]] = []
    for h in hosts:
        files = list_mrc_snapshot_files(h.host)
        for name in files:
            # Filenames are `<tenant>_<dd>.json`
            stem = name.removesuffix(".json")
            parts = stem.split("_")
            if len(parts) != 2:
                continue
            tenant, dst_raw = parts
            try:
                dst_id = int(dst_raw)
            except ValueError:
                continue
            flow_tasks.append((h.host, tenant, dst_id))
    metrics.scrape_targets.labels(source="mrc").set(len(flow_tasks))
    futures = [executor.submit(scrape_one_mrc_snapshot, metrics, *t)
               for t in flow_tasks]
    for f in as_completed(futures):
        try:
            f.result()
        except Exception:  # pragma: no cover
            log.exception("mrc scrape future raised")
    metrics.scrape_duration.labels(source="mrc").set(
        time.monotonic() - t0
    )


# ---------------------------------------------------------------------------
# Main loop.
# ---------------------------------------------------------------------------


def run_scrape_loop(
    metrics: Metrics,
    iface_targets: list[IfaceTarget],
    snmp6_targets: list[HostSnmp6Target],
    *,
    fast_interval_s: float = SCRAPE_INTERVAL_SECONDS,
    slow_interval_s: float = SLOW_SCRAPE_INTERVAL_SECONDS,
    workers: int = WORKER_THREADS,
    iterations: int | None = None,
) -> None:
    """Drive scrape passes forever (or `iterations` times for tests).

    fast cadence: iface + snmp6.
    slow cadence: MRC snapshots (larger payloads, JSON parse overhead).
    """
    last_slow = 0.0
    tick = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        while iterations is None or tick < iterations:
            t = time.monotonic()
            scrape_iface_pass(metrics, iface_targets, ex)
            scrape_snmp6_pass(metrics, snmp6_targets, ex)
            if t - last_slow >= slow_interval_s:
                scrape_mrc_pass(metrics, snmp6_targets, ex)
                last_slow = t
            elapsed = time.monotonic() - t
            sleep_for = max(0.0, fast_interval_s - elapsed)
            tick += 1
            if iterations is None or tick < iterations:
                time.sleep(sleep_for)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prometheus exporter for the SRv6 MRC lab.",
    )
    parser.add_argument(
        "--topo-yaml", required=True,
        help="Path to topo.yaml (e.g. topologies/4p-4x8/topo.yaml).",
    )
    parser.add_argument("--port", type=int, default=HTTP_PORT)
    parser.add_argument(
        "--interval", type=float, default=SCRAPE_INTERVAL_SECONDS,
        help="Fast (iface + snmp6) scrape cadence in seconds.",
    )
    parser.add_argument(
        "--slow-interval", type=float, default=SLOW_SCRAPE_INTERVAL_SECONDS,
        help="MRC snapshot scrape cadence in seconds.",
    )
    parser.add_argument("--workers", type=int, default=WORKER_THREADS)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if not _PROM_AVAILABLE:
        log.error(
            "prometheus_client is not installed. Install via "
            "`pip install -r contrib/visibility-poc/scraper/requirements.txt`"
        )
        return 2

    # Smoke-test docker presence before opening the http server. If
    # docker is missing we want to fail fast with a clear message
    # rather than spamming the log every tick.
    try:
        subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, check=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        log.error("docker is not available; cannot scrape (%s)", exc)
        return 3

    registry = CollectorRegistry()
    metrics = Metrics(registry)

    iface_targets, snmp6_targets = discover_targets(args.topo_yaml)
    log.info(
        "Discovered %d iface targets, %d host targets from %s",
        len(iface_targets), len(snmp6_targets), args.topo_yaml,
    )

    start_http_server(args.port, registry=registry)
    log.info("Listening on :%d/metrics", args.port)

    try:
        run_scrape_loop(
            metrics, iface_targets, snmp6_targets,
            fast_interval_s=args.interval,
            slow_interval_s=args.slow_interval,
            workers=args.workers,
        )
    except KeyboardInterrupt:
        log.info("Interrupted; exiting.")
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
