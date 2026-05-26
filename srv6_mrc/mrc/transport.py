"""Transport abstraction for the MRC probe / loss-report I/O layer.

Why this exists
---------------
Plane selection is by SO_BINDTODEVICE on a per-plane raw IPv6 socket
(invariant 8). Every send_* method builds the outer IPv6 + inner UDP
encapsulation in user space (via `srv6_mrc.encap.build_outer_packet`)
and writes to the plane's raw socket. Unit tests run on a developer
laptop without CAP_NET_RAW, so the abstraction has two implementations:

  Srv6RawTransport (lab)
      One AF_INET6 SOCK_RAW IPPROTO_RAW socket per plane, bound to
      PLANE_NICS[plane] via SO_BINDTODEVICE. Receives use a single
      plain UDP listener on SPRAY_REPORT_PORT (the daemon owns it;
      both returning stateless probes AND loss reports land here and
      are demuxed by payload magic byte).

  LoopbackUdpTransport (tests)
      Plain UDP sockets on ::1 with per-plane port offsets. Sends
      are the raw payload bytes (no encap). Plane attribution comes
      from the payload's plane_id field.

Stateless-probe model
---------------------
Probes round-trip back to the sender via a 6-slot uSID list ending in
End.DT6 on the sender's own leaf (see docs/stateless-probes-validation.md).
The peer host's role is pure IPv6 forwarding — there is no userland
probe-RX or probe-reply on the peer. Consequently this transport no
longer exposes `send_probe_reply` / `recv_probe_socket`; the sender's
daemon is the only side with a listener.

Threading
---------
A transport is thread-safe with respect to its own send_* methods;
recv helpers return one datagram per call and are driven by the
daemon's RX threads.
"""

from __future__ import annotations

import logging
import os
import socket
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

from ..encap import (
    PAYLOAD_OFFSET,
    build_outer_packet,
    build_outer_template,
    open_raw_send_socket,
    udp6_checksum_inplace,
)
from .probe import PROBE_PAYLOAD_LEN
from ..topo import (
    NUM_LEAVES,
    NUM_PLANES,
    NUM_SPINES,
    PLANE_NICS,
    SPRAY_PROBE_PORT,
    SPRAY_REPORT_PORT,
    inner_addr,
    probe_ev_addr,
    probe_inner_src,
    probe_outer_dst,
    usid_outer_dst,
)


log = logging.getLogger(__name__)


DEFAULT_RECV_BUFSIZE = 4096
DEFAULT_SOCKET_TIMEOUT_S = 0.25

# UDP listener socket buffer for the daemon's recv path. Override with
# SRV6_MRC_RCVBUF_BYTES; kernel silently caps to net.core.rmem_max so
# the lab also needs `sysctl -w net.core.rmem_max=33554432` for the bump
# to take full effect. The granted size is logged so the JSON report
# makes the kernel cap visible.
DEFAULT_RCVBUF_BYTES = 16 * 1024 * 1024


# --- public interface ------------------------------------------------------


class MrcTransport(ABC):
    """Abstract transport for MRC stateless-probe + loss-report I/O.

    Each `send_*` method takes a `(plane, path)` EV identifier so the
    lab impl can pick the per-plane raw socket and compute the right
    outer SRv6 uSID; the loopback impl uses `plane` to pick the right
    per-plane UDP socket and ignores `path`.
    """

    @abstractmethod
    def send_probe(
        self, *, plane: int, path: int, dst_leaf: int, payload: bytes,
    ) -> None:
        """Send a stateless PROBE on EV `(plane, path)` via host_id=dst_leaf.

        The probe outer DA is a 6-slot uSID list that round-trips the
        packet back to the sender (`probe_outer_dst`); the peer host
        identified by `dst_leaf` forwards on the outer DA in the kernel
        with no userland involvement. `payload` is the `encode_probe()`
        output; carries `dst_id` in-band so the daemon's dispatch loop
        can attribute the returning probe to the right per-flow EV
        table.
        """

    @abstractmethod
    def send_loss_report(
        self, *, plane: int, path: int, dst_leaf: int, payload: bytes,
    ) -> None:
        """Send a LOSS_REPORT on EV `(plane, path)` to host_id=dst_leaf.

        Loss reports remain peer-directed (receiver -> sender feedback)
        and use the standard data-path uSID list (`usid_outer_dst`).
        """

    @abstractmethod
    def recv_socket(self) -> socket.socket:
        """Return the daemon's single UDP listener.

        Both returning stateless probes (magic 0xA5) and loss reports
        (magic 0xA7) arrive on this socket; the daemon demuxes by
        magic byte.
        """

    def close(self) -> None:
        """Close all sockets the transport owns. Idempotent."""

    def stats(self) -> Dict[str, int]:
        """Transport-level diagnostic counters.

        Stable keys:
          - ``probe_fast_path_misses``: PROBE sends that fell through
            to the scapy slow path because the template cache had no
            entry for ``(plane, path, dst_leaf)``. Should be 0 in
            steady state on a sender.
        """
        return {}


