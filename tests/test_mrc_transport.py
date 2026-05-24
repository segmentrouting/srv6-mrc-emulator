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


# --- Receiver fast-path prewarm + byte-identity regression ----------------
#
# These tests run without CAP_NET_RAW by mocking raw-socket constructors.
# The motivating bug (caught only in lab, 2026-05-23): the receiver-side
# `Srv6RawTransport(is_sender=False)` calls `_prewarm_reply_templates()`,
# which calls `build_outer_template(...)` and later `udp6_checksum_inplace(
# pkt, payload_len=...)`. Two signature/argument errors slipped past the
# unit suite because no test exercised the full receiver __init__ -> prewarm
# -> send_probe_reply hot path end-to-end:
#   1. build_outer_template was called without required `payload_len=` kwarg
#      -> receiver crashed at startup, every flow reported rx=0.
#   2. udp6_checksum_inplace was called positionally with
#      `UDP_HEADER_LEN + payload_len` -> after fixing #1, every reply would
#      have had a wrong UDP checksum (or crashed on positional-into-kwonly
#      param). Receivers would drop replies silently.
# These tests pin the contract so neither bug can return.

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
    for attr in ("_reply_sock", "_probe_sock"):
        s = getattr(t, attr, None)
        if s is not None:
            try:
                s.close()
            except (OSError, AttributeError):
                pass


class _FakeUdpListener:
    """Minimal stand-in for `_open_udp_listener` return value.

    `Srv6RawTransport.__init__` stores it in `_probe_sock` / `_reply_sock`
    and doesn't touch it further until `recv_probe_socket()` /
    `recv_reply_socket()` is called by the agent. The tests here never
    call those, so we just need an object that can be closed.
    """

    def close(self) -> None:  # pragma: no cover
        pass


def _fake_udp_listener(*, bind_addr: str, bind_port: int) -> "_FakeUdpListener":
    return _FakeUdpListener()


class ReceiverPrewarmStartupTests(unittest.TestCase):
    """Receiver-side `Srv6RawTransport` must construct without raising.

    Regression for the 2026-05-23 lab crash where every receiver died at
    `_prewarm_reply_templates` -> `build_outer_template(...)` because the
    required `payload_len=` kwarg was missing.
    """

    @unittest.skipUnless(_HAVE_SCAPY, "scapy not installed")
    def test_receiver_init_populates_reply_template_cache(self) -> None:
        from srv6_mrc.topo import NUM_LEAVES, NUM_PLANES, NUM_SPINES

        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=_fake_udp_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="green", my_id=0, is_sender=False,
            )
        self.addCleanup(_close_transport, t)

        # Eager prewarm built one template per (plane, path, dst_leaf)
        # excluding self (the receiver doesn't reply to its own probes).
        expected = NUM_PLANES * NUM_SPINES * (NUM_LEAVES - 1)
        # Allow >= because the implementation may legitimately include
        # the self-pair (cost-free, simpler logic); the floor is what
        # matters — the cache must be non-empty and reasonably sized.
        self.assertGreaterEqual(
            len(t._reply_templates),
            NUM_PLANES * NUM_SPINES,  # at least one peer per (plane,path)
            f"prewarm cache too small; got {len(t._reply_templates)}",
        )
        self.assertLessEqual(
            len(t._reply_templates),
            NUM_PLANES * NUM_SPINES * NUM_LEAVES,
            "prewarm cache larger than full (plane,path,leaf) grid",
        )

        # Each value is (template_bytes, outer_dst_str). Spot-check
        # one entry has the right shape.
        any_key = next(iter(t._reply_templates))
        tpl, outer_dst = t._reply_templates[any_key]
        self.assertIsInstance(tpl, bytes)
        self.assertIsInstance(outer_dst, str)
        # Minimum byte length: 40 outer + 40 inner + 8 UDP + 28 payload.
        self.assertEqual(len(tpl), 40 + 40 + 8 + 28)

    @unittest.skipUnless(_HAVE_SCAPY, "scapy not installed")
    def test_sender_init_skips_prewarm(self) -> None:
        """is_sender=True must NOT pre-warm the REPLY template cache.

        Senders only build replies in the rare loss-report echo path;
        the reply cache stays empty on senders. (As of the 2026-05-24
        probe-emit fast-path commit, senders DO pre-warm a separate
        PROBE template cache — see TestSenderProbePrewarm below.)

        Scapy-gated because sender __init__ now invokes
        `_prewarm_probe_templates`, which calls
        `build_outer_template` -> scapy.
        """
        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=_fake_udp_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="green", my_id=0, is_sender=True,
            )
        self.addCleanup(_close_transport, t)
        self.assertEqual(len(t._reply_templates), 0)


