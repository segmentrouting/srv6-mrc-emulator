"""Tests for parse_snmp6."""
from __future__ import annotations

import unittest

from . import _path_shim  # noqa: F401
import scraper  # type: ignore[import-not-found]


SAMPLE = """\
Ip6InReceives                   1234
Ip6InHdrErrors                  0
Ip6InDelivers                   1230
Ip6OutRequests                  999
Ip6InDiscards                   1
Udp6InDatagrams                 500
Udp6NoPorts                     2
Udp6InErrors                    0
Udp6OutDatagrams                498
Udp6RcvbufErrors                7
Udp6SndbufErrors                0
Udp6InCsumErrors                0
junk_line_with_no_value
malformed     not_an_int
"""


class ParseSnmp6Tests(unittest.TestCase):
    def test_parses_all_known_keys(self) -> None:
        out = scraper.parse_snmp6(SAMPLE)
        self.assertEqual(out["Ip6InReceives"], 1234)
        self.assertEqual(out["Ip6InDelivers"], 1230)
        self.assertEqual(out["Ip6InDiscards"], 1)
        self.assertEqual(out["Udp6InDatagrams"], 500)
        self.assertEqual(out["Udp6RcvbufErrors"], 7)
        self.assertEqual(out["Udp6InCsumErrors"], 0)

    def test_malformed_lines_skipped(self) -> None:
        out = scraper.parse_snmp6(SAMPLE)
        self.assertNotIn("junk_line_with_no_value", out)
        self.assertNotIn("malformed", out)

    def test_exposed_keys_all_present_in_sample(self) -> None:
        out = scraper.parse_snmp6(SAMPLE)
        for key in scraper.SNMP6_EXPOSED_KEYS:
            self.assertIn(key, out, f"sample is missing exposed key {key}")

    def test_empty_input(self) -> None:
        self.assertEqual(scraper.parse_snmp6(""), {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
