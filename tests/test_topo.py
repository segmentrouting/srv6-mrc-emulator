import unittest

from srv6_mrc import topo


class TestTopoConstants(unittest.TestCase):
    def test_fabric_shape(self):
        self.assertEqual(topo.NUM_PLANES, 4)
        self.assertEqual(topo.NUM_SPINES, 8)
        self.assertEqual(topo.NUM_LEAVES, 16)
        self.assertEqual(topo.PLANE_NICS, ("eth1", "eth2", "eth3", "eth4"))
        self.assertEqual(topo.SPRAY_PORT, 9999)

    def test_reference_pairs_match_spray(self):
        # Must match the `spray` reference-pairs map (srv6_mrc.cli.spray)
        # and routes.py:REFERENCE_PAIRS_SPINES exactly. If you change one,
        # change them all.
        expected = {
            (0, 15): 0, (1, 14): 2, (2, 13): 4, (3, 12): 6,
            (4, 11): 1, (5, 10): 3, (6, 9): 5,  (7, 8):  7,
        }
        self.assertEqual(topo.REFERENCE_PAIRS_SPINES, expected)


class TestCurrentTopology(unittest.TestCase):
    """Verifies the typed `Topology` accessor agrees with the legacy
    module-level constants and is a stable singleton.

    Refactor 1 Phase B is migrating call sites from the legacy
    constants to the typed accessor; both must produce identical
    dimensions for any topology the lab can deploy.
    """

    def test_dimensions_match_module_constants(self):
        t = topo.current_topology()
        self.assertEqual(t.planes, topo.NUM_PLANES)
        self.assertEqual(t.spines_per_plane, topo.NUM_SPINES)
        self.assertEqual(t.leaves_per_plane, topo.NUM_LEAVES)
        self.assertEqual(t.tenants, topo.TENANTS)

    def test_singleton_identity(self):
        # Identity stability matters: any consumer that memoizes
        # against `id(topology)` (planned in policy.py) should not
        # see a different instance on subsequent calls.
        a = topo.current_topology()
        b = topo.current_topology()
        self.assertIs(a, b)


class TestTenantRegistry(unittest.TestCase):
    """The tenant -> u16 mapping used on the wire by MRC PROBE v2.

    These tests pin the wire-id values so a topology yaml reorder
    doesn't silently change the network protocol.
    """

    def test_tenant_id_for_known_tenants(self):
        self.assertEqual(topo.tenant_id("green"), 1)
        self.assertEqual(topo.tenant_id("yellow"), 2)

    def test_unknown_tenant_raises(self):
        with self.assertRaises(ValueError):
            topo.tenant_id("notatenant")

    def test_tenant_name_round_trip(self):
        for name in topo.TENANTS:
            self.assertEqual(topo.tenant_name(topo.tenant_id(name)), name)

    def test_unknown_tenant_id_raises(self):
        with self.assertRaises(ValueError):
            topo.tenant_name(0xFFFF)

    def test_zero_id_reserved(self):
        # We never hand out tenant_id 0; it's reserved for "unknown / unset".
        self.assertNotIn(0, topo.TENANT_BY_ID)
        for tid in topo.TENANT_ID.values():
            self.assertGreater(tid, 0)


class TestSpineFor(unittest.TestCase):
    def test_reference_pairs_table(self):
        self.assertEqual(topo.spine_for(0, 15), 0)
        self.assertEqual(topo.spine_for(15, 0), 0)   # canonicalized
        self.assertEqual(topo.spine_for(7, 8), 7)

    def test_fallback_hash_in_range(self):
        # Non-reference pair -> deterministic hash in [0, 8).
        for a in range(16):
            for b in range(16):
                if a == b:
                    continue
                s = topo.spine_for(a, b)
                self.assertIn(s, range(topo.NUM_SPINES))

    def test_fallback_is_symmetric(self):
        self.assertEqual(topo.spine_for(2, 5), topo.spine_for(5, 2))


