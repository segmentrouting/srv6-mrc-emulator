"""End-to-end loopback tests for the MRC probe/report I/O agents
(stateless-probe v4).

Under v4 the receiver no longer participates in the probe loop: the
peer host's leaf forwards the probe back as pure IPv6 forwarding,
and the sender's daemon sees a byte-identical (modulo hlim) copy on
its recv socket. There is no PROBE_REPLY, no match table, no RTT
ring, no reply-handler / reply-age instrumentation.

The receiver-feedback (LOSS_REPORT) path is unchanged from v3.

These tests exercise the real SenderMrcAgent + ReceiverMrcAgent on
::1 with very short timer intervals. The probe loop is verified by:

  - Sender side: ``record_probe_sent`` is called once per EV per
    round (visible via ``probe_emit_buckets`` totals).
  - Sender side: ``record_probe_recv`` updates the EVStateTable
    sliding window (driven directly from the daemon dispatcher
    in real life; here we either inject directly or rely on the
    full daemon pipeline tested elsewhere).
  - Receiver side: LOSS_REPORT round trip moves
    ``SenderMrcAgent.stats`` counters.

We do NOT verify probe RTT or sweep-timeouts on loopback — those
concepts no longer exist on the sender side under v4.
"""

from __future__ import annotations

import socket
import threading
import time
import unittest
from typing import Dict, Tuple

from srv6_mrc.mrc.agent import (
    AgentConfig,
    ReceiverMrcAgent,
    SenderMrcAgent,
)
from srv6_mrc.mrc.ev_state import EVStateTable
from srv6_mrc.mrc.transport import LoopbackUdpTransport
from srv6_mrc.topo import (
    NUM_PLANES,
    NUM_SPINES,
    tenant_id as topo_tenant_id,
)


# All loopback tests use these very-short cadences.
FAST_CONFIG = AgentConfig(
    probe_interval_ms=20,
    loss_window_ms=40,
    max_window_skew_ms=200,
    use_loopback=True,
)


def _wait_for(predicate, *, timeout_s: float = 2.0,
              poll_s: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


class _PortAllocator:
    def __init__(self, start: int = 30000) -> None:
        self._next = start
        self._lock = threading.Lock()

    def take(self, n: int = 1) -> int:
        with self._lock:
            v = self._next
            self._next += n
            return v


PORTS = _PortAllocator()


def _make_send_sockets() -> Dict[int, socket.socket]:
    socks: Dict[int, socket.socket] = {}
    for p in range(NUM_PLANES):
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, 0)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("::1", 0))
        s.settimeout(0.05)
        socks[p] = s
    return socks


def _make_rx_socket(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, 0)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("::1", port))
    s.settimeout(0.05)
    return s


def _build_loopback_pair(
    *, sender_report_port: int, receiver_probe_port: int,
) -> Tuple[LoopbackUdpTransport, LoopbackUdpTransport]:
    sender_send = _make_send_sockets()
    sender_rx = _make_rx_socket(sender_report_port)
    sender_xport = LoopbackUdpTransport(
        is_sender=True,
        per_plane_send_sockets=sender_send,
        rx_socket=sender_rx,
        peer_rx_port=receiver_probe_port,
    )
    receiver_send = _make_send_sockets()
    receiver_rx = _make_rx_socket(receiver_probe_port)
    receiver_xport = LoopbackUdpTransport(
        is_sender=False,
        per_plane_send_sockets=receiver_send,
        rx_socket=receiver_rx,
        peer_rx_port=sender_report_port,
    )
    return sender_xport, receiver_xport


