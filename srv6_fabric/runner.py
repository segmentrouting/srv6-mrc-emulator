"""Send/recv library — the engine behind `tools/spray.py` and the orchestrator.

This module replaces the ad-hoc send loop + RecvState class in
`tools/spray.py` with two library functions that take a `SprayPolicy` and
emit machine-readable per-flow records.

Layering rules:
  - Top-level imports are stdlib-only. Anything that needs scapy or
    raw sockets is imported lazily inside the function that uses it.
    This lets the orchestrator (running on the docker host, no scapy)
    import this module to access result types and `parse_payload`.
  - Wire format matches tools/spray.py exactly:
        outer IPv6 (nh=41)
          inner IPv6
            UDP(sport=dport=SPRAY_PORT)
              !QB : seq (8B) + plane (1B) + 32B pad
    Don't change this without coordinating with spray.py.

Public API:
  - run_sender(flow, policy, rate_pps, duration_s) -> SenderResult
  - run_receiver(self_host, self_id, tenant, idle_timeout_s,
                 stop_event=None) -> dict  (multi-flow report)
  - parse_payload(raw_bytes) -> (seq, plane) | None
"""

from __future__ import annotations

import logging
import re
import signal
import socket
import struct
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .policy import SprayPolicy
from .reorder import ReorderTracker
from .topo import (
    NUM_PLANES, SPRAY_PORT, PLANE_NICS,
    FlowKey, host_underlay_addr, inner_addr, spine_for, usid_outer_dst,
)

# 8 bytes seq + 1 byte plane = 9 bytes header in the UDP payload.
_PAYLOAD_HDR = "!QB"
_PAYLOAD_HDR_LEN = struct.calcsize(_PAYLOAD_HDR)   # 9
_PAD = b"X" * 32                                    # frame >= 64 bytes


# --- public dataclasses -----------------------------------------------------

@dataclass(frozen=True)
class FlowEndpoint:
    """One direction of a flow: src -> dst, with tenant+ids resolved.

    The `src_port`/`dst_port` are baked here too because hash-based
    policies bind to a 5-tuple.
    """
    tenant: str
    src_id: int
    dst_id: int
    src_port: int = SPRAY_PORT
    dst_port: int = SPRAY_PORT

    def to_flow_key(self) -> FlowKey:
        return FlowKey(
            src_addr=inner_addr(self.tenant, self.src_id),
            dst_addr=inner_addr(self.tenant, self.dst_id),
            src_port=self.src_port,
            dst_port=self.dst_port,
        )


@dataclass
class SenderResult:
    """Send-side outcome. Mirrors the JSON record in mrc/README.md, sender side."""
    flow: FlowEndpoint
    policy: str
    rate_pps: int
    duration_s: float
    sent: int = 0
    elapsed_s: float = 0.0
    per_plane_sent: dict[int, int] = field(default_factory=dict)
    # Per-EV send counts when EV-spray is active. Keyed by (plane, spine).
    # Empty when the policy doesn't expose pick_ev(); per_plane_sent is
    # the source of truth in that case. Reports include this in addition
    # to (not instead of) per_plane_sent so the rolled-up plane view
    # stays consistent across policies.
    per_ev_sent: dict[tuple[int, int], int] = field(default_factory=dict)
    errors: int = 0
    # Spine chosen for this run (informational; useful in reports). When
    # the policy is EV-aware (per_ev_sent populated) this field holds
    # the legacy single-spine value from spine_for() for backward
    # compatibility, but the meaningful per-packet picks live in
    # per_ev_sent.
    spine: int = 0

    def to_dict(self) -> dict:
        return {
            "src": host_for(self.flow.tenant, self.flow.src_id),
            "dst": host_for(self.flow.tenant, self.flow.dst_id),
            "tenant": self.flow.tenant,
            "policy": self.policy,
            "rate_pps": self.rate_pps,
            "duration_s": self.duration_s,
            "spine": self.spine,
            "sent": self.sent,
            "elapsed_s": round(self.elapsed_s, 3),
            "per_plane_sent": dict(sorted(self.per_plane_sent.items())),
            # Stringify EV tuple keys for JSON serializability; the
            # canonical form "P<plane>:S<spine>" is human-readable and
            # parses cleanly back into ints.
            "per_ev_sent": {
                f"P{p}:S{s}": n
                for (p, s), n in sorted(self.per_ev_sent.items())
            },
            "errors": self.errors,
        }


