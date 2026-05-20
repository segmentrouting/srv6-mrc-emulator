import unittest
from collections import Counter

from srv6_mrc import policy
from srv6_mrc.topo import FlowKey, NUM_PLANES, NUM_SPINES


F = FlowKey("2001:db8:bbbb:00::2", "2001:db8:bbbb:0f::2", 9999, 9999)


class TestRoundRobin(unittest.TestCase):
    def test_strictly_cycles(self):
        p = policy.RoundRobin()
        out = [p.pick(i, F) for i in range(12)]
        self.assertEqual(out, [0, 1, 2, 3] * 3)


class TestHash5Tuple(unittest.TestCase):
    def test_same_flow_same_plane(self):
        p = policy.Hash5Tuple()
        choices = {p.pick(i, F) for i in range(1000)}
        self.assertEqual(len(choices), 1)

    def test_distribution_over_many_flows(self):
        # Use a realistic mix of varying tuple fields; if you only vary one
        # field with strong correlation (e.g. seq i, mod 4) you'll find
        # FNV-1a's low bits track that correlation. Real workloads vary all
        # of src/dst/sport with independent entropy.
        p = policy.Hash5Tuple()
        counts = Counter()
        for i in range(2048):
            flow = FlowKey(
                f"src-{i}", f"dst-{(i * 7) % 97}",
                9000 + (i * 13) % 65535, 9999,
            )
            counts[p.pick(0, flow)] += 1
        for plane in range(NUM_PLANES):
            self.assertGreater(counts[plane], 0)
        # Not strict uniformity — just a sanity floor.
        for plane in range(NUM_PLANES):
            self.assertGreater(counts[plane], 2048 // (NUM_PLANES * 4))


class TestWeighted(unittest.TestCase):
    def test_distribution_tracks_weights(self):
        p = policy.Weighted(weights=(0.4, 0.3, 0.2, 0.1))
        counts = Counter()
        n = 10_000
        for i in range(n):
            counts[p.pick(i, F)] += 1
        # Low-discrepancy sequence — tolerance is tight but not zero.
        self.assertAlmostEqual(counts[0] / n, 0.4, delta=0.02)
        self.assertAlmostEqual(counts[1] / n, 0.3, delta=0.02)
        self.assertAlmostEqual(counts[2] / n, 0.2, delta=0.02)
        self.assertAlmostEqual(counts[3] / n, 0.1, delta=0.02)

    def test_uniform_weights_match_round_robin_roughly(self):
        p = policy.Weighted(weights=(1, 1, 1, 1))
        counts = Counter(p.pick(i, F) for i in range(8000))
        for plane in range(NUM_PLANES):
            self.assertAlmostEqual(counts[plane] / 8000, 0.25, delta=0.02)

    def test_deterministic(self):
        p1 = policy.Weighted(weights=(1, 2, 3, 4))
        p2 = policy.Weighted(weights=(1, 2, 3, 4))
        a = [p1.pick(i, F) for i in range(200)]
        b = [p2.pick(i, F) for i in range(200)]
        self.assertEqual(a, b)

    def test_validation(self):
        with self.assertRaises(ValueError):
            policy.Weighted(weights=(1, 2, 3))           # wrong count
        with self.assertRaises(ValueError):
            policy.Weighted(weights=(1, -1, 1, 1))       # negative
        with self.assertRaises(ValueError):
            policy.Weighted(weights=(0, 0, 0, 0))        # zero sum


class TestPolicyFromSpec(unittest.TestCase):
    def test_string_forms(self):
        self.assertIsInstance(policy.policy_from_spec("round_robin"),
                              policy.RoundRobin)
        self.assertIsInstance(policy.policy_from_spec("hash5tuple"),
                              policy.Hash5Tuple)

    def test_weighted(self):
        p = policy.policy_from_spec({"weighted": [1, 1, 1, 1]})
        self.assertIsInstance(p, policy.Weighted)

    def test_health_aware_removed(self):
        # `health_aware` (legacy ICMPv6-driven wrapper) was removed; the
        # MRC path is `health_aware_mrc`. Confirm the old key is now an
        # unknown-policy error rather than silently constructing something.
        with self.assertRaises(ValueError):
            policy.policy_from_spec({"health_aware": "round_robin"})

    def test_bad_specs(self):
        with self.assertRaises(ValueError):
            policy.policy_from_spec("nonesuch")
        with self.assertRaises(ValueError):
            policy.policy_from_spec({"weighted": [1]})        # wrong shape

    def test_health_aware_mrc_returns_factory(self):
        # `health_aware_mrc` is deferred construction: policy_from_spec
        # doesn't have an EVStateTable so it can't build the live policy
        # itself. The caller (spray.py parse_policy) finishes binding.
        p = policy.policy_from_spec("health_aware_mrc")
        self.assertIsInstance(p, policy.HealthAwareMrcFactory)
        self.assertEqual(p.name, "health_aware_mrc")

    def test_factory_pick_is_error(self):
        # Calling pick() on an unbound factory is a programmer error;
        # ensure we fail loud rather than silently producing garbage.
        with self.assertRaises(RuntimeError):
            policy.HealthAwareMrcFactory().pick(0, F)


class TestHealthAwareMrc(unittest.TestCase):
    """Driving HealthAwareMrc through real EVStateTable state changes.

    These exercise the full integration: weights() snapshot in the table
    is converted into a CDF in the policy, and (seq, flow) maps through
    the golden-ratio scheme to a plane. The point isn't to re-test
    weighted picking — TestWeighted covers that — but to verify the
    policy faithfully follows the table.
    """
    def _table(self, **cfg_overrides):
        from srv6_mrc.mrc.ev_state import EVStateTable, EVStateConfig
        cfg = EVStateConfig(**cfg_overrides) if cfg_overrides else None
        return EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES,
            num_paths=NUM_SPINES, cfg=cfg,
        )

    def test_uniform_when_all_unknown(self):
        # Cold-start = dormant table = all planes UNKNOWN = uniform
        # weights. Distribution should cover every plane.
        table = self._table()
        p = policy.HealthAwareMrc(table=table, tenant="green")
        counts = Counter(p.pick(i, F) for i in range(4096))
        for plane in range(NUM_PLANES):
            self.assertGreater(counts[plane], 0)

    def test_demoted_plane_gets_zero_picks(self):
        # Drive every EV on plane 1 to ASSUMED_BAD via probe timeouts;
        # verify the policy never picks plane 1. (We don't compare
        # against another `Weighted` here because the floor logic can
        # keep a "bad" plane nonzero if too many are bad; only one
        # plane's worth of demotes keeps us above the floor.)
        table = self._table(
            probe_fail_threshold=3,
            min_active_evs=1,
        )
        for path in range(NUM_SPINES):
            for _ in range(3):
                table.record_probe_result("green", 1, path, success=False)
        from srv6_mrc.mrc.ev_state import EVState
        for path in range(NUM_SPINES):
            self.assertEqual(
                table.state("green", 1, path), EVState.ASSUMED_BAD
            )
        p = policy.HealthAwareMrc(table=table, tenant="green")
        seen = {p.pick(i, F) for i in range(4096)}
        self.assertNotIn(1, seen)

    def test_live_state_change_takes_effect_next_pick(self):
        # No caching of weights inside the policy: a demote between two
        # picks should reshape the distribution. Sample 2k picks before
        # and after; plane 0's share must drop to zero.
        table = self._table(
            probe_fail_threshold=3,
            min_active_evs=1,
        )
        p = policy.HealthAwareMrc(table=table, tenant="green")
        before = Counter(p.pick(i, F) for i in range(2048))
        for path in range(NUM_SPINES):
            for _ in range(3):
                table.record_probe_result("green", 0, path, success=False)
        after = Counter(p.pick(i, F) for i in range(2048, 4096))
        # Plane 0 was uniform-share (~25%) before; after fully demoting
        # all its EVs it should be zero given the min-active-evs floor.
        self.assertGreater(before[0], 100)
        self.assertEqual(after[0], 0)

    def test_deterministic_given_fixed_state(self):
        # Same (seq, flow) and same table state must yield the same
        # plane on repeated calls. Critical for trace reproducibility.
        table = self._table()
        p = policy.HealthAwareMrc(table=table, tenant="green")
        first = [p.pick(i, F) for i in range(256)]
        second = [p.pick(i, F) for i in range(256)]
        self.assertEqual(first, second)

    def test_unknown_tenant_rejected(self):
        table = self._table()
        with self.assertRaises(ValueError):
            policy.HealthAwareMrc(table=table, tenant="not-a-tenant")

    def test_plane_count_mismatch_rejected(self):
        # The policy assumes NUM_PLANES (the topology constant) matches
        # the table. A mismatch is a configuration bug, not a runtime
        # one; fail at construction.
        from srv6_mrc.mrc.ev_state import EVStateTable
        bad = EVStateTable(
            tenants=("green",), num_planes=NUM_PLANES + 1,
            num_paths=NUM_SPINES,
        )
        with self.assertRaises(ValueError):
            policy.HealthAwareMrc(table=bad, tenant="green")

    def test_factory_bind_produces_live_policy(self):
        table = self._table()
        live = policy.HealthAwareMrcFactory().bind(table=table, tenant="green")
        self.assertIsInstance(live, policy.HealthAwareMrc)
        # Smoke: it actually picks something in range.
        self.assertIn(live.pick(0, F), range(NUM_PLANES))


