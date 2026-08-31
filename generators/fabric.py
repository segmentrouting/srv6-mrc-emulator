#!/usr/bin/env python3
"""
Generate a 4-plane x (8-spine x 16-leaf) SRv6 CLOS for docker-sonic-vs + Containerlab.

Design summary
--------------
* 4 planes (p0..p3), each plane is a fully independent 8x16 Clos.
  - Spine nodes: p<P>-spine<S>      P in 0..3, S in 0..7
  - Leaf  nodes: p<P>-leaf<L>       P in 0..3, L in 0..15 (hex)
* 16 green hosts + 16 yellow hosts. Each host has 4 uplinks (eth1..eth4),
  one to the same-numbered leaf in each plane.
    green-host<NN>:eth(P+1) -> p<P>-leaf<NN>:Ethernet32   (Vrf-green on leaf)
    yellow-host<NN>:eth(P+1) -> p<P>-leaf<NN>:Ethernet36  (default VRF on leaf)

Addressing — per-plane uSID blocks
----------------------------------
The cluster is a /30 of /32 plane blocks under fc00::/16:

    cluster aggregate :  fc00:0000::/30   (covers all 4 planes)
    plane <P> block   :  fc00:000<P>::/32

Locators (unique per node, fabric-wide):
    spine:  fc00:000<P>:1<S>::/48     loopback fc00:000<P>:1<S>::1
    leaf :  fc00:000<P>:2<L>::/48     loopback fc00:000<P>:2<L>::1

Fabric P2P (reused identically per plane; planes are L2-isolated):
    2001:db8:fab:<spine*16+leaf>::/127     spine ::0  /  leaf ::1

Host uplinks (plane-independent anycast — carries SRv6-encap traffic):
    green-host<NN>  eth(P+1) : 2001:db8:bbbb:<NN>::2/64  (anycast, nodad)
                               leaf-side gw 2001:db8:bbbb:<NN>::1 (anycast)
    yellow-host<NN> eth(P+1) : 2001:db8:cccc:<NN>::2/64  (anycast, nodad)
                               leaf-side gw 2001:db8:cccc:<NN>::1 (anycast)

Host tenant addresses (plane-independent, sprayable per MRC/SRv6 model):
    green-host<NN>  : 2001:db8:bbbb:<NN>::2  — anycast on all 4 NICs (nodad)
                       egress-leaf Ethernet32 in Vrf-green carries the matching
                       ::1/64 connected gateway, identical on every leaf in
                       every plane that homes this host. Decap via uDT6 d000.
    yellow-host<NN> : 2001:db8:cccc:<NN>::2  — anycast on all 4 NICs + lo
                       (nodad). Phase 1a: same address pattern as green; the
                       egress leaf forwards via uA(host-port) on Ethernet36
                       to the host's anycast NIC; the host decaps via
                       seg6local End.DT6 table 0 (one entry per plane NIC,
                       unchanged) and the inner DA resolves on lo.

The bbbb/cccc inner destinations are plane-agnostic so a sender can spray a
single flow across all 4 planes (varying only the SID list) and the receiver
sees one socket. Plane identity lives in the OUTER SID list, never in the
inner/tenant address.

uSID function-bits namespace (per plane block)
----------------------------------------------
* f00<S>    : leaf-side uA toward spine S in this plane    (used on leaves)
* e00<L>    : spine-side uA toward leaf L in this plane    (used on spines)
* d000      : tenant-ID uDT6 -> Vrf-green                  (every leaf)
* d001      : tenant-ID uDT6 -> table 0                    (every yellow host,
                                                            installed once per
                                                            plane on its NIC)

Plane is encoded in the locator block; function-bits no longer need to carry
plane. A controller-generated path like
    fc00:0002:20a:f203:d000::
unambiguously says: "plane 2, deliver to leaf0a, egress toward spine03 (uA),
then decap into Vrf-green at the next hop". Each plane is a self-contained /32
which simplifies WAN advertisement, plane isolation, and fault domain reasoning.

Controller-driven (no BGP / no IGP)
-----------------------------------
frr.conf carries no router bgp. Instead each node has:
  - segment-routing srv6 locator (uN) and the static uA / uDT SIDs above
  - static IPv6 routes for every remote locator in its own plane (via the
    correct connected /127) so packets actually have a FIB entry to forward
    on. This is the minimum data plane on which the controller programs end-
    to-end SR policies and tenant routes.

Run:  python3 generate_fabric.py
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# Module-level constants populated from topo.yaml in main(). Default
# values (4p-4x8) are set here so a bare `import generators.fabric`
# in tests or REPL still works; main() overrides them from the YAML
# before any write_* function runs.
NUM_PLANES = 4
NUM_SPINES = 4
NUM_LEAVES = 8

TOPOLOGY_NAME = "sonic-docker-4p-4x8"
SONIC_IMAGE = "docker-sonic-vs:latest"
HOST_IMAGE = "alpine-srv6-scapy:1.0"

# Filled in by main(): CONFIG_DIR is <topo-dir>/config, TOPO_DIR is the
# directory containing the topo.yaml driving this run, REF_LEAF_CONFIG
# is the SONiC PORT-table reference (committed under the same tree).
SCRIPT_DIR = Path(__file__).resolve().parent       # generators/
REPO_ROOT = SCRIPT_DIR.parent                       # repo root
TOPO_DIR: Path = REPO_ROOT / "topologies" / "4p-4x8"
CONFIG_DIR: Path = TOPO_DIR / "config"
REF_LEAF_CONFIG: Path = CONFIG_DIR / "p0-leaf00" / "config_db.json"

# Embedded SONiC PORT-table template, identical across every spine and
# leaf in every topology size (docker-sonic-vs ships a single 32-port
# config). Used as the default seed so new topologies don't need a
# pre-existing config_db.json before the first `make regen`.
EMBEDDED_PORT_TEMPLATE: Path = SCRIPT_DIR / "sonic_vs_port_table.json"

# Management subnet (172.20.18.0/24): plenty of room for 96 switches + 32 hosts.
MGMT_SUBNET_BASE = "172.20.18"
SPINE_MGMT_START = 10   # .10 .. .41   (4 planes * 8 spines = 32)
LEAF_MGMT_START = 50    # .50 .. .113  (4 planes * 16 leaves = 64)
HOST_MGMT_START = 200   # .200 .. .231 (16 green + 16 yellow = 32)

GREEN_VRF = "Vrf-green"

# Function-bits in d-space for tenant-ID uDT6 SIDs. Tenant SIDs live INSIDE
# each plane's /32 (since the locator block already identifies the plane).
# Green is decapped at the egress leaf; yellow is decapped at the destination
# host (which therefore needs one seg6local per plane on its respective NIC).
TENANT_GREEN_SID_FUNC = 0xd000
TENANT_YELLOW_SID_FUNC = 0xd001


def plane_block_prefix(plane: int) -> str:
    """fc00:000<P> — the /32 high bits of plane <P>."""
    return f"fc00:000{plane:x}"


def plane_aggregate(plane: int) -> str:
    return f"{plane_block_prefix(plane)}::/32"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def hex1(n: int) -> str:
    return f"{n:x}"


def link_idx(spine: int, leaf: int) -> int:
    return spine * NUM_LEAVES + leaf


def p2p_prefix(spine: int, leaf: int) -> str:
    """Reused identically per plane."""
    return f"2001:db8:fab:{link_idx(spine, leaf):04x}"


def spine_name(plane: int, spine: int) -> str:
    return f"p{plane}-spine{spine:02d}"


def leaf_name(plane: int, leaf: int) -> str:
    return f"p{plane}-leaf{leaf:02d}"


def spine_locator(plane: int, spine: int) -> str:
    return f"{plane_block_prefix(plane)}:1{hex1(spine)}::/48"


def spine_loopback(plane: int, spine: int) -> str:
    return f"{plane_block_prefix(plane)}:1{hex1(spine)}::1"


def spine_loopback_v4(plane: int, spine: int) -> str:
    return f"10.0.{plane}.{spine + 1}"


def leaf_locator(plane: int, leaf: int) -> str:
    return f"{plane_block_prefix(plane)}:2{hex1(leaf)}::/48"


def leaf_loopback(plane: int, leaf: int) -> str:
    return f"{plane_block_prefix(plane)}:2{hex1(leaf)}::1"


def leaf_loopback_v4(plane: int, leaf: int) -> str:
    return f"10.1.{plane}.{leaf + 1}"


def leaf_uplink_eth(spine: int) -> str:
    """On a leaf, uplink toward spine S is Ethernet(S*4)."""
    return f"Ethernet{spine * 4}"


def spine_downlink_eth(leaf: int) -> str:
    """On a spine, downlink toward leaf L is Ethernet(L*4)."""
    return f"Ethernet{leaf * 4}"


def leaf_ua_sid(plane: int, spine: int) -> str:
    """f00<S>: leaf-side uA SID toward spine S, inside plane <P>'s block."""
    return f"{plane_block_prefix(plane)}:f00{hex1(spine)}::/48"