def host_for(tenant: str, host_id: int) -> str:
    return f"{tenant}-host{host_id:02d}"


# --- payload encode/decode (no scapy required) ------------------------------

def encode_payload(seq: int, plane: int) -> bytes:
    """Build the UDP payload (9-byte header + 32-byte pad). Stable wire fmt."""
    return struct.pack(_PAYLOAD_HDR, seq, plane) + _PAD


def parse_payload(raw: bytes) -> Optional[tuple[int, int]]:
    """Inverse of encode_payload. Returns (seq, plane) or None for too-short."""
    if len(raw) < _PAYLOAD_HDR_LEN:
        return None
    seq, plane = struct.unpack(_PAYLOAD_HDR, raw[:_PAYLOAD_HDR_LEN])
    return seq, plane


# --- send -------------------------------------------------------------------

def _open_send_socket(iface: str) -> socket.socket:
    """Raw IPv6 socket bound to a single NIC via SO_BINDTODEVICE.

    Per AGENTS.md invariant 8 — plane selection MUST be NIC-bound, not
    route-metric-bound. Kernel ECMP would defeat plane spray since green's
    inner dst is anycast.
    """
    s = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_RAW)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, iface.encode())
    except PermissionError as e:
        raise PermissionError(
            f"SO_BINDTODEVICE on {iface} needs CAP_NET_RAW. "
            "Run inside the alpine host containers or as root."
        ) from e
    return s


def _build_packet_bytes(src_underlay: str, dst_outer: str,
                        src_inner: str, dst_inner: str,
                        seq: int, plane: int) -> bytes:
    """Build full outer/inner/UDP bytes. Lazy-imports scapy."""
    # Local import keeps the orchestrator side (no scapy) able to import this
    # module without an error. AGENTS.md notes scapy noise on import.
    import logging as _logging
    _logging.getLogger("scapy.runtime").setLevel(_logging.ERROR)
    from scapy.all import IPv6, UDP  # type: ignore

    payload = encode_payload(seq, plane)
    inner = (IPv6(src=src_inner, dst=dst_inner)
             / UDP(sport=SPRAY_PORT, dport=SPRAY_PORT)
             / payload)
    outer = IPv6(src=src_underlay, dst=dst_outer, nh=41) / inner
    return bytes(outer)


