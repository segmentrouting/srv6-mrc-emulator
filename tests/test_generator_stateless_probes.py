"""Stateless-probe generator regression tests.

These pin the design invariants from
`docs/stateless-probes-validation.md` and the runtime side's
`srv6_mrc/topo.py::probe_ev_addr` against what the generator
actually emits into `topology.clab.yaml`. If any of these fail in
a future generator change, the lab will silently lose probes —
not crash — so the only safety net is here.

Invariants pinned:

  G1. Per-EV /128 addresses computed by the generator are
      identical to `srv6_mrc.topo.probe_ev_addr(...)` for every
      (host, plane, path) tuple.

  G2. Each per-EV /128 is configured on EXACTLY ONE yellow host
      (the owner). A duplicate would silently consume the probe
      at the wrong host's kernel ingress.

  G3. The peer host's role in the round trip is pure IPv6
      forwarding. The generator must enable
      `net.ipv6.conf.all.forwarding=1` on every yellow host;
      alpine defaults forwarding=0 and the kernel would drop
      the probe at the peer.

  G4. The reserved inner-src placeholder
      `srv6_mrc.topo.probe_inner_src("yellow")` must never appear
      anywhere in the generated clab `exec:` blocks. Configuring
      it locally would cause the kernel to claim the (otherwise
      unrouted) inner-src as its own.

  G5. Every leaf in every plane has exactly one `dfff` static-sid
      in its frr.conf, pointing into vrf default (Linux table
      main). Missing it kills the round trip at the sender's leaf;
      pointing into a VRF would deliver to the wrong RIB.
"""

from __future__ import annotations

import importlib
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOPO_4P_4X8 = REPO_ROOT / "topologies" / "4p-4x8"
CLAB_YAML = TOPO_4P_4X8 / "topology.clab.yaml"


def _load_fabric_module():
    """Import generators.fabric and bind it to the 4p-4x8 topology.

    The generator's module-level NUM_* constants are bound by main();
    importing in isolation leaves them at the 4p-4x8 defaults but
    silently — so the test explicitly loads the YAML and binds them
    to guarantee the regression test compares apples to apples.
    """
    import yaml  # type: ignore[import-not-found]

    fab = importlib.import_module("generators.fabric")
    with open(TOPO_4P_4X8 / "topo.yaml") as f:
        t = yaml.safe_load(f)
    fab.NUM_PLANES = int(t["planes"])
    fab.NUM_SPINES = int(t["spines_per_plane"])
    fab.NUM_LEAVES = int(t["leaves_per_plane"])
    return fab


def _yellow_host_blocks() -> dict[str, list[str]]:
    """Return {hostname: [exec-line, ...]} for every yellow-host node
    in the 4p-4x8 generated clab YAML."""
    text = CLAB_YAML.read_text()
    blocks: dict[str, list[str]] = {}
    # Naive parser: scan for `    yellow-host<NN>:` and capture
    # subsequent `        - "..."` lines under the `exec:` key until
    # the next `    <name>:` or `  links:` line.
    cur_host: str | None = None
    in_exec = False
    for line in text.splitlines():
        m = re.match(r"^    (yellow-host\d+):\s*$", line)
        if m:
            cur_host = m.group(1)
            blocks[cur_host] = []
            in_exec = False
            continue
        if re.match(r"^    [a-zA-Z][\w-]+:\s*$", line) or line.startswith("  links:"):
            cur_host = None
            in_exec = False
            continue
        if cur_host is None:
            continue
        if re.match(r"^      exec:\s*$", line):
            in_exec = True
            continue
        if in_exec and line.startswith("        - "):
            blocks[cur_host].append(line.strip())
        elif in_exec and line and not line.startswith("        "):
            # Left the exec block.
            in_exec = False
    return blocks