def spine_ua_sid(plane: int, leaf: int) -> str:
    """e00<L>: spine-side uA SID toward leaf L, inside plane <P>'s block."""
    return f"{plane_block_prefix(plane)}:e00{hex1(leaf)}::/48"


def leaf_host_ua_sid(plane: int, eth_port_num: int) -> str:
    """e<NNN>: leaf-side uA SID toward whatever's attached on Ethernet<eth_port_num>.

    Same e-space and same numbering rule as spine→leaf: function value is the
    NIC ordinal (port_number / 4), so Ethernet0=e000, Ethernet4=e001,
    Ethernet32=e008, Ethernet36=e009, Ethernet40=e00a, etc.

    This matches the spine pattern exactly (on a spine, leaf L is on
    Ethernet<L*4>, which is NIC ordinal L = e00<L>) so a single rule applies
    fabric-wide: 'e<ordinal> = step down out the port at this NIC ordinal'.
    """
    nic_ordinal = eth_port_num // 4
    return f"{plane_block_prefix(plane)}:e{nic_ordinal:03x}::/48"


def green_udt_sid(plane: int) -> str:
    """uDT6 -> Vrf-green for plane <P>."""
    return f"{plane_block_prefix(plane)}:{TENANT_GREEN_SID_FUNC:04x}::/48"


def yellow_udt_sid(plane: int) -> str:
    """uDT6 -> table 0 for plane <P> (installed on yellow hosts)."""
    return f"{plane_block_prefix(plane)}:{TENANT_YELLOW_SID_FUNC:04x}::/48"


