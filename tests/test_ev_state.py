"""Unit tests for srv6_mrc.mrc.ev_state.

Tests are deterministic — no clock, no threads, no sockets. The state
machine is driven entirely by `record_probe_result()` and
`record_loss_window()` calls.

The table is per-(tenant, plane, path). Most tests use `num_paths=1`
so they read like single-EV-per-plane tests (effectively the legacy
shape), and a separate section exercises multi-path semantics.
"""

import threading
import unittest

from srv6_mrc.mrc.ev_state import (
    EVState,
    EVStateConfig,
    EVStateTable,
)


def _table(
    num_planes: int = 4,
    num_paths: int = 1,
    tenants=("green", "yellow"),
    cfg: EVStateConfig | None = None,
    on_transition=None,
    lock=None,                # single-threaded tests
) -> EVStateTable:
    return EVStateTable(
        tenants=tenants,
        num_planes=num_planes,
        num_paths=num_paths,
        cfg=cfg or EVStateConfig(),
        on_transition=on_transition,
        lock=lock,
    )


def _seed_good(table: EVStateTable, tenant: str, evs):
    """Drive an iterable of (plane, path) to GOOD via 5 probe successes."""
    for plane, path in evs:
        for _ in range(5):
            table.record_probe_result(
                tenant, plane, path, success=True, rtt_ns=1_000_000,
            )


class TestInitialState(unittest.TestCase):
    def test_all_evs_start_unknown(self):
        t = _table(num_planes=4, num_paths=2)
        for tenant in ("green", "yellow"):
            for p in range(4):
                for q in range(2):
                    self.assertIs(t.state(tenant, p, q), EVState.UNKNOWN)

    def test_min_active_default(self):
        t = _table(num_planes=4, num_paths=1)
        # default = max(1, 4 // 2) = 2
        self.assertEqual(t.min_active, 2)
        t2 = _table(num_planes=4, num_paths=2)
        # default = max(1, 8 // 2) = 4
        self.assertEqual(t2.min_active, 4)
        t3 = _table(num_planes=2, num_paths=1)
        self.assertEqual(t3.min_active, 1)

    def test_min_active_explicit(self):
        cfg = EVStateConfig(min_active_evs=3)
        t = _table(num_planes=4, num_paths=1, cfg=cfg)
        self.assertEqual(t.min_active, 3)

    def test_min_active_clamps_to_total_evs(self):
        cfg = EVStateConfig(min_active_evs=99)
        t = _table(num_planes=4, num_paths=2, cfg=cfg)
        # clamp at num_planes * num_paths = 8
        self.assertEqual(t.min_active, 8)

    def test_empty_tenants_rejected(self):
        with self.assertRaises(ValueError):
            EVStateTable(tenants=(), num_planes=4, num_paths=1, lock=None)

    def test_zero_planes_rejected(self):
        with self.assertRaises(ValueError):
            EVStateTable(
                tenants=("green",), num_planes=0, num_paths=1, lock=None,
            )

    def test_zero_paths_rejected(self):
        with self.assertRaises(ValueError):
            EVStateTable(
                tenants=("green",), num_planes=4, num_paths=0, lock=None,
            )


