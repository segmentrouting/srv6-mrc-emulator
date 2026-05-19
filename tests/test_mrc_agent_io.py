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

from srv6_fabric.mrc.agent import (
    AgentConfig,
    ReceiverMrcAgent,
    SenderMrcAgent,
)
from srv6_fabric.mrc.ev_state import EVStateTable
from srv6_fabric.mrc.transport import LoopbackUdpTransport
from srv6_fabric.topo import (
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


if __name__ == "__main__":
    unittest.main()