# Stateless-probe leaf-side uDT6 SID (per-plane). Decap target for the
# 6-slot round-trip uSID list; the inner packet is delivered via the
# leaf's connected route to its attached host's tenant NIC. One per
# leaf per plane block — NOT per-EV, NOT per-host. See
# `srv6_mrc/topo.py::PROBE_LEAF_DFFF_PREFIX_FMT` and
# `docs/stateless-probes-validation.md`.
def probe_dfff_sid(plane: int) -> str:
    return f"{plane_block_prefix(plane)}:dfff::/48"


# Stateless-probe per-(host, plane, path) /128 unicast addresses.
# These mirror `srv6_mrc/topo.py::probe_ev_addr` exactly — keep in
# sync. Plane is encoded via the NIC ordinal (eth1 = plane 0, so
# NIC = plane + 1); path is the spine index 0..NUM_SPINES-1. The
# low byte of the trailing hextet is (NIC << 4) | path, so the
# 4p-4x8 yellow layout puts e.g. host00 eth1 path0 at cccc:0::10
# and host00 eth4 path3 at cccc:0::43.
#
# Invariants pinned by tests/test_generator_stateless_probes.py:
#   1. Each /128 is configured on EXACTLY ONE host (the owner).
#   2. The owner's eth(plane+1) is the only NIC carrying it.
#   3. The inner-src placeholder cccc::ffff is never configured.
def probe_ev_addr_yellow(host: int, plane: int, path: int) -> str:
    nic = plane + 1  # eth1 = 1, ..., eth4 = 4
    low_byte = (nic << 4) | path
    return f"2001:db8:cccc:{host:x}::{low_byte:x}"


def probe_ev_addr_green(host: int, plane: int, path: int) -> str:
    """Green stateless-probe per-(host, plane, path) /128 unicast address.
    
    Mirrors probe_ev_addr_yellow with bbbb:: prefix instead of cccc::.
    Configured on the green host's per-plane NIC (eth1..eth4). When a probe
    returns via the leaf's d000 End.DT6 (Vrf-green), the leaf forwards the
    decapped inner packet to the host via the connected /64 route.
    """
    nic = plane + 1  # eth1 = 1, ..., eth4 = 4
    low_byte = (nic << 4) | path
    return f"2001:db8:bbbb:{host:x}::{low_byte:x}"


def host_uplink_prefix(color: str, plane: int, host: int) -> str:
    """DEPRECATED (Phase 1a): per-(host,plane) underlay /64.

    Retained only for transitional callers/tests; the generator itself no
    longer uses this for either tenant. Both green and yellow now share the
    plane-independent anycast plan exposed by `green_host_anycast_prefix`
    and `yellow_host_anycast_prefix`. Will be removed in a future commit
    once no external callers remain.
    """
    base = "bbbb" if color == "green" else "cccc"
    return f"2001:db8:{base}:{hex1(plane)}{host:02x}"


def green_host_anycast_addr(host: int) -> str:
    """Plane-independent green tenant address.

    Configured (with `nodad`) on all 4 of the host's NICs and on every leaf's
    Ethernet32-side /64 in Vrf-green. Identical bytes on every plane so a
    sprayed flow's inner dst doesn't change with plane choice.
    """
    return f"2001:db8:bbbb:{host:02x}::2"


def green_host_anycast_prefix(host: int) -> str:
    """The /64 the green host's anycast address sits in (plane-independent)."""
    return f"2001:db8:bbbb:{host:02x}"


def yellow_host_anycast_prefix(host: int) -> str:
    """The /64 the yellow host's anycast address sits in (plane-independent).

    Phase 1a: mirrors green's pattern with `bbbb`→`cccc`. The same /64 is
    carried on every leaf's Ethernet36 in every plane, and on every yellow
    host's eth1..eth4. The leaf-side gateway is `<pfx>::1` (anycast across
    all 4 planes' leaves); the host-side anycast is `<pfx>::2` (also
    anycast across the host's 4 NICs and on lo).
    """
    return f"2001:db8:cccc:{host:02x}"


def yellow_host_anycast_addr(host: int) -> str:
    """Plane-independent yellow tenant anycast address (Phase 1a).

    Mirrors green's pattern exactly with `bbbb`→`cccc`. Assigned as
    `nodad` on every yellow host's `eth1..eth4` and on `lo`. Reached
    via the receiving host's per-plane `seg6local End.DT6 table 0`
    decap: the outer SRv6 uSID list terminates at the host, decap
    fires on eth(P+1) ingress, and the inner packet — whose DA is
    `2001:db8:cccc:<NN>::2` — is delivered to the local stack via
    the anycast addr present on eth1..eth4.

    Prior to Phase 1a the inner was `2001:db8:cccd:<NN>::1` on `lo`
    only, plus a separate per-plane underlay `2001:db8:cccc:<P><NN>::2`
    on each eth. Both are retired — see docs/architecture.md §2.
    """
    return f"2001:db8:cccc:{host:02x}::2"


# Deprecated alias for backward compatibility with anything still
# importing the old name. New code should use yellow_host_anycast_addr.
yellow_host_loopback_addr = yellow_host_anycast_addr