def run_sender(flow: FlowEndpoint,
               policy: SprayPolicy,
               rate_pps: int,
               duration_s: float,
               *,
               stop_event: Optional[threading.Event] = None,
               progress_cb=None) -> SenderResult:
    """Run a single-flow sender loop with the given policy.

    Args:
        flow: src/dst tuple resolved upstream
        policy: SprayPolicy; called once per packet
        rate_pps: positive int, packets/sec
        duration_s: 0 = run until stop_event/SIGINT
        stop_event: optional Event for external cancellation
        progress_cb: optional fn(seq, plane) called per packet (debug)

    Returns: SenderResult
    """
    if flow.src_id == flow.dst_id:
        raise ValueError(f"src and dst must differ (both {flow.src_id})")

    spine = spine_for(flow.src_id, flow.dst_id)
    src_inner = inner_addr(flow.tenant, flow.src_id)
    dst_inner = inner_addr(flow.tenant, flow.dst_id)
    flow_key = flow.to_flow_key()

    # Detect EV-aware policies (those exposing pick_ev). For non-EV policies
    # we precompute the outer DA once per plane at startup using
    # spine_for(); for EV policies the spine varies per packet and the
    # outer DA is built in the hot loop. We also precompute per-plane
    # src_underlay and sendto sockaddr in both cases, since those don't
    # depend on spine.
    ev_aware = hasattr(policy, "pick_ev")

    # One raw socket per plane, all opened upfront so per-packet cost is
    # just a sendto.
    sockets: dict[int, socket.socket] = {}
    plane_src_underlay: dict[int, str] = {}
    plane_meta: dict[int, tuple[str, str, tuple]] = {}
    try:
        for p in range(NUM_PLANES):
            sockets[p] = _open_send_socket(PLANE_NICS[p])
            src_u = host_underlay_addr(flow.tenant, p, flow.src_id)
            plane_src_underlay[p] = src_u
            # Legacy fixed-spine precompute used by non-EV policies.
            outer_d_fixed = usid_outer_dst(flow.tenant, p, spine, flow.dst_id)
            plane_meta[p] = (src_u, outer_d_fixed, (outer_d_fixed, 0, 0, 0))

        result = SenderResult(
            flow=flow, policy=policy.name,
            rate_pps=rate_pps, duration_s=duration_s, spine=spine,
        )

        interval = 1.0 / rate_pps if rate_pps > 0 else 0.0
        t_start = time.monotonic()
        deadline = t_start + duration_s if duration_s > 0 else float("inf")
        next_tx = t_start

        seq = 0
        try:
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    break
                if ev_aware:
                    plane, ev_spine = policy.pick_ev(seq, flow_key)
                    if not 0 <= plane < NUM_PLANES:
                        raise RuntimeError(
                            f"policy {policy.name!r} returned out-of-range "
                            f"plane {plane}"
                        )
                    src_u = plane_src_underlay[plane]
                    outer_d = usid_outer_dst(
                        flow.tenant, plane, ev_spine, flow.dst_id
                    )
                    sa = (outer_d, 0, 0, 0)
                else:
                    plane = policy.pick(seq, flow_key)
                    if not 0 <= plane < NUM_PLANES:
                        raise RuntimeError(
                            f"policy {policy.name!r} returned out-of-range "
                            f"plane {plane}"
                        )
                    ev_spine = spine
                    src_u, outer_d, sa = plane_meta[plane]
                pkt = _build_packet_bytes(
                    src_u, outer_d, src_inner, dst_inner, seq, plane,
                )
                try:
                    sockets[plane].sendto(pkt, sa)
                    result.per_plane_sent[plane] = \
                        result.per_plane_sent.get(plane, 0) + 1
                    if ev_aware:
                        ev_key = (plane, ev_spine)
                        result.per_ev_sent[ev_key] = \
                            result.per_ev_sent.get(ev_key, 0) + 1
                    result.sent += 1
                    if progress_cb is not None:
                        progress_cb(seq, plane)
                except OSError:
                    result.errors += 1
                seq += 1

                if interval > 0:
                    next_tx += interval
                    slack = next_tx - time.monotonic()
                    if slack > 0:
                        time.sleep(slack)
                    else:
                        # Falling behind; free-run rather than spin.
                        next_tx = time.monotonic()
        except KeyboardInterrupt:
            pass

        result.elapsed_s = time.monotonic() - t_start
        return result

    finally:
        for s in sockets.values():
            try:
                s.close()
            except OSError:
                pass


# --- recv -------------------------------------------------------------------

