"""End-to-end loopback tests for the MRC probe/report I/O agents.

These tests exercise the real SenderMrcAgent + ReceiverMrcAgent on
::1 with very short timer intervals so the tests complete in well under
a second. We DO NOT use SO_BINDTODEVICE here (loopback rejects it) and
we do NOT use raw sockets (CAP_NET_RAW is unavailable to the test
runner). Instead, both agents are constructed with an explicit
LoopbackUdpTransport that exchanges raw PROBE / PROBE_REPLY /
LOSS_REPORT payload bytes over loopback UDP — same payload bytes the
lab transport would put inside an SRv6 carrier, just without the
outer headers.

What we test
------------
1. Probe round-trip: sender emits probes, receiver responds, the
   sender's EVStateTable.record_probe_result is called with success=True
   and a positive rtt_ns for at least one EV.
2. Probe timeout: with a receiver NOT started, the sender's sweep
   thread fires probe_result(success=False) entries into the EV table.
3. Loss-report end-to-end: receiver records data packets, agent emits a
   LOSS_REPORT, sender receives + decodes it and feeds the EV table via
   apply_loss_report (we check LossFusionStats counters).
4. Plane isolation in the receiver socket cache: receiver learns each
   sender's reply EV from probes; without a probe ever arriving, no
   loss report should be sent.

These tests are intentionally tolerant to scheduling jitter: they wait
up to a few hundred milliseconds for the relevant condition to become
true, polling the EVStateTable / LossFusionStats.
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


# All loopback tests use these very-short cadences. With 20ms intervals
# we get ~5 probe rounds in 100ms, which is plenty of signal.
FAST_CONFIG = AgentConfig(
    probe_interval_ms=20,
    probe_timeout_ms=40,
    loss_window_ms=40,
    max_window_skew_ms=200,
    use_loopback=True,
)


def _wait_for(predicate, *, timeout_s: float = 2.0,
              poll_s: float = 0.01) -> bool:
    """Spin until predicate() is true or timeout. Returns the final
    predicate value so the caller can assert on it."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return predicate()


class _PortAllocator:
    """Hand out distinct port offsets per test so concurrently-running
    tests in a single process don't collide. We bias well above the
    SPRAY_PROBE_PORT/SPRAY_REPORT_PORT defaults but stay under 65535."""

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
    """Build NUM_PLANES per-plane ephemeral UDP sockets for sending.

    The loopback transport doesn't actually need plane-distinct
    sockets for correctness (it embeds plane in the payload), but
    keeping them per-plane preserves the lab-shape so the test's
    transport object structurally mirrors Srv6RawTransport. Each
    socket is bound to an ephemeral port so the kernel doesn't see
    duplicate binds between concurrent tests.
    """
    socks: Dict[int, socket.socket] = {}
    for p in range(NUM_PLANES):
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, 0)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("::1", 0))  # ephemeral
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
    """Construct paired sender + receiver LoopbackUdpTransports.

    Both transports run inside the same process; the sender's send_*
    methods target the receiver's rx port and vice versa. The caller
    allocates the two ports from the shared PortAllocator so
    concurrent tests don't collide.
    """
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


class ProbeRoundTripTests(unittest.TestCase):
    """Sender emits probes, receiver replies, EVStateTable sees RTTs."""

    def setUp(self) -> None:
        sender_report_port = PORTS.take(1)
        receiver_probe_port = PORTS.take(1)

        self.table = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )
        sender_xport, receiver_xport = _build_loopback_pair(
            sender_report_port=sender_report_port,
            receiver_probe_port=receiver_probe_port,
        )
        self.sender = SenderMrcAgent(
            tenant="green",
            src_id=0,
            dst_id=15,
            table=self.table,
            config=FAST_CONFIG,
            transport=sender_xport,
        )
        self.receiver = ReceiverMrcAgent(
            tenant="green",
            my_id=15,
            config=FAST_CONFIG,
            transport=receiver_xport,
        )

    def tearDown(self) -> None:
        self.sender.stop(timeout_s=0.5)
        self.receiver.stop(timeout_s=0.5)

    def test_sender_emits_probes_and_receives_replies(self) -> None:
        """RTT samples land in the EV table within 250ms."""
        self.receiver.start()
        self.sender.start()

        def saw_a_reply() -> bool:
            for plane in range(NUM_PLANES):
                for path in range(NUM_SPINES):
                    if self.table.rtt_p50_ns("green", plane, path) is not None:
                        return True
            return False

        self.assertTrue(_wait_for(saw_a_reply, timeout_s=1.0),
                        "no probe reply landed in EV table within 1s")

    def test_receiver_learns_sender_after_first_probe(self) -> None:
        self.receiver.start()
        self.sender.start()

        tid = topo_tenant_id("green")
        key = (tid, 0)
        self.assertTrue(
            _wait_for(lambda: key in self.receiver.known_senders(),
                      timeout_s=1.0),
            "receiver never cached sender's reply EV",
        )