def load_port_template() -> dict:
    # Prefer a per-topology seed if one already exists (preserves any
    # local edits a user made to the PORT block for that topology).
    if REF_LEAF_CONFIG.is_file():
        with open(REF_LEAF_CONFIG, encoding="utf-8") as f:
            return json.load(f)["PORT"]
    # Fall back to the bundled template so a fresh topology only needs
    # a topo.yaml — no manual seed-file copy.
    if EMBEDDED_PORT_TEMPLATE.is_file():
        with open(EMBEDDED_PORT_TEMPLATE, encoding="utf-8") as f:
            return json.load(f)
    raise SystemExit(
        f"Missing PORT template: tried {REF_LEAF_CONFIG} and "
        f"{EMBEDDED_PORT_TEMPLATE}"
    )


# ---------------------------------------------------------------------------
# config_db.json
# ---------------------------------------------------------------------------

def write_leaf_config_db(plane: int, leaf: int, port_table: dict) -> None:
    hostname = leaf_name(plane, leaf)
    rid_v4 = leaf_loopback_v4(plane, leaf)
    lo6 = leaf_loopback(plane, leaf)

    iface: dict = {
        "Loopback0": {},
        f"Loopback0|{rid_v4}/32": {},
        f"Loopback0|{lo6}/128": {},
    }
    # Uplinks toward all 8 spines (in this plane).
    for s in range(NUM_SPINES):
        eth = leaf_uplink_eth(s)
        pfx = p2p_prefix(s, leaf)
        iface[eth] = {}
        iface[f"{eth}|{pfx}::1/127"] = {}

    # Host-facing ports.
    # Ethernet32 -> green host (in Vrf-green). The host's tenant address is
    # plane-independent (anycast across its 4 NICs); every leaf in every plane
    # carries the SAME /64 on its green port so the egress decap can deliver
    # to the connected gateway regardless of which plane the SR-encap arrived
    # on. No static /128 needed in Vrf-green.
    # Ethernet36 -> yellow host (default VRF; host-based SRv6 model).
    # Phase 1a: yellow now mirrors green's anycast address plan exactly
    # (`bbbb`→`cccc`). Every leaf in every plane carries the SAME
    # `cccc:<NN>::1/64` on Ethernet36; the host carries `cccc:<NN>::2` as
    # nodad anycast on eth1..eth4 (and lo). The leaf-to-host /64 is
    # plane-independent — there is no longer a per-plane underlay /64 for
    # yellow.
    green_anycast_pfx = green_host_anycast_prefix(leaf)
    yellow_anycast_pfx = yellow_host_anycast_prefix(leaf)
    iface["Ethernet32"] = {"vrf_name": GREEN_VRF}
    iface[f"Ethernet32|{green_anycast_pfx}::1/64"] = {}
    iface["Ethernet36"] = {}
    iface[f"Ethernet36|{yellow_anycast_pfx}::1/64"] = {}

    mac = f"02:42:ac:12:{plane:x}{leaf:x}:01"

    cfg = {
        "DEVICE_METADATA": {
            "localhost": {
                "mac": mac,
                "switch_type": "switch",
                "buffer_model": "traditional",
                "hwsku": "Force10-S6000",
                "hostname": hostname,
                "docker_routing_config_mode": "split",
            }
        },
        "LOOPBACK_INTERFACE": {
            "Loopback0": {},
            f"Loopback0|{rid_v4}/32": {},
            f"Loopback0|{lo6}/128": {},
        },
        "VRF": {GREEN_VRF: {}},
        "INTERFACE": iface,
        "PORT": port_table,
    }

    out = CONFIG_DIR / hostname / "config_db.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)
        f.write("\n")


def write_spine_config_db(plane: int, spine: int, port_table: dict) -> None:
    hostname = spine_name(plane, spine)
    rid_v4 = spine_loopback_v4(plane, spine)
    lo6 = spine_loopback(plane, spine)

    iface: dict = {
        "Loopback0": {},
        f"Loopback0|{rid_v4}/32": {},
        f"Loopback0|{lo6}/128": {},
    }
    for leaf in range(NUM_LEAVES):
        eth = spine_downlink_eth(leaf)
        pfx = p2p_prefix(spine, leaf)
        iface[eth] = {}
        iface[f"{eth}|{pfx}::0/127"] = {}

    mac = f"02:42:ac:11:{plane:x}{spine:x}:01"

    cfg = {
        "DEVICE_METADATA": {
            "localhost": {
                "mac": mac,
                "switch_type": "switch",
                "buffer_model": "traditional",
                "hwsku": "Force10-S6000",
                "hostname": hostname,
                "docker_routing_config_mode": "split",
            }
        },
        "LOOPBACK_INTERFACE": {
            "Loopback0": {},
            f"Loopback0|{rid_v4}/32": {},
            f"Loopback0|{lo6}/128": {},
        },
        "INTERFACE": iface,
        "PORT": port_table,
    }

    out = CONFIG_DIR / hostname / "config_db.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)
        f.write("\n")


# ---------------------------------------------------------------------------
# frr.conf  (no BGP, no IGP — pure SRv6 + connected/locator statics)
# ---------------------------------------------------------------------------

