"""Multi-flow demux verification for MrcDaemon (stateless-probe v4).

Under the stateless-probe design the daemon's ``_demux`` is keyed by
``dst_id`` (the peer host id encoded as a single byte in the probe
payload), NOT by IPv6 peer address. The dispatcher hot loop:

  - 0xA5 (PROBE) — extracts ``payload.dst_id``, looks up the matching
    per-flow ``SenderMrcAgent``, calls ``record_probe_recv``.
  - 0xA7 (LOSS_REPORT) — extracts the peer host id from ``peer_addr``
    via ``host_id_from_inner_addr`` and looks up the SAME demux
    (peer_host_id is the sender's dst_id), then calls
    ``_handle_loss_report`` on the matching agent.

These tests verify:
    1. ``_demux`` is keyed by ``dst_id`` (int), with exactly one
       entry per ``(tenant, dst_id)`` flow.
    2. Per-flow ``EVStateTable`` allocation: one table per
       ``(tenant, dst_id)``, never shared across flows. Locks the
       2026-05-24 fix.
    3. Returning-probe dispatch routes to the correct agent based
       on ``payload.dst_id``, even when peer_addr is identical
       across flows (the loopback transport always reports
       ``peer_addr=("::1", ...)``, which now poses no problem
       because demux is payload-driven).
"""
from __future__ import annotations

import socket
import tempfile
import threading
import time
import unittest
from collections import Counter
from typing import Dict, Tuple
from unittest import mock

from srv6_mrc.mrc.agent import AgentConfig
from srv6_mrc.mrc.daemon import DaemonFlow, MrcDaemon
from srv6_mrc.mrc.probe import encode_probe
from srv6_mrc.mrc.transport import LoopbackUdpTransport
from srv6_mrc.topo import (
    NUM_PLANES,
    NUM_SPINES,
    tenant_id as topo_tenant_id,
)


FAST_CONFIG = AgentConfig(
    probe_interval_ms=20,
    loss_window_ms=40,
    max_window_skew_ms=200,
    use_loopback=True,
)


class _PortAllocator:
    def __init__(self, start: int = 32000) -> None:
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


def _build_loopback_transport(*, rx_port: int, peer_rx_port: int):
    return LoopbackUdpTransport(
        is_sender=True,
        per_plane_send_sockets=_make_send_sockets(),
        rx_socket=_make_rx_socket(rx_port),
        peer_rx_port=peer_rx_port,
    )


class _FakeRecvSocket:
    """Canned-recvfrom socket for driving _dispatch_loop directly."""

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


