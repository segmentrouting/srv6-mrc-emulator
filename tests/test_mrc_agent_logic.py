"""Unit tests for the pure-logic pieces of the MRC agent.

Covers:
  - srv6_fabric.mrc.probe_clock.ProbeClock
  - srv6_fabric.mrc.loss_window.LossWindowTable
  - srv6_fabric.mrc.loss_compute.{SentWindow, SentWindowRing,
    compute_loss_ratio, apply_loss_report}

These modules are sockets-free; they're the inner loop that the
commit-2b agent.py I/O layer wraps. Testing them in isolation gives us
a deterministic regression net before threading + sockets enter the
picture.
"""

import unittest

from srv6_fabric.mrc.ev_state import EVState, EVStateConfig, EVStateTable
from srv6_fabric.mrc.loss_compute import (
    LossFusionStats,
    SentWindow,
    SentWindowRing,
    apply_loss_report,
    compute_loss_ratio,
)
from srv6_fabric.mrc.loss_window import LossWindowTable
from srv6_fabric.mrc.probe import LossReport, PlaneLossRecord
from srv6_fabric.mrc.probe_clock import ProbeClock


# ---------------------------------------------------------------------------
# ProbeClock
# ---------------------------------------------------------------------------

class TestProbeClockEmit(unittest.TestCase):
    def test_emit_returns_monotonic_req_ids_per_plane(self):
        c = ProbeClock(num_planes=4, probe_timeout_ns=50_000_000)
        ids_p0 = [c.emit(0, now_ns=t)[0] for t in range(0, 5)]
        ids_p1 = [c.emit(1, now_ns=t)[0] for t in range(0, 5)]
        # Per-plane ids are independent; both start at 0.
        self.assertEqual(ids_p0, [0, 1, 2, 3, 4])
        self.assertEqual(ids_p1, [0, 1, 2, 3, 4])

    def test_emit_records_outstanding(self):
        c = ProbeClock(num_planes=2, probe_timeout_ns=1_000)
        c.emit(0, now_ns=10)
        c.emit(0, now_ns=20)
        c.emit(1, now_ns=30)
        self.assertEqual(c.outstanding(0), 2)
        self.assertEqual(c.outstanding(1), 1)

    def test_req_id_wraps_at_u16(self):
        # Exhaust the u16 space on plane 0; next id should wrap to 0.
        c = ProbeClock(
            num_planes=1, probe_timeout_ns=1,
            max_outstanding_per_plane=1,  # force LRU eviction so we don't OOM
        )
        for t in range(0x10001):
            req_id, _ = c.emit(0, now_ns=t)
        # After wrapping, the very next emit hands out 1 (we just used
        # 0 on the wrap-around iteration).
        next_id, _ = c.emit(0, now_ns=0x10001)
        self.assertEqual(next_id, 1)


class TestProbeClockMatchReply(unittest.TestCase):
    def test_match_returns_rtt(self):
        c = ProbeClock(num_planes=2, probe_timeout_ns=1_000_000_000)
        req_id, tx_ns = c.emit(0, now_ns=1_000)
        rtt = c.match_reply(
            req_id=req_id, plane=0, reply_tx_ns=tx_ns, now_ns=1_500,
        )
        self.assertEqual(rtt, 500)
        # Outstanding count drops back to zero after match.
        self.assertEqual(c.outstanding(0), 0)

    def test_unknown_req_id_is_stale(self):
        c = ProbeClock(num_planes=1, probe_timeout_ns=1_000_000)
        rtt = c.match_reply(req_id=42, plane=0, reply_tx_ns=0, now_ns=100)
        self.assertIsNone(rtt)
        self.assertEqual(c.stats()["stale_replies"], 1)

    def test_wrong_plane_is_stale(self):
        # Probe emitted on plane 0; reply claims plane 1. We never
        # emitted req_id=0 on plane 1, so this is stale on plane 1
        # AND the original is still outstanding on plane 0.
        c = ProbeClock(num_planes=2, probe_timeout_ns=1_000_000)
        req_id, tx_ns = c.emit(0, now_ns=10)
        rtt = c.match_reply(
            req_id=req_id, plane=1, reply_tx_ns=tx_ns, now_ns=20,
        )
        self.assertIsNone(rtt)
        self.assertEqual(c.outstanding(0), 1)
        self.assertEqual(c.stats()["stale_replies"], 1)

    def test_mismatched_tx_ns_is_stale(self):
        # Reply with same req_id but different tx_ns indicates a
        # post-wrap collision (different probe, same id). Reject.
        c = ProbeClock(num_planes=1, probe_timeout_ns=1_000_000)
        req_id, tx_ns = c.emit(0, now_ns=100)
        rtt = c.match_reply(
            req_id=req_id, plane=0, reply_tx_ns=tx_ns + 1, now_ns=200,
        )
        self.assertIsNone(rtt)
        # Original entry is preserved (we didn't pop it).
        self.assertEqual(c.outstanding(0), 1)


