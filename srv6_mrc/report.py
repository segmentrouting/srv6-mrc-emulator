"""Per-scenario result merging + summary rendering.

The orchestrator (run.py) runs one or more senders and one or more receivers
per scenario, each producing a JSON record (the dicts returned by
`run_sender(...).to_dict()` and `run_receiver(...)`). This module merges
those records into a single scenario-level result and renders an ASCII
summary suitable for stdout / a logfile.

Schema reference: `mrc/lib/reorder.py` `FlowStats.to_dict()` and
`mrc/lib/runner.py` `SenderResult.to_dict()`. The receiver-side per-flow
record uses these keys: src, dst, sport, dport, received, duplicates,
loss, per_plane_recv, reorder_hist, reorder_max, reorder_mean, reorder_p99.

The matching rule between senders and receivers is by (tenant, src_host,
dst_host): each sender's `dst` (a host name) must appear as some
receiver's `host`, and that receiver's flow list must contain a flow
with matching src/dst inner addresses. We surface mismatches as warnings
in the report rather than exceptions, because partial visibility is
genuinely useful when debugging a blackholed plane.
"""

from __future__ import annotations

import json
import ipaddress
from dataclasses import asdict, dataclass, field
from typing import Any


def _canon_addr(s: str) -> str:
    """Canonicalize an IPv6 (or IPv4) literal so string compares are stable.

    Senders compute addresses via topo.inner_addr() which returns the
    zero-padded form '2001:db8:bbbb:0f::2'; receivers get them from scapy
    which returns the RFC 5952 compressed form '2001:db8:bbbb:f::2'. Both
    must hash to the same key when merging records.
    """
    try:
        return str(ipaddress.ip_address(s))
    except (ValueError, TypeError):
        return s


# --- merged per-flow row ----------------------------------------------------

@dataclass
class FlowRow:
    """Single sender↔receiver pairing — what the user actually wants to see."""
    src_host: str
    dst_host: str
    tenant: str
    policy: str
    spine: int
    rate_pps: int
    duration_s: float

    # Sender-side
    sent: int = 0
    elapsed_s: float = 0.0
    per_plane_sent: dict[int, int] = field(default_factory=dict)
    # Populated only by EV-aware policies (e.g. ev_spray). Keys are
    # "P<plane>:S<spine>" strings (the sender already serializes the
    # tuple form for JSON-safety in SenderResult.to_dict). Empty for
    # non-EV runs, where per_plane_sent alone tells the story.
    per_ev_sent: dict[str, int] = field(default_factory=dict)
    send_errors: int = 0

    # Receiver-side (None means no matching receiver record found).
    # Keys match FlowStats.to_dict() in lib/reorder.py.
    received: int | None = None
    loss: int | None = None
    duplicates: int | None = None
    reorder_max: int | None = None
    reorder_mean: float | None = None
    reorder_p99: int | None = None
    reorder_hist: dict[int, int] = field(default_factory=dict)
    # Number of packets that arrived out-of-order (any non-zero histogram
    # bin). Convenience, derived from reorder_hist.
    reordered: int | None = None
    per_plane_recv: dict[int, int] = field(default_factory=dict)
    per_nic_rx: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # MRC diagnostics — populated only when the sender was running
    # health_aware_mrc; None otherwise so the JSON shape stays compact
    # on non-MRC flows. Shape: {"ev_state": <EVStateTable.snapshot()>,
    # "loss_fusion": <LossFusionStats fields>}. See
    # srv6_mrc.cli.spray._loss_fusion_stats_to_dict for the producer.
    mrc: dict[str, Any] | None = None

    def loss_pct(self) -> float | None:
        if self.sent <= 0 or self.received is None:
            return None
        return 100.0 * max(self.sent - self.received, 0) / self.sent

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["loss_pct"] = self.loss_pct()
        return d


# --- top-level report -------------------------------------------------------


