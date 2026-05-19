import subprocess
import unittest

from srv6_mrc import netem
from srv6_mrc.topo import NUM_LEAVES, NUM_PLANES


# --- parse_target -----------------------------------------------------------

class TestParseTarget(unittest.TestCase):
    def test_plane(self):
        nics = netem.parse_target("plane 2")
        self.assertEqual(len(nics), 2 * NUM_LEAVES)
        # all on eth3 (plane 2)
        self.assertTrue(all(n.ifname == "eth3" for n in nics))
        # covers both tenants
        tenants = {n.container.split("-host")[0] for n in nics}
        self.assertEqual(tenants, {"green", "yellow"})
        # one entry per (tenant, host_id)
        self.assertEqual(len({n.container for n in nics}), 2 * NUM_LEAVES)

    def test_plane_extra_whitespace(self):
        nics = netem.parse_target("  plane   0  ")
        self.assertEqual(len(nics), 2 * NUM_LEAVES)
        self.assertTrue(all(n.ifname == "eth1" for n in nics))

    def test_plane_out_of_range(self):
        with self.assertRaises(ValueError):
            netem.parse_target(f"plane {NUM_PLANES}")

    def test_host_only(self):
        nics = netem.parse_target("host green-host00")
        self.assertEqual(len(nics), NUM_PLANES)
        self.assertTrue(all(n.container == "green-host00" for n in nics))
        self.assertEqual({n.ifname for n in nics},
                         {f"eth{p+1}" for p in range(NUM_PLANES)})

    def test_host_plane(self):
        nics = netem.parse_target("host yellow-host15 plane 3")
        self.assertEqual(nics, [netem.NICTarget("yellow-host15", "eth4")])

    def test_bad_host_name(self):
        with self.assertRaises(ValueError):
            netem.parse_target("host red-host00")
        with self.assertRaises(ValueError):
            netem.parse_target("host green-host16")     # id out of range
        with self.assertRaises(ValueError):
            netem.parse_target("host green-host0")      # missing leading zero

    def test_bad_grammar(self):
        for bad in ["", "plane", "plane abc", "leaf 0",
                    "host", "host green-host00 plane",
                    "garbage"]:
            with self.subTest(target=bad), self.assertRaises(ValueError):
                netem.parse_target(bad)

    def test_non_string(self):
        with self.assertRaises(ValueError):
            netem.parse_target(123)  # type: ignore[arg-type]


# --- normalize_spec ---------------------------------------------------------

class TestNormalizeSpec(unittest.TestCase):
    def test_simple_loss(self):
        self.assertEqual(netem.normalize_spec("loss 5%"), ["loss", "5%"])

    def test_combined(self):
        self.assertEqual(
            netem.normalize_spec("delay 50ms 10ms 25%"),
            ["delay", "50ms", "10ms", "25%"],
        )

    def test_blackhole_sugar(self):
        self.assertEqual(netem.normalize_spec("blackhole"), ["loss", "100%"])

    def test_bad_token_rejected(self):
        # shell-injection-shaped inputs blocked early
        for bad in ["loss; rm -rf /", "loss 5%; echo hi", "loss `id`",
                    "delay $(date)"]:
            with self.subTest(spec=bad), self.assertRaises(ValueError):
                netem.normalize_spec(bad)

    def test_bad_leading_keyword(self):
        with self.assertRaises(ValueError):
            netem.normalize_spec("fictional 5%")

    def test_empty(self):
        with self.assertRaises(ValueError):
            netem.normalize_spec("")
        with self.assertRaises(ValueError):
            netem.normalize_spec("   ")


# --- argv builders ----------------------------------------------------------

class TestArgvBuilders(unittest.TestCase):
    def test_add_argv(self):
        argv = netem._nsenter_tc_argv_add(1234, "eth2", ["loss", "5%"])
        self.assertEqual(argv, [
            "nsenter", "-t", "1234", "-n",
            "tc", "qdisc", "add", "dev", "eth2", "root", "netem",
            "loss", "5%",
        ])

    def test_del_argv(self):
        argv = netem._nsenter_tc_argv_del(1234, "eth2")
        self.assertEqual(argv, [
            "nsenter", "-t", "1234", "-n",
            "tc", "qdisc", "del", "dev", "eth2", "root",
        ])
        # No netem tokens at the end
        self.assertNotIn("netem", argv)


# --- mock runner ------------------------------------------------------------

class MockRunner:
    """Records every argv and returns rc=0 by default."""

    def __init__(self, rc_for=None, stdout_for=None):
        self.calls: list[list[str]] = []
        self._rc_for = rc_for or {}
        self._stdout_for = stdout_for or {}

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        key = tuple(argv)
        rc = self._rc_for.get(key, 0)
        # default stdout: pretend `docker inspect` returns a PID
        if argv[:2] == ["docker", "inspect"]:
            out = self._stdout_for.get(key, "9999\n")
        else:
            out = self._stdout_for.get(key, "")
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