class TestProbeClockSweepTimeouts(unittest.TestCase):
    def test_sweep_returns_old_outstanding(self):
        c = ProbeClock(num_planes=2, probe_timeout_ns=100)
        c.emit(0, now_ns=0)
        c.emit(0, now_ns=50)
        c.emit(1, now_ns=200)
        # At now_ns=150, deadline = 50; entries with tx_ns <= 50 are
        # stale: both p0 entries (0 and 50). p1's 200 is fresh.
        timed_out = c.sweep_timeouts(now_ns=150)
        # The exact req_ids depend on emit order.
        self.assertEqual(sorted(t[0] for t in timed_out), [0, 0])
        # Both planes' entries that timed out are p0.
        self.assertTrue(all(plane == 0 for plane, _ in timed_out))
        self.assertEqual(c.outstanding(0), 0)
        self.assertEqual(c.outstanding(1), 1)

    def test_sweep_idempotent(self):
        c = ProbeClock(num_planes=1, probe_timeout_ns=10)
        c.emit(0, now_ns=0)
        first = c.sweep_timeouts(now_ns=100)
        second = c.sweep_timeouts(now_ns=200)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_timeout_count_tracks_sweep(self):
        c = ProbeClock(num_planes=1, probe_timeout_ns=10)
        c.emit(0, now_ns=0)
        c.emit(0, now_ns=5)
        c.sweep_timeouts(now_ns=100)
        self.assertEqual(c.stats()["timeout"], [2])


class TestProbeClockEvictsAtCap(unittest.TestCase):
    def test_evict_oldest_at_capacity(self):
        # Cap of 2 means the third emit pushes out the first.
        c = ProbeClock(
            num_planes=1, probe_timeout_ns=1_000_000_000,
            max_outstanding_per_plane=2,
        )
        id0, _ = c.emit(0, now_ns=10)
        id1, _ = c.emit(0, now_ns=20)
        id2, _ = c.emit(0, now_ns=30)
        # id0 was evicted; matching it should be stale.
        self.assertIsNone(c.match_reply(
            req_id=id0, plane=0, reply_tx_ns=10, now_ns=40,
        ))
        # id1 and id2 still match.
        self.assertEqual(c.match_reply(
            req_id=id1, plane=0, reply_tx_ns=20, now_ns=40,
        ), 20)
        self.assertEqual(c.match_reply(
            req_id=id2, plane=0, reply_tx_ns=30, now_ns=40,
        ), 10)


class TestProbeClockValidation(unittest.TestCase):
    def test_construct_validates(self):
        with self.assertRaises(ValueError):
            ProbeClock(num_planes=0, probe_timeout_ns=1)
        with self.assertRaises(ValueError):
            ProbeClock(num_planes=1, probe_timeout_ns=0)
        with self.assertRaises(ValueError):
            ProbeClock(num_planes=1, probe_timeout_ns=1,
                       max_outstanding_per_plane=0)

    def test_emit_rejects_bad_plane(self):
        c = ProbeClock(num_planes=4, probe_timeout_ns=10)
        with self.assertRaises(ValueError):
            c.emit(plane=4, now_ns=0)
        with self.assertRaises(ValueError):
            c.emit(plane=-1, now_ns=0)


