"""Tests for discover_targets — reads a real topo.yaml fixture."""
from __future__ import annotations

import unittest
from pathlib import Path

from . import _path_shim  # noqa: F401
import scraper  # type: ignore[import-not-found]


_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOPO_4P_4X8 = _REPO_ROOT / "topologies" / "4p-4x8" / "topo.yaml"


@unittest.skipUnless(_TOPO_4P_4X8.exists(),
                     f"topology fixture not found: {_TOPO_4P_4X8}")
class DiscoverTargetsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.iface, cls.hosts = scraper.discover_targets(_TOPO_4P_4X8)

    def test_iface_target_count(self) -> None:
        # 4p-4x8 has planes=4, leaves_per_plane=8, tenants=[green, yellow].
        # Leaf Ethernet0: planes * leaves_per_plane = 32.
        # Host eth1..eth4 per host: tenants*leaves*planes = 2*8*4 = 64.
        # Total iface targets = 32 + 64 = 96.
        self.assertEqual(len(self.iface), 96)

    def test_host_target_count(self) -> None:
        # tenants * leaves_per_plane = 2 * 8 = 16 hosts.
        self.assertEqual(len(self.hosts), 16)

    def test_specific_leaf_iface_target(self) -> None:
        leaf_targets = [t for t in self.iface if t.tier == "leaf"]
        self.assertEqual(len(leaf_targets), 32)
        names = {(t.node, t.iface, t.plane) for t in leaf_targets}
        self.assertIn(("p0-leaf00", "Ethernet0", 0), names)
        self.assertIn(("p3-leaf07", "Ethernet0", 3), names)

    def test_specific_host_iface_target(self) -> None:
        host_ifaces = [t for t in self.iface if t.tier == "host"]
        self.assertEqual(len(host_ifaces), 64)
        names = {(t.node, t.iface, t.plane) for t in host_ifaces}
        self.assertIn(("green-host00", "eth1", 0), names)
        self.assertIn(("yellow-host07", "eth4", 3), names)

    def test_host_snmp6_targets(self) -> None:
        names = {(t.host, t.tenant) for t in self.hosts}
        self.assertIn(("green-host00", "green"), names)
        self.assertIn(("yellow-host07", "yellow"), names)

    def test_no_spine_targets_in_pr1(self) -> None:
        """Spine NIC scrape deferred to PR 2 — confirm tier='spine' absent."""
        spine_targets = [t for t in self.iface if t.tier == "spine"]
        self.assertEqual(spine_targets, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