class TestEvSpray(unittest.TestCase):
    """EV-spray round-robin across (plane, spine)."""

    def _flow(self):
        return FlowKey("2001:db8:bbbb::2", "2001:db8:bbbb:f::2", 9999, 9999)

    def test_default_fanout_is_num_spines(self):
        from srv6_mrc.topo import NUM_SPINES
        p = policy.EvSpray()
        self.assertEqual(p.paths_per_plane, NUM_SPINES)

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            policy.EvSpray(paths_per_plane=0)
        from srv6_mrc.topo import NUM_SPINES
        with self.assertRaises(ValueError):
            policy.EvSpray(paths_per_plane=NUM_SPINES + 1)

    def test_pick_ev_returns_valid_plane_and_spine(self):
        from srv6_mrc.topo import NUM_SPINES
        p = policy.EvSpray(paths_per_plane=4)
        flow = self._flow()
        for seq in range(200):
            plane, spine = p.pick_ev(seq, flow)
            self.assertIn(plane, range(NUM_PLANES))
            self.assertIn(spine, range(NUM_SPINES))

    def test_pick_ev_round_robin_full_cycle(self):
        """One full cycle visits every EV exactly once (plane-major
        check: planes rotate fastest)."""
        p = policy.EvSpray(paths_per_plane=4)
        flow = self._flow()
        ev_count = NUM_PLANES * 4
        seen = [p.pick_ev(seq, flow) for seq in range(ev_count)]
        self.assertEqual(len(set(seen)), ev_count,
                         "round-robin must visit every EV once per cycle")

    def test_pick_ev_plane_rotates_every_packet(self):
        # Successive packets must visit different planes (spine-major
        # walk). This is the anti-clustering property.
        p = policy.EvSpray(paths_per_plane=4)
        flow = self._flow()
        planes = [p.pick_ev(seq, flow)[0] for seq in range(NUM_PLANES * 2)]
        # Every NUM_PLANES-window should contain all planes.
        for start in range(0, len(planes), NUM_PLANES):
            window = planes[start:start + NUM_PLANES]
            self.assertEqual(set(window), set(range(NUM_PLANES)))

    def test_pick_returns_plane_only(self):
        """pick() is the backward-compat shim and must return an int."""
        p = policy.EvSpray(paths_per_plane=4)
        flow = self._flow()
        v = p.pick(0, flow)
        self.assertIsInstance(v, int)
        self.assertIn(v, range(NUM_PLANES))

    def test_deterministic_per_flow(self):
        p1 = policy.EvSpray(paths_per_plane=4)
        p2 = policy.EvSpray(paths_per_plane=4)
        flow = self._flow()
        for seq in range(50):
            self.assertEqual(p1.pick_ev(seq, flow), p2.pick_ev(seq, flow))

    def test_full_fanout_uses_all_spines(self):
        from srv6_mrc.topo import NUM_SPINES
        p = policy.EvSpray()  # default = NUM_SPINES
        flow = self._flow()
        ev_count = NUM_PLANES * NUM_SPINES
        seen_spines = {p.pick_ev(seq, flow)[1] for seq in range(ev_count)}
        self.assertEqual(seen_spines, set(range(NUM_SPINES)))

    def test_n_less_than_max_uses_subset(self):
        from srv6_mrc.topo import NUM_SPINES
        if NUM_SPINES < 2:
            self.skipTest("requires NUM_SPINES >= 2 for subset test")
        p = policy.EvSpray(paths_per_plane=2)
        flow = self._flow()
        ev_count = NUM_PLANES * 2
        seen_spines = {p.pick_ev(seq, flow)[1] for seq in range(ev_count)}
        # Exactly 2 distinct spines, both in [0, NUM_SPINES).
        self.assertEqual(len(seen_spines), 2)
        for s in seen_spines:
            self.assertIn(s, range(NUM_SPINES))


