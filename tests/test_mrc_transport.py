"""Unit tests for srv6_mrc.mrc.transport socket-buffer behavior.

The MRC probe / probe-reply / loss-report listener socket is the
fan-in chokepoint for the MRC control plane. At 56-sender all-to-all
scale on the lab, the kernel's default UDP rcv buffer
(net.core.rmem_default ~208 KB) is insufficient: silent UDP drops
manifest as probe timeouts and trigger false EV demotes
(unknown -> assumed_bad cascades with probe_timeouts=3,
loss_windows=0). transport._open_udp_listener now bumps SO_RCVBUF to
DEFAULT_RCVBUF_BYTES (16 MB), overridable via SRV6_MRC_RCVBUF_BYTES.

These tests bind real UDP sockets on the loopback address so they
work without CAP_NET_RAW. We pick ephemeral ports via bind(...,0)
elsewhere; here we use a fixed high port and SO_REUSEPORT, closing
sockets in tearDown.
"""

from __future__ import annotations

import os
import socket
import unittest
from unittest import mock

from srv6_mrc.mrc import transport
from srv6_mrc.mrc.transport import (
    DEFAULT_RCVBUF_BYTES,
    _open_udp_listener,
    _rcvbuf_bytes_from_env,
)


# Pick a high port unlikely to collide with anything else in CI.
_TEST_PORT = 49997


class RcvbufEnvTests(unittest.TestCase):
    """_rcvbuf_bytes_from_env honors SRV6_MRC_RCVBUF_BYTES."""

    def test_default_when_env_unset(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SRV6_MRC_RCVBUF_BYTES", None)
            self.assertEqual(_rcvbuf_bytes_from_env(), DEFAULT_RCVBUF_BYTES)

    def test_env_override_positive_int(self) -> None:
        with mock.patch.dict(
            os.environ, {"SRV6_MRC_RCVBUF_BYTES": "1048576"}
        ):
            self.assertEqual(_rcvbuf_bytes_from_env(), 1048576)

    def test_env_override_invalid_falls_back(self) -> None:
        with mock.patch.dict(
            os.environ, {"SRV6_MRC_RCVBUF_BYTES": "not-a-number"}
        ):
            self.assertEqual(_rcvbuf_bytes_from_env(), DEFAULT_RCVBUF_BYTES)

    def test_env_override_non_positive_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"SRV6_MRC_RCVBUF_BYTES": "0"}):
            self.assertEqual(_rcvbuf_bytes_from_env(), DEFAULT_RCVBUF_BYTES)
        with mock.patch.dict(os.environ, {"SRV6_MRC_RCVBUF_BYTES": "-100"}):
            self.assertEqual(_rcvbuf_bytes_from_env(), DEFAULT_RCVBUF_BYTES)