def run_receiver(self_host: str,
                 self_id: int,
                 tenant: str,
                 *,
                 idle_timeout_s: float = 6.0,
                 stop_event: Optional[threading.Event] = None,
                 nics: tuple[str, ...] = PLANE_NICS,
                 install_signal_handlers: bool = True,
                 on_packet=None) -> dict:
    """Multi-flow receiver. Sniffs all plane NICs in parallel, demultiplexes
    by FlowKey, computes per-flow loss + reorder histograms.

    Returns the JSON-able report shape:
        {
          "host": "green-host15",
          "tenant": "green",
          "per_nic":   {"eth1": N, ...},  # aggregate across all flows
          "per_plane": {0: N, ...},
          "flows": [
              { "src": "...", "dst": "...", ... },  # per FlowStats.to_dict()
              ...
          ],
        }

    `nics` is parameterized so unit tests can pass a mock or smaller list.

    `on_packet` (optional): callable invoked once per successfully decoded
    data packet, with signature `on_packet(flow_key: FlowKey, plane: int,
    seq: int)`. Used by the MRC receiver agent to feed its loss-window
    accountant. Callback exceptions are caught + logged but never crash
    the sniffer (the receiver's job is to keep counting).
    """
    # Lazy scapy import — keeps orchestrator (no scapy) able to import this.
    logging.getLogger("scapy.runtime").setLevel(logging.ERROR)
    from scapy.all import IPv6, UDP, AsyncSniffer  # type: ignore

    tracker = ReorderTracker()
    per_nic: Counter[str] = Counter()
    per_plane: Counter[int] = Counter()
    last_rx = [0.0]                # monotonic time of most recent packet

    def handle(pkt) -> None:
        if IPv6 not in pkt:
            return
        nic = getattr(pkt, "sniffed_on", None) or "?"
        outer = pkt[IPv6]

        # Peel SRv6 encap if present (yellow case; green is already decapped).
        if outer.nh == 41:
            inner = outer.payload
            if not isinstance(inner, IPv6) or UDP not in inner:
                return
            udp = inner[UDP]
            inner_src = inner.src
            inner_dst = inner.dst
        else:
            if UDP not in pkt:
                return
            udp = pkt[UDP]
            inner_src = outer.src
            inner_dst = outer.dst

        if udp.dport != SPRAY_PORT:
            return

        parsed = parse_payload(bytes(udp.payload))
        if parsed is None:
            return
        seq, plane = parsed

        flow = FlowKey(
            src_addr=inner_src,
            dst_addr=inner_dst,
            src_port=int(udp.sport),
            dst_port=int(udp.dport),
        )
        tracker.observe(flow, seq, plane=plane)
        per_nic[nic] += 1
        per_plane[plane] += 1
        last_rx[0] = time.monotonic()
        if on_packet is not None:
            try:
                on_packet(flow, plane, seq)
            except Exception as e:  # noqa: BLE001 — sniffer must keep counting
                logging.getLogger(__name__).debug(
                    "run_receiver on_packet hook raised %s; ignoring", e,
                )

    bpf = f"ip6 proto 41 or udp port {SPRAY_PORT}"
    sniffers = []
    try:
        for nic in nics:
            sn = AsyncSniffer(iface=nic, filter=bpf, prn=handle, store=False)
            sn.start()
            sniffers.append(sn)

        stop_flag = {"flag": False}
        if install_signal_handlers:
            def _sig(*_): stop_flag["flag"] = True
            signal.signal(signal.SIGINT, _sig)
            signal.signal(signal.SIGTERM, _sig)

        while True:
            if stop_event is not None and stop_event.is_set():
                break
            if stop_flag["flag"]:
                break
            time.sleep(0.25)
            if (idle_timeout_s > 0 and last_rx[0] > 0
                    and (time.monotonic() - last_rx[0]) >= idle_timeout_s):
                break
    finally:
        for sn in sniffers:
            try:
                sn.stop()
            except Exception:
                pass

    return {
        "host": self_host,
        "self_id": self_id,
        "tenant": tenant,
        "per_nic":   {n: per_nic[n] for n in nics},
        "per_plane": {p: per_plane[p] for p in range(NUM_PLANES)},
        "flows":     [f.to_dict() for f in tracker.flows()],
    }


# --- host identity helper (for tools/spray.py shim) -------------------------

_HOSTNAME_RE = re.compile(r"(green|yellow)-host(\d{2})$")


def detect_self_id(hostname: Optional[str] = None) -> tuple[str, int]:
    """Infer (tenant, host_id) from container hostname `<tenant>-host<NN>`.

    Returns tuple; raises ValueError on malformed input.
    """
    h = hostname if hostname is not None else socket.gethostname()
    m = _HOSTNAME_RE.match(h)
    if not m:
        raise ValueError(
            f"cannot infer (tenant, host_id) from hostname {h!r}; "
            f"expected '<green|yellow>-host<NN>'"
        )
    return m.group(1), int(m.group(2))
