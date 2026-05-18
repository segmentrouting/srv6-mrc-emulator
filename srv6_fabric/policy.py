"""Spray policies — given (seq, flow), pick a plane.

Implementations all behind a single `pick()` method so the runner is
oblivious to which policy it's using. Health-awareness comes in two
flavors:

  - `HealthAware`: a static wrapper that filters out planes in a
    mutable `down: set[int]` set, owned by the legacy ICMPv6-based
    health monitor (see srv6_fabric/health.py). Picks of a down plane
    walk forward to the next healthy plane.

  - `HealthAwareMrc`: an MRC-aware policy that reads per-pick from an
    EVStateTable's normalized weight tuple. Demoted planes (state
    ASSUMED_BAD) get weight 0; degraded/unknown planes (state UNKNOWN)
    get reduced weight. The weighted CDF is recomputed each pick so
    updates from probe/loss-report threads take effect immediately,
    without coordination with the sender hot loop. Deterministic per
    (seq, flow) given a fixed weights snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, TYPE_CHECKING

from .topo import NUM_PLANES, NUM_SPINES, FlowKey, select_spines

if TYPE_CHECKING:
    # Imported lazily inside HealthAwareMrc to avoid a circular import
    # between srv6_fabric.policy and srv6_fabric.mrc.ev_state.
    from .mrc.ev_state import EVStateTable


class SprayPolicy(Protocol):
    """Pick a plane for the next packet of the given flow."""

    name: str

    def pick(self, seq: int, flow: FlowKey) -> int: ...


# --- concrete policies ------------------------------------------------------

@dataclass
class RoundRobin:
    name: str = "round_robin"

    def pick(self, seq: int, flow: FlowKey) -> int:
        return seq % NUM_PLANES


@dataclass
class EvSpray:
    """Round-robin across the full EV set = NUM_PLANES * paths_per_plane.

    Each EV is a (plane, spine) tuple. The spine subset for a given
    (src, dst) pair is computed once via `select_spines()` and stays
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
    paths_per_plane: int = NUM_SPINES
    name: str = "ev_spray"

    # Per-flow cache: FlowKey -> tuple of spine indices. EvSpray.pick is
    # called once per packet so the dict lookup is the hot path; we
    # accept it because the alternative (preselect at runner startup) is
    # leaky when the same policy instance serves multiple flows.
    _spine_subsets: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not (1 <= self.paths_per_plane <= NUM_SPINES):
            raise ValueError(
                f"paths_per_plane must be 1..{NUM_SPINES}, "
                f"got {self.paths_per_plane}"
            )

    def _spines_for_flow(self, flow: FlowKey) -> tuple[int, ...]:
        """Per-flow spine subset, cached. Symmetric in flow direction."""
        cached = self._spine_subsets.get(flow)
        if cached is not None:
            return cached
        # FlowKey carries IPv6 strings, not host ids. Use the flow hash
        # as a stand-in for (src_id, dst_id) since select_spines only
        # needs a stable pair fingerprint. We canonicalize on addresses
        # so both directions of the same pair get the same subset.
        lo, hi = sorted((flow.src_addr, flow.dst_addr))
        # Derive synthetic numeric ids from the address strings for
        # select_spines (which expects ints). Hash to small ints so
        # select_spines's FNV-1a step gets fresh entropy.
        a = (hash(lo) & 0xFFFFFFFF) % 1024
        b = (hash(hi) & 0xFFFFFFFF) % 1024
        subset = select_spines(a, b, self.paths_per_plane)
        self._spine_subsets[flow] = subset
        return subset

    def pick_ev(self, seq: int, flow: FlowKey) -> tuple[int, int]:
        """Return (plane, spine) for this packet. Round-robin plane-major."""
        spines = self._spines_for_flow(flow)
        ev_count = NUM_PLANES * len(spines)
        ev_idx = seq % ev_count
        # plane-major: ev_idx = plane * len(spines) + spine_idx_in_subset
        # ... but plane-major would visit (P0,S0), (P0,S1), ..., (P0,Sk-1),
        # (P1,S0), .... That hot-spots a single plane for k packets in a
        # row, which defeats the anti-clustering point of round-robin.
        # Use spine-major instead: (P0,S0), (P1,S0), (P2,S0), (P3,S0),
        # (P0,S1), (P1,S1), ... -- successive packets always change plane.
        plane = ev_idx % NUM_PLANES
        spine_idx = (ev_idx // NUM_PLANES) % len(spines)
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

    def pick(self, seq: int, flow: FlowKey) -> int:
        return flow.hash5() % NUM_PLANES


@dataclass
class Weighted:
    """Plane choice from a discrete distribution. Deterministic per seq
    (no RNG state) so two runs with the same seed produce identical traces.

    Weights are normalized internally; they don't need to sum to 1.
    """
    weights: tuple[float, ...]
    name: str = "weighted"

    # Precomputed cumulative thresholds in [0, 1).
    _cdf: tuple[float, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.weights) != NUM_PLANES:
            raise ValueError(
                f"weights must have {NUM_PLANES} entries, got {len(self.weights)}"
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
class HealthAware:
    """Wrap any policy with plane-health filtering.

    `down` is a mutable set of plane indices; the runner's health probe owns
    it. `pick` calls the inner policy, and if it returns a down plane, walks
    forward (mod NUM_PLANES) to the next healthy one. If *all* planes are
    down, returns the inner choice unchanged (degrades to "send and hope").
    """
    inner: SprayPolicy
    down: set[int] = field(default_factory=set)
    name: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", f"health_aware({self.inner.name})")

    def pick(self, seq: int, flow: FlowKey) -> int:
        choice = self.inner.pick(seq, flow)
        if not self.down or choice not in self.down:
            return choice
        if len(self.down) >= NUM_PLANES:
            return choice  # nothing healthy — emit anyway
        for step in range(1, NUM_PLANES):
            cand = (choice + step) % NUM_PLANES
            if cand not in self.down:
                return cand
        return choice


@dataclass
class HealthAwareMrc:
    """MRC-aware weighted spray driven by an EVStateTable.

    Reads `table.weights(tenant)` per pick and draws a plane from the
    resulting distribution using the same deterministic golden-ratio
    scheme as `Weighted`. Because `weights()` reflects the live state
    machine (probes + loss reports), demotions and recoveries take
    effect on the next packet, no extra synchronisation needed: weight
    tuples are replaced atomically by EVStateTable on every state
    transition.

    Compared to `HealthAware(inner=...)`:
      - HealthAware is binary (down or up) and walks forward to find the
        next up plane on a "down hit".
      - HealthAwareMrc is graded (GOOD/UNKNOWN/ASSUMED_BAD map to
        configurable weights) and draws from a normalized CDF every
        pick, so the distribution smoothly follows EV state changes.

    Cold-start: when no probes have replied yet, every plane is UNKNOWN
    and `weights()` returns a uniform distribution (per EVStateConfig
    defaults). Pick distributes ~uniformly across all planes — fine for
    a clean fabric and indistinguishable from round_robin in expectation.

    All-bad pathological case: prevented by the `ev_min_active` floor
    in EVStateTable. If somehow reached, `weights()` falls back to
    uniform; pick still works.

    Construction is typically via `parse_policy(..., tenant=..., table=...)`
    in spray.py; the bare scenario YAML form `policy: health_aware_mrc`
    is translated by `policy_from_spec` to a *factory* that the caller
    finishes by supplying the live EVStateTable, since `policy_from_spec`
    has no view of the per-sender tenant or the runtime table.
    """
    table: "EVStateTable"
    tenant: str
    name: str = field(init=False)

    def __post_init__(self) -> None:
        if self.tenant not in self.table.tenants:
            raise ValueError(
                f"tenant {self.tenant!r} not configured in EVStateTable "
                f"(known: {self.table.tenants})"
            )
        if self.table.num_planes != NUM_PLANES:
            raise ValueError(
                f"EVStateTable.num_planes={self.table.num_planes} but "
                f"topo NUM_PLANES={NUM_PLANES}; tables must match topology"
            )
        object.__setattr__(self, "name", f"health_aware_mrc({self.tenant})")

    def pick(self, seq: int, flow: FlowKey) -> int:
        # Single tuple read; EVStateTable rebuilds + atomically replaces
        # this tuple on every state transition, so we get a coherent
        # snapshot without holding the table's lock.
        weights = self.table.weights(self.tenant)
        # Safety net: EVStateTable guarantees a positive-sum weight tuple
        # under normal operation, but defensively guard against an
        # all-zero tuple (e.g. if a future change to the floor logic
        # regresses) by falling back to uniform.
        if sum(weights) <= 0:
            return seq % NUM_PLANES
        cdf = _build_cdf(weights)
        return _weighted_pick(seq, flow, cdf)


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
    """
    name: str = "health_aware_mrc"

    def bind(self, table: "EVStateTable", tenant: str) -> HealthAwareMrc:
        return HealthAwareMrc(table=table, tenant=tenant)

    # Make the factory acceptable wherever a SprayPolicy is expected for
    # diagnostic plumbing (printing the policy name in dry-run output).
    # Calling pick() on an unbound factory is a programmer error.
    def pick(self, seq: int, flow: FlowKey) -> int:
        raise RuntimeError(
            "HealthAwareMrcFactory.pick() called on an unbound factory; "
            "call .bind(table, tenant) to produce a real policy"
        )


def policy_from_spec(spec) -> SprayPolicy:
    """Build a policy from a scenario `policy:` value.

    Accepted forms:
      "round_robin"
      "hash5tuple"
      "health_aware_mrc"                       (returns a factory; see below)
      {"weighted": [0.4, 0.3, 0.2, 0.1]}
      {"health_aware": "round_robin"}
      {"health_aware": {"weighted": [...]}}

    `health_aware_mrc` is special: it returns a HealthAwareMrcFactory
    rather than a ready-to-use policy because policy_from_spec has no
    EVStateTable or tenant context. Callers running the sender hot path
    must finish construction by calling .bind(table, tenant) on the
    returned factory.
    """
    if isinstance(spec, str):
        if spec == "round_robin":
            return RoundRobin()
        if spec == "hash5tuple":
            return Hash5Tuple()
        if spec == "ev_spray":
            # Bare form: full fan-out (paths_per_plane = NUM_SPINES). To
            # tune, pass `{ev_spray: <int>}` in the YAML.
            return EvSpray()
        if spec == "health_aware_mrc":
            return HealthAwareMrcFactory()
        raise ValueError(f"unknown policy: {spec!r}")
    if isinstance(spec, dict) and len(spec) == 1:
        (kind, value), = spec.items()
        if kind == "weighted":
            return Weighted(weights=tuple(float(x) for x in value))
        if kind == "ev_spray":
            return EvSpray(paths_per_plane=int(value))
        if kind == "health_aware":
            return HealthAware(inner=policy_from_spec(value))
        raise ValueError(f"unknown policy kind: {kind!r}")
    raise ValueError(f"bad policy spec: {spec!r}")
