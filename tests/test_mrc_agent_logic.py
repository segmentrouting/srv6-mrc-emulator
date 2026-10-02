"""Unit tests for the pure-logic pieces of the MRC agent.

Covers:
  - srv6_mrc.mrc.probe_clock.ProbeClock
  - srv6_mrc.mrc.loss_window.LossWindowTable
  - srv6_mrc.mrc.loss_compute.{SentWindow, SentWindowRing,
    compute_loss_ratio, apply_loss_report}

These modules are sockets-free; they're the inner loop that the
commit-2b agent.py I/O layer wraps. Testing them in isolation gives us
a deterministic regression net before threading + sockets enter the
picture.
"""

import unittest

from srv6_mrc.mrc.ev_state import EVState, EVStateConfig, EVStateTable
from srv6_mrc.mrc.loss_compute import (
    LossFusionStats,
    SentWindow,
    SentWindowRing,
    apply_loss_report,
    compute_loss_ratio,
)
from srv6_mrc.mrc.loss_window import LossWindowTable
from srv6_mrc.mrc.probe import LossReport, PlaneLossRecord


# ---------------------------------------------------------------------------
# LossWindowTable
# ---------------------------------------------------------------------------

class TestLossWindow(unittest.TestCase):
    def _flow(self):
        return ("green", 0, 15)

    def _table(self, num_planes=4, num_paths=1):
        return LossWindowTable(num_planes=num_planes, num_paths=num_paths)

    def test_empty_snapshot_is_empty(self):
        t = self._table()
        rep = t.snapshot_and_reset(self._flow())
        self.assertEqual(rep.window_id, 0)
        self.assertEqual(rep.planes, ())

    def test_single_ev_records(self):
        t = self._table()
        flow = self._flow()
        for seq in range(10):
            t.record(flow, plane=2, path=0, seq=seq)
        rep = t.snapshot_and_reset(flow)
        self.assertEqual(rep.window_id, 0)
        self.assertEqual(len(rep.planes), 1)
        rec = rep.planes[0]
        self.assertEqual(rec.plane_id, 2)
        self.assertEqual(rec.path_id, 0)
        self.assertEqual(rec.seen, 10)
        self.assertEqual(rec.expected, 10)  # min=0, max=9 -> 10
        self.assertEqual(rec.max_gap, 1)

    def test_multi_plane_records(self):
        t = self._table()
        flow = self._flow()
        # Round-robin across planes 0..3 (single path each).
        for seq in range(20):
            t.record(flow, plane=seq % 4, path=0, seq=seq)
        rep = t.snapshot_and_reset(flow)
        self.assertEqual(len(rep.planes), 4)
        # Each plane sees 5 packets, with seqs 0,4,8,12,16 (etc.).
        for rec in rep.planes:
            self.assertEqual(rec.path_id, 0)
            self.assertEqual(rec.seen, 5)
            # min..max span = 16, so expected = 17.
            self.assertEqual(rec.expected, 17)
            # Gaps between consecutive seqs on the same plane are all 4.
            self.assertEqual(rec.max_gap, 4)

    def test_multi_path_records(self):
        # Different paths on the same plane should produce separate
        # PlaneLossRecord entries.
        t = self._table(num_planes=2, num_paths=4)
        flow = self._flow()
        # Spread 16 packets across (plane=1, path=0..3); every path
        # gets 4 packets, seqs striped by path.
        for seq in range(16):
            t.record(flow, plane=1, path=seq % 4, seq=seq)
        rep = t.snapshot_and_reset(flow)
        self.assertEqual(len(rep.planes), 4)
        # Records emitted in (plane, path) order; all on plane 1, paths
        # 0..3 in order.
        for path_id, rec in enumerate(rep.planes):
            self.assertEqual(rec.plane_id, 1)
            self.assertEqual(rec.path_id, path_id)
            self.assertEqual(rec.seen, 4)
            # min..max span: e.g. path=0 sees seqs 0,4,8,12 -> 13.
            self.assertEqual(rec.expected, 13)
            self.assertEqual(rec.max_gap, 4)

    def test_window_id_increments(self):
        t = self._table(num_planes=2)
        flow = self._flow()
        t.record(flow, plane=0, path=0, seq=0)
        self.assertEqual(t.snapshot_and_reset(flow).window_id, 0)
        # Even with no traffic, the window_id increments.
        self.assertEqual(t.snapshot_and_reset(flow).window_id, 1)
        self.assertEqual(t.snapshot_and_reset(flow).window_id, 2)

    def test_reset_zeros_counters(self):
        t = self._table(num_planes=2)
        flow = self._flow()
        t.record(flow, plane=0, path=0, seq=5)
        t.snapshot_and_reset(flow)
        # After reset, a fresh single record produces seen=1, gap=0.
        t.record(flow, plane=0, path=0, seq=100)
        rep = t.snapshot_and_reset(flow)
        self.assertEqual(rep.planes[0].seen, 1)
        self.assertEqual(rep.planes[0].max_gap, 0)
        # And expected = 100 - 100 + 1 = 1, not the historical span.
        self.assertEqual(rep.planes[0].expected, 1)

    def test_max_gap_tracks_largest_jump(self):
        t = self._table(num_planes=1)
        flow = self._flow()
        for seq in [10, 11, 12, 50, 51]:
            t.record(flow, plane=0, path=0, seq=seq)
        rep = t.snapshot_and_reset(flow)
        # Largest forward jump is 50 - 12 = 38.
        self.assertEqual(rep.planes[0].max_gap, 38)

    def test_known_flows_listed(self):
        t = self._table(num_planes=2)
        t.record(("a", 0, 1), plane=0, path=0, seq=0)
        t.record(("b", 0, 2), plane=1, path=0, seq=0)
        flows = t.known_flows()
        self.assertEqual(set(flows), {("a", 0, 1), ("b", 0, 2)})

    def test_forget_drops_flow(self):
        t = self._table(num_planes=2)
        t.record(("a", 0, 1), plane=0, path=0, seq=0)
        t.forget(("a", 0, 1))
        self.assertEqual(t.known_flows(), ())
        # Forgetting an unknown flow is a no-op.
        t.forget(("nonexistent",))

    def test_validation(self):
        t = self._table(num_planes=4, num_paths=2)
        with self.assertRaises(ValueError):
            t.record(self._flow(), plane=4, path=0, seq=0)
        with self.assertRaises(ValueError):
            t.record(self._flow(), plane=0, path=2, seq=0)
        with self.assertRaises(ValueError):
            t.record(self._flow(), plane=0, path=0, seq=-1)
        with self.assertRaises(ValueError):
            LossWindowTable(num_planes=0, num_paths=1)
        with self.assertRaises(ValueError):
            LossWindowTable(num_planes=4, num_paths=0)


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
        ring = SentWindowRing(num_planes=4, num_paths=1)
        with self.assertRaises(ValueError):
            ring.push(SentWindow(start_ns=0, end_ns=100,
                                 sent=((1,), (2,), (3,))))

    def test_capacity_drops_oldest(self):
        ring = SentWindowRing(num_planes=2, num_paths=1, capacity=2)
        ring.push(SentWindow(start_ns=0, end_ns=100, sent=((10,), (10,))))
        ring.push(SentWindow(start_ns=100, end_ns=200, sent=((20,), (20,))))
        ring.push(SentWindow(start_ns=200, end_ns=300, sent=((30,), (30,))))
        # Only the last two remain.
        self.assertEqual(len(ring), 2)
        # Looking near t=50 (the dropped window's mid) finds the
        # closest of what's left, which is the [100,200] window mid=150.
        found = ring.find_closest(target_ns=50, max_skew_ns=10**9)
        self.assertEqual(found.start_ns, 100)

    def test_find_closest_within_skew(self):
        ring = SentWindowRing(num_planes=1, num_paths=1, capacity=4)
        ring.push(SentWindow(start_ns=0, end_ns=100, sent=((10,),)))
        ring.push(SentWindow(start_ns=200, end_ns=300, sent=((20,),)))
        ring.push(SentWindow(start_ns=400, end_ns=500, sent=((30,),)))
        # Closest to 250 is window [200,300] mid=250.
        w = ring.find_closest(target_ns=250, max_skew_ns=1)
        self.assertIsNotNone(w)
        self.assertEqual(w.start_ns, 200)

    def test_find_closest_returns_none_when_outside_skew(self):
        ring = SentWindowRing(num_planes=1, num_paths=1, capacity=2)
        ring.push(SentWindow(start_ns=0, end_ns=100, sent=((10,),)))
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
            tenants=("green",), num_planes=self.NUM_PLANES,
            num_paths=self.NUM_PLANES, cfg=c,
        )

    def _ring(self):
        # Tests use num_paths == NUM_PLANES so a single index can stand
        # in as both plane and path when convenient.
        return SentWindowRing(
            num_planes=self.NUM_PLANES, num_paths=self.NUM_PLANES,
        )

    @staticmethod
    def _per_ev_sent(per_plane: tuple, num_paths: int) -> tuple:
        """Helper: build a per-EV `sent` grid that attributes the whole
        per-plane count to (plane, path=0). Lets these tests target a
        single EV cell while still exercising the 2-D loss-pairing path.
        """
        return tuple(
            (n,) + (0,) * (num_paths - 1) for n in per_plane
        )

    def test_empty_report_noop(self):
        t = self._table()
        ring = self._ring()
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
        self.assertEqual(t.state("green", 0, 0), EVState.UNKNOWN)

    def test_uses_sender_counter_when_available(self):
        # Sender sent 100 on (plane=0, path=0); receiver saw 50 -> 50%
        # loss. With loss_threshold=0.05 and loss_demote_consecutive=2
        # we should see one bad-window counter increment but no demote
        # (yet).
        t = self._table(loss_threshold=0.05, loss_demote_consecutive=2)
        ring = self._ring()
        ring.push(SentWindow(
            start_ns=0, end_ns=100_000_000,
            sent=self._per_ev_sent((100, 100, 100, 100), self.NUM_PLANES),
        ))
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=0, path_id=0, seen=50, expected=80, max_gap=2),
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
        self.assertEqual(t.state("green", 0, 0), EVState.UNKNOWN)

    def test_consecutive_bad_windows_demote(self):
        t = self._table(loss_threshold=0.05, loss_demote_consecutive=2,
                        min_active_evs=1)
        ring = self._ring()
        ring.push(SentWindow(
            start_ns=0, end_ns=100_000_000,
            sent=self._per_ev_sent((100, 100, 100, 100), self.NUM_PLANES),
        ))
        bad_report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=0, path_id=0, seen=50, expected=80, max_gap=2),
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
        self.assertEqual(t.state("green", 0, 0), EVState.ASSUMED_BAD)

    def test_falls_back_to_receiver_expected(self):
        # No SentWindow in ring -> we must NOT use receiver's
        # expected_local (it's a strict and typically wildly inflated
        # upper bound under packet-level EV spray; using it produces
        # phantom EV demotions on healthy fabrics). Plane is skipped;
        # state machine is not touched.
        t = self._table(loss_threshold=0.05, loss_demote_consecutive=2)
        ring = self._ring()
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=1, path_id=0, seen=50, expected=80, max_gap=2),
        ))
        stats = LossFusionStats()
        apply_loss_report(
            table=t, tenant="green", report=report,
            sent_ring=ring, received_at_ns=0,
            max_window_skew_ns=10**9,
            stats=stats,
        )
        # Plane was skipped; no state-machine update.
        self.assertEqual(stats.planes_updated, 0)
        self.assertEqual(stats.paired_with_sent_window, 0)
        # Counter name is historical; now means "skipped because no
        # sender-side denominator was available".
        self.assertEqual(stats.fell_back_to_receiver_expected, 1)
        self.assertEqual(stats.no_pairing_window_in_ring, 1)
        # State machine MUST stay at UNKNOWN (not demoted) despite the
        # report carrying a 50/80 = 37.5% phantom loss ratio.
        self.assertEqual(t.state("green", 1, 0), EVState.UNKNOWN)

    def test_phantom_loss_fallback_does_not_demote(self):
        # Regression for the 4p-4x8 baseline phantom-demotion bug:
        # under packet-level EV spray a healthy EV that received e.g.
        # 10 packets sees seqs spanning 0..400 (because the other 15
        # EVs are taking the rest), so rec.expected = 401 and the naive
        # fallback ratio is 1 - 10/401 = 97.5% loss. Two consecutive
        # such reports (>= loss_demote_consecutive) used to demote the
        # EV. The skip-on-no-pairing fix means the EV stays UNKNOWN
        # no matter how many unpairable reports arrive.
        t = self._table(loss_threshold=0.05, loss_demote_consecutive=2,
                        min_active_evs=1)
        ring = self._ring()  # empty ring, all reports unpairable
        phantom_report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=0, path_id=0, seen=10, expected=401,
                            max_gap=80),
        ))
        for _ in range(5):
            apply_loss_report(
                table=t, tenant="green", report=phantom_report,
                sent_ring=ring, received_at_ns=0,
                max_window_skew_ns=10**9,
            )
        self.assertEqual(t.state("green", 0, 0), EVState.UNKNOWN)

    def test_paired_but_sent_zero_skips_plane(self):
        # If the receiver claims to have seen packets on an EV but the
        # sender's matched window has sent[plane][path]==0, we don't
        # know the right denominator (the receiver's view is from
        # straggler/skew packets sent in a neighbouring window). Skip.
        t = self._table(loss_threshold=0.05, loss_demote_consecutive=2)
        ring = self._ring()
        ring.push(SentWindow(
            start_ns=0, end_ns=100_000_000,
            # Plane 0 path 0 has 0 sent in this window.
            sent=self._per_ev_sent((0, 100, 100, 100), self.NUM_PLANES),
        ))
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=0, path_id=0, seen=5, expected=200,
                            max_gap=40),
        ))
        stats = LossFusionStats()
        apply_loss_report(
            table=t, tenant="green", report=report,
            sent_ring=ring, received_at_ns=50_000_000,
            max_window_skew_ns=10**9,
            stats=stats,
        )
        self.assertEqual(stats.planes_updated, 0)
        self.assertEqual(stats.fell_back_to_receiver_expected, 1)
        # Pairing succeeded (no_pairing_window_in_ring stays 0); the
        # skip is per-plane, not per-report.
        self.assertEqual(stats.no_pairing_window_in_ring, 0)
        self.assertEqual(t.state("green", 0, 0), EVState.UNKNOWN)

    def test_skips_plane_with_no_signal(self):
        # seen=0 AND expected=0 -> no info.
        t = self._table()
        ring = self._ring()
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=0, path_id=0, seen=0, expected=0, max_gap=0),
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


