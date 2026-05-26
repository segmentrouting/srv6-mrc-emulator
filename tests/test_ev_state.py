"""Unit tests for srv6_mrc.mrc.ev_state (stateless-probe sliding window).

Tests are deterministic — no clock, no threads, no sockets. The state
machine is driven entirely by `record_probe_sent` / `record_probe_recv`
/ `tick(tenant)` (the stateless-probe signal path) and
`record_loss_window()` (the receiver-feedback signal path).

The table is per-(tenant, plane, path). Most tests use `num_paths=1`
so they read like single-EV-per-plane tests, and a separate section
exercises multi-path semantics.

Most tests use a tight test config (`_FAST_CFG`: window=2 ticks,
recover=2 ticks) so the bucket math stays small and deterministic.
A couple of tests verify default-config behavior end-to-end.
"""

import threading
import unittest

from srv6_mrc.mrc.ev_state import (
    EVState,
    EVStateConfig,
    EVStateTable,
)


# Tight sliding-window config: 2-bucket window, demote on 1 failing
# window after >=3 sent, recover after 2 consecutive >=90% windows.
# Keeps the bucket math small and human-checkable in tests.
_FAST_CFG = EVStateConfig(
    probe_window_ticks=2,
    probe_min_samples=3,
    probe_fail_ratio=0.5,
    probe_recover_ratio=0.9,
    probe_recover_ticks=2,
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
        cfg=cfg if cfg is not None else _FAST_CFG,
        on_transition=on_transition,
        lock=lock,
    )


def _drive_healthy(
    table: EVStateTable, tenant: str, evs, *,
    ticks: int | None = None,
    per_tick: int = 5,
) -> None:
    """Drive the listed EVs through enough clean windows to fully fill
    the sliding window with healthy buckets, AND then drive the
    recover-ticks latch.

    Total ticks driven = `probe_window_ticks - 1` (to flush any prior
    bad bucket) + `probe_recover_ticks` (to satisfy the latch).
    Override with `ticks=N` to drive an exact number of ticks.
    """
    cfg = table.cfg
    if ticks is None:
        ticks = (cfg.probe_window_ticks - 1) + cfg.probe_recover_ticks
    for _ in range(ticks):
        for plane, path in evs:
            for _ in range(per_tick):
                table.record_probe_sent(tenant, plane, path)
                table.record_probe_recv(tenant, plane, path)
        table.tick(tenant)


def _drive_failing(
    table: EVStateTable, tenant: str, plane: int, path: int, *,
    ticks: int | None = None,
    sent_per_tick: int = 5,
) -> None:
    """Drive ONE EV through enough failing windows to demote.

    Default drives `probe_window_ticks` ticks: enough to fully flush
    the window so the window's ratio is unambiguously 0.0 regardless
    of prior bucket contents.
    """
    if ticks is None:
        ticks = table.cfg.probe_window_ticks
    for _ in range(ticks):
        for _ in range(sent_per_tick):
            table.record_probe_sent(tenant, plane, path)
        table.tick(tenant)


