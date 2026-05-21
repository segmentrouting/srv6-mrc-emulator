"""Unit tests for srv6_mrc.cli.spray helper functions.

spray.py is mostly a CLI shim, but it has a couple of small data-shaping
helpers that get exercised on every MRC run (they assemble the
`mrc_diag` dict that lands in the JSON scenario report). When those
helpers regress, the JSON report quietly loses fields and the
diagnostics that follow ("how stale are our probe replies?") go dark.

These tests pin the JSON-shape contract for `_probe_clock_stats_to_jsonable`.
"""

from __future__ import annotations

import json
import unittest

from srv6_mrc.cli.spray import _probe_clock_stats_to_jsonable


class ProbeClockStatsToJsonableTests(unittest.TestCase):
    """Tuple-keyed per-EV dicts must become JSON-serializable strings."""

    def test_empty_stats_passes_through(self) -> None:
        out = _probe_clock_stats_to_jsonable({})
        self.assertEqual(out, {})
        # round-trip through json so we know we're really JSON-safe
        json.dumps(out)

    def test_scalar_fields_pass_through(self) -> None:
        out = _probe_clock_stats_to_jsonable({"stale_replies": 42})
        self.assertEqual(out, {"stale_replies": 42})
        json.dumps(out)

    def test_tuple_keyed_dicts_become_string_keyed(self) -> None:
        stats = {
            "emit": {(0, 0): 100, (0, 1): 50, (3, 2): 25},
            "reply": {(0, 0): 90, (0, 1): 50},
            "timeout": {(0, 0): 10},
            "outstanding": {(2, 1): 3},
            "stale_replies": 7,
        }
        out = _probe_clock_stats_to_jsonable(stats)
        self.assertEqual(out["emit"], {"p0s0": 100, "p0s1": 50, "p3s2": 25})
        self.assertEqual(out["reply"], {"p0s0": 90, "p0s1": 50})
        self.assertEqual(out["timeout"], {"p0s0": 10})
        self.assertEqual(out["outstanding"], {"p2s1": 3})
        self.assertEqual(out["stale_replies"], 7)
        # Must round-trip through json
        round_tripped = json.loads(json.dumps(out))
        self.assertEqual(round_tripped, out)

    def test_empty_inner_dicts_pass_through_unchanged(self) -> None:
        """A stats() snapshot from an idle agent has empty per-EV dicts.

        We don't want to misinterpret an empty dict as "tuple-keyed";
        the helper inspects the first key and only re-shapes when it
        sees a tuple. Empty dicts must stay empty (and JSON-safe).
        """
        stats = {
            "emit": {},
            "reply": {},
            "timeout": {},
            "outstanding": {},
            "stale_replies": 0,
        }
        out = _probe_clock_stats_to_jsonable(stats)
        self.assertEqual(out, stats)
        json.dumps(out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