class TestProbePath(unittest.TestCase):
    """Single-path-per-plane: legacy-shaped tests."""

    def test_demote_after_threshold_consecutive_timeouts(self):
        t = _table()
        # Bring planes 0,1,2 to GOOD first so we have headroom under the
        # min_active=2 floor.
        _seed_good(t, "green", [(0, 0), (1, 0), (2, 0)])
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)
        # 2 timeouts -> still UNKNOWN
        t.record_probe_result("green", 3, 0, success=False)
        t.record_probe_result("green", 3, 0, success=False)
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)
        # 3rd timeout -> ASSUMED_BAD
        t.record_probe_result("green", 3, 0, success=False)
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)

    def test_one_success_resets_timeout_counter(self):
        t = _table()
        _seed_good(t, "green", [(0, 0), (1, 0), (2, 0)])
        t.record_probe_result("green", 3, 0, success=False)
        t.record_probe_result("green", 3, 0, success=False)
        t.record_probe_result(
            "green", 3, 0, success=True, rtt_ns=1_000_000,
        )
        # Two more timeouts shouldn't demote (counter was reset).
        t.record_probe_result("green", 3, 0, success=False)
        t.record_probe_result("green", 3, 0, success=False)
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)

    def test_recovery_requires_consecutive_successes(self):
        t = _table()
        _seed_good(t, "green", [(0, 0), (1, 0), (2, 0)])
        for _ in range(3):
            t.record_probe_result("green", 3, 0, success=False)
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)
        # 4 successes — not enough (threshold=5).
        for _ in range(4):
            t.record_probe_result(
                "green", 3, 0, success=True, rtt_ns=1_000_000,
            )
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)
        # 5th tips it.
        t.record_probe_result(
            "green", 3, 0, success=True, rtt_ns=1_000_000,
        )
        self.assertIs(t.state("green", 3, 0), EVState.GOOD)

    def test_recovery_blocked_by_recent_loss_demote(self):
        # Use explicit threshold/consecutive so this test stays valid
        # if defaults change. 5% threshold + 2 consecutive matches the
        # historical demote behavior tested below.
        cfg = EVStateConfig(loss_threshold=0.05, loss_demote_consecutive=2)
        t = _table(cfg=cfg)
        _seed_good(t, "green", [(0, 0), (1, 0), (2, 0)])
        # Loss-feedback path demotes plane 3 path 0.
        t.record_loss_window("green", 3, 0, seen=900, expected=1000)
        t.record_loss_window("green", 3, 0, seen=900, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)
        # Probes pass cleanly but loss-demote-counter is still non-zero,
        # so recovery must NOT fire.
        for _ in range(10):
            t.record_probe_result(
                "green", 3, 0, success=True, rtt_ns=1_000_000,
            )
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)
        # Now a clean loss window: zeros the loss counter.
        t.record_loss_window("green", 3, 0, seen=1000, expected=1000)
        # The next probe success crosses the gate (we already had 10
        # in a row).
        t.record_probe_result(
            "green", 3, 0, success=True, rtt_ns=1_000_000,
        )
        self.assertIs(t.state("green", 3, 0), EVState.GOOD)

    def test_success_requires_rtt(self):
        t = _table()
        with self.assertRaises(ValueError):
            t.record_probe_result("green", 0, 0, success=True, rtt_ns=None)

    def test_negative_rtt_rejected(self):
        t = _table()
        with self.assertRaises(ValueError):
            t.record_probe_result("green", 0, 0, success=True, rtt_ns=-1)


class TestLossPath(unittest.TestCase):
    def test_demote_on_two_consecutive_bad_windows(self):
        # Pin threshold=0.05 + consecutive=2 so the test is independent
        # of default tuning.
        cfg = EVStateConfig(loss_threshold=0.05, loss_demote_consecutive=2)
        t = _table(cfg=cfg)
        _seed_good(t, "green", [(0, 0), (1, 0), (2, 0)])
        # 10% loss > 5% threshold; one window not enough.
        t.record_loss_window("green", 3, 0, seen=900, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)
        # Second consecutive bad window -> demote.
        t.record_loss_window("green", 3, 0, seen=900, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)

    def test_loss_below_threshold_does_not_demote(self):
        t = _table()
        _seed_good(t, "green", [(0, 0), (1, 0), (2, 0)])
        # 1% loss < 5% threshold AND < threshold/2 = 2.5% (so this counts
        # as a quiet window).
        for _ in range(10):
            t.record_loss_window("green", 3, 0, seen=990, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)

    def test_mild_loss_neither_demotes_nor_clears(self):
        # ratio in (loss_threshold/2, loss_threshold] = (2.5%, 5%]:
        # ambiguous — neither demote evidence nor recovery evidence.
        # Pin threshold=0.05 + consecutive=2 for default-independence.
        cfg = EVStateConfig(loss_threshold=0.05, loss_demote_consecutive=2)
        t = _table(cfg=cfg)
        _seed_good(t, "green", [(0, 0), (1, 0), (2, 0)])
        # Prime one bad window:
        t.record_loss_window("green", 3, 0, seen=900, expected=1000)
        # Mild window: counter neither increments nor resets.
        t.record_loss_window("green", 3, 0, seen=970, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)
        # Bad window again: total bad windows = 2 -> demote.
        t.record_loss_window("green", 3, 0, seen=900, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)

    def test_expected_zero_is_noop(self):
        t = _table()
        t.record_loss_window("green", 3, 0, seen=0, expected=0)
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)

    def test_seen_exceeds_expected_clamped(self):
        # Late reorders can produce seen > expected. We clamp to expected
        # so ratio is 0 (quiet) rather than raising.
        t = _table()
        t.record_loss_window("green", 3, 0, seen=1100, expected=1000)
        # No exception; state still UNKNOWN; treated as fully-clean window.
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)

    def test_negative_inputs_rejected(self):
        t = _table()
        with self.assertRaises(ValueError):
            t.record_loss_window("green", 3, 0, seen=-1, expected=10)
        with self.assertRaises(ValueError):
            t.record_loss_window("green", 3, 0, seen=10, expected=-1)