# --- lab impl --------------------------------------------------------------


class Srv6RawTransport(MrcTransport):
    """Production transport: raw IPv6 sockets + scapy-built SRv6 outer.

    Plane selection is by SO_BINDTODEVICE on the per-plane raw socket
    (invariant 8). Each send_* method builds an outer/inner encap'd
    packet for the EV `(plane, path)` using the shared
    `encap.build_outer_packet`, then writes to `self._raw_sockets[plane]`.

    Stateless-probe model: only the sender process opens an `Srv6RawTransport`
    today; the peer host's role in the round trip is pure kernel
    forwarding. `is_sender` is retained for diagnostic / future-proofing
    but the receiver-only path (probe RX + reply TX) is gone.
    """

    def __init__(
        self, *,
        tenant: str,
        my_id: int,
        is_sender: bool = True,
    ) -> None:
        self.tenant = tenant
        self.my_id = my_id
        self._is_sender = is_sender

        # One raw send socket per plane. Used for probe emit and loss-
        # report TX.
        self._raw_sockets: Dict[int, socket.socket] = {
            p: open_raw_send_socket(PLANE_NICS[p]) for p in range(NUM_PLANES)
        }

        # Stateless-probe inner addressing:
        #   inner-src: reserved placeholder (cccc::ffff for yellow) —
        #     never configured anywhere, never used as a route key.
        #   inner-dst (probes): per-EV /128 on THIS host's own NIC —
        #     looked up via probe_ev_addr(tenant, my_id, plane, path).
        #   inner-src/dst (loss reports): standard inner anycast, like
        #     the data path; reports flow receiver->sender end-to-end.
        self._probe_inner_src = probe_inner_src(tenant)
        # Cache inner src for loss-report path (anycast inner addr).
        self._loss_inner_src = inner_addr(tenant, my_id)

        # Sender's daemon listens on SPRAY_REPORT_PORT for the demuxed
        # mix of returning stateless probes (magic 0xA5) and inbound
        # loss reports from peers (magic 0xA7). Bind to `::` so kernel-
        # decap'd inner packets reach us regardless of which per-EV /128
        # the probe was directed at.
        self._recv_sock: Optional[socket.socket] = _open_udp_listener(
            bind_addr="::", bind_port=SPRAY_REPORT_PORT,
            enable_rx_timestamp=True,
        )

        # Probe-template cache: per (plane, path, dst_leaf). Outer DA
        # depends on all three; inner DA = sender's own per-EV /128
        # depends on (plane, path) only. Pre-warmed at __init__ to
        # eliminate scapy-build GIL contention on the emit hot path
        # (the bug originally diagnosed pre-stateless-probes).
        self._probe_templates: Dict[
            Tuple[int, int, int], Tuple[bytes, str]
        ] = {}
        self._probe_fast_path_misses: int = 0
        if is_sender:
            self._prewarm_probe_templates()

    # --- send_* ---

    def send_probe(self, *, plane, path, dst_leaf, payload):
        # Hot path: byte-template cache + manual UDP6 checksum.
        # Falls back to scapy on cache miss (one-shot warn, then silent).
        key = (plane, path, dst_leaf)
        cached = self._probe_templates.get(key)
        if cached is None:
            self._probe_fast_path_misses += 1
            if self._probe_fast_path_misses == 1:
                log.warning(
                    "Srv6RawTransport: probe-template cache miss "
                    "(plane=%d path=%d dst_leaf=%d); falling back to "
                    "scapy slow path. Further misses will be silent.",
                    plane, path, dst_leaf,
                )
            self._send_probe_slow(
                plane=plane, path=path, dst_leaf=dst_leaf, payload=payload,
            )
            return

        template_bytes, outer_dst = cached
        pkt = bytearray(template_bytes)
        payload_len = len(payload)
        pkt[PAYLOAD_OFFSET:PAYLOAD_OFFSET + payload_len] = payload
        udp6_checksum_inplace(pkt, payload_len=payload_len)
        self._raw_sockets[plane].sendto(bytes(pkt), (outer_dst, 0, 0, 0))

    def _send_probe_slow(
        self, *, plane: int, path: int, dst_leaf: int, payload: bytes,
    ) -> None:
        """Scapy-build fallback for the probe-emit fast path."""
        outer_dst = probe_outer_dst(
            self.tenant, plane=plane, src_leaf=self.my_id,
            dst_leaf=dst_leaf, path=path,
        )
        dst_inner = probe_ev_addr(self.tenant, self.my_id, plane, path)
        pkt = build_outer_packet(
            src_underlay=self._probe_inner_src,  # placeholder; not routed
            dst_outer=outer_dst,
            src_inner=self._probe_inner_src,
            dst_inner=dst_inner,
            sport=SPRAY_PROBE_PORT,
            # Inner dport = REPORT_PORT so the returning probe lands on
            # the daemon's single recv socket alongside loss reports.
            dport=SPRAY_REPORT_PORT,
            payload=payload,
        )
        self._raw_sockets[plane].sendto(pkt, (outer_dst, 0, 0, 0))

    def send_loss_report(self, *, plane, path, dst_leaf, payload):
        # Loss reports flow receiver -> sender end-to-end on standard
        # data-path uSIDs (one-way; no round-trip).
        outer_dst = usid_outer_dst(
            self.tenant, plane=plane, spine=path, dst_leaf=dst_leaf,
        )
        pkt = build_outer_packet(
            src_underlay=self._loss_inner_src,
            dst_outer=outer_dst,
            src_inner=self._loss_inner_src,
            dst_inner=inner_addr(self.tenant, dst_leaf),
            sport=SPRAY_REPORT_PORT, dport=SPRAY_REPORT_PORT,
            payload=payload,
        )
        self._raw_sockets[plane].sendto(pkt, (outer_dst, 0, 0, 0))

    # --- recv ---

    def recv_socket(self) -> socket.socket:
        if self._recv_sock is None:
            raise RuntimeError("Srv6RawTransport has no recv socket")
        return self._recv_sock

    # --- fast-path template cache ---

    def _prewarm_probe_templates(self) -> None:
        """Build a probe-byte template for every (plane, path, dst_leaf).

        Outer DA differs per (plane, path, dst_leaf); inner DA is the
        sender's own per-EV /128, which differs per (plane, path) only.
        We still key on the full triple so the fast path can do a
        single dict lookup with the same arguments the agent emit loop
        already has.

        Skipped where dst_leaf == self.my_id (a host doesn't probe
        itself).

        Cost: NUM_PLANES * NUM_SPINES * (NUM_LEAVES - 1) scapy builds at
        __init__. For 4p-4x8 that's 4*4*7 = 112 (~250ms); for 4p-8x16
        it's 4*8*15 = 480 (~1s).
        """
        sport = SPRAY_PROBE_PORT
        # Inner dport = REPORT_PORT so the returning probe is demuxed
        # with loss reports on the daemon's single recv socket.
        dport = SPRAY_REPORT_PORT
        built = 0
        for plane in range(NUM_PLANES):
            for path in range(NUM_SPINES):
                # Inner dst depends only on (plane, path) — computed
                # once per (plane, path) outer-cache row.
                dst_inner = probe_ev_addr(
                    self.tenant, self.my_id, plane, path,
                )
                for dst_leaf in range(NUM_LEAVES):
                    if dst_leaf == self.my_id:
                        continue
                    outer_dst = probe_outer_dst(
                        self.tenant,
                        plane=plane, src_leaf=self.my_id,
                        dst_leaf=dst_leaf, path=path,
                    )
                    tpl = build_outer_template(
                        src_underlay=self._probe_inner_src,
                        dst_outer=outer_dst,
                        src_inner=self._probe_inner_src,
                        dst_inner=dst_inner,
                        sport=sport, dport=dport,
                        payload_len=PROBE_PAYLOAD_LEN,
                    )
                    self._probe_templates[(plane, path, dst_leaf)] = (
                        bytes(tpl), outer_dst,
                    )
                    built += 1
        log.info(
            "Srv6RawTransport: pre-warmed %d probe templates "
            "(tenant=%s my_id=%d planes=%d spines=%d leaves=%d)",
            built, self.tenant, self.my_id,
            NUM_PLANES, NUM_SPINES, NUM_LEAVES,
        )

    # --- lifecycle ---

    def stats(self) -> Dict[str, int]:
        return {
            "probe_fast_path_misses": self._probe_fast_path_misses,
        }

    def close(self) -> None:
        for s in list(self._raw_sockets.values()):
            try:
                s.close()
            except OSError:
                pass
        if self._recv_sock is not None:
            try:
                self._recv_sock.close()
            except OSError:
                pass