class TestEvSprayFromSpec(unittest.TestCase):
    """policy_from_spec must accept the ev_spray forms."""

    def test_bare_string(self):
        p = policy.policy_from_spec("ev_spray")
        self.assertIsInstance(p, policy.EvSpray)
        from srv6_mrc.topo import NUM_SPINES
        self.assertEqual(p.paths_per_plane, NUM_SPINES)

    def test_dict_with_n(self):
        p = policy.policy_from_spec({"ev_spray": 2})
        self.assertIsInstance(p, policy.EvSpray)
        self.assertEqual(p.paths_per_plane, 2)


# ---------------------------------------------------------------------------
# Refactor 1 Step 2: dual-mode topology binding
#
# Every policy class takes optional `topology=...`. When None, dimensions
# come from `topo` module globals (legacy path). When a Topology is
# supplied, dimensions come from the instance. These tests prove the two
# paths are observably identical when the supplied Topology matches the
# active module globals.
#
# When per-call-site migration completes (Phase B), every call site will
# pass an explicit Topology and the None branch will be deleted (Phase C).
# Until then, this sweep is the safety net for the dual-mode invariant.
# ---------------------------------------------------------------------------

from pathlib import Path

from srv6_mrc.topology import Topology


_DEFAULT_TOPO_YAML = (
    Path(__file__).resolve().parent.parent
    / "topologies" / "4p-8x16" / "topo.yaml"
)