class TestHostNames(unittest.TestCase):
    def test_format(self):
        self.assertEqual(topo.host_name("green", 0), "green-host00")
        self.assertEqual(topo.host_name("yellow", 15), "yellow-host15")


class TestAddresses(unittest.TestCase):
    def test_host_underlay(self):
        # Phase 1a: host_underlay_addr is deprecated; for both tenants
        # it now returns the inner anycast address (plane is ignored).
        # green-host00: 2001:db8:bbbb:00::2
        self.assertEqual(
            topo.host_underlay_addr("green", 0, 0),
            "2001:db8:bbbb:00::2",
        )
        # green is plane-independent (was already)
        self.assertEqual(
            topo.host_underlay_addr("green", 0, 7),
            topo.host_underlay_addr("green", 3, 7),
        )
        # yellow-host15: 2001:db8:cccc:0f::2 (Phase 1a anycast, mirrors
        # green's pattern with bbbb→cccc). The old per-plane underlay
        # `cccc:<P><NN>::2` no longer exists.
        self.assertEqual(
            topo.host_underlay_addr("yellow", 3, 15),
            "2001:db8:cccc:0f::2",
        )
        # yellow is now plane-independent too
        self.assertEqual(
            topo.host_underlay_addr("yellow", 0, 7),
            topo.host_underlay_addr("yellow", 3, 7),
        )

    def test_green_anycast(self):
        self.assertEqual(topo.green_anycast_addr(0),  "2001:db8:bbbb:00::2")
        self.assertEqual(topo.green_anycast_addr(15), "2001:db8:bbbb:0f::2")

    def test_yellow_anycast(self):
        # Phase 1a: yellow inner anycast mirrors green exactly with
        # bbbb→cccc. Assigned to eth1..eth4 + lo (nodad).
        self.assertEqual(topo.yellow_anycast_addr(0),  "2001:db8:cccc:00::2")
        self.assertEqual(topo.yellow_anycast_addr(15), "2001:db8:cccc:0f::2")

    def test_yellow_loopback_is_alias_of_anycast(self):
        # Phase 1a: yellow_loopback_addr retained as a deprecated alias
        # of yellow_anycast_addr for backward compatibility.
        for hid in (0, 7, 15):
            self.assertEqual(
                topo.yellow_loopback_addr(hid),
                topo.yellow_anycast_addr(hid),
            )

    def test_inner_addr_dispatch(self):
        self.assertEqual(topo.inner_addr("green", 7),  "2001:db8:bbbb:07::2")
        self.assertEqual(topo.inner_addr("yellow", 7), "2001:db8:cccc:07::2")

    def test_host_probe_peer_addr_green_is_anycast(self):
        # The green host's anycast address lives on every plane NIC, and
        # therefore the probe-peer address must NOT vary by plane —
        # plane selection comes from SO_BINDTODEVICE on the sender.
        a0 = topo.host_probe_peer_addr("green", 0, 7)
        a3 = topo.host_probe_peer_addr("green", 3, 7)
        self.assertEqual(a0, "2001:db8:bbbb:07::2")
        self.assertEqual(a0, a3,
                         "green probe-peer must be plane-independent "
                         "(anycast); plane selection comes from "
                         "SO_BINDTODEVICE on the sender side")

    def test_host_probe_peer_addr_yellow_is_anycast(self):
        # Phase 1a: yellow now mirrors green — anycast inner address on
        # eth1..eth4 + lo (nodad). Plane selection comes from
        # SO_BINDTODEVICE on the sender side. The previous loopback-only
        # inner (`cccd:<NN>::1`) and per-plane underlay
        # (`cccc:<P><NN>::2`) are both retired — see
        # docs/architecture.md §2.
        a0 = topo.host_probe_peer_addr("yellow", 0, 15)
        a3 = topo.host_probe_peer_addr("yellow", 3, 15)
        self.assertEqual(a0, "2001:db8:cccc:0f::2")
        self.assertEqual(a0, a3,
                         "yellow probe-peer must be plane-independent "
                         "(anycast); plane selection comes from "
                         "SO_BINDTODEVICE on the sender side")

    def test_host_id_from_inner_addr_green(self):
        # Round-trips inner_addr("green", N) for all host ids.
        for hid in (0, 1, 7, 14, 15):
            addr = topo.inner_addr("green", hid)
            self.assertEqual(
                topo.host_id_from_inner_addr(addr), ("green", hid),
                f"green host {hid}: addr={addr}",
            )

    def test_host_id_from_inner_addr_yellow(self):
        for hid in (0, 1, 7, 14, 15):
            addr = topo.inner_addr("yellow", hid)
            self.assertEqual(
                topo.host_id_from_inner_addr(addr), ("yellow", hid),
                f"yellow host {hid}: addr={addr}",
            )

    def test_host_id_from_inner_addr_accepts_zero_suppressed(self):
        # scapy hands us canonical (zero-suppressed) addresses; our
        # parser must accept them.
        self.assertEqual(
            topo.host_id_from_inner_addr("2001:db8:bbbb:f::2"),
            ("green", 15),
        )
        self.assertEqual(
            topo.host_id_from_inner_addr("2001:db8:cccc:0::2"),
            ("yellow", 0),
        )

    def test_host_id_from_inner_addr_rejects_garbage(self):
        for bad in (
            "not-an-address",
            "::1",
            "2001:db8:aaaa:00::2",        # wrong tenant tag
            "2001:db8:bbbb:00::1",        # green host suffix is ::2
            "2001:db8:cccc:00::1",        # yellow host suffix is ::2
            "2001:db8:bbbb:ff::2",        # host_id > 15
        ):
            self.assertIsNone(
                topo.host_id_from_inner_addr(bad),
                f"expected None for {bad!r}",
            )

    def test_leaf_gateway_addr(self):
        # green leaf gw is anycast (plane is informational only).
        self.assertEqual(
            topo.leaf_gateway_addr("green", 0, 5),
            topo.leaf_gateway_addr("green", 3, 5),
        )
        # yellow leaf gw is per-plane.
        self.assertNotEqual(
            topo.leaf_gateway_addr("yellow", 0, 5),
            topo.leaf_gateway_addr("yellow", 3, 5),
        )


