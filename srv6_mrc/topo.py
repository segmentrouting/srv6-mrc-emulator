"""Topology constants + address/SID helpers.

Single source of truth for everything that comes from the fabric shape:
plane/spine/leaf counts, tenant names, NIC ordinals, address blocks,
reference-pair spine assignments, and SID-list construction.

The constants are loaded at import time from a topo.yaml file. By
default that's `topologies/4p-4x8/topo.yaml` relative to the repo
root (a mid-size dev variant; 4p-8x16 is the full-scale reference
design and remains available via SRV6_TOPO or `make TOPO=4p-8x16`).
Override via the `SRV6_TOPO` environment variable to drive a
different topology. CLI tools running inside lab host containers have
SRV6_TOPO pre-set by the host-image entrypoint to the bind-mounted
topo.yaml.

If you change a fabric constant here, change it in `topo.yaml` (not
this file). For documentation of the YAML schema see any of the
`topologies/<name>/topo.yaml` files; for the design rationale of
the address scheme see `docs/topologies/<name>.md`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# --- topology loader --------------------------------------------------------

def _find_default_topo_yaml() -> Path:
    """Locate topologies/4p-4x8/topo.yaml relative to this file.

    srv6_mrc/topo.py is at <root>/srv6_mrc/topo.py, so the default
    topology is two levels up + topologies/4p-4x8/topo.yaml. 4p-4x8
    is a mid-size dev variant (4 planes * 4 spines * 8 leaves);
    4p-8x16 is the full-scale reference design and is selectable via
    SRV6_TOPO or `make TOPO=4p-8x16`.

    NOTE: This is a static fallback for dev/test environments without
    a live lab. CLI entrypoints (`srv6_mrc.mrc.run`, `srv6_mrc.cli.srctl`)
    set SRV6_TOPO before any srv6_mrc import so the live topology
    drives module-level constants like NUM_LEAVES.
    """
    here = Path(__file__).resolve()
    return here.parent.parent / "topologies" / "4p-4x8" / "topo.yaml"


def _load_topo() -> dict:
    """Read the topo.yaml driving this process.

    Order of precedence:
      1. $SRV6_TOPO (must point at a topo.yaml file)
      2. <repo>/topologies/4p-4x8/topo.yaml (development default)

    Falls back to a hardcoded 4p-4x8 dict if neither file is reachable
    AND yaml is missing — keeps `import srv6_mrc.topo` working in
    truly minimal environments (e.g., schema-only tooling).
    """
    path_str = os.environ.get("SRV6_TOPO")
    path = Path(path_str) if path_str else _find_default_topo_yaml()

    try:
        import yaml  # type: ignore[import-not-found]
        with open(path) as f:
            return yaml.safe_load(f)
    except (FileNotFoundError, ImportError):
        # Hardcoded fallback so tests can import without yaml installed
        # and without the file present (e.g., CI bare-clone scenarios).
        return {
            "name": "4p-4x8",
            "planes": 4,
            "spines_per_plane": 4,
            "leaves_per_plane": 8,
            "tenants": ["green", "yellow"],
            "images": {
                "sonic": "docker-sonic-vs:latest",
                "host": "alpine-srv6-scapy:1.0",
            },
            "clab": {"topology_name": "sonic-docker-4p-4x8"},
        }


_TOPO = _load_topo()


# --- typed Topology accessor -----------------------------------------------

# Lazily-built singleton `Topology` instance for the active topology.
# Refactor 1 Phase B is migrating call sites from the module-level
# constants below (NUM_PLANES, NUM_SPINES, ...) to passing this
# Topology object as an explicit parameter. During the migration both
# paths remain available; once every call site takes a Topology, the
# module-level constants and most of the helper functions in this file
# become deprecated and can be removed in Phase C / Refactor 1.
#
# We hold a reference (not just a property on _TOPO) so identity-based
# memoization in policy/topology consumers stays stable across calls.
_CURRENT_TOPOLOGY: "Topology | None" = None


def current_topology() -> "Topology":
    """Return the `Topology` instance for the active topology.

    Built once from the same `_TOPO` dict that drives the module-level
    constants in this file, so `current_topology().planes == NUM_PLANES`
    by construction. Cached on first call; subsequent calls return the
    same instance (so `is` comparisons are valid).

    Lazy import of `srv6_mrc.topology` keeps `srv6_mrc.topo` importable
    in environments where the dataclass module isn't yet on path (and
    sidesteps a potential cycle if `topology.py` ever needs to consult
    `topo.py` at import time).
    """
    global _CURRENT_TOPOLOGY
    if _CURRENT_TOPOLOGY is None:
        from srv6_mrc.topology import Topology
        _CURRENT_TOPOLOGY = Topology.from_dict(_TOPO)
    return _CURRENT_TOPOLOGY


# --- topology shape ---------------------------------------------------------

NUM_PLANES: int = _TOPO["planes"]
NUM_SPINES: int = _TOPO["spines_per_plane"]
NUM_LEAVES: int = _TOPO["leaves_per_plane"]            # also = hosts per tenant

# Containerlab topology name (matches `name:` at the top of the
# generated topology.clab.yaml). Used to construct the clab-<topo>-<node>
# container-name fallback in routes.py and netem.py when the user is
# running with the (recommended) `prefix: ""` setting and the short name
# isn't resolvable.
CLAB_TOPOLOGY_NAME: str = _TOPO.get("clab", {}).get(
    "topology_name", "sonic-docker-4p-4x8"
)

TENANTS: tuple[str, ...] = tuple(_TOPO["tenants"])

# Stable u16 identifier per tenant, used on the wire by MRC PROBE v2 to
# attribute received probes back to the correct sender flow scope. Index
# in TENANTS + 1 (so 0 is reserved for "unknown / not-set"); two-tenant
# deployments today get green=1, yellow=2. If you reorder TENANTS in a
# topology yaml you'll change every probe's tenant_id silently — keep
# the order stable across deployments.
TENANT_ID: dict[str, int] = {name: i + 1 for i, name in enumerate(TENANTS)}
TENANT_BY_ID: dict[int, str] = {v: k for k, v in TENANT_ID.items()}


def tenant_id(tenant: str) -> int:
    """Look up the wire u16 for a tenant name; raises if unknown."""
    if tenant not in TENANT_ID:
        raise ValueError(
            f"tenant {tenant!r} not in registry {sorted(TENANT_ID)}; "
            "update topology yaml or tenants tuple"
        )
    return TENANT_ID[tenant]


def tenant_name(tid: int) -> str:
    """Reverse lookup: u16 -> tenant name. Raises if unknown id."""
    if tid not in TENANT_BY_ID:
        raise ValueError(
            f"tenant_id {tid} not in registry {sorted(TENANT_BY_ID)}; "
            "probe from another deployment or version mismatch?"
        )
    return TENANT_BY_ID[tid]

# eth0 is mgmt; eth1..eth(NUM_PLANES) are the per-plane uplinks.
PLANE_NIC = lambda plane: f"eth{plane + 1}"
PLANE_NICS = tuple(PLANE_NIC(p) for p in range(NUM_PLANES))

SPRAY_PORT = 9999
SPRAY_PROBE_PORT = 9998   # MRC EV Probe / Probe Reply (see mrc/probe.py)
SPRAY_REPORT_PORT = 9997  # MRC receiver loss-feedback report

# Reference (lo, hi) host-pair -> chosen transit spine. Used by the
# spray.py demo and routes.py to pick a deterministic transit spine for
# well-known test pairs. Other pairs fall back to a hash; see
# spine_for() below.
#
# This table is hardcoded for the 4p-8x16 reference design. Topologies
# of other shapes need their own table or will use the hash fallback.
REFERENCE_PAIRS_SPINES: dict[tuple[int, int], int] = {
    (0, 15): 0, (1, 14): 2, (2, 13): 4, (3, 12): 6,
    (4, 11): 1, (5, 10): 3, (6, 9):  5, (7, 8):  7,
}


# --- identity ---------------------------------------------------------------

def host_name(tenant: str, host_id: int) -> str:
    return f"{tenant}-host{host_id:02d}"


def spine_for(src_id: int, dst_id: int) -> int:
    """Transit spine for a given pair. Reference table first, deterministic
    hash fallback."""
    a, b = (src_id, dst_id) if src_id < dst_id else (dst_id, src_id)
    s = REFERENCE_PAIRS_SPINES.get((a, b))
    if s is not None:
        return s
    return (a * NUM_LEAVES + b) % NUM_SPINES


def select_spines(src_id: int, dst_id: int, n: int) -> tuple[int, ...]:
    """Deterministic hash-derived subset of `n` spine indices in [0, NUM_SPINES).

    Used by EV-spray to pick which spines a given (src, dst) pair fans out
    across. Returns the same tuple every time for the same inputs and is
    symmetric in (src, dst) — the reverse-direction sender on the same pair
    sees the same EV set, so EV identities are stable end-to-end.

    Algorithm:
      - Canonicalize (lo, hi) = sorted (src_id, dst_id) for symmetry.
      - Seed an FNV-1a-style mixing from (lo, hi).
      - Run a partial Fisher-Yates shuffle over [0, NUM_SPINES) using
        that FNV state as a deterministic PRNG; take the first n picks.

      An earlier version of this function returned `n` *consecutive*
      spines starting at a hash-derived offset — that's only NUM_SPINES
      possible subsets (one per offset), with `n=2` leaving most pairs
      like {0,3} or {1,5} unreachable. Fisher-Yates fixes this: every
      one of the C(NUM_SPINES, n) subsets is reachable, with roughly
      equal probability across pairs. With n == NUM_SPINES the result
      is a permutation of all spines (same property the old code had).

    Args:
        src_id: source host id (0..NUM_LEAVES-1).
        dst_id: destination host id (0..NUM_LEAVES-1).
        n: number of spines to pick; must satisfy 1 <= n <= NUM_SPINES.

    Returns:
        Tuple of `n` distinct spine indices. The order within the
        subset is itself deterministic (driven by Fisher-Yates draw
        order), so the round-robin EV walk always visits the subset
        in the same sequence for a given pair.
    """
    if not (1 <= n <= NUM_SPINES):
        raise ValueError(
            f"paths_per_plane must be 1..{NUM_SPINES}, got {n!r}"
        )
    return _select_spines_from_seed(f"{min(src_id, dst_id)}|"
                                    f"{max(src_id, dst_id)}", n)


def select_spines_for_addrs(src_addr: str, dst_addr: str,
                            n: int) -> tuple[int, ...]:
    """Same as `select_spines`, but seeded from canonical IPv6 addresses.

    Used by EV-spray policy code that knows only the inner addresses
    (not the host id ints). The seed bytes are the literal address
    strings — process-stable across PYTHONHASHSEED, and symmetric in
    (src, dst).
    """
    if not (1 <= n <= NUM_SPINES):
        raise ValueError(
            f"paths_per_plane must be 1..{NUM_SPINES}, got {n!r}"
        )
    lo, hi = (src_addr, dst_addr) if src_addr <= dst_addr else \
        (dst_addr, src_addr)
    return _select_spines_from_seed(f"{lo}|{hi}", n)


# FNV-1a 64-bit constants — same as FlowKey.hash5 so we get good
# distribution without inventing a new mixer.
_FNV_OFFSET = 0xcbf29ce484222325
_FNV_PRIME = 0x100000001b3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF


def _fnv1a(seed: bytes) -> int:
    h = _FNV_OFFSET
    for b in seed:
        h ^= b
        h = (h * _FNV_PRIME) & _FNV_MASK
    return h


def _select_spines_from_seed(seed_str: str, n: int) -> tuple[int, ...]:
    """Fisher-Yates pick of `n` distinct values from [0, NUM_SPINES) using
    a deterministic PRNG seeded by `seed_str`.

    The per-draw "random" 64-bit value is the SplitMix64 mixer applied
    to (FNV-1a(seed) + step * GOLDEN_GAMMA). SplitMix64 is a published
    avalanche function with full bit-mixing in a small constant number
    of operations; it's strictly stronger than re-hashing the seed
    with FNV-1a per step (FNV-1a is fast but has weak avalanche on
    small/related inputs — short host-id-derived seeds with shared
    prefixes give visibly biased subset distributions). Using
    SplitMix64 here gives uniform spine distributions in the 12-14%
    band per spine over a 10k-pair Monte Carlo and reaches all
    C(NUM_SPINES, n) subsets.
    """
    base = _fnv1a(seed_str.encode())
    pool = list(range(NUM_SPINES))
    picks: list[int] = []
    for step in range(n):
        # SplitMix64 step: advance counter, then avalanche-mix.
        x = (base + step * _SPLITMIX_GAMMA) & _FNV_MASK
        x = ((x ^ (x >> 30)) * 0xbf58476d1ce4e5b9) & _FNV_MASK
        x = ((x ^ (x >> 27)) * 0x94d049bb133111eb) & _FNV_MASK
        x = (x ^ (x >> 31)) & _FNV_MASK
        j = x % (NUM_SPINES - step)
        picks.append(pool.pop(j))
    return tuple(picks)


# SplitMix64 weyl constant (golden-ratio derived; standard).
_SPLITMIX_GAMMA = 0x9E3779B97F4A7C15


# --- addresses --------------------------------------------------------------

def host_underlay_addr(tenant: str, plane: int, host_id: int) -> str:
    """DEPRECATED. Per-(host, plane) NIC underlay address.

    Previously this returned different shapes for the two tenants:
    - green: `2001:db8:bbbb:<P><NN>::2` — never actually assigned to any
      NIC, just a value the raw-socket sender stamped into the outer
      IPv6 source field.
    - yellow: `2001:db8:cccc:<P><NN>::2` — historically assigned to
      yellow eth(P+1) and used as both source and probe destination.

    The yellow per-plane assignment was the root cause of the yellow
    MRC probe regression (see docs/architecture.md §2). In Phase 1a we
    moved yellow to mirror green: a single inner anycast address
    `2001:db8:cccc:<NN>::2` on eth1..eth4 + lo (nodad). The per-plane
    yellow underlay no longer exists.

    This function now returns the **anycast** address for both tenants,
    ignoring the `plane` argument, for backward compatibility with
    raw-socket source-stamping call sites that don't care about the
    plane bit. New call sites should use `inner_addr(tenant, host_id)`
    directly.

    Callers binding a UDP socket to a local address SHOULD use
    `inner_addr` or `host_probe_peer_addr` instead.
    """
    _check_tenant(tenant)
    _check_plane(plane)  # validated but ignored
    _check_host(host_id)
    return inner_addr(tenant, host_id)


def host_probe_peer_addr(tenant: str, plane: int, host_id: int) -> str:
    """Inner address to UDP-send probes / loss reports TO `host_id`.

    Both tenants return the **inner** (plane-independent) host address.
    Plane attribution is done by the caller via `SO_BINDTODEVICE` on the
    sending socket; the destination address identifies *which host*, the
    socket binding identifies *which plane*.

    Green: `2001:db8:bbbb:<NN>::2` is assigned anycast (nodad) on every
    eth1..eth4 of every green host. Sender's leaf forwards via the
    bound NIC's plane fabric; the receiver's leaf decap (End.DT6) hands
    the inner packet to the host where it's accepted as local.

    Yellow (Phase 1a): `2001:db8:cccc:<NN>::2` is assigned anycast
    (nodad) on every eth1..eth4 + lo of every yellow host — mirrors
    green's pattern exactly. The sender-built outer SRv6 carrier
    encapsulates inner packets through the fabric; the receiving host's
    eth(P+1) `seg6local End.DT6 table 0` decaps onto the local stack
    where the inner anycast addr is present on the same NIC. See
    docs/architecture.md §2 for the addressing rule.

    See generators/fabric.py for the host-side address plan.
    """
    _check_tenant(tenant)
    _check_plane(plane)
    _check_host(host_id)
    return inner_addr(tenant, host_id)


def green_anycast_addr(host_id: int) -> str:
    """Plane-independent green tenant address (inner src/dst).

    Assigned anycast (nodad) on eth1..eth4 + lo of every green host.
    """
    _check_host(host_id)
    return f"2001:db8:bbbb:{host_id:02x}::2"


def yellow_anycast_addr(host_id: int) -> str:
    """Plane-independent yellow tenant address (inner src/dst).

    Phase 1a: assigned anycast (nodad) on eth1..eth4 + lo of every
    yellow host — mirrors green's address plan exactly with `bbbb`
    replaced by `cccc`. The previous per-plane underlay
    `2001:db8:cccc:<P><NN>::2` and the loopback-only inner
    `2001:db8:cccd:<NN>::1` are both retired.
    """
    _check_host(host_id)
    return f"2001:db8:cccc:{host_id:02x}::2"


def yellow_loopback_addr(host_id: int) -> str:
    """DEPRECATED — alias for `yellow_anycast_addr`.

    The historical loopback-only `cccd:<NN>::1` was replaced in Phase 1a
    by the anycast `cccc:<NN>::2` (assigned on eth+lo). This alias
    exists so older test fixtures and tooling that imported the old
    name don't break immediately. New code should use
    `yellow_anycast_addr` or `inner_addr("yellow", host_id)`.
    """
    return yellow_anycast_addr(host_id)


def inner_addr(tenant: str, host_id: int) -> str:
    _check_tenant(tenant)
    return (green_anycast_addr(host_id) if tenant == "green"
            else yellow_anycast_addr(host_id))


def host_id_from_inner_addr(addr: str) -> tuple[str, int] | None:
    """Reverse of `inner_addr`: parse an inner IPv6 address back to its
    (tenant, host_id) pair, or None if it doesn't match either tenant's
    inner-address shape.

    Used by the receiver MRC agent to attribute incoming data packets
    to a specific sender for loss-report routing. We use a regex on the
    string form (which scapy gives us) rather than ipaddress.IPv6Address
    arithmetic because the string form is always canonicalized for the
    addresses we generate.

    Tolerant to common zero-suppressed forms (e.g. "2001:db8:bbbb:f::2"
    vs "2001:db8:bbbb:0f::2") by normalizing through ipaddress.
    """
    import ipaddress
    try:
        normalized = ipaddress.IPv6Address(addr).exploded.lower()
    except (ValueError, ipaddress.AddressValueError):
        return None
    # exploded form is "2001:0db8:bbbb:00NN:0000:0000:0000:0002"
    parts = normalized.split(":")
    if len(parts) != 8:
        return None
    if parts[0] != "2001" or parts[1] != "0db8":
        return None
    tenant: str | None = None
    if parts[2] == "bbbb" and parts[7] == "0002":
        tenant = "green"
    elif parts[2] == "cccc" and parts[7] == "0002":
        tenant = "yellow"
    else:
        return None
    try:
        host_id = int(parts[3], 16)
    except ValueError:
        return None
    if not 0 <= host_id <= 15:
        return None
    return tenant, host_id


def leaf_gateway_addr(tenant: str, plane: int, host_id: int) -> str:
    """Address to ping for plane-health probes. Green: anycast leaf gw on
    Vrf-green Ethernet32 (same on every plane). Yellow: per-plane leaf gw on
    Ethernet36 underlay /64."""
    _check_tenant(tenant)
    _check_plane(plane)
    _check_host(host_id)
    if tenant == "green":
        # The green leaf-side gateway is identical across planes (anycast),
        # so the plane argument is informational only.
        return f"2001:db8:bbbb:{host_id:02x}::1"
    return f"2001:db8:cccc:{plane:x}{host_id:02x}::1"


# --- uSID outer destination -------------------------------------------------

def usid_outer_dst(tenant: str, plane: int, spine: int, dst_leaf: int) -> str:
    """Outer IPv6 destination = compressed uSID list.

    Green : fc00:000<P>:f00<S>:e00<L>:d000::
    Yellow: fc00:000<P>:f00<S>:e00<L>:e009:d001::
    """
    _check_tenant(tenant)
    _check_plane(plane)
    _check_spine(spine)
    _check_host(dst_leaf)
    head = f"fc00:000{plane:x}:f00{spine:x}:e00{dst_leaf:x}"
    return f"{head}:d000::" if tenant == "green" else f"{head}:e009:d001::"


# --- flow identity ----------------------------------------------------------

@dataclass(frozen=True)
class FlowKey:
    """Identity tuple used by hash-based policies and the reorder bookkeeper.

    Matches a 5-tuple closely enough for our purposes — protocol is always
    UDP here, src/dst are the inner (plane-independent) tenant addresses.
    """
    src_addr: str
    dst_addr: str
    src_port: int
    dst_port: int

    def hash5(self) -> int:
        # Stable across processes (Python's hash() is salted). FNV-1a 64-bit
        # over the canonical tuple string.
        s = f"{self.src_addr}|{self.dst_addr}|{self.src_port}|{self.dst_port}|17"
        h = 0xcbf29ce484222325
        for b in s.encode():
            h ^= b
            h = (h * 0x100000001b3) & 0xFFFFFFFFFFFFFFFF
        return h


# --- validation -------------------------------------------------------------

def _check_tenant(v: str) -> None:
    if v not in TENANTS:
        raise ValueError(f"tenant must be one of {TENANTS}, got {v!r}")


def _check_plane(v: int) -> None:
    if not isinstance(v, int) or not (0 <= v < NUM_PLANES):
        raise ValueError(f"plane must be 0..{NUM_PLANES - 1}, got {v!r}")


def _check_spine(v: int) -> None:
    if not isinstance(v, int) or not (0 <= v < NUM_SPINES):
        raise ValueError(f"spine must be 0..{NUM_SPINES - 1}, got {v!r}")


def _check_host(v: int) -> None:
    if not isinstance(v, int) or not (0 <= v < NUM_LEAVES):
        raise ValueError(f"host id must be 0..{NUM_LEAVES - 1}, got {v!r}")
