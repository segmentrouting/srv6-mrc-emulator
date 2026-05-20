"""Per-(tenant, plane, path) EV state machine.

Models the OCP MRC `mrc_ctl_ev_state` enum (`GOOD`, `ASSUMED_BAD`, `UNKNOWN`;
`DENIED` is fabric-admin-only and not modeled here) and the demote/recover
logic that real NICs implement in firmware.

The unit of failure is the EV — the (plane, path) pair. A "path" is the
OCP-spec name for one of several parallel routes through a plane. In our
2-tier Clos with single leaf<->spine links, path == spine; the topology
layer (topo.py) keeps the "spine" name for the physical thing, the MRC
layer (this module) uses "path" for the abstraction. agent.py bridges
the two via `NUM_SPINES` feeding `num_paths`.

This module is pure logic — no sockets, no scapy, no threads. Two signal
sources feed it via separate methods:

- `record_probe_result(...)` for active EV Probes (OCP `MRC_CTL_EP_OP_EV_PROBE`).
- `record_loss_window(...)` for receiver-side passive loss feedback
  (our trim-NACK substitute).

A `health_aware_mrc` policy reads `weights_ev()` / `state(...)` on the
TX hot path; both are lock-free and return slightly stale data, which
is fine — we're voting on EV health over hundreds of milliseconds, not
nanoseconds.

State transitions are guarded by a `threading.Lock` so the RX thread
that calls `record_*` can't race the TX-thread reads. Callers that
mutate state from a single thread can pass `lock=None` to disable.

See `docs/design-mrc.md` "Detection & re-spray" for the design
rationale, including the OCP mapping and asymmetric demote-fast /
recover-slow rule.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable


# --- enums -----------------------------------------------------------------

class EVState(Enum):
    """Matches OCP `enum mrc_ctl_ev_state`, minus DENIED."""
    UNKNOWN = "unknown"
    GOOD = "good"
    ASSUMED_BAD = "assumed_bad"


# Spray weight per state. Sums and zeros are handled in `weights_ev()`.
_STATE_WEIGHT: dict[EVState, float] = {
    EVState.GOOD: 1.0,
    EVState.UNKNOWN: 0.5,
    EVState.ASSUMED_BAD: 0.0,
}


# --- config ----------------------------------------------------------------

@dataclass(frozen=True)
class EVStateConfig:
    """Tunables for the state machine. Defaults match `docs/design-mrc.md`.

    Cadence values are stored in milliseconds for human readability;
    callers that need ns multiply at the boundary.
    """
    probe_fail_threshold: int = 3
    probe_recover_threshold: int = 5
    # loss_threshold: minimum loss ratio that counts as a "bad window"
    # for the consecutive-bad-window demote counter. Set above the
    # window-edge straggle noise floor: with packet-level EV spray over
    # 16 EVs at typical lab rates, each EV sees ~5 packets per 200ms
    # loss window; a single packet straddling the window boundary
    # (sent in window N but received in window N+1, or vice versa)
    # produces an apparent 20% loss on a healthy EV. 5% is below that
    # floor; 25% is well above it. Real EV failures from interface
    # shutdown produce 100% loss on affected EVs and demote in the
    # same number of windows regardless.
    loss_threshold: float = 0.25
    # loss_demote_consecutive: how many consecutive >loss_threshold
    # windows are required to demote. With threshold=0.25 the chance
    # of two healthy windows in a row showing >25% loss from random
    # straggle is small; three in a row is statistically negligible.
    # At loss_window_ms=200ms this gives 600ms demote latency, which
    # matches the probe path (probe_fail_threshold=3 *
    # probe_interval_ms=200 = 600ms), so neither path is the long
    # pole on a hard EV failure.
    loss_demote_consecutive: int = 3
    # `mrc_min_active_evs`: floor below which the state machine refuses
    # to demote further. None = `max(1, (num_planes * num_paths) // 2)`.
    # Counts (plane, path) EVs, not planes — a partial-spine failure on
    # one plane doesn't kill the plane.
    min_active_evs: int | None = None
    # RTT ring length (probe samples kept per EV for p50/p99 reporting).
    rtt_ring_size: int = 64

    def resolve_min_active(self, num_planes: int, num_paths: int) -> int:
        total = num_planes * num_paths
        if self.min_active_evs is None:
            return max(1, total // 2)
        return max(1, min(self.min_active_evs, total))


# --- per-EV record ---------------------------------------------------------

@dataclass
class _EVRecord:
    """All mutable per-(tenant, plane, path) bookkeeping in one place."""
    state: EVState = EVState.UNKNOWN
    # Probe signal
    consecutive_probe_timeouts: int = 0
    consecutive_probe_successes: int = 0
    rtt_ring_ns: deque[int] = field(default_factory=lambda: deque(maxlen=64))
    # Loss-feedback signal
    consecutive_loss_demote_windows: int = 0
    last_loss_ratio: float = 0.0
    # Counters surfaced in reports.
    transitions: int = 0
    demotes_suppressed_by_floor: int = 0


# --- table -----------------------------------------------------------------

# Type of the optional `on_transition(tenant, plane, path, old, new)` callback.
TransitionCb = Callable[[str, int, int, EVState, EVState], None]


class EVStateTable:
    """Mutable per-(tenant, plane, path) EV state, fed by probes + loss reports.

    Construction:
        t = EVStateTable(
            tenants=("green", "yellow"),
            num_planes=4,
            num_paths=8,
            cfg=EVStateConfig(),
        )

    Signal in:
        t.record_probe_result(
            "green", plane=2, path=5,
            success=True, rtt_ns=1_200_000,
        )
        t.record_probe_result("green", plane=2, path=5, success=False)
        t.record_loss_window(
            "green", plane=2, path=5, seen=950, expected=1000,
        )

    Policy reads:
        t.state("green", plane=2, path=5)    -> EVState
        t.weights_ev("green")                -> tuple[tuple[float, ...], ...]
                                                # shape [num_planes][num_paths]
        t.good_evs("green")                  -> frozenset[tuple[int, int]]

    Reporting:
        t.snapshot()                         -> dict suitable for JSON

    Threading:
        The internal lock protects state transitions. `state()` /
        `weights_ev()` are intentionally lock-free reads of a single
        attribute (`_weights_cache[tenant]`) that is replaced atomically
        on every state change.
    """

    def __init__(
        self,
        tenants: Iterable[str],
        num_planes: int,
        num_paths: int,
        cfg: EVStateConfig | None = None,
        on_transition: TransitionCb | None = None,
        lock: threading.Lock | None = ...,  # type: ignore[assignment]
    ) -> None:
        self._tenants = tuple(tenants)
        if not self._tenants:
            raise ValueError("tenants must be non-empty")
        if num_planes < 1:
            raise ValueError(f"num_planes must be >= 1, got {num_planes}")
        if num_paths < 1:
            raise ValueError(f"num_paths must be >= 1, got {num_paths}")
        self._num_planes = num_planes
        self._num_paths = num_paths
        self._cfg = cfg or EVStateConfig()
        self._on_transition = on_transition
        self._min_active = self._cfg.resolve_min_active(num_planes, num_paths)
        # `lock=...` (sentinel) -> default to a real lock. lock=None -> no
        # locking (single-threaded callers).
        self._lock = threading.Lock() if lock is ... else lock

        rec_cfg = self._cfg
        # 3-D storage: [tenant][plane][path] -> _EVRecord
        self._evs: dict[str, list[list[_EVRecord]]] = {
            tenant: [
                [
                    _EVRecord(
                        rtt_ring_ns=deque(maxlen=rec_cfg.rtt_ring_size),
                    )
                    for _ in range(num_paths)
                ]
                for _ in range(num_planes)
            ]
            for tenant in self._tenants
        }
        # Cache for lock-free reads. Rebuilt on every state transition.
        # Indexed by tenant -> 2-D tuple of normalized weights per EV.
        self._weights_cache: dict[str, tuple[tuple[float, ...], ...]] = {}
        for tenant in self._tenants:
            self._rebuild_weights_locked(tenant)

    # ------------------------------------------------------------------
    # Configuration / shape introspection
    # ------------------------------------------------------------------

    @property
    def tenants(self) -> tuple[str, ...]:
        return self._tenants

    @property
    def num_planes(self) -> int:
        return self._num_planes

    @property
    def num_paths(self) -> int:
        return self._num_paths

    @property
    def min_active(self) -> int:
        """Minimum non-ASSUMED_BAD EVs to keep across (plane, path) cells."""
        return self._min_active

    @property
    def cfg(self) -> EVStateConfig:
        return self._cfg

    # ------------------------------------------------------------------
    # Signal ingress: probes
    # ------------------------------------------------------------------

    def record_probe_result(
        self,
        tenant: str,
        plane: int,
        path: int,
        success: bool,
        rtt_ns: int | None = None,
    ) -> None:
        """Record one probe outcome for the (plane, path) EV.

        `success=True` requires `rtt_ns` (the measured round-trip). On
        timeout, pass `success=False` and `rtt_ns=None`.

        Transitions (per EV, not per plane):
            success: consecutive_successes++, consecutive_timeouts=0.
                When successes >= probe_recover_threshold AND the loss
                signal is also quiet (consecutive_loss_demote_windows==0)
                AND current state != GOOD, promote to GOOD.
            timeout: consecutive_timeouts++, consecutive_successes=0.
                When timeouts >= probe_fail_threshold AND current state
                != ASSUMED_BAD, demote (subject to min_active_evs floor).
        """
        self._check_tenant(tenant)
        self._check_ev(plane, path)
        with self._guard():
            rec = self._evs[tenant][plane][path]
            if success:
                if rtt_ns is None:
                    raise ValueError(
                        "record_probe_result(success=True) requires rtt_ns"
                    )
                if rtt_ns < 0:
                    raise ValueError(f"rtt_ns must be >= 0, got {rtt_ns}")
                rec.consecutive_probe_successes += 1
                rec.consecutive_probe_timeouts = 0
                rec.rtt_ring_ns.append(rtt_ns)
                if (
                    rec.state is not EVState.GOOD
                    and rec.consecutive_probe_successes
                        >= self._cfg.probe_recover_threshold
                    and rec.consecutive_loss_demote_windows == 0
                ):
                    self._transition_locked(
                        tenant, plane, path, EVState.GOOD,
                    )
            else:
                rec.consecutive_probe_timeouts += 1
                rec.consecutive_probe_successes = 0
                if (
                    rec.state is not EVState.ASSUMED_BAD
                    and rec.consecutive_probe_timeouts
                        >= self._cfg.probe_fail_threshold
                ):
                    self._try_demote_locked(tenant, plane, path)

    # ------------------------------------------------------------------
    # Signal ingress: receiver loss feedback
    # ------------------------------------------------------------------

    def record_loss_window(
        self,
        tenant: str,
        plane: int,
        path: int,
        seen: int,
        expected: int,
    ) -> None:
        """Record one loss-report window for an (plane, path) EV.

        `expected` is the number of packets the receiver believed should
        have arrived in the window (from sender-side seq numbering);
        `seen` is what actually arrived. `expected==0` is a no-op (no
        traffic on that EV in the window — neither demote nor recover
        evidence).

        Transitions:
            loss_ratio > loss_threshold:
                consecutive_loss_demote_windows++. When >=
                loss_demote_consecutive, demote (subject to floor).
            loss_ratio <= loss_threshold / 2:
                consecutive_loss_demote_windows = 0 (counts as quiet,
                contributes toward eventual recovery via probe path).
            else (mildly elevated but below demote): leave counter
                unchanged.
        """
        self._check_tenant(tenant)
        self._check_ev(plane, path)
        if seen < 0 or expected < 0:
            raise ValueError(
                f"seen/expected must be >= 0, got seen={seen} expected={expected}"
            )
        if seen > expected:
            # Reordered late arrivals can push seen past expected in a
            # given window; clamp rather than reject.
            seen = expected
        if expected == 0:
            return
        ratio = (expected - seen) / expected
        with self._guard():
            rec = self._evs[tenant][plane][path]
            rec.last_loss_ratio = ratio
            if ratio > self._cfg.loss_threshold:
                rec.consecutive_loss_demote_windows += 1
                if (
                    rec.state is not EVState.ASSUMED_BAD
                    and rec.consecutive_loss_demote_windows
                        >= self._cfg.loss_demote_consecutive
                ):
                    self._try_demote_locked(tenant, plane, path)
            elif ratio <= self._cfg.loss_threshold / 2:
                rec.consecutive_loss_demote_windows = 0
            # mild-but-non-zero loss falls through without changing the
            # counter — neither demote evidence nor recovery evidence.

    # ------------------------------------------------------------------
    # Reads (lock-free)
    # ------------------------------------------------------------------

    def state(self, tenant: str, plane: int, path: int) -> EVState:
        self._check_tenant(tenant)
        self._check_ev(plane, path)
        # Reading a single attribute of a dataclass is atomic in CPython
        # under the GIL; no lock required for the staleness we accept.
        return self._evs[tenant][plane][path].state

    def weights_ev(self, tenant: str) -> tuple[tuple[float, ...], ...]:
        """Normalized spray weights per EV for `tenant`.

        Returns a 2-D tuple of shape [num_planes][num_paths]. The sum
        over all (plane, path) cells is 1.0 when at least one EV has
        positive weight. If every EV is ASSUMED_BAD, returns uniform
        weights over all cells — the min_active_evs floor should
        normally prevent this, but if it somehow happens we degrade to
        spreading rather than collapsing.

        Policy code that wants to restrict spray to a flow-specific
        subset (e.g. a particular path-per-plane sample) can slice this
        2-D structure and renormalize at draw time.
        """
        self._check_tenant(tenant)
        return self._weights_cache[tenant]

    def good_evs(self, tenant: str) -> frozenset[tuple[int, int]]:
        """All (plane, path) pairs currently in EVState.GOOD."""
        self._check_tenant(tenant)
        return frozenset(
            (plane, path)
            for plane in range(self._num_planes)
            for path in range(self._num_paths)
            if self._evs[tenant][plane][path].state is EVState.GOOD
        )

    def rtt_p50_ns(self, tenant: str, plane: int, path: int) -> int | None:
        self._check_tenant(tenant)
        self._check_ev(plane, path)
        ring = self._evs[tenant][plane][path].rtt_ring_ns
        if not ring:
            return None
        s = sorted(ring)
        return s[len(s) // 2]

    def rtt_p99_ns(self, tenant: str, plane: int, path: int) -> int | None:
        self._check_tenant(tenant)
        self._check_ev(plane, path)
        ring = self._evs[tenant][plane][path].rtt_ring_ns
        if not ring:
            return None
        s = sorted(ring)
        idx = min(len(s) - 1, (len(s) * 99) // 100)
        return s[idx]

    def snapshot(self) -> dict:
        """JSON-friendly view of the table, suitable for report.py.

        Per-EV records are listed flat under each tenant in (plane, path)
        order. The 2-D shape is implied by `num_planes` / `num_paths`
        and recoverable as `plane = idx // num_paths`,
        `path = idx % num_paths` — callers that want the explicit grid
        can reshape themselves.
        """
        out: dict = {
            "config": {
                "probe_fail_threshold": self._cfg.probe_fail_threshold,
                "probe_recover_threshold": self._cfg.probe_recover_threshold,
                "loss_threshold": self._cfg.loss_threshold,
                "loss_demote_consecutive": self._cfg.loss_demote_consecutive,
                "min_active_evs": self._min_active,
            },
            "num_planes": self._num_planes,
            "num_paths": self._num_paths,
            "tenants": {},
        }
        for tenant in self._tenants:
            evs_out: list[dict] = []
            wcache = self._weights_cache[tenant]
            for plane in range(self._num_planes):
                for path in range(self._num_paths):
                    rec = self._evs[tenant][plane][path]
                    evs_out.append({
                        "plane": plane,
                        "path": path,
                        "state": rec.state.value,
                        "consecutive_probe_timeouts":
                            rec.consecutive_probe_timeouts,
                        "consecutive_probe_successes":
                            rec.consecutive_probe_successes,
                        "consecutive_loss_demote_windows":
                            rec.consecutive_loss_demote_windows,
                        "last_loss_ratio": round(rec.last_loss_ratio, 6),
                        "rtt_p50_ns": self.rtt_p50_ns(tenant, plane, path),
                        "rtt_p99_ns": self.rtt_p99_ns(tenant, plane, path),
                        "transitions": rec.transitions,
                        "demotes_suppressed_by_floor":
                            rec.demotes_suppressed_by_floor,
                        "weight": wcache[plane][path],
                    })
            out["tenants"][tenant] = evs_out
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_tenant(self, tenant: str) -> None:
        if tenant not in self._evs:
            raise ValueError(
                f"unknown tenant {tenant!r}; known: {self._tenants}"
            )

    def _check_ev(self, plane: int, path: int) -> None:
        if not 0 <= plane < self._num_planes:
            raise ValueError(
                f"plane {plane} out of range [0, {self._num_planes})"
            )
        if not 0 <= path < self._num_paths:
            raise ValueError(
                f"path {path} out of range [0, {self._num_paths})"
            )

    def _guard(self):
        # Wraps the optional lock so call sites don't need to branch.
        if self._lock is None:
            return _NullCtx()
        return self._lock

    def _try_demote_locked(
        self, tenant: str, plane: int, path: int,
    ) -> None:
        """Demote one EV to ASSUMED_BAD, honoring min_active_evs floor.

        Caller holds the lock (or lock is None). The floor counts EVs
        (not planes): an EV is "usable" if its state is not
        ASSUMED_BAD. UNKNOWN is treated as usable because demoting a
        large pool of UNKNOWN EVs en masse would collapse spray
        prematurely.
        """
        # Count EVs that would remain non-bad after this demote.
        usable_after = 0
        for p in range(self._num_planes):
            for q in range(self._num_paths):
                if p == plane and q == path:
                    continue
                if self._evs[tenant][p][q].state is not EVState.ASSUMED_BAD:
                    usable_after += 1
        if usable_after < self._min_active:
            rec = self._evs[tenant][plane][path]
            rec.demotes_suppressed_by_floor += 1
            return
        self._transition_locked(tenant, plane, path, EVState.ASSUMED_BAD)

    def _transition_locked(
        self, tenant: str, plane: int, path: int, new_state: EVState,
    ) -> None:
        rec = self._evs[tenant][plane][path]
        old = rec.state
        if old is new_state:
            return
        rec.state = new_state
        rec.transitions += 1
        # On promote to GOOD, clear timeout counter so we don't immediately
        # re-demote from stale data; loss window counter is already 0 (a
        # precondition for entering this branch).
        if new_state is EVState.GOOD:
            rec.consecutive_probe_timeouts = 0
        # On demote, clear success counter for symmetry.
        if new_state is EVState.ASSUMED_BAD:
            rec.consecutive_probe_successes = 0
        self._rebuild_weights_locked(tenant)
        if self._on_transition is not None:
            # Callback runs under the lock — keep it cheap (usually
            # just a log line + a counter bump).
            self._on_transition(tenant, plane, path, old, new_state)

    def _rebuild_weights_locked(self, tenant: str) -> None:
        # Flatten to compute the total, then reshape on output.
        raw: list[list[float]] = [
            [
                _STATE_WEIGHT[self._evs[tenant][plane][path].state]
                for path in range(self._num_paths)
            ]
            for plane in range(self._num_planes)
        ]
        total = sum(w for row in raw for w in row)
        if total <= 0:
            # All EVs ASSUMED_BAD — fall back to uniform so we don't
            # divide by zero and don't collapse traffic onto a single EV.
            n_cells = self._num_planes * self._num_paths
            w = 1.0 / n_cells
            self._weights_cache[tenant] = tuple(
                tuple(w for _ in range(self._num_paths))
                for _ in range(self._num_planes)
            )
            return
        self._weights_cache[tenant] = tuple(
            tuple(x / total for x in row) for row in raw
        )


# --- tiny utility ----------------------------------------------------------

class _NullCtx:
    """Context manager that does nothing — used when lock=None."""
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a) -> None:
        return None