class ProbeTimeoutTests(unittest.TestCase):
    """With no receiver listening, sender probes should time out."""

    def test_probe_timeouts_recorded_as_failures(self) -> None:
        sender_report_port = PORTS.take(1)
        receiver_probe_port = PORTS.take(1)  # nothing bound here

        table = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )
        # Build only the sender side; no rx listener on
        # receiver_probe_port, so probes get blackholed.
        sender_send = _make_send_sockets()
        sender_rx = _make_rx_socket(sender_report_port)
        sender_xport = LoopbackUdpTransport(
            is_sender=True,
            per_plane_send_sockets=sender_send,
            rx_socket=sender_rx,
            peer_rx_port=receiver_probe_port,
        )
        sender = SenderMrcAgent(
            tenant="green",
            src_id=0,
            dst_id=15,
            table=table,
            config=FAST_CONFIG,
            transport=sender_xport,
        )
        sender.start()
        try:
            # 5 probe rounds @ 20ms + 40ms timeout = ~140ms to first
            # timeout sweep. Give it 600ms to record several fails.
            def enough_fails() -> bool:
                snap = table.snapshot()["tenants"]["green"]
                for ev_entry in snap:
                    if ev_entry["consecutive_probe_timeouts"] >= 2:
                        return True
                return False
            self.assertTrue(_wait_for(enough_fails, timeout_s=1.0),
                            "probe timeouts not recorded as failures")
        finally:
            sender.stop(timeout_s=0.5)


class LossReportEndToEndTests(unittest.TestCase):
    """Receiver records data packets, emits a loss report, sender's
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

        # FlowKey shape per ReceiverMrcAgent._emit_one_round: tuple
        # whose [0]=tenant_id, [1]=src_id. We use a 3-tuple for clarity.
        tid = topo_tenant_id("green")
        flow_key = (tid, 0, 15)

        try:
            receiver.start()
            sender.start()

            # Wait for the receiver to learn the sender (so it has an
            # EV cached to send the loss report on).
            self.assertTrue(_wait_for(
                lambda: (tid, 0) in receiver.known_senders(),
                timeout_s=1.0,
            ), "receiver didn't learn sender via probe")

            # Inject some data packets on plane 0 and 2 so the receiver
            # has loss-window content to report.
            for seq in range(0, 50, 2):  # 25 packets on plane 0
                receiver.record_data(flow_key, plane=0, path=0, seq=seq)
            for seq in range(1, 50, 2):  # 25 packets on plane 2
                receiver.record_data(flow_key, plane=2, path=0, seq=seq)

            # Wait for at least one report round to be processed by the
            # sender's fusion path. Either a "ratio_applied" or a
            # "fell_back_to_receiver_expected" counter must move.
            def fusion_progress() -> bool:
                s = sender.stats
                return s.reports_processed > 0 or s.planes_updated > 0
            self.assertTrue(_wait_for(fusion_progress, timeout_s=1.5),
                            f"no loss report reached sender; "
                            f"stats={sender.stats}")
        finally:
            sender.stop(timeout_s=0.5)
            receiver.stop(timeout_s=0.5)


class ReceiverNoSenderKnownTests(unittest.TestCase):
    """If no probe has arrived, receiver should not crash trying to
    emit a LOSS_REPORT for an unknown sender."""

    def test_loss_report_skipped_when_no_sender_cached(self) -> None:
        receiver_probe_port = PORTS.take(1)
        # We only need a receiver; build its transport without a
        # paired sender. peer_rx_port is unused because the receiver
        # will never see a probe to elicit a reply.
        receiver_send = _make_send_sockets()
        receiver_rx = _make_rx_socket(receiver_probe_port)
        receiver_xport = LoopbackUdpTransport(
            is_sender=False,
            per_plane_send_sockets=receiver_send,
            rx_socket=receiver_rx,
            peer_rx_port=PORTS.take(1),  # arbitrary; never used
        )
        receiver = ReceiverMrcAgent(
            tenant="green",
            my_id=15,
            config=FAST_CONFIG,
            transport=receiver_xport,
        )
        receiver.start()
        try:
            # Record packets for a never-seen sender.
            flow_key = (topo_tenant_id("green"), 99, 15)
            for seq in range(20):
                receiver.record_data(flow_key, plane=1, path=0, seq=seq)
            # Let two loss-emit rounds elapse; nothing should explode.
            time.sleep(FAST_CONFIG.loss_window_ms * 2.5 / 1000.0)
            self.assertEqual(receiver.known_senders(), ())
        finally:
            receiver.stop(timeout_s=0.5)


class _TimestampingLoopbackTransport(LoopbackUdpTransport):
    """Loopback transport that timestamps every probe send call.

    Used by ProbePacingTests to assert _emit_loop spreads probes
    across the cadence window rather than firing them in a tight burst
    at the top of each interval. Records (monotonic_s, plane, path)
    on every send_probe(); other send_* paths are unchanged.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.send_times: list[Tuple[float, int, int]] = []
        self._send_times_lock = threading.Lock()

    def send_probe(self, *, plane, path, dst_leaf, payload) -> None:
        # Record before the actual send so the timestamp reflects when
        # the sender thread *decided* to emit, not when the kernel
        # finished the syscall. Pacing-correctness is about the former.
        with self._send_times_lock:
            self.send_times.append((time.monotonic(), plane, path))
        super().send_probe(
            plane=plane, path=path, dst_leaf=dst_leaf, payload=payload,
        )