class TestMinActiveFloor(unittest.TestCase):
    def test_floor_suppresses_demote(self):
        # num_paths=1, num_planes=4 -> total EVs = 4, default floor = 2.
        # With 2 already ASSUMED_BAD, the third demote is suppressed.
        t = _table(num_planes=4, num_paths=1)
        # Demote planes 0 and 1 (path 0) via probe timeouts.
        for p in (0, 1):
            for _ in range(3):
                t.record_probe_result("green", p, 0, success=False)
        self.assertIs(t.state("green", 0, 0), EVState.ASSUMED_BAD)
        self.assertIs(t.state("green", 1, 0), EVState.ASSUMED_BAD)
        # Try to demote plane 2 — only plane 3 would remain "usable"
        # (UNKNOWN counts toward usable). That's 1 < min_active=2, so
        # suppress.
        for _ in range(3):
            t.record_probe_result("green", 2, 0, success=False)
        self.assertIs(t.state("green", 2, 0), EVState.UNKNOWN)
        snap = t.snapshot()
        ev_2 = next(
            x for x in snap["tenants"]["green"]
            if x["plane"] == 2 and x["path"] == 0
        )
        self.assertGreaterEqual(ev_2["demotes_suppressed_by_floor"], 1)

    def test_floor_suppresses_loss_path_too(self):
        t = _table(num_planes=4, num_paths=1)
        for p in (0, 1):
            for _ in range(3):
                t.record_probe_result("green", p, 0, success=False)
        for _ in range(2):
            t.record_loss_window("green", 2, 0, seen=900, expected=1000)
        self.assertIs(t.state("green", 2, 0), EVState.UNKNOWN)

    def test_floor_with_higher_min_active(self):
        cfg = EVStateConfig(min_active_evs=3)
        t = _table(num_planes=4, num_paths=1, cfg=cfg)
        # Demote (0,0). Three usable EVs (1,2,3 path 0) remain = floor met.
        for _ in range(3):
            t.record_probe_result("green", 0, 0, success=False)
        self.assertIs(t.state("green", 0, 0), EVState.ASSUMED_BAD)
        # Demote (1,0). After demote, usable=2 < floor=3 -> suppress.
        for _ in range(3):
            t.record_probe_result("green", 1, 0, success=False)
        self.assertIs(t.state("green", 1, 0), EVState.UNKNOWN)

    def test_floor_counts_evs_not_planes(self):
        # num_planes=2, num_paths=4 -> total = 8, default floor = 4.
        # Demote 4 EVs on plane 0 (all paths). Usable_after = 4 (plane 1),
        # meeting floor=4 only because plane 1 is fully usable.
        # The fifth demote (first path on plane 1) would leave usable=3 -> suppress.
        t = _table(num_planes=2, num_paths=4)
        for q in range(4):
            for _ in range(3):
                t.record_probe_result("green", 0, q, success=False)
        for q in range(4):
            self.assertIs(t.state("green", 0, q), EVState.ASSUMED_BAD)
        # Now try to demote (1, 0). Usable_after = 3 < floor=4 -> suppress.
        for _ in range(3):
            t.record_probe_result("green", 1, 0, success=False)
        self.assertIs(t.state("green", 1, 0), EVState.UNKNOWN)