class TestUsidOuterDst(unittest.TestCase):
    def test_green_shape(self):
        # spray.md example: plane 0, spine 0, dst-leaf 15
        self.assertEqual(
            topo.usid_outer_dst("green", 0, 0, 15),
            "fc00:0000:f000:e00f:d000::",
        )

    def test_yellow_has_e009_d001(self):
        self.assertEqual(
            topo.usid_outer_dst("yellow", 2, 3, 9),
            "fc00:0002:f003:e009:e009:d001::",
        )
        # Per spray.md table:
        self.assertEqual(
            topo.usid_outer_dst("yellow", 0, 0, 15),
            "fc00:0000:f000:e00f:e009:d001::",
        )

    def test_plane_encoded_in_block(self):
        for p in range(topo.NUM_PLANES):
            dst = topo.usid_outer_dst("green", p, 0, 0)
            self.assertTrue(dst.startswith(f"fc00:000{p:x}:"))


class TestValidation(unittest.TestCase):
    def test_bad_tenant(self):
        with self.assertRaises(ValueError):
            topo.inner_addr("blue", 0)

    def test_bad_plane(self):
        with self.assertRaises(ValueError):
            topo.host_underlay_addr("green", 4, 0)

    def test_bad_spine(self):
        with self.assertRaises(ValueError):
            topo.usid_outer_dst("green", 0, 8, 0)

    def test_bad_host_id(self):
        with self.assertRaises(ValueError):
            topo.green_anycast_addr(16)


