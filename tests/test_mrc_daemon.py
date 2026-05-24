"""Unit tests for srv6_mrc.mrc.daemon.MrcDaemon.

These tests exercise the daemon end-to-end on ::1 using
LoopbackUdpTransport, so no CAP_NET_RAW is required and tests run in
well under a second.

What we cover at this stage (step a — single-flow daemon):
    1. Daemon start/stop lifecycle: threads come up, snapshots get
       written, threads exit cleanly on stop().
    2. Snapshot publisher writes per-flow JSON to the configured
       directory, with the expected schema (src_host, src_id, tenant,
       dst_id, captured_ns, ev_state).
    3. Snapshot publisher refreshes at the configured cadence
       (probe_interval_ms).
    4. final_report() returns the per-flow EV state + probe_clock +
       loss_fusion shape that the orchestrator will merge into the
       ScenarioReport.
    5. Daemon dispatcher correctly routes inbound replies into the
       per-flow agent's EVStateTable via direct _handle_probe_reply
       calls (since LoopbackUdpTransport always reports peer_addr=("::1", ...)
       we override the daemon's _demux for the multi-flow case; for
       a single flow, the trivial demux is used).

Multi-flow demux (multiple peers) is verified in
test_mrc_daemon_multiflow.py (step c).
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Dict, Tuple

from srv6_mrc.mrc.agent import (
    AgentConfig,
    ReceiverMrcAgent,
    SenderMrcAgent,
)
from srv6_mrc.mrc.daemon import DaemonFlow, MrcDaemon
from srv6_mrc.mrc.ev_state import EVStateTable
from srv6_mrc.mrc.transport import LoopbackUdpTransport
from srv6_mrc.topo import (
    NUM_PLANES,
    NUM_SPINES,
    tenant_id as topo_tenant_id,
)


# Matches the cadences in tests/test_mrc_agent_io.py so test runtime
# stays in the sub-second range.
FAST_CONFIG = AgentConfig(
    probe_interval_ms=20,
    probe_timeout_ms=40,
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
    def __init__(self, start: int = 31000) -> None:
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
    """Symmetric helper to test_mrc_agent_io._build_loopback_pair.

    Inlined here so the daemon tests don't depend on test internals
    of another file (a stable public test fixture would be cleaner;
    out of scope for this commit).
    """
    sender_xport = LoopbackUdpTransport(
        is_sender=True,
        per_plane_send_sockets=_make_send_sockets(),
        rx_socket=_make_rx_socket(sender_report_port),
        peer_rx_port=receiver_probe_port,
    )
    receiver_xport = LoopbackUdpTransport(
        is_sender=False,
        per_plane_send_sockets=_make_send_sockets(),
        rx_socket=_make_rx_socket(receiver_probe_port),
        peer_rx_port=sender_report_port,
    )
    return sender_xport, receiver_xport


class MrcDaemonLifecycleTests(unittest.TestCase):
    """start/stop bring threads up + tear them down cleanly."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mrc-daemon-test-")
        self.snap_dir = self.tmpdir
        self.sender_report_port = PORTS.take(1)
        self.receiver_probe_port = PORTS.take(1)

        self.sender_xport, self.receiver_xport = _build_loopback_pair(
            sender_report_port=self.sender_report_port,
            receiver_probe_port=self.receiver_probe_port,
        )
        self.daemon = MrcDaemon(
            src_host="green-host00",
            src_id=0,
            flows=[DaemonFlow(tenant="green", dst_id=15)],
            agent_cfg=FAST_CONFIG,
            transport=self.sender_xport,
            snapshot_dir=self.snap_dir,
        )
        # Single-flow demux: LoopbackUdpTransport reports peer_addr=("::1", ...)
        # for inbound packets, so we re-key the demux on "::1" so the
        # dispatcher routes to the only agent we have. Production
        # demux uses real IPv6 anycast addresses.
        only_agent = next(iter(self.daemon.agents.values()))
        self.daemon._demux = {"::1": only_agent}

        self.receiver = ReceiverMrcAgent(
            tenant="green", my_id=15, config=FAST_CONFIG,
            transport=self.receiver_xport,
        )

    def tearDown(self) -> None:
        try:
            self.daemon.stop(timeout_s=0.5)
        finally:
            try:
                self.receiver.stop(timeout_s=0.5)
            except Exception:
                pass
            # Best-effort temp cleanup; OK to leak on failure.
            try:
                for f in os.listdir(Path(self.tmpdir) / "green-host00"):
                    os.unlink(Path(self.tmpdir) / "green-host00" / f)
                os.rmdir(Path(self.tmpdir) / "green-host00")
            except FileNotFoundError:
                pass
            except OSError:
                pass
            try:
                os.rmdir(self.tmpdir)
            except OSError:
                pass

    def test_start_creates_snapshot_dir(self) -> None:
        self.daemon.start()
        snap_subdir = Path(self.snap_dir) / "green-host00"
        self.assertTrue(snap_subdir.is_dir(),
                        f"daemon did not create {snap_subdir}")

    def test_start_writes_initial_snapshot(self) -> None:
        self.daemon.start()
        snap_path = Path(self.snap_dir) / "green-host00" / "green_15.json"
        self.assertTrue(_wait_for(snap_path.is_file, timeout_s=1.0),
                        f"snapshot {snap_path} not written within 1s")

    def test_snapshot_schema(self) -> None:
        self.daemon.start()
        snap_path = Path(self.snap_dir) / "green-host00" / "green_15.json"
        self.assertTrue(_wait_for(snap_path.is_file, timeout_s=1.0))

        with open(snap_path) as f:
            payload = json.load(f)

        self.assertEqual(payload["src_host"], "green-host00")
        self.assertEqual(payload["src_id"], 0)
        self.assertEqual(payload["tenant"], "green")
        self.assertEqual(payload["dst_id"], 15)
        self.assertIn("captured_ns", payload)
        self.assertIsInstance(payload["captured_ns"], int)

        ev = payload["ev_state"]
        # Schema sanity: same shape EVStateTable.snapshot() returns.
        self.assertIn("config", ev)
        self.assertIn("tenants", ev)
        self.assertIn("green", ev["tenants"])
        self.assertEqual(ev["num_planes"], NUM_PLANES)
        self.assertEqual(ev["num_paths"], NUM_SPINES)
        self.assertEqual(len(ev["tenants"]["green"]),
                         NUM_PLANES * NUM_SPINES)

        # transport_stats is daemon-wide; LoopbackUdpTransport returns
        # an empty dict (the base-class default). Srv6RawTransport
        # exposes probe_fast_path_misses + reply_fast_path_misses; the
        # key must always be present so jq consumers don't need a
        # `// {}` guard at every call site.
        self.assertIn("transport_stats", payload)
        self.assertIsInstance(payload["transport_stats"], dict)

        # dispatch_stats is daemon-wide (shared dispatcher counters).
        # All five keys must always be present so lab jq queries can
        # rely on the schema rather than `// 0`-guarding each lookup.
        # Values are unsigned monotonic counters.
        self.assertIn("dispatch_stats", payload)
        ds = payload["dispatch_stats"]
        self.assertIsInstance(ds, dict)
        for key in (
            "replies_received", "replies_no_peer",
            "replies_unknown_magic",
            "replies_dispatched_probe", "replies_dispatched_loss",
        ):
            self.assertIn(key, ds, f"missing dispatch_stats key {key}")
            self.assertIsInstance(ds[key], int)
            self.assertGreaterEqual(ds[key], 0)

        # probe_reply_stats is per-flow (per agent); all four keys
        # always present. Locks the schema the same way as dispatch_stats.
        self.assertIn("probe_reply_stats", payload)
        prs = payload["probe_reply_stats"]
        self.assertIsInstance(prs, dict)
        for key in ("received", "decode_failed", "no_match", "matched"):
            self.assertIn(key, prs, f"missing probe_reply_stats key {key}")
            self.assertIsInstance(prs[key], int)
            self.assertGreaterEqual(prs[key], 0)

        # reply_latency_buckets diagnoses arrival-time of replies
        # relative to their outstanding probe entries' sweep deadline.
        self.assertIn("reply_latency_buckets", payload)
        rlb = payload["reply_latency_buckets"]
        self.assertIsInstance(rlb, dict)
        for key in ("lt_50ms", "lt_200ms", "lt_1s", "lt_5s", "ge_5s"):
            self.assertIn(key, rlb, f"missing reply_latency_buckets key {key}")
            self.assertIsInstance(rlb[key], int)
            self.assertGreaterEqual(rlb[key], 0)

    def test_snapshot_refreshes_on_cadence(self) -> None:
        """Two snapshots taken ~2*probe_interval_ms apart have distinct
        captured_ns values (publisher actually re-runs)."""
        self.daemon.start()
        snap_path = Path(self.snap_dir) / "green-host00" / "green_15.json"
        self.assertTrue(_wait_for(snap_path.is_file, timeout_s=1.0))

        with open(snap_path) as f:
            first = json.load(f)
        first_ts = first["captured_ns"]

        # Wait at least 3 * probe_interval_ms to be sure a second
        # publish has run.
        time.sleep(FAST_CONFIG.probe_interval_ms * 3 / 1000.0)

        with open(snap_path) as f:
            second = json.load(f)
        self.assertGreater(
            second["captured_ns"], first_ts,
            "snapshot publisher did not advance captured_ns",
        )

    def test_stop_after_start_is_clean(self) -> None:
        """start() then stop() leaves no stuck threads."""
        self.daemon.start()
        # Let it run a moment so all threads are firmly up.
        time.sleep(FAST_CONFIG.probe_interval_ms * 2 / 1000.0)

        self.daemon.stop(timeout_s=1.0)

        # All threads should be done after stop().
        alive = [t.name for t in self.daemon._threads if t.is_alive()]
        # Daemon threads are daemon=True so they auto-die on process
        # exit, but stop() should join them within timeout. We allow
        # some slack: the publisher's final-flush can race but must
        # exit within the next interval.
        time.sleep(FAST_CONFIG.probe_interval_ms * 2 / 1000.0)
        alive = [t.name for t in self.daemon._threads if t.is_alive()]
        self.assertEqual(alive, [],
                         f"daemon threads still alive after stop: {alive}")