# --- loopback impl (tests) -------------------------------------------------


class LoopbackUdpTransport(MrcTransport):
    """Test transport: plain UDP on ::1 with a single peer-rx port.

    No SRv6 encap — payload is sent verbatim. Tests don't traverse the
    fabric; the agent's logic exercises encode/decode + bookkeeping
    paths, not kernel forwarding. Plane attribution comes from the
    payload's plane_id field.

    Stateless-probe parity: there is no separate `send_probe_reply`
    because the lab path no longer has one. A test that wants to
    simulate a returning probe should just have the receiver-side
    fixture send a probe-shaped payload back to the sender's
    `recv_socket` via its own `send_probe`.

    Args:
        per_plane_send_sockets[p]: socket used to send on plane `p`.
            Tests can bind these to per-plane ports if they want to
            verify plane-egress symmetry.
        rx_socket: the single well-known-port listener.
        peer_rx_port: the port to send_probe / send_loss_report to.
    """

    def __init__(
        self, *,
        is_sender: bool = True,
        per_plane_send_sockets: Dict[int, socket.socket],
        rx_socket: socket.socket,
        peer_rx_port: int,
    ) -> None:
        self._is_sender = is_sender
        self._send_sockets = dict(per_plane_send_sockets)
        self._rx_socket = rx_socket
        self._peer_rx_port = peer_rx_port

    def send_probe(self, *, plane, path, dst_leaf, payload):
        self._send_sockets[plane].sendto(payload, ("::1", self._peer_rx_port))

    def send_loss_report(self, *, plane, path, dst_leaf, payload):
        self._send_sockets[plane].sendto(payload, ("::1", self._peer_rx_port))

    def recv_socket(self) -> socket.socket:
        return self._rx_socket

    def close(self) -> None:
        for s in list(self._send_sockets.values()):
            try:
                s.close()
            except OSError:
                pass
        try:
            self._rx_socket.close()
        except OSError:
            pass