class TestFlowKey(unittest.TestCase):
    def test_hash_stable_across_instances(self):
        f1 = topo.FlowKey("a", "b", 1, 2)
        f2 = topo.FlowKey("a", "b", 1, 2)
        self.assertEqual(f1.hash5(), f2.hash5())

    def test_hash_changes_with_field(self):
        base = topo.FlowKey("a", "b", 1, 2).hash5()
        self.assertNotEqual(base, topo.FlowKey("a", "b", 1, 3).hash5())
        self.assertNotEqual(base, topo.FlowKey("a", "c", 1, 2).hash5())
        self.assertNotEqual(base, topo.FlowKey("a", "b", 9, 2).hash5())


class TestSelectSpines(unittest.TestCase):
    """Deterministic per-pair spine subsets used by EV-spray."""

    def test_returns_exactly_n_distinct(self):
        # Spot-check several pairs and several N values; all must give
        # exactly N distinct spine indices in [0, NUM_SPINES).
        for src, dst in [(0, 15), (3, 12), (7, 8), (1, 1)]:
            # Note: (1,1) is a self-pair; select_spines doesn't reject
            # those since it has no concept of "different host required"
            # — that's the caller's job. Still must return a valid set.
            for n in range(1, topo.NUM_SPINES + 1):
                got = topo.select_spines(src, dst, n)
                self.assertEqual(len(got), n)
                self.assertEqual(len(set(got)), n)
                for s in got:
                    self.assertGreaterEqual(s, 0)
                    self.assertLess(s, topo.NUM_SPINES)

    def test_full_fanout_is_permutation(self):
        # n == NUM_SPINES means every spine appears exactly once.
        got = topo.select_spines(0, 15, topo.NUM_SPINES)
        self.assertEqual(sorted(got), list(range(topo.NUM_SPINES)))

    def test_deterministic(self):
        # Same inputs -> same output, every call.
        a = topo.select_spines(2, 13, 4)
        b = topo.select_spines(2, 13, 4)
        self.assertEqual(a, b)

    def test_symmetric_in_pair(self):
        # (src,dst) and (dst,src) MUST yield the same subset so the
        # reverse-direction sender sees the same EV identities.
        forward = topo.select_spines(0, 15, 4)
        reverse = topo.select_spines(15, 0, 4)
        self.assertEqual(forward, reverse)

    def test_rejects_out_of_range_n(self):
        with self.assertRaises(ValueError):
            topo.select_spines(0, 15, 0)
        with self.assertRaises(ValueError):
            topo.select_spines(0, 15, topo.NUM_SPINES + 1)
        with self.assertRaises(ValueError):
            topo.select_spines(0, 15, -1)

    def test_order_is_stable_within_subset(self):
        # The round-robin EV walk depends on the spine ORDER being
        # stable, not just the set. Calling twice must give the same
        # tuple in the same order.
        a = topo.select_spines(4, 11, 3)
        b = topo.select_spines(4, 11, 3)
        self.assertEqual(a, b)

    def test_reaches_non_consecutive_subsets(self):
        # An earlier implementation returned `n` consecutive spines
        # mod NUM_SPINES (e.g. only {0,1}, {1,2}, …) starving the
        # other C(NUM_SPINES,n) - NUM_SPINES subsets. The Fisher-Yates
        # rewrite must reach disjoint pairs too. We sweep many synthetic
        # (src, dst) values and confirm we see at least one
        # non-consecutive (gap > 1) subset.
        seen_non_consecutive = False
        for src in range(0, 100):
            sub = topo.select_spines(src, src + 1000, 2)
            a, b = sorted(sub)
            if b - a > 1 and not (a == 0 and b == topo.NUM_SPINES - 1):
                seen_non_consecutive = True
                break
        self.assertTrue(
            seen_non_consecutive,
            "select_spines never produced a non-consecutive subset over "
            "100 synthetic pairs — distribution is still rotation-only"
        )

    def test_distribution_is_balanced_for_n2(self):
        # Sanity check the spine distribution is roughly uniform. Over
        # 1000 distinct pairs at n=2 (so 2000 picks total), each spine
        # should see ~250 picks (12.5%). Allow generous tolerance —
        # we only catch catastrophic biases (entire spine starved, or
        # one spine taking 2x its share). Real-world this matters
        # because production scenarios run with 8..16 pairs and any
        # large per-spine bias means dark fabric.
        from collections import Counter
        totals = Counter()
        for i in range(1000):
            for spine in topo.select_spines(i, i + 10000, 2):
                totals[spine] += 1
        # Every spine seen at least once.
        for s in range(topo.NUM_SPINES):
            self.assertGreater(
                totals[s], 0,
                f"spine {s} got zero picks over 1000 pairs — distribution "
                f"is severely biased"
            )
        # No spine takes more than 2x its share. Ideal share = 2000 / 8
        # = 250; cap at 500.
        ideal = 2 * 1000 // topo.NUM_SPINES
        for s in range(topo.NUM_SPINES):
            self.assertLess(
                totals[s], 2 * ideal,
                f"spine {s} got {totals[s]} picks (>2x ideal {ideal}) — "
                f"distribution is biased"
            )