class MrcDaemonReplyDispatchTests(unittest.TestCase):
    """The dispatcher routes inbound replies into the right agent's
    EVStateTable, mirroring what SenderMrcAgent's removed _reply_rx_loop
    used to do."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mrc-daemon-test-")
        self.sender_report_port = PORTS.take(1)
        self.receiver_probe_port = PORTS.take(1)

        self.sender_xport, self.receiver_xport = _build_loopback_pair(
            sender_report_port=self.sender_report_port,
            receiver_probe_port=self.receiver_probe_port,
        )
        self.daemon = MrcDaemon(
            src_host="green-host00",
            src_id=0,
            flows=[DaemonFlow(tenant="green", dst_id=15)],
            agent_cfg=FAST_CONFIG,
            transport=self.sender_xport,
            snapshot_dir=self.tmpdir,
        )
        only_agent = next(iter(self.daemon.agents.values()))
        self.daemon._demux = {"::1": only_agent}
        self.receiver = ReceiverMrcAgent(
            tenant="green", my_id=15, config=FAST_CONFIG,
            transport=self.receiver_xport,
        )

    def tearDown(self) -> None:
        try:
            self.daemon.stop(timeout_s=0.5)
        finally:
            try:
                self.receiver.stop(timeout_s=0.5)
            except Exception:
                pass
            try:
                snap_subdir = Path(self.tmpdir) / "green-host00"
                if snap_subdir.is_dir():
                    for f in os.listdir(snap_subdir):
                        os.unlink(snap_subdir / f)
                    os.rmdir(snap_subdir)
                os.rmdir(self.tmpdir)
            except OSError:
                pass

    def test_replies_reach_table_via_dispatcher(self) -> None:
        """End-to-end: probe goes out, receiver responds, daemon's
        dispatcher decodes + invokes _handle_probe_reply, RTT lands."""
        self.receiver.start()
        self.daemon.start()

        agent = next(iter(self.daemon.agents.values()))

        def saw_a_reply() -> bool:
            for plane in range(NUM_PLANES):
                for path in range(NUM_SPINES):
                    if agent.table.rtt_p50_ns("green", plane, path) is not None:
                        return True
            return False

        self.assertTrue(_wait_for(saw_a_reply, timeout_s=1.5),
                        "no probe reply landed in EV table within 1.5s "
                        "via daemon dispatcher")

        # Dispatch + match counters must tick in lockstep with successful
        # RTT recording. Pre-fix lab cycle showed thousands of replies
        # at the kernel UDP layer but zero in record_probe_result; these
        # counters localize exactly which transition fails.
        ds = self.daemon._dispatch_counters
        self.assertGreater(ds["replies_received"], 0,
                           "dispatcher recvfrom counter never advanced")
        self.assertGreater(ds["replies_dispatched_probe"], 0,
                           "no reply made it to the 0xA6 dispatch branch")
        self.assertEqual(ds["replies_no_peer"], 0,
                         "loopback peer should always be in _demux")
        self.assertEqual(ds["replies_unknown_magic"], 0,
                         "unexpected unknown-magic count")

        prs = agent.probe_reply_stats
        self.assertGreater(prs["received"], 0,
                           "agent never entered _handle_probe_reply")
        self.assertGreater(prs["matched"], 0,
                           "no reply matched an outstanding probe")
        self.assertEqual(prs["decode_failed"], 0,
                         "decode_probe_reply failed on a valid payload")

        # Reply latency on a loopback transport must land in the
        # fastest bucket. If a future change accidentally swaps
        # clocks (monotonic vs realtime) or stops echoing tx_ns,
        # this asserts catch it before the lab does. Bucketing
        # happens post-decode, so bucket sum == received - decode_failed.
        rlb = agent.reply_latency_buckets
        total_bucketed = sum(rlb.values())
        self.assertEqual(
            total_bucketed, prs["received"] - prs["decode_failed"],
            "bucket sum must equal post-decode reply count",
        )
        self.assertGreater(rlb["lt_50ms"], 0,
                           "loopback replies must land in lt_50ms bucket")
        for slow in ("lt_1s", "lt_5s", "ge_5s"):
            self.assertEqual(
                rlb[slow], 0,
                f"loopback replies should never bucket as {slow}",
            )


class MrcDaemonFinalReportTests(unittest.TestCase):
    """final_report() returns the per-flow shape the orchestrator
    needs for ScenarioReport merging."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mrc-daemon-test-")
        self.sender_report_port = PORTS.take(1)
        self.receiver_probe_port = PORTS.take(1)

        self.sender_xport, self.receiver_xport = _build_loopback_pair(
            sender_report_port=self.sender_report_port,
            receiver_probe_port=self.receiver_probe_port,
        )
        self.daemon = MrcDaemon(
            src_host="green-host00",
            src_id=0,
            flows=[DaemonFlow(tenant="green", dst_id=15)],
            agent_cfg=FAST_CONFIG,
            transport=self.sender_xport,
            snapshot_dir=self.tmpdir,
        )
        only_agent = next(iter(self.daemon.agents.values()))
        self.daemon._demux = {"::1": only_agent}
        self.receiver = ReceiverMrcAgent(
            tenant="green", my_id=15, config=FAST_CONFIG,
            transport=self.receiver_xport,
        )

    def tearDown(self) -> None:
        try:
            self.daemon.stop(timeout_s=0.5)
        finally:
            try:
                self.receiver.stop(timeout_s=0.5)
            except Exception:
                pass
            try:
                snap_subdir = Path(self.tmpdir) / "green-host00"
                if snap_subdir.is_dir():
                    for f in os.listdir(snap_subdir):
                        os.unlink(snap_subdir / f)
                    os.rmdir(snap_subdir)
                os.rmdir(self.tmpdir)
            except OSError:
                pass

    def test_final_report_shape(self) -> None:
        self.receiver.start()
        self.daemon.start()
        # Let some probes complete a round-trip so probe_clock has
        # populated counters.
        time.sleep(FAST_CONFIG.probe_interval_ms * 4 / 1000.0)
        self.daemon.stop(timeout_s=1.0)

        report = self.daemon.final_report()
        self.assertEqual(report["src_host"], "green-host00")
        self.assertEqual(report["src_id"], 0)
        self.assertIn("flows", report)
        self.assertIn("green/15", report["flows"])
        per_flow = report["flows"]["green/15"]
        self.assertIn("ev_state", per_flow)
        self.assertIn("probe_clock", per_flow)
        self.assertIn("loss_fusion", per_flow)

    def test_write_final_report_file_persists_to_disk(self) -> None:
        # The orchestrator retrieves the daemon's final report via
        # `docker exec <host> cat /dev/shm/srv6-mrc/<host>/final_report.json`
        # because docker exec stdout silently drops trailing frames
        # at container exit for large payloads. The daemon MUST
        # write the file before exiting; this test pins that contract.
        self.receiver.start()
        self.daemon.start()
        time.sleep(FAST_CONFIG.probe_interval_ms * 4 / 1000.0)
        self.daemon.stop(timeout_s=1.0)

        path = self.daemon.write_final_report_file()
        # File exists at the path the orchestrator expects.
        self.assertTrue(path.exists(),
                        f"final_report.json missing at {path}")
        self.assertEqual(path.name, "final_report.json")
        # Path is under the per-host snapshot dir (mirrors the
        # `/dev/shm/srv6-mrc/<host>/final_report.json` shape in
        # the lab; here `<host>` is the tmpdir-based dir).
        self.assertEqual(path.parent, self.daemon.snapshot_dir)
        # Content is valid JSON with the same shape as final_report().
        with open(path) as f:
            on_disk = json.load(f)
        self.assertEqual(on_disk["src_host"], "green-host00")
        self.assertIn("flows", on_disk)
        self.assertIn("green/15", on_disk["flows"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