class MrcDaemonDemuxKeysTests(unittest.TestCase):
    """The ``_demux`` dict is keyed by ``dst_id`` (int), per flow."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mrc-multiflow-")
        self.sender_xport = _build_loopback_transport(
            rx_port=PORTS.take(1),
            peer_rx_port=PORTS.take(1),
        )

    def tearDown(self) -> None:
        try:
            self.sender_xport.close()
        except Exception:
            pass

    def _daemon(self, flows):
        return MrcDaemon(
            src_host="green-host00",
            src_id=0,
            flows=flows,
            agent_cfg=FAST_CONFIG,
            transport=self.sender_xport,
            snapshot_dir=self.tmpdir,
        )

    def test_demux_keys_are_dst_ids(self) -> None:
        flows = [
            DaemonFlow(tenant="green", dst_id=15),
            DaemonFlow(tenant="green", dst_id=14),
            DaemonFlow(tenant="green", dst_id=13),
        ]
        d = self._daemon(flows)
        self.assertEqual(len(d._demux), 3)
        for f in flows:
            self.assertIn(f.dst_id, d._demux,
                          f"dst_id={f.dst_id} missing from _demux: "
                          f"keys={sorted(d._demux)}")
            # Key value matches the corresponding (tenant, dst_id) agent.
            self.assertIs(
                d._demux[f.dst_id],
                d.agents[(f.tenant, f.dst_id)],
            )

    def test_demux_keys_distinct_across_dst_ids(self) -> None:
        flows = [
            DaemonFlow(tenant="green", dst_id=d) for d in (10, 11, 12, 13)
        ]
        d = self._daemon(flows)
        keys = list(d._demux.keys())
        self.assertEqual(len(keys), len(set(keys)),
                         f"duplicate demux keys: {Counter(keys)}")
        self.assertEqual(set(keys), {10, 11, 12, 13})

    def test_one_ev_state_table_per_flow_not_per_tenant(self) -> None:
        """Each ``(tenant, dst_id)`` flow gets its own EVStateTable.

        Pre-fix: one ``EVStateTable`` per tenant, shared across all
        flows on a host. Under multi-flow that caused the consecutive-
        success / consecutive-timeout counter pair to be trampled by
        concurrent writers from sibling flows on the same shared
        record. Stateless v4 uses sliding-window ratios instead of
        consecutive counters, but the same sharing hazard would apply
        to the bucket-rotation tick.

        Lock the contract: distinct ``EVStateTable`` instances per
        ``(tenant, dst_id)``, and each agent holds the one matching
        its own flow key.
        """
        flows = [
            DaemonFlow(tenant="green", dst_id=d) for d in (1, 2, 3, 7)
        ]
        d = self._daemon(flows)
        self.assertEqual(len(d.tables), 4)
        self.assertEqual(
            sorted(d.tables.keys()),
            [("green", 1), ("green", 2), ("green", 3), ("green", 7)],
        )
        seen_ids = {id(t) for t in d.tables.values()}
        self.assertEqual(
            len(seen_ids), 4,
            "EVStateTables were aliased across flows",
        )
        for f in flows:
            agent = d.agents[(f.tenant, f.dst_id)]
            self.assertIs(
                agent.table, d.tables[(f.tenant, f.dst_id)],
                f"flow {f} agent table is not its own per-flow table",
            )


class MrcDaemonMultiFlowDispatchTests(unittest.TestCase):
    """Probe payloads route to the correct agent by ``payload.dst_id``."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mrc-multiflow-")
        self.sender_xport = _build_loopback_transport(
            rx_port=PORTS.take(1),
            peer_rx_port=PORTS.take(1),
        )
        self.flows = [
            DaemonFlow(tenant="green", dst_id=15),
            DaemonFlow(tenant="green", dst_id=14),
        ]
        self.daemon = MrcDaemon(
            src_host="green-host00",
            src_id=0,
            flows=self.flows,
            agent_cfg=FAST_CONFIG,
            transport=self.sender_xport,
            snapshot_dir=self.tmpdir,
        )
        # Replace the transport's reply-socket factory with our fake
        # so the dispatcher reads from it.
        self.fake_rx = _FakeRecvSocket()
        self._patch = mock.patch.object(
            self.sender_xport, "recv_socket",
            return_value=self.fake_rx,
        )
        self._patch.start()

        # Wrap each agent's record_probe_recv with a counter.
        self.recv_counts: Counter = Counter()
        self._wrappers = []
        for (tenant, dst_id), agent in self.daemon.agents.items():
            real = agent.record_probe_recv
            key = (tenant, dst_id)

            def make_wrapper(_real, _key):
                def wrapper(plane, path):
                    self.recv_counts[_key] += 1
                    return _real(plane, path)
                return wrapper

            wrapper = make_wrapper(real, key)
            agent.record_probe_recv = wrapper  # type: ignore[assignment]
            self._wrappers.append((agent, real))

    def tearDown(self) -> None:
        try:
            self.daemon.stop(timeout_s=0.5)
        except Exception:
            pass
        try:
            self._patch.stop()
        except Exception:
            pass
        try:
            self.fake_rx.close()
        except Exception:
            pass
        for agent, real in self._wrappers:
            agent.record_probe_recv = real  # type: ignore[assignment]
        try:
            self.sender_xport.close()
        except Exception:
            pass

    def _probe(self, *, dst_id: int, plane: int = 0, path: int = 0) -> bytes:
        return encode_probe(
            plane_id=plane, path_id=path,
            tenant_id=topo_tenant_id("green"),
            src_id=0, dst_id=dst_id,
        )

    def test_probes_route_by_payload_dst_id(self) -> None:
        self.daemon.start()
        n_15, n_14 = 5, 3
        sched = ([15] * n_15) + ([14] * n_14)
        # Interleave so a buggy demux that always picked the first
        # flow would fail visibly rather than coincidentally passing.
        sched = [sched[i] for i in (0, 5, 1, 6, 2, 7, 3, 4)][:n_15 + n_14]
        for dst_id in sched:
            payload = self._probe(dst_id=dst_id)
            # peer_addr is irrelevant for probe dispatch but the loop
            # still passes it through to instrumentation paths.
            self.fake_rx.push(payload, ("::1", 9997))

        deadline = time.monotonic() + 2.0
        while (sum(self.recv_counts.values()) < n_15 + n_14
               and time.monotonic() < deadline):
            time.sleep(0.01)

        self.assertEqual(
            self.recv_counts[("green", 15)], n_15,
            f"flow 15 received wrong count: {dict(self.recv_counts)}",
        )
        self.assertEqual(
            self.recv_counts[("green", 14)], n_14,
            f"flow 14 received wrong count: {dict(self.recv_counts)}",
        )

    def test_unknown_dst_id_dropped_silently(self) -> None:
        """A probe naming a dst_id we have no flow to is dropped.

        Structural guarantee against probes from a peer we just
        stopped talking to being misdelivered to a sibling flow.
        """
        self.daemon.start()
        self.assertNotIn(7, self.daemon._demux)
        payload = self._probe(dst_id=7)
        self.fake_rx.push(payload, ("::1", 9997))
        time.sleep(0.1)
        self.assertEqual(
            sum(self.recv_counts.values()), 0,
            f"unknown-dst_id probe was delivered: "
            f"{dict(self.recv_counts)}",
        )
        self.assertGreaterEqual(
            self.daemon._dispatch_counters["probes_no_flow"], 1,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
