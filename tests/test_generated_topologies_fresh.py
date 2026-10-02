"""Committed generator output must match what `make regen` produces.

topology.clab.yaml and config/ are generated from topo.yaml and then
committed, so nothing stops a generator change from landing without a
regen of every topology. That happened once: 2p-4x8 and 4p-8x16 missed
the green per-EV probe /128s, which silently breaks green MRC probes on
those fabrics. This test regenerates each topology into a temp dir and
compares byte-for-byte. Fix a failure with `make TOPO=<name> regen`.
"""
from __future__ import annotations

import filecmp
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOPOLOGIES = REPO_ROOT / "topologies"
GENERATOR = REPO_ROOT / "generators" / "fabric.py"


def _diff_trees(a: Path, b: Path) -> list[str]:
    cmp = filecmp.dircmp(a, b)
    out = [str(a / n) for n in cmp.left_only + cmp.right_only + cmp.diff_files]
    out += [str(a / n) for n in cmp.funny_files]
    for sub in cmp.common_dirs:
        out += _diff_trees(a / sub, b / sub)
    return out


class GeneratedTopologiesFreshTests(unittest.TestCase):
    def test_every_topology_matches_regen(self) -> None:
        topo_dirs = sorted(p.parent for p in TOPOLOGIES.glob("*/topo.yaml"))
        self.assertTrue(topo_dirs, "no topologies found")
        for topo_dir in topo_dirs:
            with self.subTest(topology=topo_dir.name), \
                    tempfile.TemporaryDirectory() as tmp:
                tmp_dir = Path(tmp)
                shutil.copy(topo_dir / "topo.yaml", tmp_dir / "topo.yaml")
                subprocess.run(
                    [sys.executable, str(GENERATOR),
                     "--topo", str(tmp_dir / "topo.yaml")],
                    check=True, capture_output=True, cwd=REPO_ROOT,
                )
                stale = []
                if not filecmp.cmp(topo_dir / "topology.clab.yaml",
                                   tmp_dir / "topology.clab.yaml",
                                   shallow=False):
                    stale.append(str(topo_dir / "topology.clab.yaml"))
                stale += _diff_trees(topo_dir / "config", tmp_dir / "config")
                self.assertEqual(
                    stale, [],
                    f"stale generated files; run `make TOPO={topo_dir.name} "
                    f"regen`",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