class TestWeightsEv(unittest.TestCase):
    def test_initial_weights_sum_to_one(self):
        t = _table(num_planes=4, num_paths=2)
        w = t.weights_ev("green")
        self.assertEqual(len(w), 4)
        for row in w:
            self.assertEqual(len(row), 2)
        flat = [x for row in w for x in row]
        self.assertAlmostEqual(sum(flat), 1.0)
        # All UNKNOWN -> uniform across 8 cells.
        for x in flat:
            self.assertAlmostEqual(x, 1.0 / 8.0)

    def test_good_evs_dominate(self):
        t = _table(num_planes=4, num_paths=1)
        _seed_good(t, "green", [(0, 0), (1, 0)])
        # EVs (0,0) and (1,0) are GOOD (1.0 each); (2,0) and (3,0) are
        # UNKNOWN (0.5 each). Total = 3.0.
        w = t.weights_ev("green")
        self.assertAlmostEqual(w[0][0], 1.0 / 3.0, places=6)
        self.assertAlmostEqual(w[1][0], 1.0 / 3.0, places=6)
        self.assertAlmostEqual(w[2][0], 1.0 / 6.0, places=6)
        self.assertAlmostEqual(w[3][0], 1.0 / 6.0, places=6)

    def test_bad_evs_get_zero_weight(self):
        t = _table(num_planes=4, num_paths=1)
        _seed_good(t, "green", [(0, 0), (1, 0)])
        # Demote (2, 0) — headroom available under floor=2.
        for _ in range(3):
            t.record_probe_result("green", 2, 0, success=False)
        self.assertIs(t.state("green", 2, 0), EVState.ASSUMED_BAD)
        w = t.weights_ev("green")
        self.assertEqual(w[2][0], 0.0)
        flat = [x for row in w for x in row]
        self.assertAlmostEqual(sum(flat), 1.0)

    def test_partial_path_demote_does_not_zero_plane(self):
        # Multi-path demonstration: a degraded path on plane 0 shouldn't
        # zero out the whole plane.
        t = _table(num_planes=2, num_paths=4)
        # Bring everything to GOOD so we have headroom (8 EVs, floor=4).
        _seed_good(t, "green", [
            (p, q) for p in range(2) for q in range(4)
        ])
        # Demote (0, 1) only.
        for _ in range(3):
            t.record_probe_result("green", 0, 1, success=False)
        self.assertIs(t.state("green", 0, 1), EVState.ASSUMED_BAD)
        # Other paths on plane 0 still GOOD and weighted.
        w = t.weights_ev("green")
        self.assertEqual(w[0][1], 0.0)
        self.assertGreater(w[0][0], 0.0)
        self.assertGreater(w[0][2], 0.0)
        self.assertGreater(w[0][3], 0.0)

    def test_all_bad_degrades_to_uniform(self):
        # The min_active_evs floor normally prevents this state, so we
        # cover the safety-net branch directly by mutating internals.
        t = _table(num_planes=4, num_paths=2)
        for tenant_grid in t._evs.values():    # noqa: SLF001
            for row in tenant_grid:
                for rec in row:
                    rec.state = EVState.ASSUMED_BAD
        t._rebuild_weights_locked("green")     # noqa: SLF001
        w = t.weights_ev("green")
        flat = [x for row in w for x in row]
        for x in flat:
            self.assertAlmostEqual(x, 1.0 / 8.0)


class TestGoodEvs(unittest.TestCase):
    def test_good_set_single_path(self):
        t = _table(num_planes=4, num_paths=1)
        _seed_good(t, "green", [(0, 0), (2, 0)])
        self.assertEqual(t.good_evs("green"), frozenset({(0, 0), (2, 0)}))

    def test_good_set_multi_path(self):
        t = _table(num_planes=2, num_paths=3)
        _seed_good(t, "green", [(0, 1), (1, 0), (1, 2)])
        self.assertEqual(
            t.good_evs("green"),
            frozenset({(0, 1), (1, 0), (1, 2)}),
        )


class TestCallbacks(unittest.TestCase):
    def test_on_transition_fires_with_old_and_new(self):
        events = []
        t = _table(
            num_planes=4,
            num_paths=1,
            on_transition=lambda tenant, plane, path, old, new:
                events.append((tenant, plane, path, old, new)),
        )
        _seed_good(t, "green", [(0, 0)])
        self.assertEqual(
            events,
            [("green", 0, 0, EVState.UNKNOWN, EVState.GOOD)],
        )

    def test_no_transition_no_event(self):
        events = []
        t = _table(on_transition=lambda *a: events.append(a))
        # Below threshold -> no transition.
        t.record_probe_result("green", 0, 0, success=True, rtt_ns=1_000_000)
        t.record_probe_result("green", 0, 0, success=True, rtt_ns=1_000_000)
        self.assertEqual(events, [])

    def test_callback_carries_path_dimension(self):
        # Multi-path: callback must distinguish (plane, path) pairs.
        events = []
        t = _table(
            num_planes=2,
            num_paths=2,
            on_transition=lambda *a: events.append(a),
        )
        _seed_good(t, "green", [(0, 0), (1, 1)])
        self.assertEqual(len(events), 2)
        paths_seen = {(ev[1], ev[2]) for ev in events}
        self.assertEqual(paths_seen, {(0, 0), (1, 1)})


