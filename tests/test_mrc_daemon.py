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


class _FakeRecvSocket:
    """Canned-recvfrom socket for driving the daemon's _dispatch_loop
    directly. Mirrors test_mrc_daemon_multiflow._FakeRecvSocket."""

    def __init__(self) -> None:
        self._queue: list = []
        self._cond = threading.Condition()
        self._closed = False

    def push(self, payload: bytes, peer: Tuple[str, int]) -> None:
        with self._cond:
            self._queue.append((payload, peer))
            self._cond.notify()

    def recvfrom(self, bufsize: int):  # noqa: ARG002
        with self._cond:
            while not self._queue and not self._closed:
                self._cond.wait(timeout=0.01)
                if not self._queue and not self._closed:
                    raise socket.timeout()
            if self._closed and not self._queue:
                raise OSError("fake socket closed")
            return self._queue.pop(0)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


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
            "packets_received", "probes_dispatched", "probes_no_flow",
            "probes_decode_failed", "probes_ev_mismatch",
            "loss_reports_dispatched", "loss_reports_no_peer",
            "unknown_magic",
        ):
            self.assertIn(key, ds, f"missing dispatch_stats key {key}")
            self.assertIsInstance(ds[key], int)
            self.assertGreaterEqual(ds[key], 0)

        # probe_emit_buckets is per-flow (per agent), measures the
        # wall-clock cost of one transport.send_probe() call. Six-bucket
        # schema.
        self.assertIn("probe_emit_buckets", payload)
        peb = payload["probe_emit_buckets"]
        self.assertIsInstance(peb, dict)
        for key in (
            "lt_100us", "lt_1ms", "lt_10ms",
            "lt_100ms", "lt_1s", "ge_1s",
        ):
            self.assertIn(key, peb, f"missing probe_emit_buckets key {key}")
            self.assertIsInstance(peb[key], int)
            self.assertGreaterEqual(peb[key], 0)

        # dispatch_rx_gap_buckets / dispatch_rx_backlog_buckets are
        # the sender-RX counterpart to the receiver's
        # probe_rx_gap_buckets / probe_rx_backlog_buckets. Lock both
        # 5-key schemas so lab jq queries can rely on them without
        # `// 0` guarding.
        self.assertIn("dispatch_rx_gap_buckets", payload)
        gb = payload["dispatch_rx_gap_buckets"]
        self.assertIsInstance(gb, dict)
        for key in ("lt_1ms", "lt_10ms", "lt_100ms", "lt_1s", "ge_1s"):
            self.assertIn(key, gb,
                          f"missing dispatch_rx_gap_buckets key {key}")
            self.assertIsInstance(gb[key], int)
            self.assertGreaterEqual(gb[key], 0)

        self.assertIn("dispatch_rx_backlog_buckets", payload)
        bb = payload["dispatch_rx_backlog_buckets"]
        self.assertIsInstance(bb, dict)
        for key in ("inq_0", "le_512", "le_4k", "le_32k", "gt_32k"):
            self.assertIn(key, bb,
                          f"missing dispatch_rx_backlog_buckets key {key}")
            self.assertIsInstance(bb[key], int)
            self.assertGreaterEqual(bb[key], 0)

        # kernel_rx_dwell_buckets — kernel CLOCK_REALTIME at packet
        # ingress (SO_TIMESTAMPNS) vs userland CLOCK_REALTIME after
        # recvmsg returns. Locks the 7-key schema. The `negative` and
        # `no_timestamp` keys are diagnostic-only:
        #   - `negative`: system clock stepped backward mid-run
        #     (manual `date`, NTP step). Should be 0 in lab runs.
        #   - `no_timestamp`: SO_TIMESTAMPNS unavailable or the cmsg
        #     wasn't returned (test fakes without recvmsg, older
        #     kernels). All packets count here in the test fake path.
        self.assertIn("kernel_rx_dwell_buckets", payload)
        kd = payload["kernel_rx_dwell_buckets"]
        self.assertIsInstance(kd, dict)
        for key in (
            "lt_1ms", "lt_10ms", "lt_100ms", "lt_1s", "ge_1s",
            "negative", "no_timestamp",
        ):
            self.assertIn(key, kd,
                          f"missing kernel_rx_dwell_buckets key {key}")
            self.assertIsInstance(kd[key], int)
            self.assertGreaterEqual(kd[key], 0)

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
    """The dispatcher routes inbound stateless probes (magic 0xA5)
    into the right per-flow EVStateTable via record_probe_recv."""

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
        # Patch recv_socket with a fake so we can inject probe bytes
        # directly. The real lab uses kernel-turnaround of stateless
        # probes; the loopback transport doesn't model that.
        from unittest import mock
        self.fake_rx = _FakeRecvSocket()
        self._patch = mock.patch.object(
            self.sender_xport, "recv_socket",
            return_value=self.fake_rx,
        )
        self._patch.start()

    def tearDown(self) -> None:
        try:
            self._patch.stop()
        except Exception:
            pass
        try:
            self.fake_rx.close()
        except Exception:
            pass
        try:
            self.daemon.stop(timeout_s=0.5)
        finally:
            try:
                self.receiver_xport.close()
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
        """Stateless probes injected at the dispatcher's recv socket
        route through record_probe_recv into the per-flow EV table."""
        from srv6_mrc.mrc.probe import encode_probe
        self.daemon.start()

        agent = next(iter(self.daemon.agents.values()))
        tid = topo_tenant_id("green")
        # Inject probes spanning a few EVs.
        for plane in range(NUM_PLANES):
            for path in range(NUM_SPINES):
                payload = encode_probe(
                    plane_id=plane, path_id=path,
                    tenant_id=tid, src_id=0, dst_id=15,
                )
                self.fake_rx.push(payload, ("::1", 1234))

        def saw_a_recv() -> bool:
            snap = agent.table.snapshot()
            for rec in snap["tenants"]["green"]:
                if rec["window_recv"] > 0 or rec["total_recv"] > 0:
                    return True
            return False

        self.assertTrue(_wait_for(saw_a_recv, timeout_s=1.5),
                        "no probe recv landed in EV table within 1.5s "
                        "via daemon dispatcher")

        ds = self.daemon._dispatch_counters
        self.assertGreater(ds["packets_received"], 0,
                           "dispatcher recvfrom counter never advanced")
        self.assertGreater(ds["probes_dispatched"], 0,
                           "no probe made it to the 0xA5 dispatch branch")
        self.assertEqual(ds["unknown_magic"], 0,
                         "unexpected unknown-magic count")
        self.assertEqual(ds["probes_decode_failed"], 0,
                         "decode_probe failed on a valid payload")
        self.assertEqual(ds["probes_no_flow"], 0,
                         "all probes targeted the only configured flow")


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


