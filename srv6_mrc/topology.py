"""Topology value class — replacement for the module-level globals in
`srv6_mrc.topo`.

This is the foundation for Refactor 1 in `docs/architecture-audit.md`:
turning topology dimensions from a module-import-time side-effect (the
`SRV6_TOPO` env-var dance) into a value passed as a parameter.

This file is **purely additive** — no existing call site changes when
this module lands. Migration is per-module in subsequent commits.

Design choices
==============

1. **What's on the class:** dimensions, tenant set, clab name, and
   every helper that depends on those (validation, uSID construction,
   spine selection, leaf gateway address).

2. **What's NOT on the class:** functions that depend only on
   tenant + host_id (`inner_addr`, `host_name`, `host_id_from_inner_addr`,
   `green/yellow_anycast_addr`). These are topology-independent and stay
   as module functions in `topo.py`. Putting them on the class would be
   noise.

3. **Frozen dataclass.** Topologies are immutable; you load one and
   pass it around. Hashable so it can live in `@functools.lru_cache`
   if call sites want to memoise dimension-derived constants.

4. **Construction:** two paths — `Topology.from_yaml(path)` for the
   live load, `Topology.from_dict(d)` for tests / in-memory fixtures.
   No env-var inspection here; the caller picks the path.

5. **Validation:** identical to `topo.py`'s `_check_*` helpers, but
   bound to *this* topology's dimensions instead of module globals.
   Methods raise `ValueError` with the same messages so tests can be
   migrated without rewriting assertions.

6. **`reference_pairs_spines`:** hardcoded in `topo.py` for the
   4p-8x16 reference design. Carried as a field here so each
   Topology instance can have its own table; defaults to empty for
   topologies that don't define one (hash fallback in
   `spine_for`).

Cross-checking
==============

Every method on this class has a counterpart in `topo.py`. The test
suite (`tests/test_topology_class.py`) verifies that, for every input
the module functions accept, the new class returns an identical
result. That's how we keep the migration safe: the moment a call site
flips from module function to method, behaviour is bit-identical.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# Reference table for the 4p-8x16 design — kept here as a default so
# `Topology.from_dict({...})` without an explicit table still produces
# the historical pair selection on 4p-8x16. Other topologies pass an
# empty dict and fall through to the hash in `spine_for`.
_REFERENCE_PAIRS_4P_8X16: dict[tuple[int, int], int] = {
    (0, 15): 0, (1, 14): 2, (2, 13): 4, (3, 12): 6,
    (4, 11): 1, (5, 10): 3, (6, 9):  5, (7, 8):  7,
}


# FNV-1a 64-bit constants — same values as `topo.py`. Kept module-local
# rather than on the class because they're properties of the hash
# function, not the topology.
_FNV_OFFSET = 0xcbf29ce484222325
_FNV_PRIME = 0x100000001b3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF
_SPLITMIX_GAMMA = 0x9E3779B97F4A7C15


def _fnv1a(seed: bytes) -> int:
    h = _FNV_OFFSET
    for b in seed:
        h ^= b
        h = (h * _FNV_PRIME) & _FNV_MASK
    return h


@dataclass(frozen=True)
class Topology:
    """Immutable description of a deployed fabric.

    Construct via `from_yaml(path)` for production use or `from_dict(d)`
    for tests. All instance methods that read dimensions read them from
    `self`, not from any module-level state.

    Equality and hashing follow dataclass semantics over the listed
    fields; two Topologies with the same dimensions and tenant set are
    equal regardless of which YAML they came from. `reference_pairs_spines`
    participates in equality (a pair table difference is a meaningful
    difference) but the dict is converted to a frozenset of items to
    make the dataclass hashable.
    """

    name: str
    planes: int
    spines_per_plane: int
    leaves_per_plane: int
    tenants: tuple[str, ...]
    clab_topology_name: str
    # Stored as a frozenset of (pair, spine) entries so the whole dataclass
    # is hashable. Use `pair_spine(pair)` to query.
    _reference_pairs_items: frozenset[tuple[tuple[int, int], int]] = field(
        default_factory=frozenset
    )

    # --- construction -------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> "Topology":
        """Build a Topology from a parsed topo.yaml dict.

        Required keys: `name`, `planes`, `spines_per_plane`,
        `leaves_per_plane`, `tenants`. Optional: `clab.topology_name`
        (defaults to `sonic-docker-<name>`), `reference_pairs_spines`
        (a list of `[lo, hi, spine]` triples; defaults to the 4p-8x16
        reference table if `name` matches `4p-8x16`, otherwise empty).
        """
        name = str(d["name"])
        planes = int(d["planes"])
        spines_per_plane = int(d["spines_per_plane"])
        leaves_per_plane = int(d["leaves_per_plane"])
        tenants = tuple(str(t) for t in d["tenants"])

        clab_block = d.get("clab") or {}
        clab_name = str(clab_block.get("topology_name",
                                       f"sonic-docker-{name}"))

        ref_in = d.get("reference_pairs_spines")
        if ref_in is None:
            # Implicit default for the canonical reference design.
            ref_items = (
                _REFERENCE_PAIRS_4P_8X16.items()
                if name == "4p-8x16" else ()
            )
        else:
            # YAML form: list of [lo, hi, spine].
            ref_items = (
                ((int(lo), int(hi)), int(sp))
                for lo, hi, sp in ref_in
            )
        ref_frozen = frozenset(
            ((min(a, b), max(a, b)), s) for (a, b), s in ref_items
        )

        return cls(
            name=name,
            planes=planes,
            spines_per_plane=spines_per_plane,
            leaves_per_plane=leaves_per_plane,
            tenants=tenants,
            clab_topology_name=clab_name,
            _reference_pairs_items=ref_frozen,
        )

    @classmethod
    def from_yaml(cls, path: str | os.PathLike) -> "Topology":
        """Read and parse a topo.yaml file. Requires `pyyaml`."""
        import yaml  # type: ignore[import-not-found]
        with open(path) as f:
            return cls.from_dict(yaml.safe_load(f))

    # --- derived properties -------------------------------------------

    @property
    def num_evs_per_flow(self) -> int:
        """Maximum EV count per (src, dst) flow at full paths_per_plane."""
        return self.planes * self.spines_per_plane

    @property
    def total_evs(self) -> int:
        """Total distinct EVs in the fabric (alias for num_evs_per_flow)."""
        return self.num_evs_per_flow

    @property
    def tenant_id_map(self) -> dict[str, int]:
        """Wire u16 per tenant name. Index in tenants tuple + 1
        (0 reserved for unknown). Matches `topo.TENANT_ID`.
        """
        return {name: i + 1 for i, name in enumerate(self.tenants)}

    @property
    def tenant_by_id(self) -> dict[int, str]:
        return {v: k for k, v in self.tenant_id_map.items()}

    # --- identity helpers ---------------------------------------------

    def tenant_id(self, tenant: str) -> int:
        m = self.tenant_id_map
        if tenant not in m:
            raise ValueError(
                f"tenant {tenant!r} not in registry {sorted(m)}; "
                "update topology yaml or tenants tuple"
            )
        return m[tenant]

    def tenant_name(self, tid: int) -> str:
        m = self.tenant_by_id
        if tid not in m:
            raise ValueError(
                f"tenant_id {tid} not in registry {sorted(m)}; "
                "probe from another deployment or version mismatch?"
            )
        return m[tid]

    def plane_nic(self, plane: int) -> str:
        """eth0 is mgmt; eth1..eth(planes) are the per-plane uplinks."""
        self.check_plane(plane)
        return f"eth{plane + 1}"

    @property
    def plane_nics(self) -> tuple[str, ...]:
        return tuple(f"eth{p + 1}" for p in range(self.planes))

    # --- validation ---------------------------------------------------

    def check_tenant(self, tenant: str) -> None:
        if tenant not in self.tenants:
            raise ValueError(
                f"tenant must be one of {self.tenants}, got {tenant!r}"
            )

    def check_plane(self, plane: int) -> None:
        if not isinstance(plane, int) or not (0 <= plane < self.planes):
            raise ValueError(
                f"plane must be 0..{self.planes - 1}, got {plane!r}"
            )

    def check_spine(self, spine: int) -> None:
        if not isinstance(spine, int) or not (
                0 <= spine < self.spines_per_plane):
            raise ValueError(
                f"spine must be 0..{self.spines_per_plane - 1}, "
                f"got {spine!r}"
            )

    def check_host(self, host_id: int) -> None:
        if not isinstance(host_id, int) or not (
                0 <= host_id < self.leaves_per_plane):
            raise ValueError(
                f"host id must be 0..{self.leaves_per_plane - 1}, "
                f"got {host_id!r}"
            )

    # --- addressing (mirror of topo.py functions) ---------------------

    def inner_addr(self, tenant: str, host_id: int) -> str:
        """Plane-independent inner tenant address.

        Same shape as topo.inner_addr — the function is technically
        topology-independent but lives on Topology so callers carrying
        a Topology don't need a second import.
        """
        self.check_tenant(tenant)
        self.check_host(host_id)
        if tenant == "green":
            return f"2001:db8:bbbb:{host_id:02x}::2"
        return f"2001:db8:cccc:{host_id:02x}::2"

    def usid_outer_dst(self, tenant: str, plane: int, spine: int,
                       dst_leaf: int) -> str:
        """Outer IPv6 destination = compressed uSID list.

        Green : fc00:000<P>:f00<S>:e00<L>:d000::
        Yellow: fc00:000<P>:f00<S>:e00<L>:e009:d001::
        """
        self.check_tenant(tenant)
        self.check_plane(plane)
        self.check_spine(spine)
        self.check_host(dst_leaf)
        head = f"fc00:000{plane:x}:f00{spine:x}:e00{dst_leaf:x}"
        return (f"{head}:d000::" if tenant == "green"
                else f"{head}:e009:d001::")

    def leaf_gateway_addr(self, tenant: str, plane: int,
                          host_id: int) -> str:
        """Address to ping for plane-health probes."""
        self.check_tenant(tenant)
        self.check_plane(plane)
        self.check_host(host_id)
        if tenant == "green":
            return f"2001:db8:bbbb:{host_id:02x}::1"
        return f"2001:db8:cccc:{plane:x}{host_id:02x}::1"

    def host_name(self, tenant: str, host_id: int) -> str:
        """`<tenant>-host<NN>` — also topology-independent, but mirrored
        here for Topology-carrying callers."""
        self.check_tenant(tenant)
        self.check_host(host_id)
        return f"{tenant}-host{host_id:02d}"

    # --- spine selection (genuinely topology-dependent) ---------------

    def pair_spine(self, src_id: int, dst_id: int) -> int | None:
        """Look up the reference table for an explicit (lo, hi) pair.

        Returns None if no entry — caller falls back to `spine_for`'s
        hash. Rarely useful directly; included so tests and tools can
        introspect the table without going through the hashing path.
        """
        a, b = (src_id, dst_id) if src_id < dst_id else (dst_id, src_id)
        for pair, sp in self._reference_pairs_items:
            if pair == (a, b):
                return sp
        return None

    def spine_for(self, src_id: int, dst_id: int) -> int:
        """Transit spine for a given pair. Reference table first,
        deterministic hash fallback."""
        a, b = (src_id, dst_id) if src_id < dst_id else (dst_id, src_id)
        for pair, sp in self._reference_pairs_items:
            if pair == (a, b):
                return sp
        return (a * self.leaves_per_plane + b) % self.spines_per_plane

    def select_spines(self, src_id: int, dst_id: int, n: int
                      ) -> tuple[int, ...]:
        """Deterministic hash-derived subset of `n` spine indices in
        [0, spines_per_plane). See topo.select_spines for algorithmic
        detail; this is the topology-bound mirror.
        """
        if not (1 <= n <= self.spines_per_plane):
            raise ValueError(
                f"paths_per_plane must be 1..{self.spines_per_plane}, "
                f"got {n!r}"
            )
        return self._select_spines_from_seed(
            f"{min(src_id, dst_id)}|{max(src_id, dst_id)}", n,
        )

    def select_spines_for_addrs(self, src_addr: str, dst_addr: str,
                                n: int) -> tuple[int, ...]:
        """Same as `select_spines`, but seeded from canonical IPv6
        address strings (used by EV-spray policy code)."""
        if not (1 <= n <= self.spines_per_plane):
            raise ValueError(
                f"paths_per_plane must be 1..{self.spines_per_plane}, "
                f"got {n!r}"
            )
        lo, hi = ((src_addr, dst_addr) if src_addr <= dst_addr
                  else (dst_addr, src_addr))
        return self._select_spines_from_seed(f"{lo}|{hi}", n)

    def _select_spines_from_seed(self, seed_str: str, n: int
                                 ) -> tuple[int, ...]:
        """Fisher-Yates pick of `n` distinct values from
        [0, spines_per_plane). SplitMix64 step PRNG.

        Algorithm is intentionally bit-identical to
        `topo._select_spines_from_seed`; only the upper bound changes
        from module global to instance attribute.
        """
        base = _fnv1a(seed_str.encode())
        pool = list(range(self.spines_per_plane))
        picks: list[int] = []
        for step in range(n):
            x = (base + step * _SPLITMIX_GAMMA) & _FNV_MASK
            x = ((x ^ (x >> 30)) * 0xbf58476d1ce4e5b9) & _FNV_MASK
            x = ((x ^ (x >> 27)) * 0x94d049bb133111eb) & _FNV_MASK
            x = (x ^ (x >> 31)) & _FNV_MASK
            j = x % (self.spines_per_plane - step)
            picks.append(pool.pop(j))
        return tuple(picks)