class TestRttRing(unittest.TestCase):
    def test_p50_and_p99(self):
        t = _table()
        for rtt in [1_000_000, 2_000_000, 3_000_000, 4_000_000, 5_000_000]:
            t.record_probe_result(
                "green", 0, 0, success=True, rtt_ns=rtt,
            )
        self.assertEqual(t.rtt_p50_ns("green", 0, 0), 3_000_000)
        self.assertEqual(t.rtt_p99_ns("green", 0, 0), 5_000_000)

    def test_none_when_empty(self):
        t = _table()
        self.assertIsNone(t.rtt_p50_ns("green", 0, 0))
        self.assertIsNone(t.rtt_p99_ns("green", 0, 0))

    def test_ring_bounded(self):
        cfg = EVStateConfig(rtt_ring_size=4)
        t = _table(cfg=cfg)
        for i in range(100):
            t.record_probe_result("green", 0, 0, success=True, rtt_ns=i)
        # Only the last 4 samples remain.
        ring = t._evs["green"][0][0].rtt_ring_ns        # noqa: SLF001
        self.assertEqual(list(ring), [96, 97, 98, 99])

    def test_rings_independent_per_path(self):
        t = _table(num_planes=1, num_paths=2)
        t.record_probe_result("green", 0, 0, success=True, rtt_ns=100)
        t.record_probe_result("green", 0, 1, success=True, rtt_ns=200)
        self.assertEqual(t.rtt_p50_ns("green", 0, 0), 100)
        self.assertEqual(t.rtt_p50_ns("green", 0, 1), 200)


class TestSnapshot(unittest.TestCase):
    def test_snapshot_shape(self):
        t = _table(num_planes=4, num_paths=2)
        _seed_good(t, "green", [(0, 0)])
        snap = t.snapshot()
        self.assertIn("config", snap)
        self.assertEqual(snap["num_planes"], 4)
        self.assertEqual(snap["num_paths"], 2)
        self.assertIn("tenants", snap)
        self.assertIn("green", snap["tenants"])
        # Flat list of 4 * 2 = 8 EV records per tenant.
        self.assertEqual(len(snap["tenants"]["green"]), 8)
        ev_00 = next(
            x for x in snap["tenants"]["green"]
            if x["plane"] == 0 and x["path"] == 0
        )
        self.assertEqual(ev_00["state"], "good")
        self.assertEqual(ev_00["transitions"], 1)
        # (0,0) is GOOD (1.0); remaining 7 EVs are UNKNOWN (0.5 each).
        # total = 1.0 + 7*0.5 = 4.5; weight of (0,0) = 1.0 / 4.5
        self.assertAlmostEqual(ev_00["weight"], 1.0 / 4.5)


class TestInputValidation(unittest.TestCase):
    def test_unknown_tenant(self):
        t = _table()
        with self.assertRaises(ValueError):
            t.state("purple", 0, 0)

    def test_plane_out_of_range(self):
        t = _table(num_planes=4)
        with self.assertRaises(ValueError):
            t.state("green", 4, 0)
        with self.assertRaises(ValueError):
            t.state("green", -1, 0)

    def test_path_out_of_range(self):
        t = _table(num_planes=4, num_paths=2)
        with self.assertRaises(ValueError):
            t.state("green", 0, 2)
        with self.assertRaises(ValueError):
            t.state("green", 0, -1)


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_writes_dont_lose_transitions(self):
        # Smoke test: many threads pounding the same EV shouldn't
        # corrupt the state machine.
        t = EVStateTable(
            tenants=("green",), num_planes=4, num_paths=1,
            cfg=EVStateConfig(),
        )

        def loop_demote(plane):
            for _ in range(100):
                t.record_probe_result("green", plane, 0, success=False)

        threads = [
            threading.Thread(target=loop_demote, args=(0,))
            for _ in range(8)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        # EV (0, 0) must have ended up ASSUMED_BAD (800 timeouts under
        # min_active=2 with planes 1,2,3 still UNKNOWN-usable).
        self.assertIs(t.state("green", 0, 0), EVState.ASSUMED_BAD)


if __name__ == "__main__":
    unittest.main()