class _DualModeMixin:
    """Build a Topology that matches the active module globals.

    Tests in this module run under the default 4p-8x16 SRV6_TOPO; that
    matches `topologies/4p-8x16/topo.yaml`. If the env shifts, the
    parity assertion would catch the mismatch immediately (dimensions
    diverge -> picks diverge).
    """

    @classmethod
    def topology(cls) -> Topology:
        return Topology.from_yaml(_DEFAULT_TOPO_YAML)


class TestDualModeParity(unittest.TestCase, _DualModeMixin):
    """For every policy that's now dual-mode, the topology=None and
    topology=<matching> paths must produce identical picks across a
    range of (seq, flow) inputs."""

    SEQS = list(range(64))
    FLOWS = [
        FlowKey(f"2001:db8:bbbb:{a:02x}::2",
                f"2001:db8:bbbb:{b:02x}::2", 9999, 9999)
        for a in (0, 3, 7, 15) for b in (1, 4, 8, 11) if a != b
    ]

    def _assert_pick_identical(self, p_legacy, p_topo):
        for flow in self.FLOWS:
            for seq in self.SEQS:
                self.assertEqual(
                    p_legacy.pick(seq, flow),
                    p_topo.pick(seq, flow),
                    f"divergence at seq={seq} flow={flow}",
                )

    def test_round_robin_parity(self):
        self._assert_pick_identical(
            policy.RoundRobin(),
            policy.RoundRobin(topology=self.topology()),
        )

    def test_hash5tuple_parity(self):
        self._assert_pick_identical(
            policy.Hash5Tuple(),
            policy.Hash5Tuple(topology=self.topology()),
        )

    def test_weighted_parity(self):
        w = tuple(float(x) for x in (1, 2, 3, 4))
        self._assert_pick_identical(
            policy.Weighted(weights=w),
            policy.Weighted(weights=w, topology=self.topology()),
        )

    def test_ev_spray_pick_parity(self):
        self._assert_pick_identical(
            policy.EvSpray(),
            policy.EvSpray(topology=self.topology()),
        )

    def test_ev_spray_pick_ev_parity(self):
        # pick_ev is the actually-interesting axis (plane, spine), so
        # check it directly rather than only via the pick() projection.
        T = self.topology()
        p1 = policy.EvSpray()
        p2 = policy.EvSpray(topology=T)
        for flow in self.FLOWS:
            for seq in self.SEQS:
                self.assertEqual(p1.pick_ev(seq, flow),
                                 p2.pick_ev(seq, flow))

    def test_ev_spray_paths_per_plane_default_resolves_to_topology(self):
        T = self.topology()
        p = policy.EvSpray(topology=T)
        # paths_per_plane sentinel should be replaced with the topology's
        # spines_per_plane at __post_init__ time.
        self.assertEqual(p.paths_per_plane, T.spines_per_plane)

    def test_ev_spray_explicit_paths_per_plane_honoured(self):
        T = self.topology()
        p = policy.EvSpray(paths_per_plane=3, topology=T)
        self.assertEqual(p.paths_per_plane, 3)

    def test_factory_carries_topology_into_bound_policy(self):
        T = self.topology()
        f = policy.HealthAwareMrcFactory(topology=T)
        # The factory's paths_per_plane sentinel should also resolve.
        self.assertEqual(f.paths_per_plane, T.spines_per_plane)
        # And a bound policy should still carry the same Topology.
        from srv6_mrc.mrc.ev_state import EVStateTable
        table = EVStateTable(
            tenants=("green",),
            num_planes=T.planes,
            num_paths=T.spines_per_plane,
        )
        bound = f.bind(table=table, tenant="green")
        self.assertIs(bound.topology, T)
        self.assertEqual(bound.paths_per_plane, T.spines_per_plane)

    def test_policy_from_spec_threads_topology(self):
        T = self.topology()
        p = policy.policy_from_spec("ev_spray", topology=T)
        self.assertIs(p.topology, T)
        p2 = policy.policy_from_spec({"ev_spray": 3}, topology=T)
        self.assertIs(p2.topology, T)
        self.assertEqual(p2.paths_per_plane, 3)
        f = policy.policy_from_spec("health_aware_mrc", topology=T)
        self.assertIs(f.topology, T)