class ProbePacingTests(unittest.TestCase):
    """Verify the emit loop spreads probes across the interval (issue
    surfaced by the yellow-allreduce-ring lab run: 16 senders all
    firing 32-probe bursts in lockstep saturated spine TX queues and
    pushed RTT_p50 to ~1s, causing universal probe timeouts even with
    a healthy data path).

    These tests don't reproduce the lab failure (no fabric here);
    they pin the *invariants* that prevent it: (a) within one round,
    consecutive probes are not back-to-back, and (b) two independent
    SenderMrcAgent instances pick distinct startup jitter offsets so
    they don't lockstep when launched in the same wall-clock instant.
    """

    def _build_sender(
        self, *, src_id: int = 0,
    ) -> Tuple[SenderMrcAgent, _TimestampingLoopbackTransport]:
        sender_report_port = PORTS.take(1)
        receiver_probe_port = PORTS.take(1)
        sender_send = _make_send_sockets()
        sender_rx = _make_rx_socket(sender_report_port)
        xport = _TimestampingLoopbackTransport(
            is_sender=True,
            per_plane_send_sockets=sender_send,
            rx_socket=sender_rx,
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
        """Within one round, consecutive probes are paced ~interval/num_evs
        apart rather than emitted as a tight back-to-back burst at the
        top of the interval.

        Allows generous slack for scheduling jitter (the FAST_CONFIG
        slot is 20ms / 32 = 625us, which is well within typical Linux
        scheduling granularity), but asserts the *shape* of the
        distribution: the span across one round should be a sizable
        fraction of the interval rather than a few microseconds.
        """
        sender, xport = self._build_sender()
        sender.start()
        try:
            # Wait for at least one full round to complete. With
            # FAST_CONFIG.probe_interval_ms=20 and 32 EVs that's at
            # most ~40ms (jitter U(0,20) + one round of paced sends).
            # Give it 250ms to be safe under loaded CI.
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
            f"got {len(times)} in 250ms"
        )
        span_s = times[-1] - times[0]
        interval_s = FAST_CONFIG.probe_interval_ms / 1000.0
        # With perfect pacing the span is ~(num_evs - 1) * slot_s
        # = 31/32 * 20ms = 19.4ms. We allow span >= 25% of the
        # interval as the "definitely not bursty" floor; the original
        # buggy loop produced a span on the order of single-digit
        # microseconds (32 Python-level sendto calls back-to-back).
        min_acceptable_span_s = interval_s * 0.25
        self.assertGreater(
            span_s, min_acceptable_span_s,
            f"one-round probe span = {span_s*1000:.2f}ms, expected "
            f">{min_acceptable_span_s*1000:.2f}ms (interval="
            f"{interval_s*1000:.0f}ms, num_evs={num_evs}); "
            "looks like _emit_loop reverted to a bursty inner loop"
        )

    def test_independent_agents_have_distinct_jitter(self) -> None:
        """Two SenderMrcAgent instances pick independent startup
        jitter offsets, so their first probes are not synchronized
        even when constructed back-to-back in the same wall-clock ms.

        The jitter range is U(0, probe_interval_ms). With 32 random
        draws from that range the chance of two values landing within
        100us of each other is ~32*100us/20ms = 16%, so we sample 8
        agents and require at least one pair to differ by >= 500us.
        This is loose enough to ride out python random implementation
        details but tight enough to catch a hard-coded jitter of 0.
        """
        jitters: list[float] = []
        for src_id in range(8):
            sender, _xport = self._build_sender(src_id=src_id)
            jitters.append(sender._emit_jitter_s)
            # Don't actually start the agent — we only care about the
            # constructor-time jitter draw.
            sender.stop(timeout_s=0.0)
        interval_s = FAST_CONFIG.probe_interval_ms / 1000.0
        # All jitter values must be in [0, interval_s].
        for j in jitters:
            self.assertGreaterEqual(j, 0.0)
            self.assertLessEqual(j, interval_s)
        # At least one pair differs by >= 500us. Constant or
        # near-constant jitter would fail this assertion.
        spread = max(jitters) - min(jitters)
        self.assertGreaterEqual(
            spread, 0.0005,
            f"8 agents' jitter spread = {spread*1e6:.0f}us, expected "
            f">=500us; jitter likely hard-coded or seeded badly"
        )


