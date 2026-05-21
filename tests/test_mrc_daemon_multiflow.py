"""Step (c): multi-flow demux verification for MrcDaemon.

The single-flow daemon tests in test_mrc_daemon.py work around
LoopbackUdpTransport's invariant that every recv reports
peer_addr=("::1", ...) by re-keying _demux to {"::1": only_agent}.
That's fine for proving the dispatch -> handler pipeline, but it
sidesteps the actual demux dict — which is what cures the SO_REUSEPORT
reply-misdelivery cascade at all-to-all scale.

This file proves that:
    1. The daemon's _demux is keyed correctly: one entry per
       (tenant, dst_id) flow, keyed on inner_addr(tenant, dst_id).
    2. Inbound replies arriving with peer_addr matching flow A's
       inner-anycast are dispatched to flow A's agent and never to
       flow B's, even when both flows are active.

We test (2) without raw sockets by replacing the daemon's
`transport.recv_reply_socket()` with a fake socket whose recvfrom()
returns canned (payload, peer_addr) tuples. The dispatcher loop then
runs its real lookup logic against the real _demux dict.
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
from srv6_mrc.mrc.probe import encode_probe_reply
from srv6_mrc.mrc.transport import LoopbackUdpTransport
from srv6_mrc.topo import (
    NUM_PLANES,
    NUM_SPINES,
    inner_addr,
    tenant_id as topo_tenant_id,
)


FAST_CONFIG = AgentConfig(
    probe_interval_ms=20,
    probe_timeout_ms=40,
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
    """Stand-in for a UDP socket that yields canned recvfrom results.

    Used by MrcDaemonMultiFlowDispatchTests to drive the daemon's
    dispatcher loop with synthetic (payload, peer_addr) pairs whose
    peer_addr fields differ -- something LoopbackUdpTransport cannot
    do (it always reports peer_addr=("::1", port)).
    """

    def __init__(self) -> None:
        self._queue: list = []
        self._cond = threading.Condition()
        self._closed = False

    def push(self, payload: bytes, peer: Tuple[str, int]) -> None:
        with self._cond:
            self._queue.append((payload, peer))
            self._cond.notify()

    def recvfrom(self, bufsize: int):  # noqa: ARG002 — mimics socket API
        with self._cond:
            while not self._queue and not self._closed:
                # Mimic a short-timeout socket so the dispatcher's
                # outer loop can periodically check self._stop.
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
    """The _demux dict is keyed by inner_addr(tenant, dst_id).

    Pure construction-time test -- no threads started. Just verifies
    that the daemon's setup wired the right peer-address keys to the
    right per-flow agents. This is the "did we actually build the
    routing table correctly" sanity check that single-flow tests can't
    exercise.
    """

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

    def test_demux_keys_match_inner_anycast_per_flow(self) -> None:
        flows = [
            DaemonFlow(tenant="green", dst_id=15),
            DaemonFlow(tenant="green", dst_id=14),
            DaemonFlow(tenant="green", dst_id=13),
        ]
        d = MrcDaemon(
            src_host="green-host00",
            src_id=0,
            flows=flows,
            agent_cfg=FAST_CONFIG,
            transport=self.sender_xport,
            snapshot_dir=self.tmpdir,
        )
        # One demux entry per flow. No collisions.
        self.assertEqual(len(d._demux), 3)
        for f in flows:
            key = inner_addr(f.tenant, f.dst_id)
            self.assertIn(key, d._demux,
                          f"flow {f} missing from _demux: keys={list(d._demux)}")
            # And the agent under that key is the same as the one
            # held under the (tenant, dst_id) tuple.
            self.assertIs(d._demux[key], d.agents[(f.tenant, f.dst_id)])

    def test_demux_keys_distinct_across_dst_ids(self) -> None:
        # The original SO_REUSEPORT cascade was that *all* per-process
        # daemons heard each other's replies. Here we verify the
        # in-process demux at least gives every (tenant, dst_id) a
        # distinct key -- so misrouting between flows on the same host
        # is structurally impossible.
        flows = [
            DaemonFlow(tenant="green", dst_id=d) for d in (10, 11, 12, 13)
        ]
        d = MrcDaemon(
            src_host="green-host00",
            src_id=0,
            flows=flows,
            agent_cfg=FAST_CONFIG,
            transport=self.sender_xport,
            snapshot_dir=self.tmpdir,
        )
        keys = list(d._demux.keys())
        self.assertEqual(len(keys), len(set(keys)),
                         f"duplicate demux keys: {Counter(keys)}")


class MrcDaemonMultiFlowDispatchTests(unittest.TestCase):
    """Dispatched replies route to the right flow's agent.

    Drives the daemon's _dispatch_loop with synthesized recvfrom()
    tuples whose peer_addr differs per flow, then verifies each agent
    saw exactly the replies addressed to its peer.
    """

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
        # so the dispatcher reads from it. This is what lets us inject
        # custom peer_addr values per packet.
        self.fake_rx = _FakeRecvSocket()
        self._patch = mock.patch.object(
            self.sender_xport, "recv_reply_socket",
            return_value=self.fake_rx,
        )
        self._patch.start()

        # Wrap each agent's _handle_probe_reply with a counter so we
        # can assert WHICH agent got each dispatched packet. We pass
        # through to the real handler so the agent's internal state
        # behaves normally.
        self.recv_counts: Counter = Counter()
        self._wrappers = []
        for (tenant, dst_id), agent in self.daemon.agents.items():
            real = agent._handle_probe_reply
            key = (tenant, dst_id)

            def make_wrapper(_real, _key):
                def wrapper(payload):
                    self.recv_counts[_key] += 1
                    return _real(payload)
                return wrapper

            wrapper = make_wrapper(real, key)
            agent._handle_probe_reply = wrapper  # type: ignore[assignment]
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
            agent._handle_probe_reply = real  # type: ignore[assignment]
        try:
            self.sender_xport.close()
        except Exception:
            pass

    def _build_reply(self, *, tenant: str, src_id: int, reply_port: int = 9997,
                    plane: int = 0, path: int = 0,
                    req_id: int = 0xBEEF) -> bytes:
        """Synthesize a probe-reply payload as an EV would have sent.

        Field values are arbitrary except `tenant_id`/`src_id`, which
        the receiving agent uses to validate the reply is "for it".
        We don't need a matching outstanding req_id for this test
        because we're asserting on which agent's _handle_probe_reply
        was invoked, not on RTT-table effects (an unmatched reply
        increments stale_replies and returns).
        """
        return encode_probe_reply(
            req_id=req_id, plane_id=plane, path_id=path,
            tx_ns=time.monotonic_ns(), svc_time_ns=0,
            tenant_id=topo_tenant_id(tenant), src_id=src_id,
            reply_port=reply_port,
        )

    def test_replies_route_by_peer_addr(self) -> None:
        # Start the daemon. Probe-emit threads run, snapshot publisher
        # runs, and -- crucially -- the dispatcher reads from our
        # fake_rx instead of the real socket.
        self.daemon.start()

        # The two peers' inner-anycast addresses. These are the
        # peer_addr[0] values the dispatcher will look up in _demux.
        peer_15 = inner_addr("green", 15)
        peer_14 = inner_addr("green", 14)

        # Push 5 replies for flow (green,15) and 3 replies for
        # flow (green,14), interleaved.
        n_15, n_14 = 5, 3
        sched = ([(peer_15, 15)] * n_15) + ([(peer_14, 14)] * n_14)
        # Interleave so a buggy demux that always picked, say, the
        # first flow would fail visibly rather than coincidentally
        # passing on the count-only check.
        sched = [
            sched[i] for i in (0, 5, 1, 6, 2, 7, 3, 4)
        ][:n_15 + n_14]

        for peer_addr, peer_dst_id in sched:
            payload = self._build_reply(
                tenant="green", src_id=0,
            )
            self.fake_rx.push(payload, (peer_addr, 9997))

        # Wait for the dispatcher to drain the queue. The fake socket
        # blocks until the queue is non-empty; the dispatcher pops one
        # at a time. Total expected calls = n_15 + n_14.
        deadline = time.monotonic() + 2.0
        while (sum(self.recv_counts.values()) < n_15 + n_14
               and time.monotonic() < deadline):
            time.sleep(0.01)

        self.assertEqual(self.recv_counts[("green", 15)], n_15,
                         f"flow 15 received wrong count: {dict(self.recv_counts)}")
        self.assertEqual(self.recv_counts[("green", 14)], n_14,
                         f"flow 14 received wrong count: {dict(self.recv_counts)}")

    def test_unknown_peer_dropped_silently(self) -> None:
        # A reply from a peer we don't have a flow to (e.g. a probe
        # crossing in flight from a host we just stopped talking to)
        # must be silently dropped, NOT delivered to a random agent.
        # This is the structural guarantee that makes the daemon
        # immune to the SO_REUSEPORT misdelivery class.
        self.daemon.start()
        unknown_peer = inner_addr("green", 7)  # no flow for dst_id=7
        self.assertNotIn(unknown_peer, self.daemon._demux)

        payload = self._build_reply(tenant="green", src_id=0)
        self.fake_rx.push(payload, (unknown_peer, 9997))

        # Give the dispatcher a moment to consume + drop.
        time.sleep(0.1)

        self.assertEqual(sum(self.recv_counts.values()), 0,
                         f"unknown-peer reply was delivered: "
                         f"{dict(self.recv_counts)}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
