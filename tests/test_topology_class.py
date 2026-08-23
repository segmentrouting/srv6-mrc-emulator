"""Exhaustive parity sweep: `srv6_mrc.topology.Topology` vs `srv6_mrc.topo`.

Refactor 1 strategy is to replace import-time-bound module globals in
`srv6_mrc.topo` with a `Topology` value class passed as a parameter.
Migration is per-call-site; the class lives alongside the module
functions until every call site has flipped over.

This test pins behavioural equivalence for every method on `Topology`
that has a counterpart in `topo`. For each shipped topology
(`2p-4x8`, `4p-4x8`, `4p-8x16`), the sweep:

  1. Sets `SRV6_TOPO` to that topology's yaml.
  2. Reloads `srv6_mrc.topo` so its module globals rebind.
  3. Loads the same yaml into a `Topology` via `from_yaml`.
  4. Asserts that, for every valid input tuple in the cartesian
     product of (tenants, planes, spines, host_ids), every method on
     the class returns exactly what the matching `topo.<func>` returns.

If any assertion fails, that's a behavioural divergence — the
migration is not safe. The "exhaustive" cardinality is small enough
that the whole sweep runs in well under a second:

  4p-8x16 (largest):  2 tenants * 4 planes * 8 spines * 16 hosts
                    = 1024 (tenant, plane, spine, host) tuples.

Module-level reload note
========================

`srv6_mrc.topo._TOPO` is read once at import time. Switching topologies
requires `importlib.reload(topo)`. We do that inside `_with_topology`
which yields a context where both `topo` (reloaded) and a fresh
`Topology` instance refer to the same yaml. Restoring the
original `SRV6_TOPO` after each topology keeps subsequent tests in the
suite (which import `topo` indirectly) unaffected.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import unittest
from pathlib import Path

from srv6_mrc import topo as topo_mod
from srv6_mrc.topology import Topology


REPO_ROOT = Path(__file__).resolve().parent.parent
TOPOLOGIES = ("2p-4x8", "4p-4x8", "4p-8x16")


def _yaml_path(name: str) -> Path:
    return REPO_ROOT / "topologies" / name / "topo.yaml"


@contextlib.contextmanager
def _with_topology(name: str):
    """Bind both `topo` (reloaded) and a fresh `Topology` to `name`."""
    yaml_path = _yaml_path(name)
    if not yaml_path.exists():
        raise unittest.SkipTest(f"missing fixture {yaml_path}")
    saved = os.environ.get("SRV6_TOPO")
    os.environ["SRV6_TOPO"] = str(yaml_path)
    try:
        importlib.reload(topo_mod)
        T = Topology.from_yaml(yaml_path)
        yield T, topo_mod
    finally:
        if saved is None:
            os.environ.pop("SRV6_TOPO", None)
        else:
            os.environ["SRV6_TOPO"] = saved
        importlib.reload(topo_mod)  # leave module in default state


class _ParityBase(unittest.TestCase):
    """Mixin: a single subclass per topology runs the full sweep."""

    TOPO_NAME: str = ""  # overridden in subclasses

    @classmethod
    def setUpClass(cls):
        if not cls.TOPO_NAME:
            raise unittest.SkipTest("base class")
        cls._ctx = _with_topology(cls.TOPO_NAME)
        cls.T, cls.topo = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    # --- dimensional sanity ------------------------------------------

    def test_dimensions_match_module(self):
        self.assertEqual(self.T.planes, self.topo.NUM_PLANES)
        self.assertEqual(self.T.spines_per_plane, self.topo.NUM_SPINES)
        self.assertEqual(self.T.leaves_per_plane, self.topo.NUM_LEAVES)
        self.assertEqual(self.T.tenants, self.topo.TENANTS)
        self.assertEqual(self.T.clab_topology_name,
                         self.topo.CLAB_TOPOLOGY_NAME)

    def test_tenant_id_map_matches(self):
        self.assertEqual(self.T.tenant_id_map, self.topo.TENANT_ID)
        self.assertEqual(self.T.tenant_by_id, self.topo.TENANT_BY_ID)

    def test_plane_nics_match(self):
        self.assertEqual(self.T.plane_nics, self.topo.PLANE_NICS)
        for p in range(self.T.planes):
            self.assertEqual(self.T.plane_nic(p), self.topo.PLANE_NIC(p))

    # --- identity ----------------------------------------------------

    def test_tenant_id_round_trip(self):
        for name in self.T.tenants:
            tid = self.T.tenant_id(name)
            self.assertEqual(tid, self.topo.tenant_id(name))
            self.assertEqual(self.T.tenant_name(tid),
                             self.topo.tenant_name(tid))

    def test_host_name_parity(self):
        for tenant in self.T.tenants:
            for host_id in range(self.T.leaves_per_plane):
                self.assertEqual(self.T.host_name(tenant, host_id),
                                 self.topo.host_name(tenant, host_id))

    # --- addressing --------------------------------------------------

    def test_inner_addr_parity(self):
        for tenant in self.T.tenants:
            for host_id in range(self.T.leaves_per_plane):
                self.assertEqual(self.T.inner_addr(tenant, host_id),
                                 self.topo.inner_addr(tenant, host_id))

    def test_leaf_gateway_addr_parity(self):
        for tenant in self.T.tenants:
            for plane in range(self.T.planes):
                for host_id in range(self.T.leaves_per_plane):
                    self.assertEqual(
                        self.T.leaf_gateway_addr(tenant, plane, host_id),
                        self.topo.leaf_gateway_addr(tenant, plane, host_id),
                    )

    def test_usid_outer_dst_parity(self):
        for tenant in self.T.tenants:
            for plane in range(self.T.planes):
                for spine in range(self.T.spines_per_plane):
                    for dst_leaf in range(self.T.leaves_per_plane):
                        for sid_mode in ("uA", "uN"):
                            self.assertEqual(
                                self.T.usid_outer_dst(
                                    tenant, plane, spine, dst_leaf,
                                    sid_mode=sid_mode),
                                self.topo.usid_outer_dst(
                                    tenant, plane, spine, dst_leaf,
                                    sid_mode=sid_mode),
                            )

    def test_usid_outer_dst_bad_sid_mode_rejected(self):
        with self.assertRaises(ValueError):
            self.T.usid_outer_dst("green", 0, 0, 0, sid_mode="uB")

    # --- selection ---------------------------------------------------

    def test_spine_for_parity(self):
        L = self.T.leaves_per_plane
        for a in range(L):
            for b in range(L):
                self.assertEqual(
                    self.T.spine_for(a, b),
                    self.topo.spine_for(a, b),
                )

    def test_select_spines_parity(self):
        L = self.T.leaves_per_plane
        S = self.T.spines_per_plane
        for a in range(L):
            for b in range(L):
                for n in range(1, S + 1):
                    self.assertEqual(
                        self.T.select_spines(a, b, n),
                        self.topo.select_spines(a, b, n),
                    )

    def test_select_spines_for_addrs_parity(self):
        S = self.T.spines_per_plane
        # Sample every (tenant, src_host, dst_host); skip the inner sweep
        # over n>1 just to keep the cardinality reasonable on 4p-8x16.
        for tenant in self.T.tenants:
            for a in range(self.T.leaves_per_plane):
                for b in range(self.T.leaves_per_plane):
                    src = self.T.inner_addr(tenant, a)
                    dst = self.T.inner_addr(tenant, b)
                    for n in (1, S // 2 or 1, S):
                        self.assertEqual(
                            self.T.select_spines_for_addrs(src, dst, n),
                            self.topo.select_spines_for_addrs(src, dst, n),
                        )

    # --- validation parity -------------------------------------------

    def test_check_tenant_rejects_bad(self):
        with self.assertRaises(ValueError):
            self.T.check_tenant("not-a-tenant")

    def test_check_plane_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            self.T.check_plane(self.T.planes)
        with self.assertRaises(ValueError):
            self.T.check_plane(-1)

    def test_check_spine_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            self.T.check_spine(self.T.spines_per_plane)

    def test_check_host_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            self.T.check_host(self.T.leaves_per_plane)

    def test_select_spines_rejects_zero_and_too_many(self):
        with self.assertRaises(ValueError):
            self.T.select_spines(0, 1, 0)
        with self.assertRaises(ValueError):
            self.T.select_spines(0, 1, self.T.spines_per_plane + 1)

    # --- frozen / hashable -------------------------------------------

    def test_topology_is_hashable_and_equal_to_self(self):
        # Frozen dataclass should be usable as a dict key.
        d = {self.T: "v"}
        self.assertEqual(d[self.T], "v")
        # Reload should produce an equal topology.
        T2 = Topology.from_yaml(_yaml_path(self.TOPO_NAME))
        self.assertEqual(self.T, T2)


class TestParity_2p_4x8(_ParityBase):
    TOPO_NAME = "2p-4x8"


class TestParity_4p_4x8(_ParityBase):
    TOPO_NAME = "4p-4x8"


class TestParity_4p_8x16(_ParityBase):
    TOPO_NAME = "4p-8x16"


class TestFromDictDefaults(unittest.TestCase):
    """Constructor edge cases that don't need a yaml file."""

    def test_minimal_dict(self):
        T = Topology.from_dict({
            "name": "tiny",
            "planes": 2,
            "spines_per_plane": 2,
            "leaves_per_plane": 2,
            "tenants": ["green"],
        })
        self.assertEqual(T.planes, 2)
        self.assertEqual(T.tenants, ("green",))
        # Default clab name uses topology name.
        self.assertEqual(T.clab_topology_name, "sonic-docker-tiny")
        # No reference pairs for non-canonical topology.
        self.assertEqual(T._reference_pairs_items, frozenset())

    def test_4p_8x16_implicit_reference_pairs(self):
        """`name == 4p-8x16` without explicit reference_pairs_spines
        should still pick up the historical 8-pair table — that's the
        default-driven behaviour `topo.py` has today."""
        T = Topology.from_dict({
            "name": "4p-8x16",
            "planes": 4, "spines_per_plane": 8, "leaves_per_plane": 16,
            "tenants": ["green", "yellow"],
        })
        self.assertEqual(T.spine_for(0, 15), 0)
        self.assertEqual(T.spine_for(7, 8), 7)
        self.assertEqual(T.spine_for(15, 0), 0)  # symmetry

    def test_explicit_reference_pairs_override_default(self):
        T = Topology.from_dict({
            "name": "4p-8x16",
            "planes": 4, "spines_per_plane": 8, "leaves_per_plane": 16,
            "tenants": ["green", "yellow"],
            "reference_pairs_spines": [[0, 1, 3]],
        })
        self.assertEqual(T.spine_for(0, 1), 3)
        # (0, 15) no longer in the explicit table -> hash fallback.
        self.assertEqual(T.spine_for(0, 15),
                         (0 * 16 + 15) % 8)

    def test_clab_topology_name_override(self):
        T = Topology.from_dict({
            "name": "x",
            "planes": 1, "spines_per_plane": 1, "leaves_per_plane": 1,
            "tenants": ["green"],
            "clab": {"topology_name": "custom-name"},
        })
        self.assertEqual(T.clab_topology_name, "custom-name")


if __name__ == "__main__":
    unittest.main()