def _active_evs_from_mrc(
    mrc: dict | None,
    tenant: str,
) -> set[tuple[int, int]] | None:
    """Return (plane, path) EVs the MRC state machine considers active.

    "Active" = final weight > 0, i.e. state is GOOD or UNKNOWN. EVs in
    ASSUMED_BAD have weight 0 and are excluded, even if a handful of
    packets leaked through before the state machine demoted them.

    Returns None when there is no usable MRC snapshot for `tenant` —
    caller should fall back to `per_ev_sent`-based counting.
    """
    if not mrc:
        return None
    ev_state = mrc.get("ev_state")
    if not isinstance(ev_state, dict):
        return None
    tenants = ev_state.get("tenants")
    if not isinstance(tenants, dict):
        return None
    records = tenants.get(tenant)
    if not isinstance(records, list):
        return None
    active: set[tuple[int, int]] = set()
    for rec in records:
        try:
            if float(rec.get("weight", 0.0)) > 0.0:
                active.add((int(rec["plane"]), int(rec["path"])))
        except (TypeError, ValueError, KeyError):
            continue
    return active


def _used_evs_from_counts(per_ev_sent: dict[str, int]) -> set[tuple[int, int]]:
    """Parse "P<plane>:S<spine>" keys with count > 0 into (plane, path) tuples."""
    used: set[tuple[int, int]] = set()
    for k, n in per_ev_sent.items():
        if n <= 0:
            continue
        # Tolerate malformed keys rather than crashing the renderer.
        try:
            p_part, s_part = k.split(":", 1)
            p = int(p_part.lstrip("P"))
            s = int(s_part.lstrip("S"))
        except (ValueError, AttributeError):
            continue
        used.add((p, s))
    return used


def _missing_evs(
    used: set[tuple[int, int]],
    topology_dims: tuple[int, int],
) -> list[tuple[int, int]]:
    """Enumerate EVs in the topology grid that are NOT in `used`."""
    num_planes, paths_per_plane = topology_dims
    return [
        (p, s)
        for p in range(num_planes)
        for s in range(paths_per_plane)
        if (p, s) not in used
    ]


