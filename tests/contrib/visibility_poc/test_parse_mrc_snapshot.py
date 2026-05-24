"""Tests for parse_mrc_snapshot."""
from __future__ import annotations

import copy
import unittest

from . import _path_shim  # noqa: F401
import scraper  # type: ignore[import-not-found]


def _make_envelope() -> dict:
    """Synthesize a snapshot envelope matching tests/test_mrc_daemon.py
    ::test_snapshot_schema shape, including the new reply_latency_buckets
    block introduced in commit 813abcf."""
    return {
        "captured_ns": 1_700_000_000_000_000_000,
        "src_host": "yellow-host00",
        "src_id": 0,
        "dst_id": 7,
        "tenant": "yellow",
        "transport_stats": {"sent_ok": 100, "sent_err": 0},
        "dispatch_stats": {
            "replies_received": 1000,
            "replies_no_peer": 1,
            "replies_unknown_magic": 0,
            "replies_dispatched_probe": 800,
            "replies_dispatched_loss": 199,
        },
        "probe_reply_stats": {
            "received": 800,
            "decode_failed": 0,
            "no_match": 18,
            "matched": 782,
        },
        "reply_latency_buckets": {
            "lt_50ms": 700,
            "lt_200ms": 70,
            "lt_1s": 10,
            "lt_5s": 2,
            "ge_5s": 0,
        },
        "ev_state": {
            "num_planes": 4,
            "num_paths": 4,
            "config": {"min_active_evs": 4},
            "tenants": {
                "yellow": [
                    {
                        "plane": 0, "path": 0, "state": "good",
                        "weight": 0.0625,
                        "rtt_p50_ns": 50_000_000,
                        "consecutive_probe_successes": 10,
                        "consecutive_probe_timeouts": 0,
                        "demotes_suppressed_by_floor": 0,
                    },
                    {
                        "plane": 0, "path": 1, "state": "assumed_bad",
                        "weight": 0.0,
                        "rtt_p50_ns": None,
                        "consecutive_probe_successes": 0,
                        "consecutive_probe_timeouts": 30,
                        "demotes_suppressed_by_floor": 0,
                    },
                    {
                        "plane": 1, "path": 0, "state": "suspect",
                        "weight": 0.0625,
                        "rtt_p50_ns": 75_000_000,
                        "consecutive_probe_successes": 0,
                        "consecutive_probe_timeouts": 2,
                        "demotes_suppressed_by_floor": 5,
                    },
                ]
            },
        },
    }


class ParseMrcSnapshotTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        snap = scraper.parse_mrc_snapshot(_make_envelope())
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.src_host, "yellow-host00")
        self.assertEqual(snap.tenant, "yellow")
        self.assertEqual(snap.dst_id, 7)
        self.assertEqual(snap.captured_ns, 1_700_000_000_000_000_000)
        self.assertEqual(len(snap.ev_records), 3)
        # active = states in {good, suspect, unknown}: 0/0 + 1/0 = 2.
        self.assertEqual(snap.active_evs, 2)

    def test_ev_records_decoded(self) -> None:
        snap = scraper.parse_mrc_snapshot(_make_envelope())
        assert snap is not None
        good = snap.ev_records[0]
        self.assertEqual(good.plane, 0)
        self.assertEqual(good.path, 0)
        self.assertEqual(good.state, "good")
        self.assertAlmostEqual(good.weight, 0.0625)
        self.assertEqual(good.rtt_p50_ns, 50_000_000)
        self.assertEqual(good.consecutive_probe_successes, 10)

        bad = snap.ev_records[1]
        self.assertEqual(bad.state, "assumed_bad")
        self.assertIsNone(bad.rtt_p50_ns)
        self.assertEqual(bad.consecutive_probe_timeouts, 30)

    def test_dispatch_stats(self) -> None:
        snap = scraper.parse_mrc_snapshot(_make_envelope())
        assert snap is not None
        self.assertEqual(snap.dispatch_stats["replies_received"], 1000)
        self.assertEqual(snap.dispatch_stats["replies_dispatched_loss"], 199)

    def test_probe_reply_stats(self) -> None:
        snap = scraper.parse_mrc_snapshot(_make_envelope())
        assert snap is not None
        self.assertEqual(snap.probe_reply_stats["received"], 800)
        self.assertEqual(snap.probe_reply_stats["no_match"], 18)
        self.assertEqual(snap.probe_reply_stats["matched"], 782)

    def test_reply_latency_buckets(self) -> None:
        snap = scraper.parse_mrc_snapshot(_make_envelope())
        assert snap is not None
        self.assertEqual(snap.reply_latency_buckets["lt_50ms"], 700)
        self.assertEqual(snap.reply_latency_buckets["lt_200ms"], 70)
        self.assertEqual(snap.reply_latency_buckets["lt_1s"], 10)
        self.assertEqual(snap.reply_latency_buckets["lt_5s"], 2)
        self.assertEqual(snap.reply_latency_buckets["ge_5s"], 0)

    def test_missing_required_key_returns_none(self) -> None:
        for missing in ("tenant", "src_host", "dst_id", "captured_ns",
                        "ev_state"):
            env = _make_envelope()
            del env[missing]
            self.assertIsNone(
                scraper.parse_mrc_snapshot(env),
                f"expected None when {missing!r} is absent",
            )

    def test_tenant_list_not_a_list_returns_none(self) -> None:
        env = _make_envelope()
        env["ev_state"]["tenants"]["yellow"] = {"not": "a list"}
        self.assertIsNone(scraper.parse_mrc_snapshot(env))

    def test_dispatch_stats_missing_treated_as_empty(self) -> None:
        env = _make_envelope()
        del env["dispatch_stats"]
        snap = scraper.parse_mrc_snapshot(env)
        self.assertIsNotNone(snap)
        assert snap is not None
        self.assertEqual(snap.dispatch_stats, {})

    def test_malformed_ev_row_skipped(self) -> None:
        env = _make_envelope()
        # path missing → row dropped, others survive.
        env["ev_state"]["tenants"]["yellow"].append(
            {"plane": 2, "state": "good"}
        )
        snap = scraper.parse_mrc_snapshot(env)
        assert snap is not None
        self.assertEqual(len(snap.ev_records), 3)

    def test_does_not_mutate_input(self) -> None:
        env = _make_envelope()
        snapshot_before = copy.deepcopy(env)
        scraper.parse_mrc_snapshot(env)
        self.assertEqual(env, snapshot_before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