SRV6_LOCATOR_BLOCK = """ srv6
  encapsulation
   source-address {lo6}
  exit
  locators
   locator MAIN
    prefix {loc} block-len 32 node-len 16 func-bits 16
    behavior usid
   exit
   !
  exit
  !
  formats
   format usid-f3216
   exit
   !
   format uncompressed-f4024
   exit
   !
  exit
  !
 exit
"""


def write_leaf_frr(plane: int, leaf: int) -> None:
    hostname = leaf_name(plane, leaf)
    lo6 = leaf_loopback(plane, leaf)
    loc = leaf_locator(plane, leaf)

    # Static uA toward each spine in this plane.
    sid_lines = [f"   sid {loc} locator MAIN behavior uN"]
    for s in range(NUM_SPINES):
        eth = leaf_uplink_eth(s)
        nh = f"{p2p_prefix(s, leaf)}::0"
        sid_lines.append(
            f"   sid {leaf_ua_sid(plane, s)} locator MAIN behavior uA interface {eth} nexthop {nh}"
        )
    # Tenant-ID uDT6 for green, inside this plane's /32 block.
    sid_lines.append(
        f"   sid {green_udt_sid(plane)} locator MAIN behavior uDT6 vrf {GREEN_VRF}"
    )
    # Host-facing uA toward the yellow host on Ethernet36. Yellow uses the
    # host-based SRv6 model (host decaps via seg6local), so the egress leaf
    # needs a uA-terminated forward to the host's NIC.
    #
    # Phase 1a: next-hop is the plane-independent anycast `cccc:<NN>::2`.
    # Linux resolves it via the connected Ethernet36 /64 (also anycast in
    # this plane) and ARPs/NDPs the local segment, hitting THIS plane's host
    # NIC. The host then decaps via per-eth seg6local End.DT6 table 0.
    #
    # Note: no symmetric e032 for green. Green's Ethernet32 sits in Vrf-green,
    # but FRR's static-sids uA programs the seg6local in the default
    # routing namespace and the next-hop must resolve there. Green doesn't
    # need it anyway — the d000 uDT6 above decaps into Vrf-green and the
    # connected /64 lookup hands the packet to the host.
    yellow_host_nh = f"{yellow_host_anycast_prefix(leaf)}::2"
    sid_lines.append(
        f"   sid {leaf_host_ua_sid(plane, 36)} locator MAIN behavior uA "
        f"interface Ethernet36 nexthop {yellow_host_nh}"
    )
    # Stateless-probe round-trip decap. The final slot of every probe's
    # outer uSID list is `dfff`, landing on the sender's own leaf. We
    # decap into vrf default (Linux table main) so the inner /128 — the
    # sender's per-EV address — resolves via the leaf's connected
    # Ethernet36 /64 route to the host. One entry per plane per leaf;
    # NOT per-EV, NOT per-host (the EV identity rides the inner-dst,
    # peer identity rides the probe payload).
    sid_lines.append(
        f"   sid {probe_dfff_sid(plane)} locator MAIN behavior uDT6 vrf default"
    )
    static_block = "\n".join(sid_lines)

    # Static IPv6 routes for every spine locator in this plane (one per spine,
    # via that spine's directly-connected /127). This gives the data plane
    # enough FIB to forward SR-policy packets without an IGP.
    static_routes = []
    for s in range(NUM_SPINES):
        nh = f"{p2p_prefix(s, leaf)}::0"
        static_routes.append(f"ipv6 route {spine_locator(plane, s)} {nh}")
    # Static routes for every other leaf locator in this plane: each via all
    # 8 spines (ECMP) so any spine can forward toward that remote leaf.
    for other in range(NUM_LEAVES):
        if other == leaf:
            continue
        for s in range(NUM_SPINES):
            nh = f"{p2p_prefix(s, leaf)}::0"
            static_routes.append(f"ipv6 route {leaf_locator(plane, other)} {nh}")
    # uN-mode direct-to-host route for yellow's `d001` uDT6 SID. In uA
    # mode the outer SID list forces the leaf through the `e009`
    # adjacency SID above to reach the host; uN mode's SID list omits
    # that hop and lands directly on `d001`, which isn't a leaf-local
    # SID (the host owns it via its own seg6local route) — so the leaf
    # just needs an ordinary FIB entry pointing at its locally-attached
    # yellow host. Harmless to provision unconditionally: it doesn't
    # overlap any other configured prefix, so uA mode is unaffected.
    static_routes.append(f"ipv6 route {yellow_udt_sid(plane)} {yellow_host_nh}")
    static_routes_block = "\n".join(static_routes)

    txt = f"""hostname {hostname}
no service integrated-vtysh-config
!
password zebra
enable password zebra
!
vrf {GREEN_VRF}
 ip nht resolve-via-default
 ipv6 nht resolve-via-default
exit-vrf
!
vrf vrfdefault
 ip nht resolve-via-default
 ipv6 nht resolve-via-default
exit-vrf
!
ip nht resolve-via-default
ipv6 nht resolve-via-default
!
{static_routes_block}
!
segment-routing
 srv6
  static-sids
{static_block}
  exit
  !
 exit
 !
{SRV6_LOCATOR_BLOCK.format(lo6=lo6, loc=loc)}!
exit
!
end
"""
    out = CONFIG_DIR / hostname / "frr.conf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(txt, encoding="utf-8")


