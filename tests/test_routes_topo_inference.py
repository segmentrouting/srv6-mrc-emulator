"""Regression tests for `routes apply -f PATH` inferring SRV6_TOPO from PATH.

The bug this guards against: when an operator runs

    python3 -m srv6_mrc.cli.routes apply -f topologies/2p-4x8/routes/full-mesh.yaml

on the deploy host with no `SRV6_TOPO` exported, srv6_mrc.topo silently
falls back to the default (4p-8x16) topology. The route generator then
iterates 4 planes instead of 2 and emits hundreds of spurious `eth3`/`eth4`
route failures against hosts that only have `eth1`/`eth2`.

`srv6_mrc.cli.routes._infer_srv6_topo_from_argv` peeks at argv before
importing srv6_mrc.topo and sets SRV6_TOPO from the `-f`/`--file` path
so the topo constants come out correct.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

# Import the bare function — no side effects, no topo import in this module
# scope. We MUST NOT `from srv6_mrc.cli import routes` at module scope
# because that would also import srv6_mrc.topo and freeze its constants
# for the rest of the test process.

_HERE = Path(__file__).resolve().parent
_ROUTES_PY = _HERE.parent / "srv6_mrc" / "cli" / "routes.py"


def _load_inference_func():
    """Load just the `_infer_srv6_topo_from_argv` symbol via exec, without
    triggering the `from srv6_mrc import topo` at the bottom of routes.py.

    We slice the source up to the topo import line and exec only that prefix.
    Cheaper and more hermetic than importing the whole module."""
    src = _ROUTES_PY.read_text()
    marker = "from srv6_mrc import topo as _topo"
    idx = src.index(marker)
    prefix = src[:idx]
    ns: dict = {}
    # Provide the same `try: import yaml` shim; routes.py exits the process
    # if yaml is missing, but we never reach that branch because the prefix
    # ends before the topo import. yaml IS imported in the prefix, however,
    # so we need it available.
    exec(compile(prefix, str(_ROUTES_PY), "exec"), ns)
    return ns["_infer_srv6_topo_from_argv"]


class TestInferSrv6Topo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fn = staticmethod(_load_inference_func())
        cls.repo_root = _HERE.parent

    def test_relative_path_2p_4x8(self):
        # Real path that exists in the repo; this is the exact form the
        # Makefile passes.
        out = self.fn(["apply", "-f", "topologies/2p-4x8/routes/full-mesh.yaml"])
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith("topologies/2p-4x8/topo.yaml"), out)
        # And the file it points at must actually exist.
        self.assertTrue(Path(out).exists())

    def test_relative_path_4p_8x16(self):
        out = self.fn(["apply", "-f", "topologies/4p-8x16/routes/full-mesh.yaml"])
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith("topologies/4p-8x16/topo.yaml"), out)

    def test_absolute_path(self):
        abs_path = str(self.repo_root / "topologies" / "2p-4x8" / "routes" / "full-mesh.yaml")
        out = self.fn(["apply", "-f", abs_path])
        self.assertEqual(out, str(self.repo_root / "topologies" / "2p-4x8" / "topo.yaml"))

    def test_long_form_file_flag(self):
        out = self.fn(["apply", "--file", "topologies/2p-4x8/routes/full-mesh.yaml"])
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith("topologies/2p-4x8/topo.yaml"))

    def test_equals_form_short(self):
        out = self.fn(["apply", "-f=topologies/2p-4x8/routes/full-mesh.yaml"])
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith("topologies/2p-4x8/topo.yaml"))

    def test_equals_form_long(self):
        out = self.fn(["apply", "--file=topologies/2p-4x8/routes/full-mesh.yaml"])
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith("topologies/2p-4x8/topo.yaml"))

    def test_no_file_flag(self):
        # `routes list` and `routes delete --all` don't take -f; we must not
        # claim to infer anything.
        self.assertIsNone(self.fn(["list", "--tenant", "green"]))
        self.assertIsNone(self.fn(["delete", "--all"]))
        self.assertIsNone(self.fn([]))

    def test_path_not_under_topologies(self):
        # User supplied a one-off spec outside the topologies/ tree. We
        # can't infer; let the user's $SRV6_TOPO (or default) win.
        with tempfile.NamedTemporaryFile(suffix=".yaml") as f:
            self.assertIsNone(self.fn(["apply", "-f", f.name]))

    def test_path_under_topologies_but_not_routes_subdir(self):
        # e.g. `-f topologies/2p-4x8/scenarios/foo.yaml` is not a routes
        # spec; conservatively return None rather than guessing.
        self.assertIsNone(self.fn(
            ["apply", "-f", "topologies/2p-4x8/scenarios/yellow-baseline.yaml"]
        ))

    def test_nonexistent_topo_yaml(self):
        # Path-shape matches but topo.yaml isn't on disk -> None, so
        # srv6_mrc.topo's own fallback handles it.
        with tempfile.TemporaryDirectory() as td:
            spec = Path(td) / "topologies" / "fake-99" / "routes" / "x.yaml"
            spec.parent.mkdir(parents=True)
            spec.touch()
            out = self.fn(["apply", "-f", str(spec)])
            self.assertIsNone(out)


class TestRoutesModuleHonorsInferredTopo(unittest.TestCase):
    """Higher-level: actually exec the routes.py prefix with argv mutated
    and verify SRV6_TOPO ends up set in os.environ. This is the bit that
    the import order at the top of routes.py guarantees, and it's what
    keeps NUM_PLANES correct when the module is run as `python -m`."""

    def test_env_set_from_argv(self):
        import sys
        saved_env = os.environ.get("SRV6_TOPO")
        saved_argv = sys.argv[:]
        try:
            os.environ.pop("SRV6_TOPO", None)
            sys.argv = ["routes.py", "apply", "-f",
                        "topologies/2p-4x8/routes/full-mesh.yaml"]
            src = _ROUTES_PY.read_text()
            marker = "from srv6_mrc import topo as _topo"
            prefix = src[: src.index(marker)]
            ns: dict = {"__name__": "__not_main__"}
            exec(compile(prefix, str(_ROUTES_PY), "exec"), ns)
            self.assertIn("SRV6_TOPO", os.environ)
            self.assertTrue(
                os.environ["SRV6_TOPO"].endswith("topologies/2p-4x8/topo.yaml"),
                os.environ["SRV6_TOPO"],
            )
        finally:
            sys.argv = saved_argv
            if saved_env is None:
                os.environ.pop("SRV6_TOPO", None)
            else:
                os.environ["SRV6_TOPO"] = saved_env

    def test_existing_env_wins(self):
        import sys
        saved_env = os.environ.get("SRV6_TOPO")
        saved_argv = sys.argv[:]
        try:
            os.environ["SRV6_TOPO"] = "/tmp/operator-explicit.yaml"
            sys.argv = ["routes.py", "apply", "-f",
                        "topologies/2p-4x8/routes/full-mesh.yaml"]
            src = _ROUTES_PY.read_text()
            marker = "from srv6_mrc import topo as _topo"
            prefix = src[: src.index(marker)]
            ns: dict = {"__name__": "__not_main__"}
            exec(compile(prefix, str(_ROUTES_PY), "exec"), ns)
            # Operator's choice must not be clobbered.
            self.assertEqual(os.environ["SRV6_TOPO"],
                             "/tmp/operator-explicit.yaml")
        finally:
            sys.argv = saved_argv
            if saved_env is None:
                os.environ.pop("SRV6_TOPO", None)
            else:
                os.environ["SRV6_TOPO"] = saved_env


if __name__ == "__main__":
    unittest.main()