@dataclass
class ScenarioReport:
    scenario: str
    flows: list[FlowRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # (num_planes, paths_per_plane) for this scenario's topology. Used
    # by render_ascii to show the per-flow "evs used/expected" column
    # and to list any unused EVs. None when unknown (legacy callers /
    # tests that don't care).
    topology_dims: tuple[int, int] | None = None

    @property
    def expected_evs(self) -> int | None:
        if self.topology_dims is None:
            return None
        p, s = self.topology_dims
        return p * s

    # ----- construction ---------------------------------------------------

    @classmethod
    def from_records(cls,
                     scenario_name: str,
                     sender_records: list[dict],
                     receiver_records: list[dict],
                     topology_dims: tuple[int, int] | None = None,
                     ) -> "ScenarioReport":
        """Merge raw JSON records into a ScenarioReport.

        sender_records: list of dicts from `SenderResult.to_dict()`
        receiver_records: list of dicts from `run_receiver(...)`
        topology_dims: (num_planes, paths_per_plane) for this scenario.
            Renderer uses this to show the per-flow "used/expected" EV
            count and to list any unused EVs by index.

        Matching strategy (see module docstring): by host names from the
        sender side, and by inner IPv6 addresses on the receiver side
        (FlowStats keys flows by `(src, dst, sport, dport)` where src/dst
        are IPv6 strings).
        """
        report = cls(scenario=scenario_name, topology_dims=topology_dims)

        # Build dst_host -> receiver record map for O(1) lookup.
        recv_by_host: dict[str, dict] = {}
        for r in receiver_records:
            host = r.get("host")
            if host is None:
                report.warnings.append(
                    f"receiver record without 'host' field: {r!r}"
                )
                continue
            if host in recv_by_host:
                report.warnings.append(
                    f"duplicate receiver record for host={host}; "
                    f"keeping first, ignoring second"
                )
                continue
            recv_by_host[host] = r

        matched_receiver_flows: set[tuple[str, tuple]] = set()

        for s in sender_records:
            row = FlowRow(
                src_host=s["src"],
                dst_host=s["dst"],
                tenant=s["tenant"],
                policy=s["policy"],
                spine=s["spine"],
                rate_pps=s["rate_pps"],
                duration_s=s["duration_s"],
                sent=s["sent"],
                elapsed_s=s["elapsed_s"],
                per_plane_sent={int(k): v
                                for k, v in s["per_plane_sent"].items()},
                per_ev_sent=dict(s.get("per_ev_sent", {})),
                send_errors=s.get("errors", 0),
                mrc=s.get("mrc"),
            )

            recv = recv_by_host.get(row.dst_host)
            if recv is None:
                row.notes.append(
                    f"no receiver record for dst={row.dst_host}"
                )
                report.flows.append(row)
                continue

            # Find the flow in the receiver's flow list whose addrs match.
            # Sender doesn't expose src_addr/dst_addr in to_dict; reconstruct
            # from (tenant, host_id) via inner_addr.
            from .topo import inner_addr  # local: pure helper, no scapy
            try:
                src_id = int(row.src_host.rsplit("host", 1)[1])
                dst_id = int(row.dst_host.rsplit("host", 1)[1])
            except (ValueError, IndexError):
                row.notes.append(
                    f"cannot parse host_id from {row.src_host}/{row.dst_host}"
                )
                report.flows.append(row)
                continue

            src_addr = inner_addr(row.tenant, src_id)
            dst_addr = inner_addr(row.tenant, dst_id)
            src_canon = _canon_addr(src_addr)
            dst_canon = _canon_addr(dst_addr)

            matched = None
            for f in recv["flows"]:
                if (_canon_addr(f["src"]) == src_canon
                        and _canon_addr(f["dst"]) == dst_canon):
                    matched = f
                    matched_receiver_flows.add(
                        (row.dst_host,
                         (src_canon, dst_canon, f["sport"], f["dport"])))
                    break

            if matched is None:
                row.notes.append(
                    f"receiver {row.dst_host} saw no flow "
                    f"{src_addr} -> {dst_addr}"
                )
                report.flows.append(row)
                continue

            row.received = matched["received"]
            row.loss = matched["loss"]
            row.duplicates = matched["duplicates"]
            row.reorder_max = matched["reorder_max"]
            row.reorder_mean = matched["reorder_mean"]
            row.reorder_p99 = matched["reorder_p99"]
            row.reorder_hist = {int(k): v
                                for k, v in matched.get(
                                    "reorder_hist", {}).items()}
            # Derived: count of out-of-order arrivals = sum of bins with k>0.
            row.reordered = sum(v for k, v in row.reorder_hist.items()
                                if k > 0)
            row.per_plane_recv = {int(k): v
                                  for k, v in matched.get(
                                      "per_plane_recv", {}).items()}
            # per-NIC rx is aggregate-across-flows on the receiver side, so
            # only attach it once per (host) to the first matched flow.
            if recv.get("_per_nic_attached") is not True:
                row.per_nic_rx = {str(k): v
                                  for k, v in recv.get("per_nic", {}).items()}
                recv["_per_nic_attached"] = True

            report.flows.append(row)

        # Flag receiver flows nobody claimed.
        for host, rec in recv_by_host.items():
            for f in rec.get("flows", []):
                key = (host,
                       (_canon_addr(f["src"]), _canon_addr(f["dst"]),
                        f["sport"], f["dport"]))
                if key not in matched_receiver_flows:
                    report.warnings.append(
                        f"orphan flow at {host}: {f['src']} -> "
                        f"{f['dst']} ({f['received']} pkts)"
                    )

        return report

    # ----- serialization --------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "flows": [f.to_dict() for f in self.flows],
            "warnings": list(self.warnings),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)

    # ----- ascii rendering ------------------------------------------------

    def render_ascii(self) -> str:
        """Render a fixed-width ASCII summary, two sections:

            scenario: <name>
            =================================================================
              flow                              policy           sent     rx
              green-host00 -> green-host15      round_robin      5000   5000
              ...
            -----------------------------------------------------------------
              per-plane (sent / rx):
                plane 0:  1250 / 1248
                ...

            warnings:
              - ...
        """
        lines: list[str] = []
        lines.append(f"scenario: {self.scenario}")
        lines.append("=" * 78)

        hdr = (f"  {'flow':<30}  {'policy':<14} "
               f"{'sent':>6} {'rx':>6} {'loss%':>7} "
               f"{'reord':>6} {'max':>4} {'evs':>7}")
        lines.append(hdr)
        lines.append("  " + "-" * (len(hdr) - 2))

        for f in self.flows:
            flow_label = f"{f.src_host} -> {f.dst_host}"
            rx_str = "-" if f.received is None else str(f.received)
            reord_str = "-" if f.reordered is None else str(f.reordered)
            max_str = "-" if f.reorder_max is None else str(f.reorder_max)
            lp = f.loss_pct()
            loss_str = "-" if lp is None else f"{lp:.2f}%"
            # EV column: "used/expected" when this flow is EV-aware AND
            # we know the topology denominator; "used" when EV-aware but
            # denominator unknown; "-" for non-EV policies.
            #
            # For MRC flows we count by final EV state (weight > 0), not
            # by `per_ev_sent` keys: a freshly-demoted EV may have leaked
            # a packet or two before the state machine transitioned it
            # to ASSUMED_BAD, and we don't want those phantom keys
            # inflating the "active" count. For non-MRC flows we fall
            # back to per_ev_sent (best signal available).
            active = _active_evs_from_mrc(f.mrc, f.tenant)
            if active is None:
                active = _used_evs_from_counts(f.per_ev_sent)
            used = len(active)
            if used == 0 and not f.per_ev_sent:
                evs_str = "-"
            elif self.expected_evs is None:
                evs_str = str(used)
            else:
                evs_str = f"{used}/{self.expected_evs}"
            lines.append(
                f"  {flow_label:<30}  {f.policy:<14} "
                f"{f.sent:>6} {rx_str:>6} {loss_str:>7} "
                f"{reord_str:>6} {max_str:>4} {evs_str:>7}"
            )
            # When EV-aware and we know the denominator, list any EVs
            # the sender never selected (or that MRC demoted). Common
            # cause: link / port shutdown on the (plane, path) cell.
            if (f.per_ev_sent
                    and self.expected_evs is not None
                    and used < self.expected_evs):
                assert self.topology_dims is not None
                missing = _missing_evs(active, self.topology_dims)
                if missing:
                    lines.append(f"      ! unused EVs: {missing}")
            for note in f.notes:
                lines.append(f"      ! {note}")

        # Per-plane aggregate (sum across flows).
        plane_sent: dict[int, int] = {}
        plane_rx: dict[int, int] = {}
        for f in self.flows:
            for p, n in f.per_plane_sent.items():
                plane_sent[p] = plane_sent.get(p, 0) + n
            for p, n in f.per_plane_recv.items():
                plane_rx[p] = plane_rx.get(p, 0) + n

        if plane_sent or plane_rx:
            lines.append("")
            lines.append("  per-plane (sent / rx):")
            planes = sorted(set(plane_sent) | set(plane_rx))
            for p in planes:
                lines.append(
                    f"    plane {p}:  {plane_sent.get(p, 0):>6}"
                    f" / {plane_rx.get(p, 0):>6}"
                )

        if self.warnings:
            lines.append("")
            lines.append("  warnings:")
            for w in self.warnings:
                lines.append(f"    - {w}")

        lines.append("=" * 78)
        return "\n".join(lines)
