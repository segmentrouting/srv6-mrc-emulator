"""Unit tests for srv6_mrc (topo, policy, reorder, ...).

Run from this directory:
    python3 -m unittest discover -s tests -v

No external deps; pure-Python only.

NOTE: this module pins SRV6_TOPO to topologies/4p-8x16/topo.yaml
before any test module imports srv6_mrc. The package's *default*
topology is 4p-4x8 (mid-size dev variant), but the bulk of the test
suite was written against the 4p-8x16 reference design — it pins
specific host IDs (e.g. host15), spine counts (NUM_SPINES==8), and
EV grid sizes (32 EVs / pair) that only exist at that scale.

Tests that intentionally exercise the package default (e.g.
`test_topo.TestTopoConstants::test_fabric_shape`,
`test_srctl.TestGetHosts::test_json_format`) deliberately do NOT
pin SRV6_TOPO and will see the 4p-4x8 default. They are the
regression rail for the default-selection path.

New tests should follow one of two conventions:
  - Default-path test: do not touch SRV6_TOPO; assert against 4p-4x8.
  - Topology-pinned test: rely on this module-level SRV6_TOPO override
    (4p-8x16) or set SRV6_TOPO explicitly in setUp() if you need a
    different surface.
"""

import os
from pathlib import Path

# Pin SRV6_TOPO BEFORE any test module imports srv6_mrc. srv6_mrc.topo
# binds module-level constants (NUM_PLANES, NUM_SPINES, NUM_LEAVES,
# CLAB_TOPOLOGY_NAME) at import time from this env var; once bound,
# they don't change for the life of the interpreter. The test suite
# was written against the 4p-8x16 surface — keep it that way until/
# unless a test explicitly opts into the default 4p-4x8 path.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
_PINNED_TOPO = _REPO_ROOT / "topologies" / "4p-8x16" / "topo.yaml"
os.environ.setdefault("SRV6_TOPO", str(_PINNED_TOPO))