class TestApplyLossReportAbsentEvs(unittest.TestCase):
    """The receiver omits EVs with seen == 0 from its report, so a fully
    blackholed EV never appears in one. When the sender's paired window
    shows it sprayed that EV, absence from an otherwise non-empty report
    is 100% loss, not "no data"."""

    NUM = 4

    def _table(self, **cfg):
        base = dict(loss_threshold=0.25, loss_demote_consecutive=3,
                    min_active_evs=1)
        base.update(cfg)
        return EVStateTable(
            tenants=("green",), num_planes=self.NUM, num_paths=self.NUM,
            cfg=EVStateConfig(**base),
        )

    def _ring(self, sent):
        ring = SentWindowRing(num_planes=self.NUM, num_paths=self.NUM)
        grid = [[0] * self.NUM for _ in range(self.NUM)]
        for (p, q), n in sent.items():
            grid[p][q] = n
        ring.push(SentWindow(start_ns=0, end_ns=100_000_000,
                             sent=tuple(map(tuple, grid))))
        return ring

    # Receiver saw EV (0,0) only; (1,0) is missing from the report.
    REPORT = LossReport(window_id=0, planes=(
        PlaneLossRecord(plane_id=0, path_id=0, seen=10, expected=10,
                        max_gap=0),
    ))

    def _apply(self, t, ring, *, times=1, report=None, received_at_ns=50_000_000):
        stats = LossFusionStats()
        for _ in range(times):
            apply_loss_report(
                table=t, tenant="green", report=report or self.REPORT,
                sent_ring=ring, received_at_ns=received_at_ns,
                max_window_skew_ns=10**9, stats=stats,
            )
        return stats

    def test_blackholed_ev_is_demoted(self):
        t = self._table()
        ring = self._ring({(0, 0): 10, (1, 0): 10})
        stats = self._apply(t, ring, times=3)
        self.assertIs(t.state("green", 1, 0), EVState.ASSUMED_BAD)
        self.assertIsNot(t.state("green", 0, 0), EVState.ASSUMED_BAD)
        self.assertEqual(stats.absent_evs_counted_as_lost, 3)
        self.assertEqual(t.inspect("green", 1, 0)["last_loss_ratio"], 1.0)

    def test_absent_ev_below_min_sent_is_ignored(self):
        # A lone packet can straddle the receiver's window edge; absence
        # there is not evidence of loss.
        t = self._table()
        ring = self._ring({(0, 0): 10, (1, 0): 1})
        stats = self._apply(t, ring, times=5)
        self.assertIsNot(t.state("green", 1, 0), EVState.ASSUMED_BAD)
        self.assertEqual(stats.absent_evs_counted_as_lost, 0)

    def test_unsprayed_ev_is_ignored(self):
        t = self._table()
        ring = self._ring({(0, 0): 10})
        stats = self._apply(t, ring, times=5)
        self.assertEqual(stats.absent_evs_counted_as_lost, 0)
        for p in range(self.NUM):
            for q in range(self.NUM):
                self.assertIsNot(t.state("green", p, q), EVState.ASSUMED_BAD)

    def test_already_demoted_ev_is_not_rearmed(self):
        # Right after a demote the paired window still shows sends on
        # the EV. Counting it as lost again would re-arm the loss gate
        # and block probe recovery (the weight-0 EV never gets a clean
        # loss window).
        t = self._table()
        ring = self._ring({(0, 0): 10, (1, 0): 10})
        self._apply(t, ring, times=3)
        self.assertIs(t.state("green", 1, 0), EVState.ASSUMED_BAD)
        stats = self._apply(t, ring, times=3)
        self.assertEqual(stats.absent_evs_counted_as_lost, 0)
        self.assertEqual(
            t.inspect("green", 1, 0)["consecutive_loss_demote_windows"], 0)

    def test_unpaired_report_counts_nothing_absent(self):
        t = self._table()
        ring = self._ring({(0, 0): 10, (1, 0): 10})
        stats = self._apply(t, ring, times=3, received_at_ns=10**13)
        self.assertEqual(stats.absent_evs_counted_as_lost, 0)
        self.assertIsNot(t.state("green", 1, 0), EVState.ASSUMED_BAD)

    def test_partial_window_report_counts_nothing_absent(self):
        # Receiver's window caught only a slice of the sender's (phase
        # skew / burst straddling the edge): healthy EVs are missing
        # too, so absence is not evidence.
        t = self._table()
        ring = self._ring({(p, 0): 10 for p in range(self.NUM)})
        report = LossReport(window_id=0, planes=(
            PlaneLossRecord(plane_id=0, path_id=0, seen=8, expected=8,
                            max_gap=0),
        ))
        stats = self._apply(t, ring, times=3, report=report)
        self.assertEqual(stats.absent_evs_counted_as_lost, 0)
        self.assertEqual(stats.absent_check_skipped_low_coverage, 3)
        for p in range(1, self.NUM):
            self.assertIsNot(t.state("green", p, 0), EVState.ASSUMED_BAD)

    def test_empty_report_counts_nothing_absent(self):
        # Zero records = receiver saw nothing at all (flow idle/ended);
        # that's already handled as "no signal", not a fabric-wide loss.
        t = self._table()
        ring = self._ring({(0, 0): 10, (1, 0): 10})
        stats = self._apply(
            t, ring, times=3, report=LossReport(window_id=0, planes=()))
        self.assertEqual(stats.absent_evs_counted_as_lost, 0)