class TestInitialState(unittest.TestCase):
    def test_all_evs_start_unknown(self):
        t = _table(num_planes=4, num_paths=2)
        for tenant in ("green", "yellow"):
            for p in range(4):
                for q in range(2):
                    self.assertIs(t.state(tenant, p, q), EVState.UNKNOWN)

    def test_min_active_default(self):
        t = _table(num_planes=4, num_paths=1)
        self.assertEqual(t.min_active, 2)
        t2 = _table(num_planes=4, num_paths=2)
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
    """Sliding-window probe signal path."""

    def test_demote_on_failing_window(self):
        """Window with sent>=min_samples and recv/sent<fail_ratio demotes."""
        t = _table()
        # Bring planes 0,1,2 (path 0) to GOOD so we have headroom under
        # the min_active=2 floor.
        _drive_healthy(t, "green", [(0, 0), (1, 0), (2, 0)])
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)
        # 5 sent, 0 recv -> ratio 0 < fail_ratio (0.5). One failing
        # window demotes.
        _drive_failing(t, "green", 3, 0, ticks=1, sent_per_tick=5)
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)

    def test_below_min_samples_does_not_demote(self):
        """sent < probe_min_samples: state held even with 0 recv."""
        cfg = EVStateConfig(
            probe_window_ticks=2, probe_min_samples=10,
            probe_recover_ticks=2,
        )
        t = _table(cfg=cfg)
        # Seed 3 planes to GOOD with per_tick=10 (>= min_samples).
        _drive_healthy(t, "green", [(0, 0), (1, 0), (2, 0)],
                       per_tick=10)
        # Drive plane 3 with 2 sent per tick: window total = 4 sent
        # (over 2 buckets) < min_samples=10. State held.
        for _ in range(5):
            for _ in range(2):
                t.record_probe_sent("green", 3, 0)
            t.tick("green")
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)

    def test_recovery_requires_consecutive_healthy_windows(self):
        """ASSUMED_BAD → GOOD requires the window to clear + recover latch.

        With probe_window_ticks=2 and probe_recover_ticks=2:
          - Demote leaves the window full of failing buckets.
          - First healthy tick: window = 1 fail + 1 healthy = blended
            ratio (in-between) -> counter reset.
          - Second healthy tick: window = 2 healthy = ratio 1.0 -> counter=1.
          - Third healthy tick: counter=2 -> promote.
        Total: window_ticks-1 (flush) + recover_ticks (latch) = 3 ticks.
        """
        t = _table()
        _drive_healthy(t, "green", [(0, 0), (1, 0), (2, 0)])
        _drive_failing(t, "green", 3, 0)
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)

        cfg = t.cfg
        flush_ticks = cfg.probe_window_ticks - 1
        recover_ticks = cfg.probe_recover_ticks
        # First (flush + recover-1) healthy ticks: still ASSUMED_BAD.
        _drive_healthy(
            t, "green", [(3, 0)],
            ticks=flush_ticks + recover_ticks - 1,
        )
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)
        # One more healthy tick crosses the latch.
        _drive_healthy(t, "green", [(3, 0)], ticks=1)
        self.assertIs(t.state("green", 3, 0), EVState.GOOD)

    def test_recovery_blocked_by_recent_loss_demote(self):
        cfg = EVStateConfig(
            probe_window_ticks=2, probe_min_samples=3,
            probe_fail_ratio=0.5, probe_recover_ratio=0.9,
            probe_recover_ticks=2,
            loss_threshold=0.05, loss_demote_consecutive=2,
        )
        t = _table(cfg=cfg)
        _drive_healthy(t, "green", [(0, 0), (1, 0), (2, 0)])
        # Loss-feedback path demotes plane 3 path 0.
        t.record_loss_window("green", 3, 0, seen=900, expected=1000)
        t.record_loss_window("green", 3, 0, seen=900, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)
        # Probes go clean for many ticks — but the loss-demote-counter
        # is still non-zero, so recovery must NOT fire.
        _drive_healthy(t, "green", [(3, 0)], ticks=10)
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)
        # A clean loss window resets the loss counter.
        t.record_loss_window("green", 3, 0, seen=1000, expected=1000)
        # Probe path is already clean and consecutive_healthy_windows
        # has been climbing; one more healthy tick crosses the latch
        # now that the loss gate is open.
        _drive_healthy(t, "green", [(3, 0)], ticks=1)
        self.assertIs(t.state("green", 3, 0), EVState.GOOD)

    def test_partial_recv_holds_state(self):
        """recover_ratio > ratio >= fail_ratio: hold (no demote, no recover)."""
        cfg = EVStateConfig(
            probe_fail_ratio=0.5, probe_recover_ratio=0.9,
        )
        t = _table(cfg=cfg)
        _drive_healthy(t, "green", [(0, 0), (1, 0), (2, 0)])
        # Send 10, recv 7 = ratio 0.7. Above fail (0.5) but below
        # recover (0.9). UNKNOWN should NOT promote.
        for _ in range(10):
            for _ in range(10):
                t.record_probe_sent("green", 3, 0)
            for _ in range(7):
                t.record_probe_recv("green", 3, 0)
            t.tick("green")
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)

    def test_no_traffic_no_state_change(self):
        """Tick with no probes leaves state unchanged."""
        t = _table()
        _drive_healthy(t, "green", [(0, 0), (1, 0), (2, 0), (3, 0)])
        for _ in range(10):
            t.tick("green")
        # GOOD stays GOOD even after long quiet period.
        self.assertIs(t.state("green", 3, 0), EVState.GOOD)