# --- internals -------------------------------------------------------------


def _open_udp_listener(
    *, bind_addr: str, bind_port: int, enable_rx_timestamp: bool = False,
) -> socket.socket:
    """Open a plain AF_INET6 UDP listener bound to (bind_addr, bind_port).

    See module docstring for rcvbuf / SO_TIMESTAMPNS notes (unchanged
    from the stateful-probe design).
    """
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, 0)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    if enable_rx_timestamp:
        # Alpine python lacks the symbolic name even though the kernel
        # supports the option; fall back to numeric 35 (Linux ABI). On
        # macOS dev the setsockopt fails cleanly with ENOPROTOOPT and
        # the socket still works (no ancillary timestamps; readers must
        # tolerate that).
        ts_opt = getattr(socket, "SO_TIMESTAMPNS", 35)
        try:
            s.setsockopt(socket.SOL_SOCKET, ts_opt, 1)
        except OSError as exc:  # pragma: no cover - kernel-dependent
            log.warning(
                "mrc.transport: SO_TIMESTAMPNS failed (%s); "
                "kernel_rx_dwell_buckets will read all-zero",
                exc,
            )

    requested = _rcvbuf_bytes_from_env()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, requested)
    except OSError as exc:  # pragma: no cover - kernel always accepts
        log.warning("mrc.transport: SO_RCVBUF=%d failed: %s", requested, exc)
    granted = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    if granted < 2 * requested:
        log.warning(
            "mrc.transport: SO_RCVBUF requested=%d granted=%d "
            "(kernel rmem_max likely caps; consider "
            "`sysctl -w net.core.rmem_max=%d`)",
            requested, granted, requested * 2,
        )
    else:
        log.info(
            "mrc.transport: SO_RCVBUF requested=%d granted=%d on %s:%d",
            requested, granted, bind_addr, bind_port,
        )

    s.bind((bind_addr, bind_port))
    s.settimeout(DEFAULT_SOCKET_TIMEOUT_S)
    return s


def _rcvbuf_bytes_from_env() -> int:
    """Read SRV6_MRC_RCVBUF_BYTES override; fall back to default."""
    raw = os.environ.get("SRV6_MRC_RCVBUF_BYTES")
    if not raw:
        return DEFAULT_RCVBUF_BYTES
    try:
        n = int(raw)
    except ValueError:
        log.warning(
            "mrc.transport: SRV6_MRC_RCVBUF_BYTES=%r is not an int; "
            "using default %d", raw, DEFAULT_RCVBUF_BYTES,
        )
        return DEFAULT_RCVBUF_BYTES
    if n <= 0:
        log.warning(
            "mrc.transport: SRV6_MRC_RCVBUF_BYTES=%d non-positive; "
            "using default %d", n, DEFAULT_RCVBUF_BYTES,
        )
        return DEFAULT_RCVBUF_BYTES
    return n


__all__ = [
    "MrcTransport",
    "Srv6RawTransport",
    "LoopbackUdpTransport",
    "DEFAULT_RECV_BUFSIZE",
    "DEFAULT_SOCKET_TIMEOUT_S",
    "DEFAULT_RCVBUF_BYTES",
]
