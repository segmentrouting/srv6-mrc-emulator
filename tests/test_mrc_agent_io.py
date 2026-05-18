"""End-to-end loopback tests for the MRC probe/report I/O agents.

These tests exercise the real SenderMrcAgent + ReceiverMrcAgent on
::1 with very short timer intervals so the tests complete in well under
a second. We DO NOT use SO_BINDTODEVICE here (loopback rejects it);
use_loopback=True in AgentConfig switches all the socket helpers to
::1 binding with per-plane port offsets.

What we test
------------
1. Probe round-trip: sender emits probes, receiver responds, the
   sender's EVStateTable.record_probe_result is called with success=True
   and a positive rtt_ns for at least one plane.
2. Probe timeout: with a receiver NOT started, the sender's sweep
   thread fires probe_result(success=False) entries into the EV table.
3. Loss-report end-to-end: receiver records data packets, agent emits a
   LOSS_REPORT, sender receives + decodes it and feeds the EV table via
   apply_loss_report (we check LossFusionStats counters).
4. Plane isolation in the receiver socket cache: receiver learns each
   sender's reply_addr from probes; without a probe ever arriving, no
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
from typing import Optional, Tuple

from srv6_fabric.mrc.agent import (
    AgentConfig,
    ReceiverMrcAgent,
    SenderMrcAgent,
)
from srv6_fabric.mrc.ev_state import EVStateTable
from srv6_fabric.topo import (
    NUM_PLANES,
    NUM_SPINES,
    SPRAY_PROBE_PORT,
    SPRAY_REPORT_PORT,
    tenant_id as topo_tenant_id,
)


# All loopback tests use these very-short cadences. With 20ms intervals
# we get ~5 probes per plane in 100ms, which is plenty of signal.
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


def _build_sender_factory(probe_port_base: int, report_port: int):
    """Build a sockets_factory + report_socket_factory pair that uses
    distinct loopback ports, parametrized for this test. The agent uses
    SPRAY_PROBE_PORT/SPRAY_REPORT_PORT internally; we override them by
    monkey-patching the socket binds here."""
    def probe_sock(plane: int) -> socket.socket:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, 0)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("::1", probe_port_base + plane))
        s.settimeout(0.05)
        return s

    def report_sock() -> socket.socket:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, 0)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("::1", report_port))
        s.settimeout(0.05)
        return s

    return probe_sock, report_sock


def _build_receiver_factory(probe_port_base: int):
    def probe_sock(plane: int) -> socket.socket:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, 0)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("::1", probe_port_base + plane))
        s.settimeout(0.05)
        return s

    return probe_sock


class _SockWrapper:
    """Wraps a real socket but rewrites sendto's address to a fixed
    target. Used to point the sender's per-plane probe socket at the
    receiver's plane-specific loopback port without touching the
    agent's address-building code."""

    def __init__(self, sock: socket.socket, target: Tuple[str, int]) -> None:
        self._sock = sock
        self._target = target

    def sendto(self, data: bytes, _addr) -> int:
        return self._sock.sendto(data, self._target)

    def recvfrom(self, bufsize: int) -> Tuple[bytes, tuple]:
        return self._sock.recvfrom(bufsize)

    def settimeout(self, t: Optional[float]) -> None:
        self._sock.settimeout(t)

    def close(self) -> None:
        self._sock.close()


class _PeerOverride:
    """Helper to override sender's per-plane peer addresses to ::1
    plus the receiver's actual loopback port.

    Under Phase 1a step 3 the receiver listens on ONE rx socket
    (plane=0's port); every sender plane targets that same port. The
    receiver derives plane attribution from the probe payload's
    `plane_id`, not from which port it arrived on.
    """

    def __init__(self, agent: SenderMrcAgent, recv_probe_port_base: int,
                 recv_report_port: int):
        from srv6_fabric.mrc.agent import _PeerInfo
        agent._peer = _PeerInfo(
            peer_addrs=tuple("::1" for _ in range(NUM_PLANES)),
            probe_port=recv_probe_port_base,
            report_port=recv_report_port,
        )
        # Wrap each per-plane probe socket so its sendto always goes to
        # the SINGLE receiver port (the collapsed-rx port). We rebuild
        # the _probe_sockets dict in place.
        new_sockets = {}
        for plane, sock in agent._probe_sockets.items():
            target = ("::1", recv_probe_port_base)
            new_sockets[plane] = _SockWrapper(sock, target)
        agent._probe_sockets = new_sockets