class TestLossPath(unittest.TestCase):
    def test_demote_on_three_consecutive_bad_windows(self):
        cfg = EVStateConfig(loss_threshold=0.25, loss_demote_consecutive=3)
        t = _table(cfg=cfg)
        _drive_healthy(t, "green", [(0, 0), (1, 0), (2, 0)])
        # 30% loss > 25% threshold.
        t.record_loss_window("green", 3, 0, seen=700, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)
        t.record_loss_window("green", 3, 0, seen=700, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)
        # Third consecutive bad window -> demote.
        t.record_loss_window("green", 3, 0, seen=700, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.ASSUMED_BAD)

    def test_loss_below_threshold_does_not_demote(self):
        t = _table()
        _drive_healthy(t, "green", [(0, 0), (1, 0), (2, 0)])
        # 1% loss < default threshold (25%). Far under noise floor.
        for _ in range(10):
            t.record_loss_window("green", 3, 0, seen=990, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)

    def test_mild_loss_neither_demotes_nor_clears(self):
        # ratio in (loss_threshold/2, loss_threshold] is ambiguous:
        # neither demote evidence nor recovery evidence.
        cfg = EVStateConfig(loss_threshold=0.25, loss_demote_consecutive=2)
        t = _table(cfg=cfg)
        _drive_healthy(t, "green", [(0, 0), (1, 0), (2, 0)])
        # Prime one bad window:
        t.record_loss_window("green", 3, 0, seen=600, expected=1000)
        # Mild window (20% > threshold/2 = 12.5%, <= threshold 25%):
        # neither demotes nor resets.
        t.record_loss_window("green", 3, 0, seen=800, expected=1000)
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)
        # Bad window again: counter goes 1 -> 2 -> demote.
        t.record_loss_window("green", 3, 0, seen=600, expected=1000)
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
        self.assertIs(t.state("green", 3, 0), EVState.UNKNOWN)

    def test_negative_inputs_rejected(self):
        t = _table()
        with self.assertRaises(ValueError):
            t.record_loss_window("green", 3, 0, seen=-1, expected=10)
        with self.assertRaises(ValueError):
            t.record_loss_window("green", 3, 0, seen=10, expected=-1)