class ProbeEmitSanityTests(unittest.TestCase):
    """Sender ``_emit_loop`` calls ``record_probe_sent`` per EV per round.

    Under stateless-probe v4 the receiver is not in the probe loop.
    We can't drive a full round trip on loopback (no leaf
    forwarder) so we just verify the sender's emit cadence by
    counting probe_emit_buckets totals.
    """

    def test_sender_emits_one_probe_per_ev_per_round(self) -> None:
        sender_report_port = PORTS.take(1)
        receiver_probe_port = PORTS.take(1)
        table = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )
        xport = LoopbackUdpTransport(
            is_sender=True,
            per_plane_send_sockets=_make_send_sockets(),
            rx_socket=_make_rx_socket(sender_report_port),
            peer_rx_port=receiver_probe_port,
        )
        sender = SenderMrcAgent(
            tenant="green",
            src_id=0,
            dst_id=15,
            table=table,
            config=FAST_CONFIG,
            transport=xport,
        )
        sender.start()
        num_evs = NUM_PLANES * NUM_SPINES
        try:
            ok = _wait_for(
                lambda: sum(sender.probe_emit_buckets.values()) >= num_evs,
                timeout_s=1.0,
            )
        finally:
            sender.stop(timeout_s=0.5)
        self.assertTrue(
            ok,
            f"sender failed to emit {num_evs} probes in 1s; "
            f"buckets={dict(sender.probe_emit_buckets)}",
        )


class LossReportEndToEndTests(unittest.TestCase):
    """Receiver records data packets, emits a LOSS_REPORT, the sender's
    fusion logic ingests it."""

    def test_loss_report_round_trip_updates_fusion_stats(self) -> None:
        sender_report_port = PORTS.take(1)
        receiver_probe_port = PORTS.take(1)
        table = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )
        sender_xport, receiver_xport = _build_loopback_pair(
            sender_report_port=sender_report_port,
            receiver_probe_port=receiver_probe_port,
        )
        sender = SenderMrcAgent(
            tenant="green",
            src_id=0,
            dst_id=15,
            table=table,
            config=FAST_CONFIG,
            transport=sender_xport,
        )
        receiver = ReceiverMrcAgent(
            tenant="green",
            my_id=15,
            config=FAST_CONFIG,
            transport=receiver_xport,
        )

        tid = topo_tenant_id("green")
        flow_key = (tid, 0, 15)

        # In v4 the daemon owns the recv socket and dispatches LOSS_REPORTs
        # into agent._handle_loss_report. We instantiate the agent
        # standalone, so spin a tiny dispatcher thread that reads the
        # sender's rx socket and forwards 0xA7 payloads.
        stop_evt = threading.Event()

        def _mini_dispatcher():
            sock = sender_xport.recv_socket()
            while not stop_evt.is_set():
                try:
                    payload, _peer = sock.recvfrom(2048)
                except (socket.timeout, OSError):
                    continue
                if payload and payload[0] == 0xA7:
                    sender._handle_loss_report(payload)  # noqa: SLF001

        disp = threading.Thread(target=_mini_dispatcher, daemon=True)
        disp.start()

        try:
            receiver.start()
            sender.start()

            # Inject data packets so the receiver's loss-window
            # table has content to report. The receiver learns the
            # sender via record_data on the first packet (the
            # _SenderAddr cache is updated there in the stateless
            # design).
            for seq in range(0, 50, 2):
                receiver.record_data(flow_key, plane=0, path=0, seq=seq)
            for seq in range(1, 50, 2):
                receiver.record_data(flow_key, plane=2, path=0, seq=seq)

            self.assertTrue(
                _wait_for(
                    lambda: (tid, 0) in receiver.known_senders(),
                    timeout_s=1.0,
                ),
                "receiver never cached sender via record_data",
            )

            def fusion_progress() -> bool:
                s = sender.stats
                return s.reports_processed > 0 or s.planes_updated > 0

            self.assertTrue(
                _wait_for(fusion_progress, timeout_s=1.5),
                f"no loss report reached sender; "
                f"stats={sender.stats}",
            )
        finally:
            stop_evt.set()
            sender.stop(timeout_s=0.5)
            receiver.stop(timeout_s=0.5)
            disp.join(timeout=0.5)