class TestResolveContainerPid(unittest.TestCase):
    def test_bare_name_succeeds(self):
        r = MockRunner()
        pid = netem.resolve_container_pid("green-host00", runner=r)
        self.assertEqual(pid, 9999)
        # one call only — fallback not needed
        self.assertEqual(len(r.calls), 1)
        self.assertIn("green-host00", r.calls[0])

    def test_fallback_to_clab_prefix(self):
        bare_key = tuple(netem._docker_pid_cmd("green-host00"))
        clab_key = tuple(netem._docker_pid_cmd(
            "clab-sonic-docker-4p-8x16-green-host00"))
        r = MockRunner(
            rc_for={bare_key: 1},
            stdout_for={clab_key: "4242\n"},
        )
        pid = netem.resolve_container_pid("green-host00", runner=r)
        self.assertEqual(pid, 4242)
        self.assertEqual(len(r.calls), 2)

    def test_both_fail(self):
        r = MockRunner(rc_for={
            tuple(netem._docker_pid_cmd("ghost")): 1,
            tuple(netem._docker_pid_cmd("clab-sonic-docker-4p-8x16-ghost")): 1,
        })
        with self.assertRaises(RuntimeError):
            netem.resolve_container_pid("ghost", runner=r)


# --- Netem apply / revert ---------------------------------------------------

class TestNetemApplyRevert(unittest.TestCase):
    def test_dry_run_emits_no_runner_calls(self):
        r = MockRunner()
        nm = netem.Netem(
            faults=[netem.Fault("host green-host00 plane 2", "loss 5%")],
            runner=r,
        )
        invoked = nm.apply(dry_run=True)
        self.assertEqual(r.calls, [])  # nothing actually run
        self.assertEqual(len(invoked), 1)
        self.assertIn("loss", invoked[0])
        self.assertIn("5%", invoked[0])

    def test_apply_then_revert_pairs(self):
        r = MockRunner()
        nm = netem.Netem(
            faults=[
                netem.Fault("host green-host00 plane 2", "loss 5%"),
                netem.Fault("host yellow-host00 plane 2", "delay 10ms"),
            ],
            runner=r,
        )
        apply_argvs = nm.apply()
        self.assertEqual(len(apply_argvs), 2)
        # 2 docker inspect calls + 2 nsenter tc add calls
        self.assertEqual(len(r.calls), 4)
        revert_argvs = nm.revert()
        self.assertEqual(len(revert_argvs), 2)
        # all revert argvs are `tc qdisc del`
        for argv in revert_argvs:
            self.assertIn("del", argv)

    def test_plane_target_fans_out(self):
        r = MockRunner()
        nm = netem.Netem(
            faults=[netem.Fault("plane 1", "loss 1%")],
            runner=r,
        )
        invoked = nm.apply()
        # one nsenter call per (tenant, host) = 32
        self.assertEqual(len(invoked), 2 * NUM_LEAVES)
        # all target eth2 (plane 1)
        for argv in invoked:
            self.assertIn("eth2", argv)

    def test_apply_failure_cleans_up(self):
        # First nsenter add succeeds, second fails -> the first should be
        # reverted automatically before RuntimeError propagates.
        r = MockRunner()
        nm = netem.Netem(
            faults=[
                netem.Fault("host green-host00 plane 0", "loss 1%"),
                netem.Fault("host green-host01 plane 0", "loss 1%"),
            ],
            runner=r,
        )
        # Make the second `tc qdisc add` fail.
        def runner(argv):
            argv = list(argv)
            r.calls.append(argv)
            if argv[:2] == ["docker", "inspect"]:
                return subprocess.CompletedProcess(argv, 0,
                                                   stdout="123\n", stderr="")
            if "add" in argv and "green-host01" not in str(argv):
                # green-host01 path: docker inspect ran first; we need to
                # fail the add. Easier: fail the 2nd `tc qdisc add` call.
                pass
            # 2nd `tc add` -> fail
            adds = [c for c in r.calls if "add" in c and "tc" in c]
            if "add" in argv and "tc" in argv and len(adds) == 2:
                return subprocess.CompletedProcess(argv, 2,
                                                   stdout="", stderr="boom")
            return subprocess.CompletedProcess(argv, 0,
                                               stdout="", stderr="")

        nm.runner = runner
        with self.assertRaises(RuntimeError):
            nm.apply()
        # The successful first apply must have been reverted.
        dels = [c for c in r.calls if "del" in c and "tc" in c]
        self.assertEqual(len(dels), 1)

    def test_context_manager(self):
        r = MockRunner()
        nm = netem.Netem(
            faults=[netem.Fault("host green-host00 plane 0", "loss 1%")],
            runner=r,
        )
        with nm:
            adds = [c for c in r.calls if "add" in c and "tc" in c]
            self.assertEqual(len(adds), 1)
        dels = [c for c in r.calls if "del" in c and "tc" in c]
        self.assertEqual(len(dels), 1)

    def test_bad_target_raises_before_runner(self):
        r = MockRunner()
        nm = netem.Netem(
            faults=[netem.Fault("plane 99", "loss 1%")],
            runner=r,
        )
        with self.assertRaises(ValueError):
            nm.apply()
        self.assertEqual(r.calls, [])

    def test_bad_spec_raises_before_runner(self):
        r = MockRunner()
        nm = netem.Netem(
            faults=[netem.Fault("plane 0", "rm -rf /")],
            runner=r,
        )
        with self.assertRaises(ValueError):
            nm.apply()
        self.assertEqual(r.calls, [])


if __name__ == "__main__":
    unittest.main()