class TestMinActiveFloor(unittest.TestCase):
    def test_floor_suppresses_demote(self):
        # num_paths=1, num_planes=4 -> total = 4, default floor = 2.
        # With 2 already ASSUMED_BAD, the third demote is suppressed.
        t = _table(num_planes=4, num_paths=1)
        _drive_failing(t, "green", 0, 0, ticks=1, sent_per_tick=5)
        _drive_failing(t, "green", 1, 0, ticks=1, sent_per_tick=5)
        self.assertIs(t.state("green", 0, 0), EVState.ASSUMED_BAD)
        self.assertIs(t.state("green", 1, 0), EVState.ASSUMED_BAD)
        # Try to demote (2, 0) — only plane 3 path 0 would remain
        # "usable" (UNKNOWN counts toward usable). That's 1 < floor=2,
        # so suppress.
        _drive_failing(t, "green", 2, 0, ticks=1, sent_per_tick=5)
        self.assertIs(t.state("green", 2, 0), EVState.UNKNOWN)
        snap = t.snapshot()
        ev_2 = next(
            x for x in snap["tenants"]["green"]
            if x["plane"] == 2 and x["path"] == 0
        )
        self.assertGreaterEqual(ev_2["demotes_suppressed_by_floor"], 1)

    def test_floor_suppresses_loss_path_too(self):
        t = _table(num_planes=4, num_paths=1)
        _drive_failing(t, "green", 0, 0, ticks=1, sent_per_tick=5)
        _drive_failing(t, "green", 1, 0, ticks=1, sent_per_tick=5)
        for _ in range(3):
            t.record_loss_window("green", 2, 0, seen=600, expected=1000)
        self.assertIs(t.state("green", 2, 0), EVState.UNKNOWN)

    def test_floor_with_higher_min_active(self):
        cfg = EVStateConfig(min_active_evs=3)
        t = _table(num_planes=4, num_paths=1, cfg=cfg)
        # Demote (0,0). Three usable EVs (1,2,3 path 0) remain = floor met.
        _drive_failing(t, "green", 0, 0, ticks=1, sent_per_tick=5)
        self.assertIs(t.state("green", 0, 0), EVState.ASSUMED_BAD)
        # Demote (1,0). After demote, usable=2 < floor=3 -> suppress.
        _drive_failing(t, "green", 1, 0, ticks=1, sent_per_tick=5)
        self.assertIs(t.state("green", 1, 0), EVState.UNKNOWN)

    def test_floor_counts_evs_not_planes(self):
        # num_planes=2, num_paths=4 -> total = 8, default floor = 4.
        t = _table(num_planes=2, num_paths=4)
        for q in range(4):
            _drive_failing(t, "green", 0, q, ticks=1, sent_per_tick=5)
        for q in range(4):
            self.assertIs(t.state("green", 0, q), EVState.ASSUMED_BAD)
        # Now try to demote (1, 0). Usable_after = 3 < floor=4 -> suppress.
        _drive_failing(t, "green", 1, 0, ticks=1, sent_per_tick=5)
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
        for x in flat:
            self.assertAlmostEqual(x, 1.0 / 8.0)

    def test_good_evs_dominate(self):
        t = _table(num_planes=4, num_paths=1)
        _drive_healthy(t, "green", [(0, 0), (1, 0)])
        # EVs (0,0), (1,0) are GOOD (1.0); (2,0), (3,0) are UNKNOWN
        # (0.5 each). Total = 3.0.
        w = t.weights_ev("green")
        self.assertAlmostEqual(w[0][0], 1.0 / 3.0, places=6)
        self.assertAlmostEqual(w[1][0], 1.0 / 3.0, places=6)
        self.assertAlmostEqual(w[2][0], 1.0 / 6.0, places=6)
        self.assertAlmostEqual(w[3][0], 1.0 / 6.0, places=6)

    def test_bad_evs_get_zero_weight(self):
        t = _table(num_planes=4, num_paths=1)
        _drive_healthy(t, "green", [(0, 0), (1, 0)])
        # Demote (2, 0) — headroom available under floor=2.
        _drive_failing(t, "green", 2, 0, ticks=1, sent_per_tick=5)
        self.assertIs(t.state("green", 2, 0), EVState.ASSUMED_BAD)
        w = t.weights_ev("green")
        self.assertEqual(w[2][0], 0.0)
        flat = [x for row in w for x in row]
        self.assertAlmostEqual(sum(flat), 1.0)

    def test_partial_path_demote_does_not_zero_plane(self):
        t = _table(num_planes=2, num_paths=4)
        _drive_healthy(t, "green", [
            (p, q) for p in range(2) for q in range(4)
        ])
        # Demote (0, 1) only — _drive_failing defaults flush the
        # full window so the post-state is unambiguously ASSUMED_BAD.
        _drive_failing(t, "green", 0, 1)
        self.assertIs(t.state("green", 0, 1), EVState.ASSUMED_BAD)
        w = t.weights_ev("green")
        self.assertEqual(w[0][1], 0.0)
        self.assertGreater(w[0][0], 0.0)
        self.assertGreater(w[0][2], 0.0)
        self.assertGreater(w[0][3], 0.0)

    def test_all_bad_degrades_to_uniform(self):
        # The min_active_evs floor normally prevents this state; cover
        # the safety-net branch directly by mutating internals.
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
        _drive_healthy(t, "green", [(0, 0), (2, 0)])
        self.assertEqual(t.good_evs("green"), frozenset({(0, 0), (2, 0)}))

    def test_good_set_multi_path(self):
        t = _table(num_planes=2, num_paths=3)
        _drive_healthy(t, "green", [(0, 1), (1, 0), (1, 2)])
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
        _drive_healthy(t, "green", [(0, 0)])
        # One transition: UNKNOWN -> GOOD.
        good_events = [e for e in events if e[4] == EVState.GOOD]
        self.assertEqual(len(good_events), 1)
        self.assertEqual(good_events[0][:4],
                         ("green", 0, 0, EVState.UNKNOWN))

    def test_no_transition_no_event(self):
        events = []
        t = _table(on_transition=lambda *a: events.append(a))
        # Below probe_recover_ticks: no transition.
        for _ in range(t.cfg.probe_recover_ticks - 1):
            for _ in range(5):
                t.record_probe_sent("green", 0, 0)
                t.record_probe_recv("green", 0, 0)
            t.tick("green")
        self.assertEqual(events, [])

    def test_callback_carries_path_dimension(self):
        events = []
        t = _table(
            num_planes=2,
            num_paths=2,
            on_transition=lambda *a: events.append(a),
        )
        _drive_healthy(t, "green", [(0, 0), (1, 1)])
        promote_events = [e for e in events if e[4] == EVState.GOOD]
        self.assertEqual(len(promote_events), 2)
        paths_seen = {(ev[1], ev[2]) for ev in promote_events}
        self.assertEqual(paths_seen, {(0, 0), (1, 1)})