class ReceiverNoSenderKnownTests(unittest.TestCase):
    """If no data has been seen, receiver should not crash trying to
    emit a LOSS_REPORT for an unknown sender."""

    def test_loss_report_skipped_when_no_sender_cached(self) -> None:
        receiver_probe_port = PORTS.take(1)
        receiver_send = _make_send_sockets()
        receiver_rx = _make_rx_socket(receiver_probe_port)
        receiver_xport = LoopbackUdpTransport(
            is_sender=False,
            per_plane_send_sockets=receiver_send,
            rx_socket=receiver_rx,
            peer_rx_port=PORTS.take(1),
        )
        receiver = ReceiverMrcAgent(
            tenant="green",
            my_id=15,
            config=FAST_CONFIG,
            transport=receiver_xport,
        )
        receiver.start()
        try:
            # Let two loss-emit rounds elapse with no record_data calls.
            time.sleep(FAST_CONFIG.loss_window_ms * 2.5 / 1000.0)
            self.assertEqual(receiver.known_senders(), ())
        finally:
            receiver.stop(timeout_s=0.5)


class _TimestampingLoopbackTransport(LoopbackUdpTransport):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.send_times: list[Tuple[float, int, int]] = []
        self._send_times_lock = threading.Lock()

    def send_probe(self, *, plane, path, dst_leaf, payload) -> None:
        with self._send_times_lock:
            self.send_times.append((time.monotonic(), plane, path))
        super().send_probe(
            plane=plane, path=path, dst_leaf=dst_leaf, payload=payload,
        )


class ProbePacingTests(unittest.TestCase):
    """Within-round pacing + per-agent jitter (carried over from v3)."""

    def _build_sender(
        self, *, src_id: int = 0,
    ) -> Tuple[SenderMrcAgent, _TimestampingLoopbackTransport]:
        sender_report_port = PORTS.take(1)
        receiver_probe_port = PORTS.take(1)
        xport = _TimestampingLoopbackTransport(
            is_sender=True,
            per_plane_send_sockets=_make_send_sockets(),
            rx_socket=_make_rx_socket(sender_report_port),
            peer_rx_port=receiver_probe_port,
        )
        table = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )
        sender = SenderMrcAgent(
            tenant="green",
            src_id=src_id,
            dst_id=15,
            table=table,
            config=FAST_CONFIG,
            transport=xport,
        )
        return sender, xport

    def test_probes_spread_within_interval_not_bursty(self) -> None:
        sender, xport = self._build_sender()
        sender.start()
        try:
            deadline = time.monotonic() + 0.25
            num_evs = NUM_PLANES * NUM_SPINES
            while time.monotonic() < deadline:
                with xport._send_times_lock:
                    n = len(xport.send_times)
                if n >= num_evs:
                    break
                time.sleep(0.005)
        finally:
            sender.stop(timeout_s=0.5)

        with xport._send_times_lock:
            times = [t for t, _, _ in xport.send_times[:num_evs]]
        self.assertEqual(
            len(times), num_evs,
            f"expected {num_evs} probes in first round, "
            f"got {len(times)} in 250ms",
        )
        span_s = times[-1] - times[0]
        interval_s = FAST_CONFIG.probe_interval_ms / 1000.0
        min_acceptable_span_s = interval_s * 0.25
        self.assertGreater(
            span_s, min_acceptable_span_s,
            f"one-round probe span = {span_s*1000:.2f}ms, expected "
            f">{min_acceptable_span_s*1000:.2f}ms (interval="
            f"{interval_s*1000:.0f}ms, num_evs={num_evs}); "
            "looks like _emit_loop reverted to a bursty inner loop",
        )

    def test_independent_agents_have_distinct_jitter(self) -> None:
        jitters: list[float] = []
        for src_id in range(8):
            sender, _xport = self._build_sender(src_id=src_id)
            jitters.append(sender._emit_jitter_s)
            sender.stop(timeout_s=0.0)
        interval_s = FAST_CONFIG.probe_interval_ms / 1000.0
        for j in jitters:
            self.assertGreaterEqual(j, 0.0)
            self.assertLessEqual(j, interval_s)
        spread = max(jitters) - min(jitters)
        self.assertGreaterEqual(
            spread, 0.0005,
            f"8 agents' jitter spread = {spread*1e6:.0f}us, expected "
            f">=500us; jitter likely hard-coded or seeded badly",
        )