def write_spine_frr(plane: int, spine: int) -> None:
    hostname = spine_name(plane, spine)
    lo6 = spine_loopback(plane, spine)
    loc = spine_locator(plane, spine)

    sid_lines = [f"   sid {loc} locator MAIN behavior uN"]
    for leaf in range(NUM_LEAVES):
        eth = spine_downlink_eth(leaf)
        nh = f"{p2p_prefix(spine, leaf)}::1"
        sid_lines.append(
            f"   sid {spine_ua_sid(plane, leaf)} locator MAIN behavior uA interface {eth} nexthop {nh}"
        )
    static_block = "\n".join(sid_lines)

    # Spines need FIB entries to deliver to any leaf locator in their plane.
    static_routes = []
    for leaf in range(NUM_LEAVES):
        nh = f"{p2p_prefix(spine, leaf)}::1"
        static_routes.append(f"ipv6 route {leaf_locator(plane, leaf)} {nh}")
    static_routes_block = "\n".join(static_routes)

    txt = f"""hostname {hostname}
no service integrated-vtysh-config
!
password zebra
enable password zebra
!
ip nht resolve-via-default
ipv6 nht resolve-via-default
!
{static_routes_block}
!
segment-routing
 srv6
  static-sids
{static_block}
  exit
  !
 exit
 !
{SRV6_LOCATOR_BLOCK.format(lo6=lo6, loc=loc)}!
exit
!
end
"""
    out = CONFIG_DIR / hostname / "frr.conf"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(txt, encoding="utf-8")


# ---------------------------------------------------------------------------
# topology.clab.yaml
# ---------------------------------------------------------------------------