class ProbeEmitBucketsTests(unittest.TestCase):
    """Verify the per-call timing instrumentation around
    transport.send_probe() in _emit_loop.

    These tests pin two invariants from the 2026-05-24 emit-latency
    investigation:
      (a) on a fast transport (loopback UDP, microsecond send),
          all probes bucket into lt_100us or lt_1ms.
      (b) on a slow transport (50ms artificial sleep), probes
          bucket into lt_100ms — proving the wrapper measures the
          actual send_probe wall-clock and not just a constant.
    """

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
                f"expected at least {num_evs} probe emits, got {total}"
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
            f"buckets={dict(sender.probe_emit_buckets)}"
        )
        self.assertGreater(
            fast, 0,
            f"expected nonzero sub-millisecond emits on loopback; "
            f"buckets={dict(sender.probe_emit_buckets)}"
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
        # Monkey-patch send_probe to sleep 50ms per call. Time measured
        # by the wrapper is time.monotonic_ns(), not the agent's
        # clock_ns, so we deliberately use real sleep here.
        def slow_send(*, plane, path, dst_leaf, payload) -> None:
            time.sleep(0.05)
        xport.send_probe = slow_send  # type: ignore[assignment]

        sender = self._build_sender(transport=xport)
        sender.start()
        try:
            # 50ms per send * a few probes — wait for at least 3 emits
            # so we're not asserting on a single sample.
            self._wait_for_total(sender, 3, timeout_s=1.0)
        finally:
            sender.stop(timeout_s=1.0)

        in_lt_100ms = sender.probe_emit_buckets["lt_100ms"]
        self.assertGreaterEqual(
            in_lt_100ms, 3,
            f"expected >=3 probes in lt_100ms bucket with 50ms send; "
            f"buckets={dict(sender.probe_emit_buckets)}"
        )
        # And NOT in the fast buckets — 50ms is comfortably above 10ms.
        for fast_key in ("lt_100us", "lt_1ms", "lt_10ms"):
            self.assertEqual(
                sender.probe_emit_buckets[fast_key], 0,
                f"50ms send leaked into fast bucket {fast_key}; "
                f"buckets={dict(sender.probe_emit_buckets)}"
            )




class ReplyHandlerBucketsTests(unittest.TestCase):
    """Internal timing of _handle_probe_reply.

    On loopback (no fabric, no scapy) the handler is pure-Python:
    decode + clock_ns + bucket + probe_clock.match_reply + (maybe)
    record_probe_result. End-to-end this is microseconds. We pin
    that fast-path so a future regression that puts I/O / lock
    contention into the handler shows up here as buckets sliding
    into lt_10ms+.
    """

    def setUp(self) -> None:
        sender_report_port = PORTS.take(1)
        receiver_probe_port = PORTS.take(1)
        self.table = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )
        sender_xport, receiver_xport = _build_loopback_pair(
            sender_report_port=sender_report_port,
            receiver_probe_port=receiver_probe_port,
        )
        self.sender = SenderMrcAgent(
            tenant="green",
            src_id=0,
            dst_id=15,
            table=self.table,
            config=FAST_CONFIG,
            transport=sender_xport,
        )
        self.receiver = ReceiverMrcAgent(
            tenant="green",
            my_id=15,
            config=FAST_CONFIG,
            transport=receiver_xport,
        )

    def tearDown(self) -> None:
        self.sender.stop(timeout_s=0.5)
        self.receiver.stop(timeout_s=0.5)

    def test_loopback_replies_bucket_into_sub_millisecond(self) -> None:
        self.receiver.start()
        self.sender.start()

        def saw_matched() -> bool:
            return self.sender.probe_reply_stats["matched"] >= 4

        self.assertTrue(_wait_for(saw_matched, timeout_s=1.5),
                        f"never saw 4 matched replies; "
                        f"stats={dict(self.sender.probe_reply_stats)}")

        rhb = self.sender.reply_handler_buckets
        matched = self.sender.probe_reply_stats["matched"]
        fast = rhb["lt_10us"] + rhb["lt_100us"] + rhb["lt_1ms"]
        slow = rhb["lt_10ms"] + rhb["lt_100ms"] + rhb["ge_100ms"]
        self.assertGreaterEqual(
            fast, matched,
            f"expected sub-ms reply handler on loopback; "
            f"buckets={dict(rhb)} matched={matched}",
        )
        self.assertEqual(
            slow, 0,
            f"loopback reply handler should never take >=10ms; "
            f"buckets={dict(rhb)}",
        )


