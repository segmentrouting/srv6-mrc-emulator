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

import json
import os
import threading
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



class MrcSnapshot:
    """Snapshot-backed health-aware MRC policy.

    Wire-compatible behavioral twin of `HealthAwareMrc`, but instead of
    holding a live `EVStateTable` it reads per-EV weights from a JSON
    snapshot file written by an `MrcDaemon`. The snapshot file is the
    output of `EVStateTable.snapshot()` (see srv6_mrc.mrc.ev_state) and
    is refreshed atomically by the daemon on every probe interval.

    Lifecycle (Refactor: MRC daemon, step b)
    =========================================

    Built as the spray policy of a *data sender* process. Data senders
    no longer own a `SenderMrcAgent`; instead, one `MrcDaemon` per
    src_host owns all the agents and publishes per-flow snapshots to
    `/dev/shm/srv6-mrc/<host>/<tenant>_<dst_id>.json`. Each data sender
    constructs `MrcSnapshot(snapshot_path, tenant, ...)` pointing at
    *its* flow's snapshot, and the policy's background refresh thread
    reloads the file every `refresh_interval_ms` (default 200 ms,
    matching the daemon's snapshot cadence).

    The data sender's hot loop is unchanged: it calls `pick_ev(seq,
    flow)` exactly the same way it would call `HealthAwareMrc.pick_ev`,
    and the policy serves the most recently loaded weight grid. The
    only difference visible to the rest of the system is *where the
    EVStateTable lives*: in another process, behind a snapshot file.

    Cold-start
    ----------

    If the snapshot file does not yet exist when the policy starts (the
    daemon is still warming up its first probe round), the policy falls
    back to *uniform weights* over all (plane, path) cells. That's
    indistinguishable from the cold-start behavior of `HealthAwareMrc`
    (when every EV is UNKNOWN, `weights_ev()` also returns uniform).

    Once the daemon writes its first snapshot the refresh thread picks
    it up on the next tick and the policy becomes health-aware.

    Transient read failures
    -----------------------

    If a refresh attempt fails (file vanished, partial write, JSON
    decode error), the policy keeps the previously loaded grid rather
    than reverting to uniform. This avoids a transient `/dev/shm` glitch
    causing every data sender to suddenly distribute uniformly across
    EVs that the daemon already knows are bad.

    The daemon writes snapshots atomically (write-to-tmp + rename), so
    in practice the only failure mode at steady state is the file
    briefly not existing during process startup; the cold-start path
    handles that.

    Per-flow spine subset
    ---------------------

    Identical to `HealthAwareMrc` and `EvSpray`: hash-derived from the
    flow's address pair, cached per-flow, sized by `paths_per_plane`.
    """

    def __init__(
        self,
        snapshot_path: str,
        tenant: str,
        paths_per_plane: int = _DEFAULT_PATHS,
        topology: Topology | None = None,
        refresh_interval_ms: int = 200,
    ) -> None:
        n_planes = topology.planes if topology else NUM_PLANES
        n_spines = (topology.spines_per_plane
                    if topology else NUM_SPINES)
        if paths_per_plane == _DEFAULT_PATHS:
            paths_per_plane = n_spines
        if not (1 <= paths_per_plane <= n_spines):
            raise ValueError(
                f"paths_per_plane must be 1..{n_spines}, "
                f"got {paths_per_plane}"
            )
        if refresh_interval_ms <= 0:
            raise ValueError(
                f"refresh_interval_ms must be > 0, "
                f"got {refresh_interval_ms}"
            )

        self.snapshot_path = snapshot_path
        self.tenant = tenant
        self.paths_per_plane = paths_per_plane
        self.topology = topology
        self.refresh_interval_ms = refresh_interval_ms
        self.name = f"mrc_snapshot({tenant}@{snapshot_path})"

        self._n_planes = n_planes
        self._n_spines = n_spines
        # Atomic-swappable wgrid. Reads in pick_ev never need the lock —
        # they grab the immutable tuple-of-tuples reference once and
        # work from it. Refresh thread builds a new grid and assigns it
        # in one operation.
        self._wgrid: tuple[tuple[float, ...], ...] = self._uniform_wgrid()
        # mtime of the most recently loaded snapshot. We only reparse
        # when the file's mtime advances, to avoid hammering JSON parse
        # at refresh cadence when the daemon is idle.
        self._last_mtime: float = 0.0

        # Per-flow spine subset cache (mirrors EvSpray._spine_subsets).
        self._spine_subsets: dict = {}

        # Refresh thread machinery. Started on .start(), stopped on
        # .stop(). The data sender owns the lifecycle.
        self._stop_event = threading.Event()
        self._refresh_thread: threading.Thread | None = None

        # Diagnostic counters — surface in tests / report.py to confirm
        # the policy actually saw fresh snapshots during the run rather
        # than running cold-start uniform the whole time.
        self.refresh_attempts: int = 0
        self.refresh_loaded: int = 0
        self.refresh_errors: int = 0
        self.refresh_missing: int = 0
        # Lock guards _wgrid+_last_mtime swap and counter increments
        # against tests that drive _refresh_once() from foreign threads.
        self._lock = threading.Lock()

        # Try an initial load synchronously so a sender that starts
        # *after* the daemon is warm gets real weights on its very
        # first pick rather than 200 ms of uniform.
        self._refresh_once()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background refresh thread.

        Idempotent: calling .start() twice is a no-op (logs a warning
        in noisy environments would be nice but stdlib-only and we
        don't want to drag in logging here).
        """
        if self._refresh_thread is not None:
            return
        self._stop_event.clear()
        t = threading.Thread(
            target=self._refresh_loop,
            name=f"mrc-snapshot-refresh-{self.tenant}",
            daemon=True,
        )
        self._refresh_thread = t
        t.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Stop the refresh thread. Idempotent."""
        if self._refresh_thread is None:
            return
        self._stop_event.set()
        self._refresh_thread.join(timeout=timeout)
        self._refresh_thread = None

    # ------------------------------------------------------------------
    # Pick
    # ------------------------------------------------------------------

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

        Same contract as HealthAwareMrc.pick_ev — see that class for
        the full signature rationale.
        """
        spines = self._spines_for_flow(flow)
        n_planes = self._n_planes
        # Single-shot read of the atomic weight grid reference. The
        # refresh thread may concurrently swap _wgrid out for a new
        # tuple; we capture our reference here and that snapshot is
        # what the rest of this pick uses.
        wgrid = self._wgrid

        flat: list[float] = []
        for plane in range(n_planes):
            row = wgrid[plane]
            for sp in spines:
                flat.append(row[sp])
        total = sum(flat)
        if total <= 0:
            n = n_planes * len(spines)
            ev_idx = seq % n
        else:
            cdf = _build_cdf(tuple(flat))
            ev_idx = _weighted_pick(seq, flow, cdf)
        plane = ev_idx // len(spines)
        spine = spines[ev_idx % len(spines)]
        return plane, spine

    def pick(self, seq: int, flow: FlowKey) -> int:
        plane, _ = self.pick_ev(seq, flow)
        return plane

    # ------------------------------------------------------------------
    # Refresh / file I/O
    # ------------------------------------------------------------------

    def _uniform_wgrid(self) -> tuple[tuple[float, ...], ...]:
        n_planes = self._n_planes if hasattr(self, "_n_planes") \
            else (self.topology.planes if self.topology else NUM_PLANES)
        n_spines = self._n_spines if hasattr(self, "_n_spines") \
            else (self.topology.spines_per_plane
                  if self.topology else NUM_SPINES)
        n_total = n_planes * n_spines
        w = 1.0 / n_total if n_total > 0 else 0.0
        return tuple(tuple(w for _ in range(n_spines))
                     for _ in range(n_planes))

    def _refresh_loop(self) -> None:
        interval_s = self.refresh_interval_ms / 1000.0
        while not self._stop_event.wait(interval_s):
            self._refresh_once()

    def _refresh_once(self) -> bool:
        """Reload snapshot if mtime has advanced. Returns True iff loaded.

        Public-ish (tests poke it) but not a stable API. Caller-thread
        agnostic: safe to call from the refresh thread *or* directly
        from a test thread.
        """
        with self._lock:
            self.refresh_attempts += 1
            try:
                st = os.stat(self.snapshot_path)
            except FileNotFoundError:
                self.refresh_missing += 1
                return False
            except OSError:
                self.refresh_errors += 1
                return False
            mtime = st.st_mtime
            if mtime <= self._last_mtime:
                return False
            try:
                with open(self.snapshot_path, "rb") as f:
                    data = json.load(f)
                wgrid = self._wgrid_from_snapshot(data)
            except (OSError, json.JSONDecodeError, KeyError, ValueError,
                    TypeError):
                # Transient error: keep the existing grid, count the
                # error, try again next tick.
                self.refresh_errors += 1
                return False
            # Atomic swap. Tuple is immutable, so concurrent pick_ev
            # readers either see the old grid or the new grid in full.
            self._wgrid = wgrid
            self._last_mtime = mtime
            self.refresh_loaded += 1
            return True

    def _wgrid_from_snapshot(self, data: dict) -> tuple[tuple[float, ...], ...]:
        """Convert an EVStateTable.snapshot() dict to a wgrid tuple.

        Validates dimensions against this policy's topology so a
        misaddressed snapshot file (e.g. wrong tenant or stale
        topology) raises ValueError rather than silently producing
        an undersized grid.
        """
        n_planes = int(data["num_planes"])
        n_paths = int(data["num_paths"])
        if n_planes != self._n_planes or n_paths != self._n_spines:
            raise ValueError(
                f"snapshot dimensions ({n_planes}x{n_paths}) do not "
                f"match policy topology ({self._n_planes}x"
                f"{self._n_spines})"
            )
        tenants = data.get("tenants", {})
        if self.tenant not in tenants:
            raise KeyError(
                f"tenant {self.tenant!r} not in snapshot tenants "
                f"({list(tenants.keys())})"
            )
        evs = tenants[self.tenant]
        expected = n_planes * n_paths
        if len(evs) != expected:
            raise ValueError(
                f"snapshot tenant {self.tenant!r} has {len(evs)} EVs, "
                f"expected {expected}"
            )
        # Snapshot encodes EVs in (plane*num_paths + path) order.
        grid: list[tuple[float, ...]] = []
        for plane in range(n_planes):
            row: list[float] = []
            for path in range(n_paths):
                rec = evs[plane * n_paths + path]
                row.append(float(rec["weight"]))
            grid.append(tuple(row))
        return tuple(grid)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Diagnostic counters for inclusion in scenario reports."""
        with self._lock:
            return {
                "refresh_attempts": self.refresh_attempts,
                "refresh_loaded": self.refresh_loaded,
                "refresh_errors": self.refresh_errors,
                "refresh_missing": self.refresh_missing,
                "last_mtime": self._last_mtime,
            }


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