class TestSlidingWindowMechanics(unittest.TestCase):
    """Sliding-window internals: bucket rotation and window math."""

    def test_recv_after_send_bucket_rotated(self):
        """Late returning probe (after its send-bucket rotated): still counted in window."""
        cfg = EVStateConfig(probe_window_ticks=3, probe_min_samples=1)
        t = _table(cfg=cfg)
        # Tick 1: send 5, no recv.
        for _ in range(5):
            t.record_probe_sent("green", 0, 0)
        t.tick("green")
        # Tick 2: no send, recv 5 (the late returns from tick 1).
        for _ in range(5):
            t.record_probe_recv("green", 0, 0)
        t.tick("green")
        # Window now spans both ticks: sent=5, recv=5 -> ratio capped
        # at 1.0. Bias is recovery-direction. inspect() exposes both.
        snap = t.inspect("green", 0, 0)
        self.assertEqual(snap["window_sent"], 5)
        self.assertEqual(snap["window_recv"], 5)

    def test_window_size_honored(self):
        """Buckets older than probe_window_ticks fall off the deque."""
        cfg = EVStateConfig(probe_window_ticks=2)
        t = _table(cfg=cfg)
        # Tick 1: send 10, recv 0.
        for _ in range(10):
            t.record_probe_sent("green", 0, 0)
        t.tick("green")
        # Tick 2: send 10, recv 10.
        for _ in range(10):
            t.record_probe_sent("green", 0, 0)
            t.record_probe_recv("green", 0, 0)
        t.tick("green")
        # Tick 3: send 10, recv 10. Tick 1's bucket falls off.
        for _ in range(10):
            t.record_probe_sent("green", 0, 0)
            t.record_probe_recv("green", 0, 0)
        t.tick("green")
        snap = t.inspect("green", 0, 0)
        # Window now = tick2 + tick3 = sent 20, recv 20. Tick 1's
        # bucket (sent 10, recv 0) has rolled out.
        self.assertEqual(snap["window_sent"], 20)
        self.assertEqual(snap["window_recv"], 20)

    def test_total_counters_are_lifetime(self):
        """`total_sent` / `total_recv` accumulate across bucket rotations."""
        cfg = EVStateConfig(probe_window_ticks=2)
        t = _table(cfg=cfg)
        for _ in range(3):
            for _ in range(5):
                t.record_probe_sent("green", 0, 0)
                t.record_probe_recv("green", 0, 0)
            t.tick("green")
        snap = t.inspect("green", 0, 0)
        self.assertEqual(snap["total_sent"], 15)
        self.assertEqual(snap["total_recv"], 15)