class OpenUdpListenerRcvbufTests(unittest.TestCase):
    """_open_udp_listener applies SO_RCVBUF and binds successfully."""

    def setUp(self) -> None:
        self._socks: list[socket.socket] = []

    def tearDown(self) -> None:
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass

    def _open(self, port: int) -> socket.socket:
        s = _open_udp_listener(bind_addr="::1", bind_port=port)
        self._socks.append(s)
        return s

    def test_listener_grants_at_least_default_rcvbuf(self) -> None:
        """Granted SO_RCVBUF should be >= requested (kernel doubles).

        We don't assert exact equality because Linux silently doubles
        the request internally, and net.core.rmem_max may cap it. We
        only require the granted value to comfortably exceed the
        kernel default of ~208 KB so we know our setsockopt took
        effect to *some* degree.
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SRV6_MRC_RCVBUF_BYTES", None)
            s = self._open(_TEST_PORT)
        granted = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        # 1 MB is well above kernel default rmem_default but below
        # typical rmem_max, so this passes on stock CI runners and on
        # the lab regardless of net.core.rmem_max tuning.
        self.assertGreaterEqual(granted, 1 * 1024 * 1024)

    def test_env_override_changes_requested_size(self) -> None:
        """SRV6_MRC_RCVBUF_BYTES override is read at listener-open time."""
        small = 256 * 1024  # 256 KB; above default but small
        with mock.patch.dict(
            os.environ, {"SRV6_MRC_RCVBUF_BYTES": str(small)}
        ):
            s = self._open(_TEST_PORT + 1)
        granted = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        # Kernel doubles internally, so granted should be ~2*small or
        # the rmem_max cap, whichever is smaller. Either way it must
        # be at least `small` bytes.
        self.assertGreaterEqual(granted, small)

    def test_default_constant_is_sane(self) -> None:
        """DEFAULT_RCVBUF_BYTES is large enough to matter for fan-in."""
        # 4 MB minimum so the constant is always meaningfully above
        # net.core.rmem_default (~208 KB) on stock Linux.
        self.assertGreaterEqual(DEFAULT_RCVBUF_BYTES, 4 * 1024 * 1024)


class TransportExportsTests(unittest.TestCase):
    """DEFAULT_RCVBUF_BYTES is part of the public transport surface."""

    def test_exported_in_all(self) -> None:
        self.assertIn("DEFAULT_RCVBUF_BYTES", transport.__all__)


# --- Receiver-side listener invariants (v4 stateless-probes) -------------
#
# Stateless-probe v4 (2026-05-25) deleted the receiver-side
# `_prewarm_reply_templates` / `send_probe_reply` path entirely — the
# peer host's role in the probe round trip is pure kernel forwarding
# (see invariants 11-14 in AGENTS.md). The class below replaces the
# old `ReceiverPrewarmStartupTests` whose `_reply_templates` assertions
# referenced removed v3 infrastructure.
#
# The bug pinned by these tests: `Srv6RawTransport.__init__` used to
# unconditionally open `(::, SPRAY_REPORT_PORT)` regardless of
# `is_sender`. On every collective-comm scenario (ring, all-to-all,
# any future workload) where one host is BOTH sender and receiver,
# the sender process's `MrcDaemon` already owned that bind; the
# receiver process's `Srv6RawTransport(is_sender=False)` re-bound
# the same `(::, SPRAY_REPORT_PORT)` with SO_REUSEPORT, and the
# kernel hash-steered ~half the returning stateless probes to the
# receiver-process socket — which had no consumer. EVs went
# universally to `assumed_bad` with `window_recv=0`. Lab diagnosis
# 2026-05-25 cycle 14. Fix: gate `_open_udp_listener` on
# `is_sender=True`.


try:
    from scapy.all import IPv6, UDP, Raw  # noqa: F401
    _HAVE_SCAPY = True
except ImportError:  # pragma: no cover
    _HAVE_SCAPY = False


def _fake_raw_send_socket(_iface: str) -> socket.socket:
    """Stand-in for `open_raw_send_socket` that needs no CAP_NET_RAW.

    Returns a closed AF_INET6 SOCK_DGRAM socket — enough for the
    transport's `__init__` to stash by plane. The hot path uses
    `.sendto(...)`, which we don't exercise here; if a test does
    exercise it, it'll get the expected OSError, which is a clear
    signal vs a silent wrong-bytes failure.
    """
    return socket.socket(socket.AF_INET6, socket.SOCK_DGRAM, 0)


def _close_transport(t: "transport.Srv6RawTransport") -> None:
    """Close every socket a `Srv6RawTransport` holds — avoids
    ResourceWarning noise in test output."""
    for s in t._raw_sockets.values():
        try:
            s.close()
        except (OSError, AttributeError):
            pass
    s = getattr(t, "_recv_sock", None)
    if s is not None:
        try:
            s.close()
        except (OSError, AttributeError):
            pass


class _FakeUdpListener:
    """Minimal stand-in for `_open_udp_listener` return value.

    Stateless-probe v4: `Srv6RawTransport.__init__` opens ONE listener
    on (::, SPRAY_REPORT_PORT) for the sender's daemon, and ONLY when
    `is_sender=True`. Tests below assert that constraint; they never
    actually read from this socket.
    """

    def close(self) -> None:  # pragma: no cover
        pass


def _fake_udp_listener(
    *, bind_addr: str, bind_port: int,
    enable_rx_timestamp: bool = False,
) -> "_FakeUdpListener":
    return _FakeUdpListener()


class ReceiverNoReportListenerTests(unittest.TestCase):
    """Receiver-side `Srv6RawTransport(is_sender=False)` MUST NOT open
    the `(::, SPRAY_REPORT_PORT)` listener.

    The MRC daemon (one per host, always `is_sender=True`) is the sole
    authoritative owner of that bind per AGENTS.md "Hard invariant:
    Never bind `(::, SPRAY_REPORT_PORT)` with SO_REUSEPORT more than
    once per host." A double-bind on hosts that are both sender AND
    receiver (every ring / all-to-all scenario) silently kills the
    stateless-probe round trip by hash-steering half the returning
    probes to a socket with no consumer.

    These tests don't need scapy (sender-side prewarm is mocked out
    via raw-socket-constructor patching, but receiver-side prewarm
    is GONE in v4 — there's nothing to skip for).
    """

    def _open_calls(self) -> list:
        """Capture invocations of `_open_udp_listener` for assertion."""
        return []

    def test_receiver_does_not_open_listener(self) -> None:
        calls: list = []

        def tracking_listener(**kw):
            calls.append(kw)
            return _FakeUdpListener()

        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=tracking_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="yellow", my_id=0, is_sender=False,
            )
        self.addCleanup(_close_transport, t)

        self.assertEqual(
            calls, [],
            "is_sender=False must NOT open a UDP listener — the MRC "
            "daemon (is_sender=True, one per host) owns that bind. "
            "See AGENTS.md SO_REUSEPORT cascade gotcha.",
        )
        self.assertIsNone(
            t._recv_sock,
            "receiver-side transport must have _recv_sock=None",
        )

    def test_receiver_recv_socket_raises(self) -> None:
        """Calling `recv_socket()` on a receiver-side transport must
        raise loudly. Any code path that does so on a receiver is a
        bug — receivers only emit loss reports via the per-plane raw
        sockets; they have no inbound demuxed UDP stream."""
        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=_fake_udp_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="yellow", my_id=0, is_sender=False,
            )
        self.addCleanup(_close_transport, t)

        with self.assertRaisesRegex(RuntimeError, "no recv socket"):
            t.recv_socket()

    @unittest.skipUnless(_HAVE_SCAPY, "scapy not installed")
    def test_sender_opens_listener_exactly_once(self) -> None:
        """is_sender=True opens (::, SPRAY_REPORT_PORT) exactly once.
        The sender-process MrcDaemon owns this single bind per host."""
        from srv6_mrc.topo import SPRAY_REPORT_PORT

        calls: list = []

        def tracking_listener(**kw):
            calls.append(kw)
            return _FakeUdpListener()

        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=tracking_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="yellow", my_id=0, is_sender=True,
            )
        self.addCleanup(_close_transport, t)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["bind_addr"], "::")
        self.assertEqual(calls[0]["bind_port"], SPRAY_REPORT_PORT)
        self.assertIsNotNone(t._recv_sock)




class SenderPrewarmStartupTests(unittest.TestCase):
    """Sender-side `Srv6RawTransport` must pre-warm the probe template
    cache. Receivers (v4 stateless-probes) have NO template cache at
    all — their role is loss-report TX only, via `_raw_sockets[plane]`.
    """

    @unittest.skipUnless(_HAVE_SCAPY, "scapy not installed")
    def test_sender_init_populates_probe_template_cache(self) -> None:
        from srv6_mrc.topo import NUM_LEAVES, NUM_PLANES, NUM_SPINES

        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=_fake_udp_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="yellow", my_id=0, is_sender=True,
            )
        self.addCleanup(_close_transport, t)

        # One template per (plane, path, dst_leaf) excluding self.
        self.assertGreaterEqual(
            len(t._probe_templates),
            NUM_PLANES * NUM_SPINES,
            f"probe pre-warm cache too small; "
            f"got {len(t._probe_templates)}",
        )
        self.assertLessEqual(
            len(t._probe_templates),
            NUM_PLANES * NUM_SPINES * NUM_LEAVES,
            "probe pre-warm cache larger than full grid",
        )

        any_key = next(iter(t._probe_templates))
        tpl, outer_dst = t._probe_templates[any_key]
        self.assertIsInstance(tpl, bytes)
        self.assertIsInstance(outer_dst, str)
        # v4 probe payload = PROBE_PAYLOAD_LEN bytes (10 in current
        # wire format; size constant validated via the wire-format
        # tests in test_probe.py).
        from srv6_mrc.mrc.probe import PROBE_PAYLOAD_LEN
        self.assertEqual(len(tpl), 40 + 40 + 8 + PROBE_PAYLOAD_LEN)

    def test_receiver_init_has_empty_probe_cache(self) -> None:
        """Receivers don't emit probes; the probe cache must stay
        empty so a stray sender-side codepath running on a receiver
        crashes loudly instead of silently using a stale template.

        Not scapy-gated: `_prewarm_probe_templates` is only called
        on the sender path (gated by `if is_sender:` at line 208
        of transport.py), so the receiver init never imports scapy.
        """
        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=_fake_udp_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="yellow", my_id=0, is_sender=False,
            )
        self.addCleanup(_close_transport, t)
        self.assertEqual(len(t._probe_templates), 0)


class TransportStatsTests(unittest.TestCase):
    """`MrcTransport.stats()` is the public diagnostic surface for
    the probe fast-path miss counter. Consumed by
    `MrcDaemon._publish_all_snapshots` so per-host jq diagnostics
    can spot template-cache holes without code changes / log scraping.
    """

    def test_base_class_default_returns_empty_dict(self) -> None:
        """The ABC's default `stats()` returns an empty dict so test
        fakes and LoopbackUdpTransport don't have to override."""
        from srv6_mrc.mrc.transport import LoopbackUdpTransport

        # LoopbackUdpTransport doesn't override stats(); it should
        # inherit the base-class empty-dict default.
        t = LoopbackUdpTransport.__new__(LoopbackUdpTransport)
        self.assertEqual(t.stats(), {})

    @unittest.skipUnless(_HAVE_SCAPY, "scapy not installed")
    def test_srv6_raw_transport_exposes_fast_path_misses(self) -> None:
        """`Srv6RawTransport.stats()` exposes the probe fast-path
        miss counter; it should be 0 at construction. (v4 removed
        the reply-side counter along with `send_probe_reply`.)"""
        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=_fake_udp_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="yellow", my_id=0, is_sender=True,
            )
        self.addCleanup(_close_transport, t)
        stats = t.stats()
        self.assertEqual(stats["probe_fast_path_misses"], 0)

        # Forcing a cache miss increments the counter.
        t._probe_templates.clear()
        t._probe_fast_path_misses += 1  # simulate one miss
        self.assertEqual(t.stats()["probe_fast_path_misses"], 1)