class ProbeEmitBucketsTests(unittest.TestCase):
    """Per-call timing instrumentation around transport.send_probe()."""

    def _build_sender(
        self, *, transport: LoopbackUdpTransport,
    ) -> SenderMrcAgent:
        table = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )
        return SenderMrcAgent(
            tenant="green",
            src_id=0,
            dst_id=15,
            table=table,
            config=FAST_CONFIG,
            transport=transport,
        )

    def _wait_for_total(self, sender: SenderMrcAgent, target: int,
                        timeout_s: float) -> int:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            total = sum(sender.probe_emit_buckets.values())
            if total >= target:
                return total
            time.sleep(0.005)
        return sum(sender.probe_emit_buckets.values())

    def test_loopback_send_buckets_into_sub_millisecond(self) -> None:
        sender_report_port = PORTS.take(1)
        receiver_probe_port = PORTS.take(1)
        xport = LoopbackUdpTransport(
            is_sender=True,
            per_plane_send_sockets=_make_send_sockets(),
            rx_socket=_make_rx_socket(sender_report_port),
            peer_rx_port=receiver_probe_port,
        )
        sender = self._build_sender(transport=xport)
        sender.start()
        try:
            num_evs = NUM_PLANES * NUM_SPINES
            total = self._wait_for_total(sender, num_evs, timeout_s=0.5)
            self.assertGreaterEqual(
                total, num_evs,
                f"expected at least {num_evs} probe emits, got {total}",
            )
        finally:
            sender.stop(timeout_s=0.5)

        fast = (sender.probe_emit_buckets["lt_100us"]
                + sender.probe_emit_buckets["lt_1ms"])
        slow = (sender.probe_emit_buckets["lt_10ms"]
                + sender.probe_emit_buckets["lt_100ms"]
                + sender.probe_emit_buckets["lt_1s"]
                + sender.probe_emit_buckets["ge_1s"])
        self.assertEqual(
            slow, 0,
            f"loopback send_probe should never take >=10ms; "
            f"buckets={dict(sender.probe_emit_buckets)}",
        )
        self.assertGreater(
            fast, 0,
            f"expected nonzero sub-millisecond emits on loopback; "
            f"buckets={dict(sender.probe_emit_buckets)}",
        )

    def test_slow_send_buckets_into_lt_100ms(self) -> None:
        sender_report_port = PORTS.take(1)
        receiver_probe_port = PORTS.take(1)
        xport = LoopbackUdpTransport(
            is_sender=True,
            per_plane_send_sockets=_make_send_sockets(),
            rx_socket=_make_rx_socket(sender_report_port),
            peer_rx_port=receiver_probe_port,
        )

        def slow_send(*, plane, path, dst_leaf, payload) -> None:
            time.sleep(0.05)
        xport.send_probe = slow_send  # type: ignore[assignment]

        sender = self._build_sender(transport=xport)
        sender.start()
        try:
            self._wait_for_total(sender, 3, timeout_s=1.0)
        finally:
            sender.stop(timeout_s=1.0)

        in_lt_100ms = sender.probe_emit_buckets["lt_100ms"]
        self.assertGreaterEqual(
            in_lt_100ms, 3,
            f"expected >=3 probes in lt_100ms bucket with 50ms send; "
            f"buckets={dict(sender.probe_emit_buckets)}",
        )
        for fast_key in ("lt_100us", "lt_1ms", "lt_10ms"):
            self.assertEqual(
                sender.probe_emit_buckets[fast_key], 0,
                f"50ms send leaked into fast bucket {fast_key}; "
                f"buckets={dict(sender.probe_emit_buckets)}",
            )


if __name__ == "__main__":
    unittest.main()