def write_topology_yaml(path: Path, topo_dict: dict) -> None:
    L = []
    L.append(f"name: {TOPOLOGY_NAME}")
    L.append('prefix: ""')
    L.append("")
    L.append("mgmt:")
    L.append("  network: mgt")
    L.append(f"  ipv4-subnet: {MGMT_SUBNET_BASE}.0/24")
    L.append("")
    L.append("topology:")
    L.append("  nodes:")
    L.append("")

    # Spines
    mgmt = SPINE_MGMT_START
    for p in range(NUM_PLANES):
        for s in range(NUM_SPINES):
            L.append(f"    {spine_name(p, s)}:")
            L.append("      kind: linux")
            L.append(f"      image: {SONIC_IMAGE}")
            L.append(f"      mgmt-ipv4: {MGMT_SUBNET_BASE}.{mgmt}")
            L.append("")
            mgmt += 1

    # Leaves
    mgmt = LEAF_MGMT_START
    for p in range(NUM_PLANES):
        for leaf in range(NUM_LEAVES):
            L.append(f"    {leaf_name(p, leaf)}:")
            L.append("      kind: linux")
            L.append(f"      image: {SONIC_IMAGE}")
            L.append(f"      mgmt-ipv4: {MGMT_SUBNET_BASE}.{mgmt}")
            L.append("")
            mgmt += 1

    # Hosts: 16 green + 16 yellow, each with 4 uplinks.
    mgmt = HOST_MGMT_START
    for color in ("green", "yellow"):
        for n in range(NUM_LEAVES):
            name = f"{color}-host{n:02d}"
            L.append(f"    {name}:")
            L.append("      kind: linux")
            L.append(f"      image: {HOST_IMAGE}")
            L.append(f"      mgmt-ipv4: {MGMT_SUBNET_BASE}.{mgmt}")
            # Mount the active topology's topo.yaml read-only into the
            # host container at the path srv6_mrc.topo expects
            # (SRV6_TOPO baked into the image). This is the *only*
            # piece of host filesystem state the container needs; the
            # srv6_mrc package itself is pip-installed at image
            # build time. The orchestrator (`run-scenario`) runs on
            # the lab host and reaches into containers via `docker
            # exec`; it does NOT execute inside the host containers.
            L.append("      binds:")
            L.append("        - topo.yaml:/etc/srv6_mrc/topo.yaml:ro")
            L.append("      exec:")

            # NIC underlay addresses. Both tenants now use the same anycast
            # pattern: identical /128-effective address on all 4 NICs and on
            # lo, with `nodad` (DAD would fight itself across the duplicates,
            # and L2 isolation per plane means there's no real conflict). The
            # leaf-side /64 carries the matching ::1 gateway, also identical
            # across all 4 planes. Phase 1a: yellow flipped from a per-plane
            # underlay (`cccc:<P><NN>::2/64` on eth, `cccd:<NN>::1/128` on lo)
            # to a unified anycast plan mirroring green (`bbbb`→`cccc`).
            if color == "green":
                anycast = green_host_anycast_addr(n)
            else:
                anycast = yellow_host_anycast_addr(n)
            for p in range(NUM_PLANES):
                eth = f"eth{p + 1}"
                L.append(
                    f'        - "ip -6 addr add {anycast}/64 dev {eth} nodad"'
                )

            # Anycast tenant address also on lo for yellow. The host-side
            # seg6local End.DT6 decap rule looks up the inner DA in
            # `table 0` (default RT); having the anycast on lo guarantees
            # it resolves as a local-delivery address even when no NIC is
            # the egress interface (e.g., during NIC fault-injection that
            # downs the arrival eth temporarily).
            #
            # Green does not need this: green decaps on the *leaf* into
            # Vrf-green and the connected /64 hands the packet to the host
            # naturally; the inner address lives on green's eth1..4 only.
            if color == "yellow":
                L.append(
                    f'        - "ip -6 addr add {anycast}/128 dev lo nodad"'
                )

            # Stateless-probe per-EV /128 addresses. One /128 per (plane,
            # path) on the matching NIC; the returning probe's inner-dst is
            # the sender's own per-EV address so the kernel claims it locally
            # and the daemon's recv socket sees it. These addresses MUST NOT
            # appear on any other host or the round trip dies silently at
            # that host's kernel ingress (verified by
            # tests/test_generator_stateless_probes.py).
            #
            # Yellow: probe turns around at the peer host via kernel
            # forwarding (needs forwarding=1).
            # Green: probe turns around at the remote leaf via d000 End.DT6
            # into Vrf-green; leaf forwards decapped inner packet to host via
            # connected /64 route.
            if color == "yellow":
                # Allow the kernel to forward the probe at the *peer*
                # host: the peer's inner-dst is NOT locally configured
                # (by invariant), so the kernel must fall through to
                # an FIB lookup on the still-encapped outer DA. Without
                # forwarding=1 the kernel silently drops it. Alpine
                # defaults forwarding=0 — see docs/stateless-probes-
                # validation.md.
                L.append(
                    '        - "sysctl -w net.ipv6.conf.all.forwarding=1"'
                )
            
            for p in range(NUM_PLANES):
                eth = f"eth{p + 1}"
                for ev_path in range(NUM_SPINES):
                    if color == "green":
                        ev_addr = probe_ev_addr_green(n, p, ev_path)
                    else:  # yellow
                        ev_addr = probe_ev_addr_yellow(n, p, ev_path)
                    L.append(
                        f'        - "ip -6 addr add {ev_addr}/128 '
                        f'dev {eth} nodad"'
                    )

            # One reachability route per plane: each plane's /32 uSID block
            # is reached via that plane's NIC. Unambiguous (no accidental
            # cross-plane ECMP); the controller picks plane-specific
            # destinations like fc00:0002:... to pin a flow to plane 2.
            # Both tenants now use a plane-independent anycast gateway: the
            # gateway literal is identical on every plane and Linux resolves
            # it on the per-NIC L2 segment so the resolution hits THIS
            # plane's leaf.
            for p in range(NUM_PLANES):
                eth = f"eth{p + 1}"
                if color == "green":
                    gw = f"{green_host_anycast_prefix(n)}::1"
                else:
                    gw = f"{yellow_host_anycast_prefix(n)}::1"
                L.append(
                    f'        - "ip -6 route add {plane_aggregate(p)} via {gw} dev {eth}"'
                )

            # Tenant prefix /48 reachable via plane 0 by default; controller
            # may override per-flow. Same pattern as 01-sonic-vs.
            base = "bbbb" if color == "green" else "cccc"
            tenant_prefix = f"2001:db8:{base}::/48"
            if color == "green":
                gw0 = f"{green_host_anycast_prefix(n)}::1"
            else:
                gw0 = f"{yellow_host_anycast_prefix(n)}::1"
            L.append(
                f'        - "ip -6 route add {tenant_prefix} via {gw0} dev eth1"'
            )

            # Yellow hosts run host-based SRv6: install a uDT6 seg6local on
            # EACH plane's NIC, since the destination address inside the
            # encapsulated outer IPv6 carries the plane's block prefix. After
            # decap the inner /128 sits on lo (above) so the table-0 lookup
            # delivers locally regardless of which plane the packet arrived on.
            if color == "yellow":
                for p in range(NUM_PLANES):
                    eth = f"eth{p + 1}"
                    L.append(
                        f'        - "ip -6 route add {yellow_udt_sid(p)} '
                        f'dev {eth} encap seg6local action End.DT6 table 0"'
                    )
            L.append("")
            mgmt += 1

    # Visibility stack (optional): scraper + Prometheus + Grafana
    # Controlled by visibility.enabled in topo.yaml.
    # See visibility/README.md and docs/design-visibility.md.
    visibility_cfg = topo_dict.get("visibility", {})
    if visibility_cfg.get("enabled", False):
        L.append("    # Visibility stack containers")
        L.append("    srv6-scraper:")
        L.append("      kind: linux")
        L.append("      image: srv6-scraper:1.0")
        L.append("      binds:")
        L.append("        - /var/run/docker.sock:/var/run/docker.sock")
        L.append("        - topo.yaml:/etc/srv6_mrc/topo.yaml:ro")
        L.append("      mgmt-ipv4: 172.20.18.250")
        L.append("")
        L.append("    srv6-prometheus:")
        L.append("      kind: linux")
        L.append("      image: prom/prometheus:v2.55.0")
        L.append("      mgmt-ipv4: 172.20.18.251")
        L.append("      binds:")
        L.append("        - visibility/prometheus.yml:/etc/prometheus/prometheus.yml:ro")
        L.append("      ports:")
        L.append('        - "9090:9090"')
        L.append("      cmd: >")
        L.append("        --config.file=/etc/prometheus/prometheus.yml")
        L.append("        --storage.tsdb.path=/prometheus")
        L.append("        --storage.tsdb.retention.time=1h")
        L.append("        --web.listen-address=:9090")
        L.append("")
        L.append("    srv6-grafana:")
        L.append("      kind: linux")
        L.append("      image: grafana/grafana:11.2.0")
        L.append("      mgmt-ipv4: 172.20.18.252")
        L.append("      env:")
        L.append('        GF_AUTH_ANONYMOUS_ENABLED: "true"')
        L.append('        GF_AUTH_ANONYMOUS_ORG_ROLE: "Viewer"')
        L.append('        GF_AUTH_DISABLE_LOGIN_FORM: "true"')
        L.append('        GF_USERS_DEFAULT_THEME: "dark"')
        L.append("      binds:")
        L.append("        - visibility/grafana/provisioning:/etc/grafana/provisioning:ro")
        L.append("        - visibility/grafana/dashboards:/var/lib/grafana/dashboards:ro")
        L.append("      ports:")
        L.append('        - "3000:3000"')
        L.append("")

    L.append("  links:")
    L.append("")

    # Fabric: 4 planes * 8 spines * 16 leaves = 512 links.
    # Leaf eth(s+1) <-> spine eth(leaf+1) (eth0 is mgmt; eth1 is first data nic).
    for p in range(NUM_PLANES):
        for s in range(NUM_SPINES):
            for leaf in range(NUM_LEAVES):
                L.append(
                    f'    - endpoints: ["{leaf_name(p, leaf)}:eth{s + 1}", '
                    f'"{spine_name(p, s)}:eth{leaf + 1}"]'
                )

    # Host links: each host has 4 uplinks, one per plane.
    # Leaf side uses eth9 (Ethernet32, green) and eth10 (Ethernet36, yellow).
    # i.e. data NIC index = (port_number/4) + 1.  Ethernet32 -> eth9, Ethernet36 -> eth10.
    for p in range(NUM_PLANES):
        for n in range(NUM_LEAVES):
            L.append(
                f'    - endpoints: ["{leaf_name(p, n)}:eth9", '
                f'"green-host{n:02d}:eth{p + 1}"]'
            )
            L.append(
                f'    - endpoints: ["{leaf_name(p, n)}:eth10", '
                f'"yellow-host{n:02d}:eth{p + 1}"]'
            )

    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _load_topo(topo_path: Path) -> dict:
    """Read topo.yaml driving this generator run."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as e:
        raise SystemExit(
            "PyYAML is required to run the generator. "
            "Install with: pip install -e '.[runtime]'"
        ) from e
    with open(topo_path) as f:
        return yaml.safe_load(f)


def main() -> None:
    global NUM_PLANES, NUM_SPINES, NUM_LEAVES
    global TOPOLOGY_NAME, SONIC_IMAGE, HOST_IMAGE
    global TOPO_DIR, CONFIG_DIR, REF_LEAF_CONFIG

    ap = argparse.ArgumentParser(
        description="Generate containerlab topology + SONiC configs from topo.yaml"
    )
    ap.add_argument(
        "--topo",
        type=Path,
        default=REPO_ROOT / "topologies" / "4p-4x8" / "topo.yaml",
        help="Path to topo.yaml (default: topologies/4p-4x8/topo.yaml)",
    )
    args = ap.parse_args()

    topo_path = args.topo.resolve()
    if not topo_path.is_file():
        raise SystemExit(f"topo.yaml not found: {topo_path}")

    t = _load_topo(topo_path)

    # Bind module-level constants for all the write_* helpers.
    NUM_PLANES = int(t["planes"])
    NUM_SPINES = int(t["spines_per_plane"])
    NUM_LEAVES = int(t["leaves_per_plane"])
    TOPOLOGY_NAME = t["clab"]["topology_name"]
    SONIC_IMAGE = t["images"]["sonic"]
    HOST_IMAGE = t["images"]["host"]

    TOPO_DIR = topo_path.parent                     # topologies/<name>/
    CONFIG_DIR = TOPO_DIR / "config"
    REF_LEAF_CONFIG = CONFIG_DIR / "p0-leaf00" / "config_db.json"

    port_table = load_port_template()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    for p in range(NUM_PLANES):
        for s in range(NUM_SPINES):
            write_spine_config_db(p, s, port_table)
            write_spine_frr(p, s)
        for leaf in range(NUM_LEAVES):
            write_leaf_config_db(p, leaf, port_table)
            write_leaf_frr(p, leaf)

    topo_clab = TOPO_DIR / "topology.clab.yaml"
    write_topology_yaml(topo_clab, t)

    n_spines = NUM_PLANES * NUM_SPINES
    n_leaves = NUM_PLANES * NUM_LEAVES
    n_hosts = 2 * NUM_LEAVES
    n_fabric_links = NUM_PLANES * NUM_SPINES * NUM_LEAVES
    n_host_links = NUM_PLANES * NUM_LEAVES * 2  # green+yellow per leaf per plane

    print(f"Wrote {topo_clab}")
    print(f"Wrote SONiC configs under {CONFIG_DIR}/")
    print(
        f"  {n_spines} spines, {n_leaves} leaves, {n_hosts} hosts, "
        f"{n_fabric_links} fabric links + {n_host_links} host links"
    )
    print(f"Deploy lab: containerlab deploy -t {topo_clab.relative_to(REPO_ROOT)}")
    print(f"Push configs: scripts/config.sh all")


if __name__ == "__main__":
    main()