@unittest.skipUnless(_HAVE_SCAPY, "scapy not installed")
class ProbeFastPathByteIdentityTests(unittest.TestCase):
    """`send_probe` fast path produces bytes identical to scapy.

    Mirrors `FastPathByteIdentityTests` (which covers send_probe_reply)
    for the sender-side fast-path emit. Pattern is identical: deliberately
    pop the cached template to force the slow path, capture its bytes,
    restore the template, capture the fast-path bytes, assert equality.

    The byte-identity oracle is the only honest check for "is the UDP6
    checksum right" — bugs at this layer are invisible to unit tests
    of the helpers in isolation (see AGENTS.md gotcha: "Every
    byte-template / manual-checksum helper call site must be exercised
    through the public transport API end-to-end").
    """

    def test_fast_path_bytes_match_slow_path_for_sample(self) -> None:
        plane = 1
        path = 2
        dst_leaf = 3
        payload = bytes(range(28))

        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=_fake_udp_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="yellow", my_id=0, is_sender=True,
            )
            self.addCleanup(_close_transport, t)

            cache_key = (plane, path, dst_leaf)
            self.assertIn(
                cache_key, t._probe_templates,
                "sender pre-warm should have built this probe template",
            )
            saved = t._probe_templates.pop(cache_key)

            slow_bytes: list[bytes] = []
            slow_dst: list = []

            def slow_sendto(buf, dst):
                slow_bytes.append(bytes(buf))
                slow_dst.append(dst)

            t._raw_sockets[plane].sendto = slow_sendto  # type: ignore[assignment]
            t.send_probe(
                plane=plane, path=path, dst_leaf=dst_leaf, payload=payload,
            )
            self.assertEqual(len(slow_bytes), 1)

            t._probe_templates[cache_key] = saved
            fast_bytes: list[bytes] = []
            fast_dst: list = []

            def fast_sendto(buf, dst):
                fast_bytes.append(bytes(buf))
                fast_dst.append(dst)

            t._raw_sockets[plane].sendto = fast_sendto  # type: ignore[assignment]
            t.send_probe(
                plane=plane, path=path, dst_leaf=dst_leaf, payload=payload,
            )
            self.assertEqual(len(fast_bytes), 1)

        self.assertEqual(
            fast_bytes[0], slow_bytes[0],
            "probe fast-path bytes diverge from scapy slow-path bytes — "
            "likely a header field or UDP6 checksum bug",
        )
        self.assertEqual(fast_dst[0], slow_dst[0])

        self.assertEqual(
            t._probe_fast_path_misses, 1,
            "exactly one miss expected (the deliberate pop above)",
        )


# (Removed in v4 stateless-probes: FastPathByteIdentityTests used to
# pin `send_probe_reply` byte-identity against scapy. v4 deleted
# `send_probe_reply` and `_reply_templates` entirely — the receiver's
# role in the probe round trip is now pure kernel forwarding, no
# reply emission. `ProbeFastPathByteIdentityTests` above still pins
# byte-identity for the surviving `send_probe` fast path.)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
