"""Spray policies — given (seq, flow), pick a plane (or EV).

Implementations all behind a single `pick()` method so the runner is
oblivious to which policy it's using.

`HealthAwareMrc` is the live MRC-aware policy: it reads per-pick from
an `EVStateTable`'s per-EV (plane, path) weight grid. Demoted EVs
(state ASSUMED_BAD) get weight 0; degraded/unknown EVs (state UNKNOWN)
get reduced weight. The weighted CDF is rebuilt each pick from the
live atomic snapshot of the table, so updates from probe/loss-report
threads take effect immediately, without coordination with the sender
hot loop. Like `EvSpray`, it respects a per-flow spine subset so
different flows visit different fabric slices; unlike `EvSpray` the
within-flow distribution is health-weighted instead of round-robin.
Deterministic per (seq, flow) given a fixed weights snapshot.

Dual-mode topology binding (Refactor 1 Step 2)
==============================================

Every policy class accepts an optional `topology: Topology | None`. When
None (the historical default), the policy reads fabric dimensions from
the `srv6_mrc.topo` module-level globals (`NUM_PLANES`, `NUM_SPINES`,
`select_spines_for_addrs`). When provided, dimensions and spine
selection come from the supplied `Topology` instance instead.

Dual-mode is a staging step. New call sites should pass an explicit
`topology=...`; existing call sites flip over one at a time without
breaking. Once every call site is migrated the None branch will be
removed and `topology` will become a required parameter (Phase C of
the refactor).

The two modes are observably identical when the supplied Topology
matches the module globals — that's what `tests/test_policy.py`'s
dual-mode parity sweep enforces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, TYPE_CHECKING

from .topo import (NUM_PLANES, NUM_SPINES, FlowKey,
                   select_spines_for_addrs)
from .topology import Topology

if TYPE_CHECKING:
    # Imported lazily inside HealthAwareMrc to avoid a circular import
    # between srv6_mrc.policy and srv6_mrc.mrc.ev_state.
    from .mrc.ev_state import EVStateTable


# Sentinel for "user didn't pass paths_per_plane; derive from topology
# at __post_init__ time". We can't use a literal int default like
# NUM_SPINES because in dual-mode we want a Topology with different
# spines_per_plane to override the historical default. None is taken;
# a private object() is unambiguous.
_DEFAULT_PATHS = -1  # int so `paths_per_plane: int` stays typed


class SprayPolicy(Protocol):
    """Pick a plane for the next packet of the given flow."""

    name: str

    def pick(self, seq: int, flow: FlowKey) -> int: ...


# --- concrete policies ------------------------------------------------------

@dataclass
class RoundRobin:
    name: str = "round_robin"
    topology: Topology | None = None

    def _num_planes(self) -> int:
        return self.topology.planes if self.topology else NUM_PLANES

    def pick(self, seq: int, flow: FlowKey) -> int:
        return seq % self._num_planes()


@dataclass
class EvSpray:
    """Round-robin across the full EV set = NUM_PLANES * paths_per_plane.

    Each EV is a (plane, spine) tuple. The spine subset for a given
    (src, dst) pair is computed once via `select_spines_for_addrs()` and stays
    constant for the lifetime of the policy; the round-robin walks
    plane-major (plane 0 spines, then plane 1 spines, ...) so successive
    packets visit different planes (giving the same anti-clustering
    benefit as plain round-robin) while also varying spine within each
    plane (giving leaf->spine ECMP entropy on the wire, which plain
    round-robin lacks).

    Use this when you want per-packet entropy across the full fabric,
    not just per-plane. With paths_per_plane = NUM_SPINES (the default)
    every spine on every plane is exercised; with paths_per_plane < N
    the per-pair subset is hash-derived so different pairs use
    different spine subsets and the whole fabric still sees traffic.

    Returns:
      pick(seq, flow) -> plane (backward-compat for callers that only
        want the plane; computed from the EV the walk would choose).
      pick_ev(seq, flow) -> (plane, spine) — preferred. The runner uses
        this to build the outer DA's `<S>` hextet per packet.

    Future step 3 (per-EV health) will subclass / extend this with an
    "active EV mask" read from an EVStateTable, so a degraded EV gets
    skipped on its turn. For step 1 every EV is always active.
    """
    paths_per_plane: int = _DEFAULT_PATHS
    name: str = "ev_spray"
    topology: Topology | None = None

    # Per-flow cache: FlowKey -> tuple of spine indices. EvSpray.pick is
    # called once per packet so the dict lookup is the hot path; we
    # accept it because the alternative (preselect at runner startup) is
    # leaky when the same policy instance serves multiple flows.
    _spine_subsets: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        n_spines = (self.topology.spines_per_plane
                    if self.topology else NUM_SPINES)
        if self.paths_per_plane == _DEFAULT_PATHS:
            self.paths_per_plane = n_spines
        if not (1 <= self.paths_per_plane <= n_spines):
            raise ValueError(
                f"paths_per_plane must be 1..{n_spines}, "
                f"got {self.paths_per_plane}"
            )

    def _spines_for_flow(self, flow: FlowKey) -> tuple[int, ...]:
        """Per-flow spine subset, cached. Symmetric in flow direction.

        Seeded from the canonical (lo, hi) of the inner IPv6 addresses,
        NOT from Python's `hash()`. `hash(str)` is salted by
        PYTHONHASHSEED and varies across processes, so an earlier
        implementation gave 8 senders 8 different spine subsets for the
        "same" pair, and the lossy `% 1024` collapse on top of that
        starved certain spines fabric-wide. select_spines_for_addrs
        feeds the canonicalized address string straight into FNV-1a,
        which is process-stable and full-entropy.
        """
        cached = self._spine_subsets.get(flow)
        if cached is not None:
            return cached
        if self.topology is not None:
            subset = self.topology.select_spines_for_addrs(
                flow.src_addr, flow.dst_addr, self.paths_per_plane,
            )
        else:
            subset = select_spines_for_addrs(
                flow.src_addr, flow.dst_addr, self.paths_per_plane,
            )
        self._spine_subsets[flow] = subset
        return subset

    def pick_ev(self, seq: int, flow: FlowKey) -> tuple[int, int]:
        """Return (plane, spine) for this packet. Round-robin plane-major."""
        spines = self._spines_for_flow(flow)
        n_planes = self.topology.planes if self.topology else NUM_PLANES
        ev_count = n_planes * len(spines)
        ev_idx = seq % ev_count
        # plane-major: ev_idx = plane * len(spines) + spine_idx_in_subset
        # ... but plane-major would visit (P0,S0), (P0,S1), ..., (P0,Sk-1),
        # (P1,S0), .... That hot-spots a single plane for k packets in a
        # row, which defeats the anti-clustering point of round-robin.
        # Use spine-major instead: (P0,S0), (P1,S0), (P2,S0), (P3,S0),
        # (P0,S1), (P1,S1), ... -- successive packets always change plane.
        plane = ev_idx % n_planes
        spine_idx = (ev_idx // n_planes) % len(spines)
        return plane, spines[spine_idx]

    def pick(self, seq: int, flow: FlowKey) -> int:
        """Backward-compat shim returning just the plane."""
        plane, _ = self.pick_ev(seq, flow)
        return plane


@dataclass
class Hash5Tuple:
    """Per-flow plane affinity. Same flow → same plane (mimics ECMP).

    For meaningful spread you need many flows; a single flow pins to one
    plane. This is included for comparison with `round_robin` under load,
    not because it's MRC-correct on its own.
    """
    name: str = "hash5tuple"
    topology: Topology | None = None

    def pick(self, seq: int, flow: FlowKey) -> int:
        n_planes = self.topology.planes if self.topology else NUM_PLANES
        return flow.hash5() % n_planes


@dataclass
class Weighted:
    """Plane choice from a discrete distribution. Deterministic per seq
    (no RNG state) so two runs with the same seed produce identical traces.

    Weights are normalized internally; they don't need to sum to 1.
    """
    weights: tuple[float, ...]
    name: str = "weighted"
    topology: Topology | None = None

    # Precomputed cumulative thresholds in [0, 1).
    _cdf: tuple[float, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        n_planes = self.topology.planes if self.topology else NUM_PLANES
        if len(self.weights) != n_planes:
            raise ValueError(
                f"weights must have {n_planes} entries, "
                f"got {len(self.weights)}"
            )
        if any(w < 0 for w in self.weights):
            raise ValueError(f"weights must be >= 0, got {self.weights}")
        if sum(self.weights) <= 0:
            raise ValueError("weights sum to zero")
        object.__setattr__(self, "_cdf", _build_cdf(self.weights))

    def pick(self, seq: int, flow: FlowKey) -> int:
        return _weighted_pick(seq, flow, self._cdf)


# --- shared helpers for weighted picking -----------------------------------

# Golden-ratio additive recurrence: low-discrepancy, no RNG state needed.
# Mixing flow.hash5() ensures different flows take different draws at the
# same seq, which prevents a single flow with a deterministic weight
# distribution from over-using its modal plane in lockstep with the
# weights (the "all flows pick plane 0 every odd packet" failure mode).
_GOLDEN_RATIO_64 = 0x9E3779B97F4A7C15


def _build_cdf(weights: tuple[float, ...]) -> tuple[float, ...]:
    """Normalize weights into a cumulative distribution in [0, 1].

    Caller guarantees: len(weights) > 0 and sum(weights) > 0. The last
    entry is forced to exactly 1.0 to guard against fp drift in the tail
    of the linear search in _weighted_pick.
    """
    total = sum(weights)
    cum = 0.0
    cdf: list[float] = []
    for w in weights:
        cum += w / total
        cdf.append(cum)
    cdf[-1] = 1.0
    return tuple(cdf)


def _weighted_pick(seq: int, flow: FlowKey, cdf: tuple[float, ...]) -> int:
    """Deterministic draw from a CDF.

    Maps (flow, seq) -> u in [0, 1), then returns the first index whose
    CDF threshold strictly exceeds u. Walks the CDF linearly; we only
    have NUM_PLANES (2-8) entries so branch-predicted scan beats bsearch.
    """
    x = (flow.hash5() + seq * _GOLDEN_RATIO_64) & 0xFFFFFFFFFFFFFFFF
    u = x / float(1 << 64)
    for p, threshold in enumerate(cdf):
        if u < threshold:
            return p
    return len(cdf) - 1  # unreachable, cdf[-1] == 1.0


@dataclass
class HealthAwareMrc:
    """MRC-aware weighted per-EV spray driven by an EVStateTable.

    Reads `table.weights_ev(tenant)` per pick and draws a (plane, path)
    pair from the resulting 2-D distribution using the same
    deterministic golden-ratio scheme as `Weighted`. Because
    `weights_ev()` reflects the live state machine (probes + loss
    reports), demotions and recoveries take effect on the next packet —
    weight tables are replaced atomically by EVStateTable on every state
    transition, so we never hold the table's lock here.

    Like `EvSpray`, the policy respects a per-flow spine subset
    (`paths_per_plane`, default = `NUM_SPINES` = all paths). The
    subset is hash-derived from the flow's address pair so different
    flows visit different subsets of the fabric, and within each flow
    the policy spreads across `NUM_PLANES * paths_per_plane` EVs
    weighted by the table's per-EV health.

    Compared to a binary up/down policy, `HealthAwareMrc` is graded
    (GOOD/UNKNOWN/ASSUMED_BAD map to configurable weights) and operates
    per EV, so a partial-path failure on one plane doesn't kill the
    whole plane.

    Cold-start: when no probes have replied yet, every EV is UNKNOWN
    and `weights_ev()` returns a uniform distribution. Pick distributes
    ~uniformly across all EVs in the flow's subset — fine for a clean
    fabric and indistinguishable from `EvSpray` round-robin in
    expectation.

    All-bad pathological case: prevented by the `min_active_evs` floor
    in EVStateTable. If somehow reached, `weights_ev()` falls back to
    uniform; pick still works.

    Construction is typically via `parse_policy(..., tenant=..., table=...)`
    in spray.py; the bare scenario YAML form `policy: health_aware_mrc`
    is translated by `policy_from_spec` to a *factory* that the caller
    finishes by supplying the live EVStateTable, since `policy_from_spec`
    has no view of the per-sender tenant or the runtime table.
    """
    table: "EVStateTable"
    tenant: str
    paths_per_plane: int = _DEFAULT_PATHS
    topology: Topology | None = None
    name: str = field(init=False)

    # Per-flow spine subset cache (mirrors EvSpray._spine_subsets).
    _spine_subsets: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        n_planes = self.topology.planes if self.topology else NUM_PLANES
        n_spines = (self.topology.spines_per_plane
                    if self.topology else NUM_SPINES)
        if self.tenant not in self.table.tenants:
            raise ValueError(
                f"tenant {self.tenant!r} not configured in EVStateTable "
                f"(known: {self.table.tenants})"
            )
        if self.table.num_planes != n_planes:
            raise ValueError(
                f"EVStateTable.num_planes={self.table.num_planes} but "
                f"topology planes={n_planes}; tables must match topology"
            )
        if self.table.num_paths != n_spines:
            raise ValueError(
                f"EVStateTable.num_paths={self.table.num_paths} but "
                f"topology spines_per_plane={n_spines}; tables must match topology"
            )
        if self.paths_per_plane == _DEFAULT_PATHS:
            self.paths_per_plane = n_spines
        if not (1 <= self.paths_per_plane <= n_spines):
            raise ValueError(
                f"paths_per_plane must be 1..{n_spines}, "
                f"got {self.paths_per_plane}"
            )
        object.__setattr__(self, "name", f"health_aware_mrc({self.tenant})")

    def _spines_for_flow(self, flow: FlowKey) -> tuple[int, ...]:
        """Per-flow spine subset, cached (same scheme as EvSpray)."""
        cached = self._spine_subsets.get(flow)
        if cached is not None:
            return cached
        if self.topology is not None:
            subset = self.topology.select_spines_for_addrs(
                flow.src_addr, flow.dst_addr, self.paths_per_plane,
            )
        else:
            subset = select_spines_for_addrs(
                flow.src_addr, flow.dst_addr, self.paths_per_plane,
            )
        self._spine_subsets[flow] = subset
        return subset

    def pick_ev(self, seq: int, flow: FlowKey) -> tuple[int, int]:
        """Return (plane, spine) for this packet.

        The second element is a physical spine ID (the value the runner
        feeds to `usid_outer_dst(spine=...)`), not a 0..paths_per_plane
        index. This matches `EvSpray.pick_ev`'s contract so the runner
        consumes them identically.
        """
        spines = self._spines_for_flow(flow)
        n_planes = self.topology.planes if self.topology else NUM_PLANES
        # 2-D table read; atomic tuple swap on transitions.
        wgrid = self.table.weights_ev(self.tenant)
        # Slice rows by all planes, columns by the flow's spine subset.
        # Build a flat weight vector over (plane, subset_idx) pairs.
        flat: list[float] = []
        for plane in range(n_planes):
            row = wgrid[plane]
            for sp in spines:
                flat.append(row[sp])
        total = sum(flat)
        if total <= 0:
            # Safety net: defensively spread uniformly across the
            # flow's EV set when the slice is all-zero (which shouldn't
            # happen under normal floor behaviour but might under e.g.
            # an exotic test setup that demotes a whole spine subset).
            n = n_planes * len(spines)
            ev_idx = seq % n
        else:
            cdf = _build_cdf(tuple(flat))
            ev_idx = _weighted_pick(seq, flow, cdf)
        # Decode flat index back to (plane, subset_idx).
        plane = ev_idx // len(spines)
        spine = spines[ev_idx % len(spines)]
        return plane, spine

    def pick(self, seq: int, flow: FlowKey) -> int:
        """Backward-compat shim returning just the plane.

        Provided so non-EV-aware call sites (e.g. tests that only care
        about the plane axis) keep working. EV-aware runners use
        `pick_ev()` directly.
        """
        plane, _ = self.pick_ev(seq, flow)
        return plane



# --- construction from scenario YAML ---------------------------------------

@dataclass
class HealthAwareMrcFactory:
    """Deferred-construction stub for `health_aware_mrc`.

    `policy_from_spec` returns this when the scenario YAML asks for an
    MRC-aware policy, because policy_from_spec has no view of the runtime
    EVStateTable or the sender's tenant. The caller (typically
    spray.py's parse_policy) resolves the factory by calling .bind(table,
    tenant) once the EV state machine is wired up, producing a fully
    constructed HealthAwareMrc.

    Carrying this marker through the spec graph (rather than failing in
    policy_from_spec) lets the scenario validator and dry-runs accept
    `health_aware_mrc` even without a live EV table, so we can validate
    YAML shapes early.

    `paths_per_plane` is carried so the YAML form
    `{health_aware_mrc: <int>}` can configure the per-flow spine subset
    size in the same way as `{ev_spray: <int>}`. Default = NUM_SPINES
    (full fan-out).
    """
    paths_per_plane: int = _DEFAULT_PATHS
    name: str = "health_aware_mrc"
    topology: Topology | None = None

    def __post_init__(self) -> None:
        # Resolve the sentinel default eagerly so the factory faithfully
        # carries the same paths_per_plane the bound policy will see.
        if self.paths_per_plane == _DEFAULT_PATHS:
            n_spines = (self.topology.spines_per_plane
                        if self.topology else NUM_SPINES)
            self.paths_per_plane = n_spines

    def bind(self, table: "EVStateTable", tenant: str) -> HealthAwareMrc:
        return HealthAwareMrc(
            table=table, tenant=tenant,
            paths_per_plane=self.paths_per_plane,
            topology=self.topology,
        )

    # Make the factory acceptable wherever a SprayPolicy is expected for
    # diagnostic plumbing (printing the policy name in dry-run output).
    # Calling pick() on an unbound factory is a programmer error.
    def pick(self, seq: int, flow: FlowKey) -> int:
        raise RuntimeError(
            "HealthAwareMrcFactory.pick() called on an unbound factory; "
            "call .bind(table, tenant) to produce a real policy"
        )


def policy_from_spec(spec, topology: Topology | None = None) -> SprayPolicy:
    """Build a policy from a scenario `policy:` value.

    Accepted forms:
      "round_robin"
      "hash5tuple"
      "ev_spray"                               (full fan-out)
      "health_aware_mrc"                       (returns a factory; see below)
      {"weighted": [0.4, 0.3, 0.2, 0.1]}
      {"ev_spray": <int>}                      (paths_per_plane)
      {"health_aware_mrc": <int>}              (paths_per_plane on factory)
      {"health_aware": "round_robin"}
      {"health_aware": {"weighted": [...]}}

    `health_aware_mrc` is special: it returns a HealthAwareMrcFactory
    rather than a ready-to-use policy because policy_from_spec has no
    EVStateTable or tenant context. Callers running the sender hot path
    must finish construction by calling .bind(table, tenant) on the
    returned factory.

    `topology` (Refactor 1 dual-mode): if provided, every constructed
    policy carries the topology through. When None, policies fall back
    to module globals — same as today.
    """
    if isinstance(spec, str):
        if spec == "round_robin":
            return RoundRobin(topology=topology)
        if spec == "hash5tuple":
            return Hash5Tuple(topology=topology)
        if spec == "ev_spray":
            # Bare form: full fan-out (paths_per_plane = topology's
            # spines_per_plane, or NUM_SPINES under the module-global
            # fallback). To tune, pass `{ev_spray: <int>}` in the YAML.
            return EvSpray(topology=topology)
        if spec == "health_aware_mrc":
            return HealthAwareMrcFactory(topology=topology)
        raise ValueError(f"unknown policy: {spec!r}")
    if isinstance(spec, dict) and len(spec) == 1:
        (kind, value), = spec.items()
        if kind == "weighted":
            return Weighted(
                weights=tuple(float(x) for x in value),
                topology=topology,
            )
        if kind == "ev_spray":
            return EvSpray(paths_per_plane=int(value), topology=topology)
        if kind == "health_aware_mrc":
            return HealthAwareMrcFactory(
                paths_per_plane=int(value),
                topology=topology,
            )
        raise ValueError(f"unknown policy kind: {kind!r}")
    raise ValueError(f"bad policy spec: {spec!r}")