# ---------------------------------------------------------------------------
# LossWindowTable
# ---------------------------------------------------------------------------

class TestLossWindow(unittest.TestCase):
    def _flow(self):
        return ("green", 0, 15)

    def test_empty_snapshot_is_empty(self):
        t = LossWindowTable(num_planes=4)
        rep = t.snapshot_and_reset(self._flow())
        self.assertEqual(rep.window_id, 0)
        self.assertEqual(rep.planes, ())

    def test_single_plane_records(self):
        t = LossWindowTable(num_planes=4)
        flow = self._flow()
        for seq in range(10):
            t.record(flow, plane=2, seq=seq)
        rep = t.snapshot_and_reset(flow)
        self.assertEqual(rep.window_id, 0)
        self.assertEqual(len(rep.planes), 1)
        rec = rep.planes[0]
        self.assertEqual(rec.plane_id, 2)
        self.assertEqual(rec.seen, 10)
        self.assertEqual(rec.expected, 10)  # min=0, max=9 -> 10
        self.assertEqual(rec.max_gap, 1)

    def test_multi_plane_records(self):
        t = LossWindowTable(num_planes=4)
        flow = self._flow()
        # Round-robin across planes 0..3.
        for seq in range(20):
            t.record(flow, plane=seq % 4, seq=seq)
        rep = t.snapshot_and_reset(flow)
        self.assertEqual(len(rep.planes), 4)
        # Each plane sees 5 packets, with seqs 0,4,8,12,16 (etc.).
        for rec in rep.planes:
            self.assertEqual(rec.seen, 5)
            # min..max span = 16, so expected = 17.
            self.assertEqual(rec.expected, 17)
            # Gaps between consecutive seqs on the same plane are all 4.
            self.assertEqual(rec.max_gap, 4)

    def test_window_id_increments(self):
        t = LossWindowTable(num_planes=2)
        flow = self._flow()
        t.record(flow, plane=0, seq=0)
        self.assertEqual(t.snapshot_and_reset(flow).window_id, 0)
        # Even with no traffic, the window_id increments.
        self.assertEqual(t.snapshot_and_reset(flow).window_id, 1)
        self.assertEqual(t.snapshot_and_reset(flow).window_id, 2)

    def test_reset_zeros_counters(self):
        t = LossWindowTable(num_planes=2)
        flow = self._flow()
        t.record(flow, plane=0, seq=5)
        t.snapshot_and_reset(flow)
        # After reset, a fresh single record produces seen=1, gap=0.
        t.record(flow, plane=0, seq=100)
        rep = t.snapshot_and_reset(flow)
        self.assertEqual(rep.planes[0].seen, 1)
        self.assertEqual(rep.planes[0].max_gap, 0)
        # And expected = 100 - 100 + 1 = 1, not the historical span.
        self.assertEqual(rep.planes[0].expected, 1)

    def test_max_gap_tracks_largest_jump(self):
        t = LossWindowTable(num_planes=1)
        flow = self._flow()
        for seq in [10, 11, 12, 50, 51]:
            t.record(flow, plane=0, seq=seq)
        rep = t.snapshot_and_reset(flow)
        # Largest forward jump is 50 - 12 = 38.
        self.assertEqual(rep.planes[0].max_gap, 38)

    def test_known_flows_listed(self):
        t = LossWindowTable(num_planes=2)
        t.record(("a", 0, 1), plane=0, seq=0)
        t.record(("b", 0, 2), plane=1, seq=0)
        flows = t.known_flows()
        self.assertEqual(set(flows), {("a", 0, 1), ("b", 0, 2)})

    def test_forget_drops_flow(self):
        t = LossWindowTable(num_planes=2)
        t.record(("a", 0, 1), plane=0, seq=0)
        t.forget(("a", 0, 1))
        self.assertEqual(t.known_flows(), ())
        # Forgetting an unknown flow is a no-op.
        t.forget(("nonexistent",))

    def test_validation(self):
        t = LossWindowTable(num_planes=4)
        with self.assertRaises(ValueError):
            t.record(self._flow(), plane=4, seq=0)
        with self.assertRaises(ValueError):
            t.record(self._flow(), plane=0, seq=-1)
        with self.assertRaises(ValueError):
            LossWindowTable(num_planes=0)


