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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