class MrcDaemonDispatchRxBucketsTests(unittest.TestCase):
    """The dispatcher records (a) inter-recvfrom gap and (b) SIOCINQ
    backlog buckets per packet so the lab can localize whether the
    multi-second reply-latency tail observed in cycle 7 lives in the
    sender's recv path (we're starved) or upstream (wire/receiver).

    The targeted assertion is the post-condition: after pushing N
    replies through a loopback-backed daemon, bucket counts sum to
    expected values regardless of which buckets win. macOS without
    termios.FIONREAD degrades to backlog all-zero (no-op) and gap
    still populated; we model that explicitly by monkeypatching
    `daemon._HAVE_SIOCINQ`.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mrc-daemon-rx-bkt-")
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
        # Outbound sender used to push synthetic replies into the
        # daemon's reply socket. Bound to ::1 so peer_addr will
        # match _demux's "::1" key.
        self.injector = socket.socket(
            socket.AF_INET6, socket.SOCK_DGRAM, 0)
        self.injector.bind(("::1", 0))

    def tearDown(self) -> None:
        try:
            self.injector.close()
        except OSError:
            pass
        try:
            self.daemon.stop(timeout_s=0.5)
        finally:
            try:
                self.receiver_xport.close()
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

    def _inject(self, n: int, *, magic: int = 0xA5) -> None:
        # Build a valid 10-byte v4 PROBE so the dispatcher counts it
        # as probes_dispatched (not probes_decode_failed) — bucketing
        # happens BEFORE decode either way, so the dwell/gap/backlog
        # buckets fire identically.
        from srv6_mrc.mrc.probe import encode_probe
        tid = topo_tenant_id("green")
        body = encode_probe(
            plane_id=0, path_id=0, tenant_id=tid, src_id=0, dst_id=15,
        )
        if magic != 0xA5:
            body = bytes([magic]) + body[1:]
        for _ in range(n):
            self.injector.sendto(
                body, ("::1", self.sender_report_port))

    def test_buckets_populate_and_sum_to_n(self) -> None:
        self.daemon.start()
        N = 20
        self._inject(N)
        ds = self.daemon._dispatch_counters
        self.assertTrue(
            _wait_for(lambda: ds["packets_received"] >= N, timeout_s=1.5),
            f"dispatcher only received {ds['packets_received']}/{N}",
        )
        # Gap bucket skips the first recv (no prior baseline), so
        # gap-bucket sum is N-1. Backlog bucket fires on every recv
        # (no skip), so sum is N — BUT only if FIONREAD is available;
        # mirror the runtime check.
        from srv6_mrc.mrc import daemon as daemon_mod
        gap_total = sum(self.daemon._dispatch_rx_gap_buckets.values())
        self.assertEqual(
            gap_total, ds["packets_received"] - 1,
            f"gap bucket sum {gap_total} != recv-1 "
            f"({ds['packets_received']-1})",
        )
        backlog_total = sum(
            self.daemon._dispatch_rx_backlog_buckets.values())
        if daemon_mod._HAVE_SIOCINQ:
            self.assertEqual(
                backlog_total, ds["packets_received"],
                f"backlog bucket sum {backlog_total} != recv "
                f"({ds['packets_received']})",
            )
        else:
            self.assertEqual(
                backlog_total, 0,
                "FIONREAD unavailable: backlog buckets must stay zero",
            )

    def test_macos_degradation_backlog_zero_gap_populated(self) -> None:
        # Force the macOS no-op path even on Linux CI so the contract
        # is exercised on every platform. Patching the module-level
        # flag is sufficient — `_dispatch_loop` reads it once per
        # iteration via the import-time `_HAVE_SIOCINQ` name.
        from srv6_mrc.mrc import daemon as daemon_mod
        saved_have = daemon_mod._HAVE_SIOCINQ
        saved_inq = daemon_mod._SIOCINQ
        daemon_mod._HAVE_SIOCINQ = False
        daemon_mod._SIOCINQ = None
        try:
            self.daemon.start()
            N = 12
            self._inject(N)
            ds = self.daemon._dispatch_counters
            self.assertTrue(
                _wait_for(
                    lambda: ds["packets_received"] >= N, timeout_s=1.5),
                f"dispatcher only received {ds['packets_received']}/{N}",
            )
            # Gap path is OS-independent — must still bucket.
            self.assertEqual(
                sum(self.daemon._dispatch_rx_gap_buckets.values()),
                ds["packets_received"] - 1,
            )
            # Backlog must be all-zero on the degraded path.
            self.assertEqual(
                sum(self.daemon._dispatch_rx_backlog_buckets.values()),
                0,
                "macOS-degraded path must not populate backlog buckets",
            )
        finally:
            daemon_mod._HAVE_SIOCINQ = saved_have
            daemon_mod._SIOCINQ = saved_inq

    def test_kernel_rx_dwell_sums_to_recv_count(self) -> None:
        """Every recv increments EXACTLY one kernel_rx_dwell bucket.

        On Linux with SO_TIMESTAMPNS available, packets land in one of
        the dwell buckets (`lt_1ms` / `lt_10ms` / ...). On macOS or
        kernels without SO_TIMESTAMPNS, every packet lands in
        `no_timestamp`. Either way: sum(buckets) == packets_received,
        no double-counts and no skips. If we lose this invariant we
        lose the ability to trust the dwell counters at all in lab
        diagnostics.
        """
        self.daemon.start()
        N = 16
        self._inject(N)
        ds = self.daemon._dispatch_counters
        self.assertTrue(
            _wait_for(lambda: ds["packets_received"] >= N, timeout_s=1.5),
            f"dispatcher only received {ds['packets_received']}/{N}",
        )
        dwell_total = sum(self.daemon._kernel_rx_dwell_buckets.values())
        self.assertEqual(
            dwell_total, ds["packets_received"],
            f"kernel_rx_dwell bucket sum {dwell_total} != recv "
            f"({ds['packets_received']})",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()