# ---------------------------------------------------------------------------
# compute_loss_ratio
# ---------------------------------------------------------------------------

class TestComputeLossRatio(unittest.TestCase):
    def test_no_loss(self):
        self.assertEqual(compute_loss_ratio(seen=100, sent_or_expected=100), 0.0)

    def test_half_loss(self):
        self.assertAlmostEqual(
            compute_loss_ratio(seen=50, sent_or_expected=100), 0.5,
        )

    def test_total_loss(self):
        self.assertEqual(compute_loss_ratio(seen=0, sent_or_expected=100), 1.0)

    def test_seen_above_sent_clamps_to_zero(self):
        # Late arrivals from a previous window can exceed.
        self.assertEqual(
            compute_loss_ratio(seen=110, sent_or_expected=100), 0.0,
        )

    def test_zero_denominator(self):
        self.assertEqual(compute_loss_ratio(seen=0, sent_or_expected=0), 0.0)
        self.assertEqual(
            compute_loss_ratio(seen=10, sent_or_expected=0), 0.0,
        )


# ---------------------------------------------------------------------------
# SentWindowRing
# ---------------------------------------------------------------------------

class TestSentWindowRing(unittest.TestCase):
    def test_push_validates_plane_count(self):
        ring = SentWindowRing(num_planes=4)
        with self.assertRaises(ValueError):
            ring.push(SentWindow(start_ns=0, end_ns=100, sent=(1, 2, 3)))

    def test_capacity_drops_oldest(self):
        ring = SentWindowRing(num_planes=2, capacity=2)
        ring.push(SentWindow(start_ns=0, end_ns=100, sent=(10, 10)))
        ring.push(SentWindow(start_ns=100, end_ns=200, sent=(20, 20)))
        ring.push(SentWindow(start_ns=200, end_ns=300, sent=(30, 30)))
        # Only the last two remain.
        self.assertEqual(len(ring), 2)
        # Looking near t=50 (the dropped window's mid) finds the
        # closest of what's left, which is the [100,200] window mid=150.
        found = ring.find_closest(target_ns=50, max_skew_ns=10**9)
        self.assertEqual(found.start_ns, 100)

    def test_find_closest_within_skew(self):
        ring = SentWindowRing(num_planes=1, capacity=4)
        ring.push(SentWindow(start_ns=0, end_ns=100, sent=(10,)))
        ring.push(SentWindow(start_ns=200, end_ns=300, sent=(20,)))
        ring.push(SentWindow(start_ns=400, end_ns=500, sent=(30,)))
        # Closest to 250 is window [200,300] mid=250.
        w = ring.find_closest(target_ns=250, max_skew_ns=1)
        self.assertIsNotNone(w)
        self.assertEqual(w.start_ns, 200)

    def test_find_closest_returns_none_when_outside_skew(self):
        ring = SentWindowRing(num_planes=1, capacity=2)
        ring.push(SentWindow(start_ns=0, end_ns=100, sent=(10,)))
        # Mid is 50; target 1_000_000 is far. Skew threshold tight.
        w = ring.find_closest(target_ns=1_000_000, max_skew_ns=10)
        self.assertIsNone(w)


# ---------------------------------------------------------------------------
# apply_loss_report
# ---------------------------------------------------------------------------