class TestSelectSpinesForAddrs(unittest.TestCase):
    """Address-seeded variant used by EvSpray (FlowKey carries addrs,
    not host ids)."""

    def test_deterministic_per_address_pair(self):
        a = topo.select_spines_for_addrs("fc00::1", "fc00::ff", 3)
        b = topo.select_spines_for_addrs("fc00::1", "fc00::ff", 3)
        self.assertEqual(a, b)

    def test_symmetric_in_pair(self):
        forward = topo.select_spines_for_addrs("fc00::1", "fc00::ff", 4)
        reverse = topo.select_spines_for_addrs("fc00::ff", "fc00::1", 4)
        self.assertEqual(forward, reverse)

    def test_rejects_out_of_range_n(self):
        with self.assertRaises(ValueError):
            topo.select_spines_for_addrs("fc00::1", "fc00::ff", 0)
        with self.assertRaises(ValueError):
            topo.select_spines_for_addrs(
                "fc00::1", "fc00::ff", topo.NUM_SPINES + 1
            )

    def test_process_stable_no_hash_seed_dependency(self):
        # Regression: an earlier EvSpray implementation derived the
        # subset from Python's hash() of the address strings, which is
        # salted by PYTHONHASHSEED and produced different subsets in
        # different sender processes. select_spines_for_addrs must NOT
        # depend on hash(): the FNV mixer should give a value derived
        # purely from the address byte string.
        import subprocess, sys, json
        # Compute locally.
        local = topo.select_spines_for_addrs(
            "2001:db8:bbbb::2", "2001:db8:bbbb:f::2", 4
        )
        # Compute in a subprocess with a different PYTHONHASHSEED.
        out = subprocess.check_output([
            sys.executable, "-c",
            "from srv6_mrc.topo import select_spines_for_addrs; "
            "import json; "
            "print(json.dumps(list(select_spines_for_addrs("
            "'2001:db8:bbbb::2','2001:db8:bbbb:f::2',4))))"
        ], env={"PYTHONHASHSEED": "12345"})
        sub = tuple(json.loads(out))
        self.assertEqual(local, sub,
                         "select_spines_for_addrs varies with "
                         "PYTHONHASHSEED — must not depend on hash()")

    def test_returns_exactly_n_distinct(self):
        for n in range(1, topo.NUM_SPINES + 1):
            sub = topo.select_spines_for_addrs("a", "z", n)
            self.assertEqual(len(sub), n)
            self.assertEqual(len(set(sub)), n)
            for s in sub:
                self.assertIn(s, range(topo.NUM_SPINES))


if __name__ == "__main__":
    unittest.main()
