"""Transport abstraction for the MRC probe / loss-report I/O layer.

Why this exists
---------------
Pre-Phase-1b/step-2-commit-5, the MRC agent talked to the wire through
plain UDP sockets and relied on the kernel SRv6 route tables to deliver
PROBEs (and PROBE_REPLIES, and LOSS_REPORTs) to the inner tenant
anycast on the peer host. That worked accidentally at
paths_per_plane=1 because kernel ECMP across a single bound NIC is a
no-op; it broke the moment we wanted per-EV path steering, because no
kernel route exists that maps "send to cccc:<NN>::2 via plane P spine
S".  See AGENTS.md invariant 8 ("plane selection MUST be NIC-bound,
not route-metric-bound") and the design note at the head of encap.py.

The fix is to follow the data path exactly: build the outer SRv6
header in user space (via `srv6_mrc.encap.build_outer_packet`) and
write the resulting bytes to a raw IPv6 socket bound to the plane's
NIC via SO_BINDTODEVICE. This is what `runner.py` already does for
spray data packets; the MRC probe path now shares the same encap
builder so probes traverse the fabric through the same EV the data
they're measuring traverses.

Raw sockets require CAP_NET_RAW. Unit tests run on a developer laptop
without root, so we cannot use raw sockets in tests. Hence the split:

  Srv6RawTransport (lab)
      One AF_INET6 SOCK_RAW IPPROTO_RAW socket per plane, bound to
      PLANE_NICS[plane] via SO_BINDTODEVICE. Sends are
      `encap.build_outer_packet` outputs. Receives use a single plain
      UDP listener (the kernel decaps the outer SRv6 carrier; the
      inner UDP arrives natively).

  LoopbackUdpTransport (tests)
      Plain UDP sockets on ::1 with per-plane port offsets. Sends are
      the raw probe / report payload bytes (no encap wrapping). The
      `dst_leaf` argument is ignored because the loopback fixture
      already binds sender + receiver in the same process.

Both implementations expose the same `MrcTransport` interface, so the
SenderMrcAgent / ReceiverMrcAgent bodies don't branch on `use_loopback`.

Threading
---------
A transport is thread-safe with respect to its own send_* methods (the
underlying socket sendto/sendmsg calls are atomic per-call in Linux,
and we don't share buffers across threads). Receive helpers return one
datagram per call and are expected to be driven by the agent's RX
threads.
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
from .probe import PROBE_REPLY_PAYLOAD_LEN
from ..topo import (
    NUM_LEAVES,
    NUM_PLANES,
    NUM_SPINES,
    PLANE_NICS,
    SPRAY_PROBE_PORT,
    SPRAY_REPORT_PORT,
    host_underlay_addr,
    inner_addr,
    usid_outer_dst,
)


log = logging.getLogger(__name__)


DEFAULT_RECV_BUFSIZE = 4096
DEFAULT_SOCKET_TIMEOUT_S = 0.25

# UDP listener socket buffer for the probe / probe-reply / loss-report
# RX path. The Linux default rmem_default is ~208 KB which is far too
# small for all-to-all MRC at 56-sender scale where a single listener
# socket fans in probes/replies from every peer. Empirically the
# default causes silent UDP drops -> probe timeouts -> false EV
# demotes (the "all transitions are unknown->assumed_bad with
# probe_timeouts=3, loss_windows=0" cascade fingerprint).
#
# Override with SRV6_MRC_RCVBUF_BYTES; the kernel will silently cap to
# net.core.rmem_max so the lab also needs `sysctl -w
# net.core.rmem_max=33554432` (or higher) for the bump to take full
# effect. We log the granted size after setsockopt + getsockopt so the
# JSON report makes the kernel cap visible.
DEFAULT_RCVBUF_BYTES = 16 * 1024 * 1024


# --- public interface ------------------------------------------------------


class MrcTransport(ABC):
    """Abstract transport for MRC probe / probe-reply / loss-report I/O.

    Each `send_*` method takes a `(plane, path)` EV identifier so the
    lab impl can pick the per-plane raw socket and compute the right
    outer SRv6 uSID; the loopback impl uses `plane` to pick the right
    per-plane UDP socket and ignores `path` (loopback doesn't model
    spine selection).
    """

    @abstractmethod
    def send_probe(
        self, *, plane: int, path: int, dst_leaf: int, payload: bytes,
    ) -> None:
        """Send a PROBE on EV `(plane, path)` to host_id=dst_leaf.

        `payload` is the encode_probe() output. The transport adds the
        SRv6 outer header (lab) or addresses the loopback UDP socket
        (tests).
        """

    @abstractmethod
    def send_probe_reply(
        self, *, plane: int, path: int, dst_leaf: int, payload: bytes,
    ) -> None:
        """Send a PROBE_REPLY on EV `(plane, path)` to host_id=dst_leaf."""

    @abstractmethod
    def send_loss_report(
        self, *, plane: int, path: int, dst_leaf: int, payload: bytes,
    ) -> None:
        """Send a LOSS_REPORT on EV `(plane, path)` to host_id=dst_leaf."""

    @abstractmethod
    def recv_reply_socket(self) -> socket.socket:
        """Return the socket the sender uses to receive PROBE_REPLY +
        LOSS_REPORT (both arrive on the same well-known port,
        dispatched by magic byte in the agent)."""

    @abstractmethod
    def recv_probe_socket(self) -> socket.socket:
        """Return the socket the receiver uses to receive PROBEs."""

    def close(self) -> None:
        """Close all sockets the transport owns. Idempotent."""
        # Default no-op; concrete impls override.


# --- lab impl --------------------------------------------------------------


class Srv6RawTransport(MrcTransport):
    """Production transport: raw IPv6 sockets + scapy-built SRv6 outer.

    Per AGENTS.md invariant 8 plane selection is by SO_BINDTODEVICE on
    the per-plane raw socket. Each send_* method builds an outer/inner
    encap'd packet for the EV `(plane, path)` using the shared
    `encap.build_outer_packet`, then writes to `self._raw_sockets[plane]`.

    Both sender and receiver use this transport; the agent's role
    (`is_sender`) decides which RX socket is returned by
    `recv_reply_socket()` / `recv_probe_socket()`.
    """

    def __init__(
        self, *,
        tenant: str,
        my_id: int,
        is_sender: bool,
    ) -> None:
        self.tenant = tenant
        self.my_id = my_id
        self._is_sender = is_sender

        # One raw send socket per plane. Both sender and receiver need
        # all NUM_PLANES sockets because either side may need to send
        # an SRv6-encapped packet on any plane (sender: probes;
        # receiver: probe replies and loss reports).
        self._raw_sockets: Dict[int, socket.socket] = {
            p: open_raw_send_socket(PLANE_NICS[p]) for p in range(NUM_PLANES)
        }

        # Cache the local inner anycast — used as inner src for every
        # outgoing encap'd packet, independent of which EV we pick.
        self._src_inner = inner_addr(tenant, my_id)
        # Per-plane underlay used as the outer-IPv6 src for SO_BINDTODEVICE
        # plane attribution. Computed lazily so unused planes don't pay
        # the topo lookup cost (negligible, but tidier).
        self._src_underlay_by_plane: Tuple[str, ...] = tuple(
            host_underlay_addr(tenant, p, my_id) for p in range(NUM_PLANES)
        )

        # Sender listens on SPRAY_REPORT_PORT for both PROBE_REPLIES and
        # LOSS_REPORTs (dispatched by magic byte in the agent's RX
        # loop). Receiver listens on SPRAY_PROBE_PORT for PROBEs.
        # Bind to `::` (any addr) so kernel-decap'd inner packets reach
        # us regardless of which inner anycast they were destined to;
        # the kernel hands us the inner UDP after decap.
        if is_sender:
            self._reply_sock: Optional[socket.socket] = _open_udp_listener(
                bind_addr="::", bind_port=SPRAY_REPORT_PORT,
            )
            self._probe_sock: Optional[socket.socket] = None
        else:
            self._reply_sock = None
            self._probe_sock = _open_udp_listener(
                bind_addr="::", bind_port=SPRAY_PROBE_PORT,
            )

        # Reply-template cache: keyed by (plane, path, dst_leaf), value is
        # a pre-built bytearray with outer+inner+UDP headers populated and
        # zero-filled payload + zero UDP checksum. The per-reply hot path
        # clones this, splices the 28B payload at [PAYLOAD_OFFSET:], and
        # runs `udp6_checksum_inplace` — eliminating ~2-3ms of scapy build
        # per reply. Eagerly pre-warmed for receivers (is_sender=False)
        # because that's where the bottleneck lives (the agent's
        # probe-RX loop must reply at probe-emit rate); senders skip the
        # warm-up since they only build replies in the rare loss-report
        # echo path. Cache miss falls back to scapy via `send_probe_reply`.
        self._reply_templates: Dict[
            Tuple[int, int, int], Tuple[bytes, str]
        ] = {}
        # Probe-template cache: symmetric to _reply_templates but for the
        # sender's emit hot path. PROBE and PROBE_REPLY share the same
        # 28B payload struct, so templates differ only in UDP ports
        # (probe: SPRAY_PROBE_PORT both sides; reply: PROBE_PORT ->
        # REPORT_PORT). Pre-warmed for senders (is_sender=True) to fix
        # the all-to-all probe-emit GIL contention diagnosed in the
        # "Universal probe failure" investigation: at 7 dst x 16 EVs x
        # 5 rounds/sec = 560 scapy builds/sec/host, the emit threads
        # were starving the reply-RX loop. The fast path drops emit
        # cost from ~2-3ms to ~10-20us per probe.
        self._probe_templates: Dict[
            Tuple[int, int, int], Tuple[bytes, str]
        ] = {}
        self._fast_path_misses: int = 0
        self._probe_fast_path_misses: int = 0
        if not is_sender:
            self._prewarm_reply_templates()
        else:
            self._prewarm_probe_templates()

    # --- send_* ---

    def send_probe(self, *, plane, path, dst_leaf, payload):
        # Hot path: byte-template cache + manual UDP6 checksum. Mirrors
        # send_probe_reply's fast path. Falls back to scapy on cache
        # miss (one-shot warn, then silent).
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
            self._send_encapped(
                plane=plane, path=path, dst_leaf=dst_leaf, payload=payload,
                sport=SPRAY_PROBE_PORT, dport=SPRAY_PROBE_PORT,
            )
            return

        template_bytes, outer_dst = cached
        pkt = bytearray(template_bytes)
        payload_len = len(payload)
        pkt[PAYLOAD_OFFSET:PAYLOAD_OFFSET + payload_len] = payload
        udp6_checksum_inplace(pkt, payload_len=payload_len)
        self._raw_sockets[plane].sendto(bytes(pkt), (outer_dst, 0, 0, 0))

    def send_probe_reply(self, *, plane, path, dst_leaf, payload):
        # Replies go to the sender's report-listener port. Hot path: use
        # the pre-built byte template for (plane, path, dst_leaf), splice
        # the payload, fix the UDP6 checksum, then raw sendto. Falls back
        # to the scapy slow path on cache miss (logged once); receivers
        # pre-warm the full grid in __init__ so misses should be zero in
        # practice, but a malformed dst_leaf or fresh-peer race must not
        # crash the loop.
        key = (plane, path, dst_leaf)
        cached = self._reply_templates.get(key)
        if cached is None:
            self._fast_path_misses += 1
            if self._fast_path_misses == 1:
                log.warning(
                    "Srv6RawTransport: reply-template cache miss "
                    "(plane=%d path=%d dst_leaf=%d); falling back to "
                    "scapy slow path. Further misses will be silent.",
                    plane, path, dst_leaf,
                )
            self._send_encapped(
                plane=plane, path=path, dst_leaf=dst_leaf, payload=payload,
                sport=SPRAY_PROBE_PORT, dport=SPRAY_REPORT_PORT,
            )
            return

        template_bytes, outer_dst = cached
        # Slice the template + splice payload in one allocation. The
        # template is held immutable (bytes) so concurrent callers on
        # the same key cannot corrupt each other.
        pkt = bytearray(template_bytes)
        payload_len = len(payload)
        pkt[PAYLOAD_OFFSET:PAYLOAD_OFFSET + payload_len] = payload
        udp6_checksum_inplace(pkt, payload_len=payload_len)
        self._raw_sockets[plane].sendto(bytes(pkt), (outer_dst, 0, 0, 0))

    def send_loss_report(self, *, plane, path, dst_leaf, payload):
        # LOSS_REPORTs also go to the sender's report-listener port,
        # multiplexed with PROBE_REPLY on the same socket via magic byte.
        self._send_encapped(
            plane=plane, path=path, dst_leaf=dst_leaf, payload=payload,
            sport=SPRAY_REPORT_PORT, dport=SPRAY_REPORT_PORT,
        )

    def _send_encapped(
        self, *, plane: int, path: int, dst_leaf: int,
        payload: bytes, sport: int, dport: int,
    ) -> None:
        """Common SRv6-outer build + raw-socket send."""
        outer_dst = usid_outer_dst(
            self.tenant, plane=plane, spine=path, dst_leaf=dst_leaf,
        )
        pkt = build_outer_packet(
            src_underlay=self._src_underlay_by_plane[plane],
            dst_outer=outer_dst,
            src_inner=self._src_inner,
            dst_inner=inner_addr(self.tenant, dst_leaf),
            sport=sport, dport=dport,
            payload=payload,
        )
        # IPv6 raw sendto wants a (host, port, flow, scope_id) tuple;
        # port is ignored for SOCK_RAW IPPROTO_RAW (we embedded UDP
        # inside our payload already).
        self._raw_sockets[plane].sendto(pkt, (outer_dst, 0, 0, 0))

    # --- recv_* ---

    def recv_reply_socket(self) -> socket.socket:
        if self._reply_sock is None:
            raise RuntimeError(
                "Srv6RawTransport(is_sender=False) has no reply socket"
            )
        return self._reply_sock

    def recv_probe_socket(self) -> socket.socket:
        if self._probe_sock is None:
            raise RuntimeError(
                "Srv6RawTransport(is_sender=True) has no probe socket"
            )
        return self._probe_sock

    # --- fast-path template cache ---

    def _prewarm_reply_templates(self) -> None:
        """Build a reply-byte template for every (plane, path, peer_leaf).

        Run once at receiver __init__. Cost is one scapy outer-build per
        template — for 4p-4x8 that's 4*4*8=128 builds (~250ms), for
        4p-8x16 it's 4*8*16=512 (~1s), for N=32 it's 4*8*32=1024 (~2s).
        Accepted as one-time receiver startup cost; the alternative is
        a synchronous scapy build on every reply at 530+ replies/sec
        per receiver, which is the bug we're fixing.

        Skip building a template where dst_leaf == self.my_id (a host
        doesn't probe itself, so no reply will ever be needed).
        """
        own_src_underlay_by_plane = self._src_underlay_by_plane
        own_src_inner = self._src_inner
        # Reply sport/dport are fixed by the protocol contract
        # (PROBE arrived on SPRAY_PROBE_PORT; reply goes to
        # SPRAY_REPORT_PORT on the sender's report listener).
        sport = SPRAY_PROBE_PORT
        dport = SPRAY_REPORT_PORT
        built = 0
        for plane in range(NUM_PLANES):
            src_underlay = own_src_underlay_by_plane[plane]
            for path in range(NUM_SPINES):
                for dst_leaf in range(NUM_LEAVES):
                    if dst_leaf == self.my_id:
                        continue
                    outer_dst = usid_outer_dst(
                        self.tenant,
                        plane=plane, spine=path, dst_leaf=dst_leaf,
                    )
                    dst_inner = inner_addr(self.tenant, dst_leaf)
                    tpl = build_outer_template(
                        src_underlay=src_underlay,
                        dst_outer=outer_dst,
                        src_inner=own_src_inner,
                        dst_inner=dst_inner,
                        sport=sport, dport=dport,
                        payload_len=PROBE_REPLY_PAYLOAD_LEN,
                    )
                    self._reply_templates[(plane, path, dst_leaf)] = (
                        bytes(tpl), outer_dst,
                    )
                    built += 1
        log.info(
            "Srv6RawTransport: pre-warmed %d reply templates "
            "(tenant=%s my_id=%d planes=%d spines=%d leaves=%d)",
            built, self.tenant, self.my_id,
            NUM_PLANES, NUM_SPINES, NUM_LEAVES,
        )

    def _prewarm_probe_templates(self) -> None:
        """Build a probe-byte template for every (plane, path, peer_leaf).

        Symmetric to `_prewarm_reply_templates`; the only differences
        are UDP sport/dport (probe: SPRAY_PROBE_PORT both ends) and the
        payload-length constant (`PROBE_REPLY_PAYLOAD_LEN` since PROBE
        and PROBE_REPLY share the same 28B struct layout). Same skip
        rule: no template where dst_leaf == self.my_id (a host doesn't
        probe itself).

        Run once at sender __init__. Cost matches the receiver-side
        warm-up (4*S*L scapy builds, ~250ms-2s depending on topology).
        Justified by the probe-emit GIL contention that this fast path
        eliminates: see send_probe and the AGENTS.md "Universal probe
        failure" investigation for the motivation.
        """
        own_src_underlay_by_plane = self._src_underlay_by_plane
        own_src_inner = self._src_inner
        sport = SPRAY_PROBE_PORT
        dport = SPRAY_PROBE_PORT
        built = 0
        for plane in range(NUM_PLANES):
            src_underlay = own_src_underlay_by_plane[plane]
            for path in range(NUM_SPINES):
                for dst_leaf in range(NUM_LEAVES):
                    if dst_leaf == self.my_id:
                        continue
                    outer_dst = usid_outer_dst(
                        self.tenant,
                        plane=plane, spine=path, dst_leaf=dst_leaf,
                    )
                    dst_inner = inner_addr(self.tenant, dst_leaf)
                    tpl = build_outer_template(
                        src_underlay=src_underlay,
                        dst_outer=outer_dst,
                        src_inner=own_src_inner,
                        dst_inner=dst_inner,
                        sport=sport, dport=dport,
                        payload_len=PROBE_REPLY_PAYLOAD_LEN,
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

    def close(self) -> None:
        for s in list(self._raw_sockets.values()):
            try:
                s.close()
            except OSError:
                pass
        for s in (self._reply_sock, self._probe_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


# --- loopback impl (tests) -------------------------------------------------


class LoopbackUdpTransport(MrcTransport):
    """Test transport: plain UDP on ::1 with a single peer-rx port.

    No SRv6 encap is applied — the payload is sent verbatim. This is
    valid because tests don't traverse the fabric; sender and receiver
    are typically in the same process, and the agent's logic exercises
    the encode/decode + bookkeeping paths, not the kernel forwarding.

    Plane attribution comes from the payload's `plane_id` field, which
    every PROBE / PROBE_REPLY / LOSS_REPORT carries — the same source
    of truth the lab uses on the receiver side (post-Phase-1a step 3:
    a single rx socket attributes plane from the payload). The tests
    don't need per-plane port multiplexing on the wire.

    Arguments:
        is_sender: True iff this transport instance belongs to a
            SenderMrcAgent. Used by recv_*_socket() to enforce that
            the right role asks for the right rx socket.
        per_plane_send_sockets[p]: socket used to send anything on
            plane `p`. Tests can bind these to per-plane ports if they
            want to verify plane-egress symmetry (e.g. via getsockname
            inspection), or alias them all to one socket for simpler
            wiring. The transport itself does not depend on the bind.
        rx_socket: the single well-known-port listener for inbound
            replies (sender side) or inbound probes (receiver side).
        peer_rx_port: the port to send_probe / send_probe_reply /
            send_loss_report to. The test fixture is responsible for
            arranging that the peer's rx_socket is bound there.
    """

    def __init__(
        self, *,
        is_sender: bool,
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

    def send_probe_reply(self, *, plane, path, dst_leaf, payload):
        self._send_sockets[plane].sendto(payload, ("::1", self._peer_rx_port))

    def send_loss_report(self, *, plane, path, dst_leaf, payload):
        self._send_sockets[plane].sendto(payload, ("::1", self._peer_rx_port))

    def recv_reply_socket(self) -> socket.socket:
        if not self._is_sender:
            raise RuntimeError(
                "LoopbackUdpTransport(is_sender=False) has no reply socket"
            )
        return self._rx_socket

    def recv_probe_socket(self) -> socket.socket:
        if self._is_sender:
            raise RuntimeError(
                "LoopbackUdpTransport(is_sender=True) has no probe socket"
            )
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


def _open_udp_listener(*, bind_addr: str, bind_port: int) -> socket.socket:
    """Open a plain AF_INET6 UDP listener bound to (bind_addr, bind_port).

    No SO_BINDTODEVICE — the kernel delivers the inner UDP after
    decap'ing the SRv6 carrier on whichever NIC the outer arrived
    through. SO_REUSEADDR + SO_REUSEPORT make multi-process tests
    happy (e.g. when several spray.py --role recv binaries share a
    host in CI). Timeout matches the rest of the agent so RX loops
    can poll `_stop` at a fixed cadence.

    SO_RCVBUF is bumped to DEFAULT_RCVBUF_BYTES (override via
    SRV6_MRC_RCVBUF_BYTES). The kernel doubles the requested size and
    silently caps it to net.core.rmem_max; we read back the granted
    size with getsockopt and log it so lab runs surface kernel caps.
    """
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, 0)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass

    requested = _rcvbuf_bytes_from_env()
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, requested)
    except OSError as exc:  # pragma: no cover - kernel always accepts
        log.warning("mrc.transport: SO_RCVBUF=%d failed: %s", requested, exc)
    granted = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    if granted < 2 * requested:
        # Kernel doubles internally, so granted >= 2*requested means we
        # got what we asked for. Anything less means rmem_max capped us
        # and the lab needs to raise net.core.rmem_max.
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