class TestApplyLossReport(unittest.TestCase):
    NUM_PLANES = 4

    def _table(self, **cfg):
        c = EVStateConfig(**cfg) if cfg else None
        return EVStateTable(
            tenants=("green",), num_planes=self.NUM_PLANES, cfg=c,
        )

    def test_empty_report_noop(self):
        t = self._table()
        ring = SentWindowRing(num_planes=self.NUM_PLANES)
        stats = LossFusionStats()
        apply_loss_report(
            table=t, tenant="green",
            report=LossReport(window_id=0, planes=()),
            sent_ring=ring, received_at_ns=0,
            max_window_skew_ns=10**9,
            stats=stats,
        )
        self.assertEqual(stats.reports_processed, 0)
        # No EV state changes either.
        self.assertEqual(t.state("green", 0), EVState.UNKNOWN)

    def test_uses_sender_counter_when_available(self):
        # Sender sent 100 on plane 0; receiver saw 50 -> 50% loss.
        # With loss_threshold=0.05 and loss_demote_consecutive=2 we
        # should see one bad-window counter increment but no demote
        # (yet).
        t = self._table(loss_threshold=0.05, loss_demote_consecutive=2)
        ring = SentWindowRing(num_planes=self.NUM_PLANES)
        ring.push(SentWindow(
            start_ns=0, end_ns=100_000_000,
            sent=(100, 100, 100, 100),
        ))
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=0, spine_id=0, seen=50, expected=80, max_gap=2),
        ))
        stats = LossFusionStats()
        apply_loss_report(
            table=t, tenant="green", report=report,
            sent_ring=ring, received_at_ns=50_000_000,
            max_window_skew_ns=10**9,
            stats=stats,
        )
        self.assertEqual(stats.planes_updated, 1)
        self.assertEqual(stats.paired_with_sent_window, 1)
        self.assertEqual(stats.fell_back_to_receiver_expected, 0)
        # Still UNKNOWN; needs another bad window to demote.
        self.assertEqual(t.state("green", 0), EVState.UNKNOWN)

    def test_consecutive_bad_windows_demote(self):
        t = self._table(loss_threshold=0.05, loss_demote_consecutive=2,
                        min_active_planes=1)
        ring = SentWindowRing(num_planes=self.NUM_PLANES)
        ring.push(SentWindow(
            start_ns=0, end_ns=100_000_000,
            sent=(100, 100, 100, 100),
        ))
        bad_report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=0, spine_id=0, seen=50, expected=80, max_gap=2),
        ))
        # Two consecutive bad reports for plane 0.
        apply_loss_report(
            table=t, tenant="green", report=bad_report,
            sent_ring=ring, received_at_ns=50_000_000,
            max_window_skew_ns=10**9,
        )
        apply_loss_report(
            table=t, tenant="green", report=bad_report,
            sent_ring=ring, received_at_ns=50_000_000,
            max_window_skew_ns=10**9,
        )
        self.assertEqual(t.state("green", 0), EVState.ASSUMED_BAD)

    def test_falls_back_to_receiver_expected(self):
        # No SentWindow in ring. Use receiver's expected_local field.
        t = self._table(loss_threshold=0.05, loss_demote_consecutive=2)
        ring = SentWindowRing(num_planes=self.NUM_PLANES)
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=1, spine_id=0, seen=50, expected=80, max_gap=2),
        ))
        stats = LossFusionStats()
        apply_loss_report(
            table=t, tenant="green", report=report,
            sent_ring=ring, received_at_ns=0,
            max_window_skew_ns=10**9,
            stats=stats,
        )
        self.assertEqual(stats.planes_updated, 1)
        self.assertEqual(stats.fell_back_to_receiver_expected, 1)
        self.assertEqual(stats.no_pairing_window_in_ring, 1)

    def test_skips_plane_with_no_signal(self):
        # seen=0 AND expected=0 -> no info.
        t = self._table()
        ring = SentWindowRing(num_planes=self.NUM_PLANES)
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=0, spine_id=0, seen=0, expected=0, max_gap=0),
        ))
        stats = LossFusionStats()
        apply_loss_report(
            table=t, tenant="green", report=report,
            sent_ring=ring, received_at_ns=0,
            max_window_skew_ns=10**9,
            stats=stats,
        )
        self.assertEqual(stats.planes_updated, 0)
        self.assertEqual(stats.planes_skipped_no_data, 1)


if __name__ == "__main__":
    unittest.main()