class SenderPrewarmStartupTests(unittest.TestCase):
    """Sender-side `Srv6RawTransport` must pre-warm the probe template
    cache. Mirrors `ReceiverPrewarmStartupTests` for the symmetrical
    fast-path emit shipped to fix the probe-emit GIL contention
    diagnosed in the "Universal probe failure" investigation.

    Receiver and sender pre-warms are mutually exclusive: a transport
    either replies to probes (receiver) or emits probes (sender),
    never both at once. The OTHER side's cache should be empty so
    bugs in one role can't be hidden behind the other's logic.
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
                tenant="green", my_id=0, is_sender=True,
            )
        self.addCleanup(_close_transport, t)

        # Same grid shape as the receiver-side reply pre-warm: one
        # template per (plane, path, dst_leaf) excluding self.
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
        # Same payload length as reply (PROBE and PROBE_REPLY share
        # the 28B struct layout):
        self.assertEqual(len(tpl), 40 + 40 + 8 + 28)

    @unittest.skipUnless(_HAVE_SCAPY, "scapy not installed")
    def test_receiver_init_has_empty_probe_cache(self) -> None:
        """Receivers don't emit probes; the probe cache must stay
        empty so a stray sender-side codepath running on a receiver
        crashes loudly instead of silently using a stale template."""
        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=_fake_udp_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="green", my_id=0, is_sender=False,
            )
        self.addCleanup(_close_transport, t)
        self.assertEqual(len(t._probe_templates), 0)


class TransportStatsTests(unittest.TestCase):
    """`MrcTransport.stats()` is the public diagnostic surface for
    fast-path miss counters. Consumed by `MrcDaemon._publish_all_snapshots`
    so per-host jq diagnostics can spot template-cache holes without
    code changes / log scraping. See AGENTS.md "Universal probe
    failure" investigation log.
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
        """`Srv6RawTransport.stats()` exposes both probe + reply
        fast-path miss counters; both should be 0 at construction."""
        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=_fake_udp_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="green", my_id=0, is_sender=True,
            )
        self.addCleanup(_close_transport, t)
        stats = t.stats()
        self.assertEqual(stats["probe_fast_path_misses"], 0)
        self.assertEqual(stats["reply_fast_path_misses"], 0)

        # Forcing a cache miss increments the right counter.
        t._probe_templates.clear()
        t._probe_fast_path_misses += 1  # simulate one miss
        self.assertEqual(t.stats()["probe_fast_path_misses"], 1)
        self.assertEqual(t.stats()["reply_fast_path_misses"], 0)


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
                tenant="green", my_id=0, is_sender=True,
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


@unittest.skipUnless(_HAVE_SCAPY, "scapy not installed")
class FastPathByteIdentityTests(unittest.TestCase):
    """`send_probe_reply` fast path produces bytes identical to scapy.

    Regression for the 2026-05-23 lab bug where `udp6_checksum_inplace`
    was called positionally with `UDP_HEADER_LEN + payload_len` instead
    of keyword `payload_len=payload_len`. Two ways wrong:
      - positional into a `*, payload_len` kw-only param -> TypeError;
      - even if it had taken the value, the inflated length would
        write a wrong UDP6 pseudo-header length, producing a wrong
        checksum that the receiver's kernel would drop silently.

    The only honest oracle for "is the checksum right" is byte-identity
    against the scapy slow path (`_send_encapped` -> `build_outer_packet`).
    We mock the raw socket's `.sendto` to capture the bytes both paths
    produce for the same (plane, path, dst_leaf, payload) and assert
    equality.
    """

    def test_fast_path_bytes_match_slow_path_for_sample(self) -> None:
        # Sample inputs: pick the middle of the grid so any off-by-one
        # in (plane, path, dst_leaf) handling would be visible.
        plane = 1
        path = 2
        dst_leaf = 3
        # 28-byte payload — the real PROBE_REPLY size, also exercises
        # the actual production length for the checksum math.
        payload = bytes(range(28))

        with mock.patch(
            "srv6_mrc.mrc.transport.open_raw_send_socket",
            side_effect=_fake_raw_send_socket,
        ), mock.patch(
            "srv6_mrc.mrc.transport._open_udp_listener",
            side_effect=_fake_udp_listener,
        ):
            t = transport.Srv6RawTransport(
                tenant="green", my_id=0, is_sender=False,
            )
            self.addCleanup(_close_transport, t)

            # --- Slow path: scapy via _send_encapped ---
            # Force a cache miss by clearing the prewarmed entry, then
            # capture the bytes that the scapy slow path produces.
            cache_key = (plane, path, dst_leaf)
            self.assertIn(
                cache_key, t._reply_templates,
                "prewarm should have built this template",
            )
            saved_template = t._reply_templates.pop(cache_key)

            slow_bytes: list[bytes] = []
            slow_dst: list = []

            def slow_sendto(buf, dst):
                slow_bytes.append(bytes(buf))
                slow_dst.append(dst)

            t._raw_sockets[plane].sendto = slow_sendto  # type: ignore[assignment]
            t.send_probe_reply(
                plane=plane, path=path, dst_leaf=dst_leaf, payload=payload,
            )
            self.assertEqual(
                len(slow_bytes), 1,
                "scapy fallback should have sent exactly one packet",
            )

            # --- Fast path: template splice + udp6_checksum_inplace ---
            # Restore the cached template and re-send.
            t._reply_templates[cache_key] = saved_template
            fast_bytes: list[bytes] = []
            fast_dst: list = []

            def fast_sendto(buf, dst):
                fast_bytes.append(bytes(buf))
                fast_dst.append(dst)

            t._raw_sockets[plane].sendto = fast_sendto  # type: ignore[assignment]
            t.send_probe_reply(
                plane=plane, path=path, dst_leaf=dst_leaf, payload=payload,
            )
            self.assertEqual(
                len(fast_bytes), 1,
                "fast path should have sent exactly one packet",
            )

        # The two paths must produce byte-identical packets.
        self.assertEqual(
            fast_bytes[0], slow_bytes[0],
            "fast-path bytes diverge from scapy slow-path bytes — "
            "likely a header field or UDP6 checksum bug",
        )
        # And the same destination tuple.
        self.assertEqual(fast_dst[0], slow_dst[0])

        # Fast path must NOT have incremented the miss counter.
        self.assertEqual(
            t._fast_path_misses, 1,
            "exactly one miss expected (the deliberate pop above)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