class TestLossPathTimeline(unittest.TestCase):
    """Event-timeline simulation of the loss path at lab defaults
    (300 ms windows on both sides, 500 ms pairing skew) with the
    receiver's window phase-shifted from the sender's and per-packet
    jitter. Pins the rate/threshold trade-off: no healthy EV is ever
    demoted, and a blackholed EV is demoted even at the committed
    scenarios' 100 pps."""

    W = 0.3
    SKEW_NS = 500_000_000

    def _run(self, *, rate_pps, seed, drop=None, dur_s=30.0):
        import random
        rnd = random.Random(seed)
        phase = rnd.uniform(0, self.W)
        n_p = n_q = 4
        t = EVStateTable(tenants=("g",), num_planes=n_p, num_paths=n_q)
        ring = SentWindowRing(num_planes=n_p, num_paths=n_q)
        rx = LossWindowTable(num_planes=n_p, num_paths=n_q)
        evs = [(p, q) for q in range(n_q) for p in range(n_p)]
        events = []
        for i in range(int(rate_pps * dur_s)):
            ts = i / rate_pps + rnd.uniform(0, 0.05)
            ev = evs[i % len(evs)]
            events.append((ts, 0, "send", ev, i))
            if ev != drop:
                events.append((ts + rnd.uniform(0.0005, 0.02), 1,
                               "recv", ev, i))
        k = 1
        while k * self.W < dur_s + 1:
            events.append((k * self.W, 2, "rotate", None, 0))
            events.append((k * self.W + phase, 3, "report", None, 0))
            k += 1
        events.sort()
        cur = [[0] * n_q for _ in range(n_p)]
        start = 0.0
        for ts, _, kind, ev, seq in events:
            if kind == "send":
                cur[ev[0]][ev[1]] += 1
            elif kind == "recv":
                rx.record("f", ev[0], ev[1], seq)
            elif kind == "rotate":
                ring.push(SentWindow(start_ns=int(start * 1e9),
                                     end_ns=int(ts * 1e9),
                                     sent=tuple(map(tuple, cur))))
                cur = [[0] * n_q for _ in range(n_p)]
                start = ts
            else:
                apply_loss_report(
                    table=t, tenant="g",
                    report=rx.snapshot_and_reset("f"), sent_ring=ring,
                    received_at_ns=int((ts + 0.001) * 1e9),
                    max_window_skew_ns=self.SKEW_NS,
                )
        return t, evs

    def test_healthy_fabric_never_demotes(self):
        for rate in (100, 500):
            for seed in range(5):
                t, evs = self._run(rate_pps=rate, seed=seed)
                bad = [ev for ev in evs
                       if t.state("g", *ev) is EVState.ASSUMED_BAD]
                self.assertEqual(bad, [], f"rate={rate} seed={seed}")

    def test_blackholed_ev_demoted_at_scenario_rate(self):
        for seed in range(5):
            t, _ = self._run(rate_pps=100, seed=seed, drop=(2, 1))
            self.assertIs(t.state("g", 2, 1), EVState.ASSUMED_BAD,
                          f"seed={seed}")


if __name__ == "__main__":
    unittest.main()