class ReplyAgeStatsTests(unittest.TestCase):
    """Cross-check that (now_ns - reply.tx_ns) ages reflect real
    wall-clock between probe emit and reply decode. We force a 50ms
    gap by stalling the receiver's reply send so the reply payload
    carries a tx_ns that is ~50ms older than now() when the sender
    decodes it.
    """

    def test_age_stats_track_emit_to_decode_delta(self) -> None:
        sender_report_port = PORTS.take(1)
        receiver_probe_port = PORTS.take(1)
        self.table = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )
        sender_xport, receiver_xport = _build_loopback_pair(
            sender_report_port=sender_report_port,
            receiver_probe_port=receiver_probe_port,
        )
        # Wrap the receiver's send_probe_reply with a 50ms pre-send
        # sleep. The receiver computes its reply.tx_ns from the
        # incoming probe's tx_ns echo (verbatim), so this stall
        # adds latency entirely on the wire-time axis the sender
        # measures as `now_ns - reply.tx_ns`.
        original_send = receiver_xport.send_probe_reply

        def delayed_send(*args, **kwargs):
            time.sleep(0.05)
            return original_send(*args, **kwargs)

        receiver_xport.send_probe_reply = delayed_send  # type: ignore[assignment]

        self.sender = SenderMrcAgent(
            tenant="green",
            src_id=0,
            dst_id=15,
            table=self.table,
            config=FAST_CONFIG,
            transport=sender_xport,
        )
        self.receiver = ReceiverMrcAgent(
            tenant="green",
            my_id=15,
            config=FAST_CONFIG,
            transport=receiver_xport,
        )
        try:
            self.receiver.start()
            self.sender.start()
            self.assertTrue(
                _wait_for(
                    lambda: self.sender.reply_age_count >= 1,
                    timeout_s=2.0,
                ),
                f"never observed any reply age sample; "
                f"stats={dict(self.sender.probe_reply_stats)}",
            )
        finally:
            self.sender.stop(timeout_s=0.5)
            self.receiver.stop(timeout_s=0.5)

        self.assertGreaterEqual(
            self.sender.reply_age_max_ns, 50_000_000,
            f"reply_age_max_ns expected >=50ms with 50ms stall; "
            f"got max={self.sender.reply_age_max_ns} "
            f"min={self.sender.reply_age_min_ns} "
            f"count={self.sender.reply_age_count}",
        )
        self.assertGreaterEqual(
            self.sender.reply_age_min_ns, 50_000_000,
            f"reply_age_min_ns expected >=50ms when every reply is "
            f"stalled by 50ms; got min={self.sender.reply_age_min_ns} "
            f"count={self.sender.reply_age_count}",
        )


if __name__ == "__main__":
    unittest.main()
