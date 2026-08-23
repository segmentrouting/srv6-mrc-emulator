"""Tests for srv6_mrc.cli.srctl — the unified control CLI.

These tests exercise the argparse layer + each subcommand's pure
output. The `run` subcommand's actual scenario execution is exercised
by tests/mrc/test_run.py; here we only cover the name-resolution and
dispatch paths (using --list and a stubbed run_main).
"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

from srv6_mrc.cli import srctl


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Invoke srctl.main(argv); return (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = srctl.main(argv)
    return rc, out.getvalue(), err.getvalue()


class TestParseHost(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(srctl._parse_host("green-host00"), ("green", 0))
        self.assertEqual(srctl._parse_host("yellow-host15"), ("yellow", 15))

    def test_malformed_raises(self):
        with self.assertRaises(ValueError):
            srctl._parse_host("greenhost00")
        with self.assertRaises(ValueError):
            srctl._parse_host("green-srv00")

    def test_unknown_tenant_raises(self):
        with self.assertRaises(ValueError):
            srctl._parse_host("purple-host00")

    def test_out_of_range_raises(self):
        with self.assertRaises(ValueError):
            srctl._parse_host("green-host99")


class TestGetTopology(unittest.TestCase):
    def test_table_format_default(self):
        rc, out, _ = _run(["get", "topology"])
        self.assertEqual(rc, 0)
        self.assertIn("planes:", out)
        self.assertIn("tenants:", out)
        # Test suite pins SRV6_TOPO=4p-8x16 via tests/__init__.py:
        self.assertIn("4", out)  # planes
        self.assertIn("8", out)  # spines

    def test_json_format(self):
        rc, out, _ = _run(["get", "topology", "-o", "json"])
        self.assertEqual(rc, 0)
        d = json.loads(out)
        self.assertEqual(d["planes"], 4)
        self.assertEqual(d["spines_per_plane"], 8)
        self.assertEqual(d["leaves_per_plane"], 16)
        self.assertEqual(d["tenants"], ["green", "yellow"])


class TestGetHosts(unittest.TestCase):
    def test_default_lists_both_tenants(self):
        rc, out, _ = _run(["get", "hosts"])
        self.assertEqual(rc, 0)
        # 16 hosts * 2 tenants = 32 data rows + 1 header
        self.assertIn("green-host00", out)
        self.assertIn("yellow-host15", out)
        self.assertIn("HOST", out)
        self.assertIn("INNER_ADDR", out)

    def test_filter_by_tenant(self):
        rc, out, _ = _run(["get", "hosts", "--tenant", "green"])
        self.assertEqual(rc, 0)
        self.assertIn("green-host00", out)
        self.assertNotIn("yellow-host", out)

    def test_unknown_tenant_rejected(self):
        rc, out, err = _run(["get", "hosts", "--tenant", "purple"])
        self.assertEqual(rc, 2)
        self.assertIn("unknown tenant", err)

    def test_json_format(self):
        rc, out, _ = _run(["get", "hosts", "--tenant", "green", "-o", "json"])
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 16)
        # First row should be green-host00, anycast addr ends in :00::2.
        self.assertEqual(rows[0]["host"], "green-host00")
        self.assertEqual(rows[0]["tenant"], "green")
        self.assertIn(":00::2", rows[0]["inner_addr"])


class TestGetEvs(unittest.TestCase):
    def test_default_n_lists_32_evs(self):
        # NUM_PLANES * NUM_SPINES = 4 * 8 = 32 EVs
        rc, out, _ = _run(["get", "evs", "green-host00", "green-host15",
                           "-o", "json"])
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 32)
        # Sorted by (plane, path)
        self.assertEqual(rows[0]["plane"], 0)
        self.assertEqual(rows[0]["path"], 0)
        # Green outer DA: fc00:000<P>:f00<S>:e00<L>:d000::
        self.assertTrue(rows[0]["sid"].startswith("fc00:0000:f000:e00f:d000"))

    def test_yellow_pair_has_e009_in_sid(self):
        rc, out, _ = _run(["get", "evs", "yellow-host00", "yellow-host15",
                           "-o", "json"])
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 32)
        # Yellow outer DA includes the e009 host-decap hop.
        self.assertIn("e009", rows[0]["sid"])
        self.assertIn(":d001::", rows[0]["sid"])

    def test_sid_flag_defaults_to_ua(self):
        rc, out, _ = _run(["get", "evs", "green-host00", "green-host15",
                           "-o", "json"])
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertTrue(rows[0]["sid"].startswith("fc00:0000:f000:e00f:d000"))

    def test_sid_flag_un_uses_node_locators(self):
        rc, out, _ = _run(["get", "evs", "green-host00", "green-host15",
                           "-o", "json", "--sid", "uN"])
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        # plane=0 spine=0 dst_leaf=15(0xf): node locators 1<S>=10, 2<L>=2f.
        self.assertTrue(rows[0]["sid"].startswith("fc00:0000:10:2f:d000"))

    def test_sid_flag_un_in_sid_output_format(self):
        rc, out, _ = _run(["get", "evs", "yellow-host00", "yellow-host15",
                           "-o", "sid", "--sid", "uN"])
        self.assertEqual(rc, 0)
        self.assertIn("e009", out)
        self.assertNotIn("f000", out)

    def test_n_subset_shrinks_grid(self):
        rc, out, _ = _run(["get", "evs", "green-host00", "green-host15",
                           "-n", "4", "-o", "json"])
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        # 4 planes * 4 spines = 16 EVs
        self.assertEqual(len(rows), 16)

    def test_n_out_of_range_rejected(self):
        rc, _, err = _run(["get", "evs", "green-host00", "green-host15",
                           "-n", "99"])
        self.assertEqual(rc, 2)
        self.assertIn("-n must be 1..", err)

    def test_cross_tenant_rejected(self):
        rc, _, err = _run(["get", "evs", "green-host00", "yellow-host15"])
        self.assertEqual(rc, 2)
        self.assertIn("cross-tenant", err)

    def test_malformed_host_rejected(self):
        rc, _, err = _run(["get", "evs", "not-a-host", "green-host15"])
        self.assertEqual(rc, 2)
        self.assertIn("not in form", err)

    def test_sid_format_matches_scenario_printout(self):
        # The -o sid format mimics the indented per-EV listing the
        # ev-spray scenarios print at start-of-run. It groups by spine
        # (Fisher-Yates order from select_spines), not by plane.
        rc, out, _ = _run(["get", "evs", "yellow-host07", "yellow-host08",
                           "-n", "8", "-o", "sid"])
        self.assertEqual(rc, 0)
        self.assertIn("yellow-host07 -> yellow-host08", out)
        self.assertIn("P0:S", out)
        self.assertIn("P3:S", out)
        # 32 EV lines + 1 header = at least 33 lines
        self.assertGreaterEqual(len(out.strip().splitlines()), 33)

    def test_table_columns_present(self):
        rc, out, _ = _run(["get", "evs", "green-host00", "green-host15"])
        self.assertEqual(rc, 0)
        # Header row uses uppercased column names.
        self.assertIn("PLANE", out)
        self.assertIn("PATH", out)
        self.assertIn("EV", out)
        self.assertIn("SID", out)


class TestRun(unittest.TestCase):
    def test_list_prints_scenarios(self):
        rc, out, _ = _run(["run", "--list"])
        self.assertEqual(rc, 0)
        # At least the canonical green-mrc-baseline scenario should be
        # listed; test suite pins SRV6_TOPO=4p-8x16 via tests/__init__.py.
        self.assertIn("green-mrc-baseline", out)
        self.assertIn("yellow-mrc-ev-spray", out)

    def test_unknown_scenario_rejected_with_listing(self):
        rc, _, err = _run(["run", "bogus-scenario"])
        self.assertEqual(rc, 2)
        self.assertIn("not found", err)
        # Available list should be in the error message.
        self.assertIn("green-mrc-baseline", err)

    def test_missing_scenario_arg_errors(self):
        rc, _, err = _run(["run"])
        self.assertEqual(rc, 2)
        self.assertIn("requires <scenario>", err)

    def test_known_scenario_delegates_to_run_main(self):
        # Don't actually execute the scenario — stub out the delegate
        # and confirm srctl forwarded the resolved YAML path + flags.
        with mock.patch("srv6_mrc.mrc.run.main", return_value=0) as m:
            rc, _, _ = _run(["run", "green-mrc-baseline",
                             "--verbose", "--dry-run"])
        self.assertEqual(rc, 0)
        m.assert_called_once()
        argv = m.call_args[0][0]
        self.assertTrue(argv[0].endswith("green-mrc-baseline.yaml"))
        self.assertIn("--verbose", argv)
        self.assertIn("--dry-run", argv)

    def test_duration_flag_forwarded(self):
        # `srctl run <scen> --duration 5s` must forward `--duration 5s`
        # verbatim to run-scenario; the run-scenario layer owns parsing
        # and override semantics (single source of truth).
        with mock.patch("srv6_mrc.mrc.run.main", return_value=0) as m:
            rc, _, _ = _run(["run", "green-mrc-baseline", "--duration", "5s"])
        self.assertEqual(rc, 0)
        argv = m.call_args[0][0]
        self.assertIn("--duration", argv)
        i = argv.index("--duration")
        self.assertEqual(argv[i + 1], "5s")

    def test_duration_flag_absent_when_unset(self):
        with mock.patch("srv6_mrc.mrc.run.main", return_value=0) as m:
            rc, _, _ = _run(["run", "green-mrc-baseline"])
        self.assertEqual(rc, 0)

    def test_sid_flag_forwarded(self):
        with mock.patch("srv6_mrc.mrc.run.main", return_value=0) as m:
            rc, _, _ = _run(["run", "green-mrc-baseline", "--sid", "uN"])
        self.assertEqual(rc, 0)
        argv = m.call_args[0][0]
        self.assertIn("--sid", argv)
        i = argv.index("--sid")
        self.assertEqual(argv[i + 1], "uN")

    def test_sid_flag_absent_when_unset(self):
        with mock.patch("srv6_mrc.mrc.run.main", return_value=0) as m:
            rc, _, _ = _run(["run", "green-mrc-baseline"])
        self.assertEqual(rc, 0)
        argv = m.call_args[0][0]
        self.assertNotIn("--sid", argv)
        argv = m.call_args[0][0]
        self.assertNotIn("--duration", argv)


if __name__ == "__main__":
    unittest.main()