class TestTopologyValidation(unittest.TestCase):
    """When a Topology with different dimensions is supplied, the
    policy's own dimension reads must reflect the topology, not the
    module globals."""

    def _alt_topology(self) -> Topology:
        # Construct a topology whose dimensions differ from the active
        # 4p-8x16 module globals, so a divergence test actually diverges.
        return Topology.from_dict({
            "name": "tiny", "planes": 2,
            "spines_per_plane": 2, "leaves_per_plane": 2,
            "tenants": ["green", "yellow"],
        })

    def test_round_robin_planes_track_topology(self):
        p = policy.RoundRobin(topology=self._alt_topology())
        out = [p.pick(i, F) for i in range(8)]
        # 2-plane topology -> cycle period 2, not 4.
        self.assertEqual(out, [0, 1] * 4)

    def test_weighted_validation_against_topology_planes(self):
        T = self._alt_topology()
        # 2-plane topology: 4 weights should be rejected.
        with self.assertRaises(ValueError):
            policy.Weighted(weights=(1, 1, 1, 1), topology=T)
        # 2 weights should be accepted.
        policy.Weighted(weights=(1, 1), topology=T)

    def test_ev_spray_validates_against_topology_spines(self):
        T = self._alt_topology()
        # spines_per_plane = 2 on the tiny topology.
        with self.assertRaises(ValueError):
            policy.EvSpray(paths_per_plane=3, topology=T)
        policy.EvSpray(paths_per_plane=2, topology=T)


if __name__ == "__main__":
    unittest.main()