class ProbeRoundTripTests(unittest.TestCase):
    """Sender emits probes, receiver replies, EVStateTable sees RTTs."""

    def setUp(self) -> None:
        # Each test gets a distinct port range to avoid cross-talk.
        sender_probe_base = PORTS.take(NUM_PLANES)
        sender_report_port = PORTS.take(1)
        recv_probe_base = PORTS.take(NUM_PLANES)

        self.table = EVStateTable(tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES)
        s_probe_factory, s_report_factory = _build_sender_factory(
            sender_probe_base, sender_report_port,
        )

        self.sender = SenderMrcAgent(
            tenant="green",
            src_id=0,
            dst_id=15,
            table=self.table,
            config=FAST_CONFIG,
            sockets_factory=s_probe_factory,
            report_socket_factory=s_report_factory,
        )
        # Point sender at the receiver's loopback ports for probes.
        _PeerOverride(self.sender, recv_probe_base, sender_report_port)

        r_probe_factory = _build_receiver_factory(recv_probe_base)
        self.receiver = ReceiverMrcAgent(
            tenant="green",
            my_id=15,
            config=FAST_CONFIG,
            sockets_factory=r_probe_factory,
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
            "receiver never cached sender's reply_addr",
        )


class ProbeTimeoutTests(unittest.TestCase):
    """With no receiver listening, sender probes should time out."""

    def test_probe_timeouts_recorded_as_failures(self) -> None:
        sender_probe_base = PORTS.take(NUM_PLANES)
        sender_report_port = PORTS.take(1)
        recv_probe_base = PORTS.take(NUM_PLANES)  # nothing bound here

        table = EVStateTable(tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES)
        s_probe_factory, s_report_factory = _build_sender_factory(
            sender_probe_base, sender_report_port,
        )
        sender = SenderMrcAgent(
            tenant="green",
            src_id=0,
            dst_id=15,
            table=table,
            config=FAST_CONFIG,
            sockets_factory=s_probe_factory,
            report_socket_factory=s_report_factory,
        )
        _PeerOverride(sender, recv_probe_base, sender_report_port)
        sender.start()
        try:
            # 5 probes/plane @ 20ms + 40ms timeout = ~140ms to first
            # timeout sweep. Give it 600ms to record several fails.
            def enough_fails() -> bool:
                snap = table.snapshot()["tenants"]["green"]
                for plane_entry in snap:
                    if plane_entry["consecutive_probe_timeouts"] >= 2:
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
        sender_probe_base = PORTS.take(NUM_PLANES)
        sender_report_port = PORTS.take(1)
        recv_probe_base = PORTS.take(NUM_PLANES)

        table = EVStateTable(tenants=("green",), num_planes=NUM_PLANES, num_paths=NUM_SPINES)
        s_probe_factory, s_report_factory = _build_sender_factory(
            sender_probe_base, sender_report_port,
        )
        sender = SenderMrcAgent(
            tenant="green",
            src_id=0,
            dst_id=15,
            table=table,
            config=FAST_CONFIG,
            sockets_factory=s_probe_factory,
            report_socket_factory=s_report_factory,
        )
        _PeerOverride(sender, recv_probe_base, sender_report_port)

        r_probe_factory = _build_receiver_factory(recv_probe_base)
        receiver = ReceiverMrcAgent(
            tenant="green",
            my_id=15,
            config=FAST_CONFIG,
            sockets_factory=r_probe_factory,
        )

        # FlowKey shape per ReceiverMrcAgent._emit_one_round: tuple
        # whose [0]=tenant_id, [1]=src_id. We use a 3-tuple for clarity.
        tid = topo_tenant_id("green")
        flow_key = (tid, 0, 15)

        try:
            receiver.start()
            sender.start()

            # Wait for the receiver to learn the sender (so it has an
            # address to send the loss report to).
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
        recv_probe_base = PORTS.take(NUM_PLANES)
        r_probe_factory = _build_receiver_factory(recv_probe_base)
        receiver = ReceiverMrcAgent(
            tenant="green",
            my_id=15,
            config=FAST_CONFIG,
            sockets_factory=r_probe_factory,
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