class TestSnapshot(unittest.TestCase):
    def test_snapshot_shape(self):
        t = _table(num_planes=4, num_paths=2)
        _drive_healthy(t, "green", [(0, 0)])
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
        self.assertGreaterEqual(ev_00["transitions"], 1)
        # (0,0) is GOOD (1.0); remaining 7 EVs UNKNOWN (0.5 each).
        # Total = 1.0 + 7*0.5 = 4.5; weight of (0,0) = 1/4.5.
        self.assertAlmostEqual(ev_00["weight"], 1.0 / 4.5)
        # Per-EV diagnostic fields surfaced.
        for key in (
            "window_sent", "window_recv", "total_sent", "total_recv",
            "last_probe_ratio", "consecutive_healthy_windows",
            "consecutive_loss_demote_windows", "last_loss_ratio",
            "demotes_suppressed_by_floor",
        ):
            self.assertIn(key, ev_00, f"snapshot missing {key}")

    def test_snapshot_config_block(self):
        t = _table()
        snap = t.snapshot()
        cfg_block = snap["config"]
        for key in (
            "probe_window_ticks", "probe_min_samples",
            "probe_fail_ratio", "probe_recover_ratio",
            "probe_recover_ticks",
            "loss_threshold", "loss_demote_consecutive",
            "min_active_evs",
        ):
            self.assertIn(key, cfg_block, f"config missing {key}")


class TestInputValidation(unittest.TestCase):
    def test_unknown_tenant(self):
        t = _table()
        with self.assertRaises(ValueError):
            t.state("purple", 0, 0)
        with self.assertRaises(ValueError):
            t.record_probe_sent("purple", 0, 0)
        with self.assertRaises(ValueError):
            t.record_probe_recv("purple", 0, 0)
        with self.assertRaises(ValueError):
            t.tick("purple")

    def test_plane_out_of_range(self):
        t = _table(num_planes=4)
        with self.assertRaises(ValueError):
            t.state("green", 4, 0)
        with self.assertRaises(ValueError):
            t.state("green", -1, 0)
        with self.assertRaises(ValueError):
            t.record_probe_sent("green", 4, 0)
        with self.assertRaises(ValueError):
            t.record_probe_recv("green", -1, 0)

    def test_path_out_of_range(self):
        t = _table(num_planes=4, num_paths=2)
        with self.assertRaises(ValueError):
            t.state("green", 0, 2)
        with self.assertRaises(ValueError):
            t.state("green", 0, -1)


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_writes_dont_lose_transitions(self):
        # Smoke test: many threads driving signals shouldn't corrupt
        # the state machine. We drive a failing-window scenario with
        # multiple writer threads and verify the state ends ASSUMED_BAD.
        t = EVStateTable(
            tenants=("green",), num_planes=4, num_paths=1,
            cfg=EVStateConfig(),
        )

        def loop_send(plane):
            for _ in range(100):
                t.record_probe_sent("green", plane, 0)

        # 8 sender threads all driving EV (0, 0) -> 800 sent, 0 recv.
        threads = [
            threading.Thread(target=loop_send, args=(0,))
            for _ in range(8)
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        # One tick rolls those 800 into the window; planes 1/2/3 are
        # still UNKNOWN-usable, so the floor is met and (0,0) demotes.
        t.tick("green")
        self.assertIs(t.state("green", 0, 0), EVState.ASSUMED_BAD)


if __name__ == "__main__":
    unittest.main()
