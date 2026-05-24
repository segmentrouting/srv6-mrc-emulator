"""Tests for parse_ip_link_stats."""
from __future__ import annotations

import unittest

from . import _path_shim  # noqa: F401  -- sys.path injection
import scraper  # type: ignore[import-not-found]


ALPINE_SAMPLE = """\
2: eth1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9000 qdisc noqueue state UP qlen 1000
    link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff
    RX:  bytes packets errors dropped overrun mcast
         12345  678     0      1       0      0
    TX:  bytes packets errors dropped carrier collsns
         23456  789     2      3       0       0
"""

# iproute2 >=5.x adds a `missed` column on RX.
MODERN_SAMPLE = """\
3: eth2: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9000 qdisc noqueue state UP qlen 1000
    link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff
    RX:  bytes packets errors dropped missed mcast
         100    10      0      0       0      0
    TX:  bytes packets errors dropped carrier collsns
         200    20      0      0       0       0
"""

MALFORMED_NO_TX = """\
2: eth1: <UP> mtu 9000
    link/ether aa:bb:cc:dd:ee:ff
    RX:  bytes packets errors dropped overrun mcast
         1      2       3      4       5      6
"""

MALFORMED_VALUE_NOT_INT = """\
2: eth1: <UP> mtu 9000
    link/ether aa:bb:cc:dd:ee:ff
    RX:  bytes packets errors dropped overrun mcast
         not    a       number 4       5      6
    TX:  bytes packets errors dropped carrier collsns
         1      2       3      4       5      6
"""


class ParseIpLinkStatsTests(unittest.TestCase):
    def test_alpine_basic(self) -> None:
        ctrs = scraper.parse_ip_link_stats(ALPINE_SAMPLE)
        self.assertIsNotNone(ctrs)
        assert ctrs is not None
        self.assertEqual(ctrs.rx_bytes, 12345)
        self.assertEqual(ctrs.rx_packets, 678)
        self.assertEqual(ctrs.rx_errors, 0)
        self.assertEqual(ctrs.rx_dropped, 1)
        self.assertEqual(ctrs.tx_bytes, 23456)
        self.assertEqual(ctrs.tx_packets, 789)
        self.assertEqual(ctrs.tx_errors, 2)
        self.assertEqual(ctrs.tx_dropped, 3)

    def test_modern_iproute2_with_missed_column(self) -> None:
        ctrs = scraper.parse_ip_link_stats(MODERN_SAMPLE)
        self.assertIsNotNone(ctrs)
        assert ctrs is not None
        # `missed` is parsed positionally and ignored — bytes/packets/errors/
        # dropped still come back correctly.
        self.assertEqual(ctrs.rx_bytes, 100)
        self.assertEqual(ctrs.rx_packets, 10)
        self.assertEqual(ctrs.tx_bytes, 200)

    def test_missing_tx_block_returns_none(self) -> None:
        self.assertIsNone(scraper.parse_ip_link_stats(MALFORMED_NO_TX))

    def test_non_integer_values_returns_none(self) -> None:
        self.assertIsNone(scraper.parse_ip_link_stats(MALFORMED_VALUE_NOT_INT))

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(scraper.parse_ip_link_stats(""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