class StatelessProbeGeneratorInvariants(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.fab = _load_fabric_module()
        # Runtime-side per-EV addressing must match the generator.
        from srv6_mrc.topo import probe_ev_addr, probe_inner_src
        cls.probe_ev_addr = staticmethod(probe_ev_addr)
        cls.probe_inner_src = staticmethod(probe_inner_src)
        if not CLAB_YAML.is_file():
            raise unittest.SkipTest(
                f"generated clab YAML missing: {CLAB_YAML}; "
                "run `python3 generators/fabric.py "
                "--topo topologies/4p-4x8/topo.yaml`"
            )
        cls.blocks = _yellow_host_blocks()

    # G1 ----------------------------------------------------------------
    def test_generator_matches_runtime_per_ev_addr(self) -> None:
        """Generator's probe_ev_addr_yellow must equal
        srv6_mrc.topo.probe_ev_addr('yellow', ...) for the full grid."""
        for host in range(self.fab.NUM_LEAVES):
            for plane in range(self.fab.NUM_PLANES):
                for path in range(self.fab.NUM_SPINES):
                    gen = self.fab.probe_ev_addr_yellow(host, plane, path)
                    rt = self.probe_ev_addr("yellow", host, plane, path)
                    self.assertEqual(
                        gen, rt,
                        f"generator/runtime disagree on "
                        f"yellow host={host} plane={plane} path={path}: "
                        f"gen={gen!r} runtime={rt!r}",
                    )

    # G2 ----------------------------------------------------------------
    def test_per_ev_addrs_unique_per_host(self) -> None:
        """Each per-EV /128 must appear on EXACTLY ONE host."""
        seen: dict[str, str] = {}  # ev_addr -> owning host
        pat = re.compile(
            r'ip -6 addr add (2001:db8:cccc:[0-9a-f]+::[0-9a-f]+)/128 '
            r'dev (eth\d+)'
        )
        for host, lines in self.blocks.items():
            owner_id = int(host[len("yellow-host"):])
            for line in lines:
                m = pat.search(line)
                if not m:
                    continue
                addr, eth = m.group(1), m.group(2)
                # Skip the anycast /64 — that's a different rule
                # (this regex only matches /128 lines anyway).
                if addr in seen:
                    self.fail(
                        f"per-EV /128 {addr} configured on both "
                        f"{seen[addr]} and {host} — round trip would "
                        f"die at {seen[addr]}'s kernel ingress"
                    )
                seen[addr] = host
                # Check the addr decodes back to owner_id.
                from srv6_mrc.topo import probe_ev_from_inner_dst
                decoded = probe_ev_from_inner_dst(addr)
                self.assertIsNotNone(
                    decoded,
                    f"generated addr {addr} doesn't decode via "
                    f"probe_ev_from_inner_dst",
                )
                _tenant, host_id, plane, _path = decoded
                self.assertEqual(
                    host_id, owner_id,
                    f"addr {addr} on {host} decodes to host_id={host_id}"
                )
                self.assertEqual(
                    f"eth{plane + 1}", eth,
                    f"addr {addr} on {host} is on {eth} but decodes "
                    f"to plane={plane} (expected eth{plane + 1})",
                )

        # Total count: NUM_LEAVES hosts * NUM_PLANES NICs * NUM_SPINES paths
        expected = (
            self.fab.NUM_LEAVES * self.fab.NUM_PLANES * self.fab.NUM_SPINES
        )
        self.assertEqual(
            len(seen), expected,
            f"expected {expected} unique per-EV /128 addresses "
            f"({self.fab.NUM_LEAVES} hosts * {self.fab.NUM_PLANES} planes "
            f"* {self.fab.NUM_SPINES} paths); got {len(seen)}",
        )

    # G3 ----------------------------------------------------------------
    def test_ipv6_forwarding_enabled_on_every_yellow_host(self) -> None:
        for host, lines in self.blocks.items():
            self.assertTrue(
                any(
                    "sysctl -w net.ipv6.conf.all.forwarding=1" in line
                    for line in lines
                ),
                f"{host} is missing net.ipv6.conf.all.forwarding=1; "
                f"alpine defaults to 0 and the peer-side kernel will "
                f"drop the probe at FIB lookup time",
            )

    # G4 ----------------------------------------------------------------
    def test_inner_src_placeholder_never_configured(self) -> None:
        placeholder = self.probe_inner_src("yellow")
        text = CLAB_YAML.read_text()
        self.assertNotIn(
            placeholder, text,
            f"reserved inner-src {placeholder} appears in clab YAML; "
            f"it must NEVER be locally configured (kernel would claim "
            f"it as a local address and break the probe's source-routing)",
        )

    # G5 ----------------------------------------------------------------
    def test_dfff_static_sid_present_on_every_leaf(self) -> None:
        leaf_count = self.fab.NUM_PLANES * self.fab.NUM_LEAVES
        seen = 0
        for plane in range(self.fab.NUM_PLANES):
            for leaf in range(self.fab.NUM_LEAVES):
                frr = (
                    TOPO_4P_4X8 / "config" / f"p{plane}-leaf{leaf:02d}"
                    / "frr.conf"
                )
                self.assertTrue(frr.is_file(), f"missing {frr}")
                txt = frr.read_text()
                # Expect exactly one dfff static-sid line, decapping
                # into vrf default (Linux table main).
                pat = re.compile(
                    rf"sid fc00:000{plane:x}:dfff::/48 locator MAIN "
                    rf"behavior uDT6 vrf default"
                )
                matches = pat.findall(txt)
                self.assertEqual(
                    len(matches), 1,
                    f"p{plane}-leaf{leaf:02d} frr.conf has "
                    f"{len(matches)} dfff static-sid lines; expected 1",
                )
                seen += 1
        self.assertEqual(seen, leaf_count)


if __name__ == "__main__":
    unittest.main()
