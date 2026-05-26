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

Stateless-probe model (feature/stateless-probes branch):

Two signal sources feed this table:

- `record_probe_sent(tenant, plane, path)` — called by the sender's
  emit loop when it puts a probe on the wire.
- `record_probe_recv(tenant, plane, path)` — called by the daemon's
  dispatch loop when a probe round-trips back to the sender. There
  is no per-probe correspondence (no req_id, no match table) — the
  signal is purely "did a probe make it back on this EV?".
- `record_loss_window(...)` — receiver-side passive loss feedback
  (unchanged from prior versions).

EV health derivation:

A per-EV sliding window tracks (sent, recv) counts over the last
`probe_window_ticks` ticks. Each tick is one `probe_interval_ms`
slot, advanced by `tick(tenant)` from agent's window loop. The
window's recv/sent ratio is the EV's health signal.

- Demote (GOOD/UNKNOWN -> ASSUMED_BAD) when window_sent >=
  probe_min_samples AND window_recv / window_sent < probe_fail_ratio.
- Recover (ASSUMED_BAD -> GOOD) when window_sent >= probe_min_samples
  AND window_recv / window_sent >= probe_recover_ratio for
  `probe_recover_ticks` consecutive evaluations.

A `health_aware_mrc` policy reads `weights_ev()` / `state(...)` on the
TX hot path; both are lock-free and return slightly stale data, which
is fine — we're voting on EV health over hundreds of milliseconds, not
nanoseconds.

State transitions are guarded by a `threading.Lock` so the RX thread
that calls `record_*` can't race the TX-thread reads. Callers that
mutate state from a single thread can pass `lock=None` to disable.

See `docs/design-mrc.md` "Detection & re-spray" and
`docs/stateless-probes-validation.md` for the design rationale.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable


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

    Stateless-probe parameters:

    - `probe_window_ticks`: how many ticks (one tick per probe slot,
      i.e. one per `probe_interval_ms` on the agent) the sliding
      window covers. With probe_interval_ms=200 and window_ticks=5
      that's a 1-second window — long enough to average over the
      jitter floor, short enough to react to a hard EV failure in
      ~1 round of the demote threshold.
    - `probe_min_samples`: minimum probes sent in the window before
      we trust the ratio. Below this we hold state. Prevents
      premature demote on slow-start.
    - `probe_fail_ratio`: recv/sent ratio below this in a window
      counts as a failing EV. 0.5 = "missing more than half" is
      well above the per-probe drop rate we expect on a healthy
      fabric (essentially zero).
    - `probe_recover_ratio`: recv/sent ratio at or above this
      across `probe_recover_ticks` consecutive evaluations promotes
      a previously-bad EV back to GOOD. Higher than fail_ratio so
      we don't flap between states.
    - `probe_recover_ticks`: how many consecutive healthy windows
      are required to recover. Asymmetric vs demote (1 bad window
      is enough to demote; N good windows required to recover).
    """
    # Stateless-probe sliding-window thresholds
    probe_window_ticks: int = 5
    probe_min_samples: int = 3
    probe_fail_ratio: float = 0.5
    probe_recover_ratio: float = 0.9
    probe_recover_ticks: int = 5

    # loss_threshold: minimum loss ratio that counts as a "bad window"
    # for the consecutive-bad-window demote counter. Set above the
    # window-edge straggle noise floor: with packet-level EV spray over
    # 16 EVs at typical lab rates, each EV sees ~5 packets per 200ms
    # loss window; a single packet straddling the window boundary
    # (sent in window N but received in window N+1, or vice versa)
    # produces an apparent 20% loss on a healthy EV. 5% is below that
    # floor; 25% is well above it.
    loss_threshold: float = 0.25
    # loss_demote_consecutive: how many consecutive >loss_threshold
    # windows are required to demote.
    loss_demote_consecutive: int = 3
    # `mrc_min_active_evs`: floor below which the state machine refuses
    # to demote further. None = `max(1, (num_planes * num_paths) // 2)`.
    min_active_evs: int | None = None

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
    # Stateless-probe sliding window.
    # Each bucket holds (sent, recv) for one tick. The window's total
    # is the sum across all buckets; ratio = sum_recv / sum_sent.
    #
    # `current_sent` / `current_recv` are the in-progress bucket
    # (not yet rotated into `buckets`). `tick()` shifts them in.
    current_sent: int = 0
    current_recv: int = 0
    buckets: deque[tuple[int, int]] = field(
        default_factory=lambda: deque(maxlen=5)
    )
    # Lifetime totals (diagnostic; surfaced in snapshot).
    total_sent: int = 0
    total_recv: int = 0
    # Consecutive healthy windows (for recovery latch).
    consecutive_healthy_windows: int = 0
    # Loss-feedback signal (unchanged).
    consecutive_loss_demote_windows: int = 0
    last_loss_ratio: float = 0.0
    # Last evaluated window ratio (diagnostic).
    last_probe_ratio: float = 0.0
    # Counters surfaced in reports.
    transitions: int = 0
    demotes_suppressed_by_floor: int = 0


# --- table -----------------------------------------------------------------

TransitionCb = Callable[[str, int, int, EVState, EVState], None]


class EVStateTable:
    """Mutable per-(tenant, plane, path) EV state, fed by stateless probes
    + loss reports.

    Construction:
        t = EVStateTable(
            tenants=("yellow",),
            num_planes=4,
            num_paths=4,
            cfg=EVStateConfig(),
        )

    Signal in:
        t.record_probe_sent("yellow", plane=2, path=1)
        t.record_probe_recv("yellow", plane=2, path=1)
        t.tick("yellow")  # called once per probe_interval_ms
        t.record_loss_window(
            "yellow", plane=2, path=1, seen=950, expected=1000,
        )

    Policy reads:
        t.state("yellow", plane=2, path=1)   -> EVState
        t.weights_ev("yellow")               -> tuple[tuple[float, ...], ...]
        t.good_evs("yellow")                 -> frozenset[tuple[int, int]]

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
        self._lock = threading.Lock() if lock is ... else lock

        win = self._cfg.probe_window_ticks
        # 3-D storage: [tenant][plane][path] -> _EVRecord
        self._evs: dict[str, list[list[_EVRecord]]] = {
            tenant: [
                [
                    _EVRecord(buckets=deque(maxlen=win))
                    for _ in range(num_paths)
                ]
                for _ in range(num_planes)
            ]
            for tenant in self._tenants
        }
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
        return self._min_active

    @property
    def cfg(self) -> EVStateConfig:
        return self._cfg

    # ------------------------------------------------------------------
    # Signal ingress: stateless probes
    # ------------------------------------------------------------------

    def record_probe_sent(
        self, tenant: str, plane: int, path: int,
    ) -> None:
        """Bump the in-progress bucket's sent count for one EV.

        Called by the agent emit loop every time a probe is put on the
        wire. The bucket is rotated into the sliding window by `tick()`.
        """
        self._check_tenant(tenant)
        self._check_ev(plane, path)
        with self._guard():
            rec = self._evs[tenant][plane][path]
            rec.current_sent += 1
            rec.total_sent += 1

    def record_probe_recv(
        self, tenant: str, plane: int, path: int,
    ) -> None:
        """Bump the in-progress bucket's recv count for one EV.

        Called by the daemon dispatch loop when a probe round-trips
        back to the sender (identified by inner-dst -> (plane, path)
        via topo.probe_ev_from_inner_dst). No per-probe match — recv
        is a free counter under recv >= 0.

        It is normal for `current_recv` to occasionally exceed
        `current_sent` within a single bucket: a probe sent in
        bucket N may return after bucket N has rotated out. The
        sliding-window total over `probe_window_ticks` is what the
        health signal samples; per-bucket imbalance is expected.
        """
        self._check_tenant(tenant)
        self._check_ev(plane, path)
        with self._guard():
            rec = self._evs[tenant][plane][path]
            rec.current_recv += 1
            rec.total_recv += 1

    def tick(self, tenant: str) -> None:
        """Advance the sliding window by one bucket for all EVs of `tenant`.

        Called by the agent's window loop once per `probe_interval_ms`.
        Each EV's in-progress (sent, recv) bucket is appended to its
        ring (oldest bucket auto-falls off via `deque(maxlen=...)`),
        the in-progress counters reset, and the resulting window
        ratio drives state transitions.
        """
        self._check_tenant(tenant)
        with self._guard():
            for plane in range(self._num_planes):
                for path in range(self._num_paths):
                    rec = self._evs[tenant][plane][path]
                    rec.buckets.append((rec.current_sent, rec.current_recv))
                    rec.current_sent = 0
                    rec.current_recv = 0
                    self._evaluate_locked(tenant, plane, path)

    def _evaluate_locked(
        self, tenant: str, plane: int, path: int,
    ) -> None:
        """Re-evaluate one EV's health after a bucket rotation.

        Called by `tick()` under the lock. Computes the window's
        recv/sent ratio and applies demote/recover rules.
        """
        rec = self._evs[tenant][plane][path]
        sent = sum(s for s, _ in rec.buckets)
        recv = sum(r for _, r in rec.buckets)
        if sent == 0:
            rec.last_probe_ratio = 0.0
            # No traffic in window — neither demote nor recover evidence.
            return
        # Cap recv at sent for the ratio (recv > sent can happen
        # transiently when a probe returns after its send bucket
        # rotated out; the lifetime totals stay honest).
        capped_recv = min(recv, sent)
        ratio = capped_recv / sent
        rec.last_probe_ratio = ratio
        cfg = self._cfg

        # Below min_samples: hold state. Reset healthy-window counter
        # so we don't accidentally recover off a thin sample.
        if sent < cfg.probe_min_samples:
            rec.consecutive_healthy_windows = 0
            return

        if ratio < cfg.probe_fail_ratio:
            rec.consecutive_healthy_windows = 0
            if rec.state is not EVState.ASSUMED_BAD:
                self._try_demote_locked(tenant, plane, path)
            return

        if ratio >= cfg.probe_recover_ratio:
            rec.consecutive_healthy_windows += 1
            if (
                rec.state is not EVState.GOOD
                and rec.consecutive_loss_demote_windows == 0
                and rec.consecutive_healthy_windows >= cfg.probe_recover_ticks
            ):
                self._transition_locked(
                    tenant, plane, path, EVState.GOOD,
                )
            return

        # In-between (recover_ratio > ratio >= fail_ratio): hold.
        rec.consecutive_healthy_windows = 0

    # ------------------------------------------------------------------
    # Signal ingress: receiver loss feedback (unchanged)
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

        See module docstring for transition semantics. Unchanged from
        the stateful-probe design — loss windows remain a parallel
        signal independent of probe success.
        """
        self._check_tenant(tenant)
        self._check_ev(plane, path)
        if seen < 0 or expected < 0:
            raise ValueError(
                f"seen/expected must be >= 0, got seen={seen} expected={expected}"
            )
        if seen > expected:
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

    # ------------------------------------------------------------------
    # Reads (lock-free)
    # ------------------------------------------------------------------

    def state(self, tenant: str, plane: int, path: int) -> EVState:
        self._check_tenant(tenant)
        self._check_ev(plane, path)
        return self._evs[tenant][plane][path].state

    def inspect(self, tenant: str, plane: int, path: int) -> dict[str, Any]:
        self._check_tenant(tenant)
        self._check_ev(plane, path)
        rec = self._evs[tenant][plane][path]
        return {
            "state": rec.state.value,
            "current_sent": rec.current_sent,
            "current_recv": rec.current_recv,
            "window_sent": sum(s for s, _ in rec.buckets),
            "window_recv": sum(r for _, r in rec.buckets),
            "total_sent": rec.total_sent,
            "total_recv": rec.total_recv,
            "last_probe_ratio": rec.last_probe_ratio,
            "consecutive_healthy_windows": rec.consecutive_healthy_windows,
            "consecutive_loss_demote_windows":
                rec.consecutive_loss_demote_windows,
            "last_loss_ratio": rec.last_loss_ratio,
            "transitions": rec.transitions,
            "demotes_suppressed_by_floor": rec.demotes_suppressed_by_floor,
        }

    def weights_ev(self, tenant: str) -> tuple[tuple[float, ...], ...]:
        self._check_tenant(tenant)
        return self._weights_cache[tenant]

    def good_evs(self, tenant: str) -> frozenset[tuple[int, int]]:
        self._check_tenant(tenant)
        return frozenset(
            (plane, path)
            for plane in range(self._num_planes)
            for path in range(self._num_paths)
            if self._evs[tenant][plane][path].state is EVState.GOOD
        )

    def snapshot(self) -> dict:
        """JSON-friendly view of the table, suitable for report.py."""
        out: dict = {
            "config": {
                "probe_window_ticks": self._cfg.probe_window_ticks,
                "probe_min_samples": self._cfg.probe_min_samples,
                "probe_fail_ratio": self._cfg.probe_fail_ratio,
                "probe_recover_ratio": self._cfg.probe_recover_ratio,
                "probe_recover_ticks": self._cfg.probe_recover_ticks,
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
                    window_sent = sum(s for s, _ in rec.buckets)
                    window_recv = sum(r for _, r in rec.buckets)
                    evs_out.append({
                        "plane": plane,
                        "path": path,
                        "state": rec.state.value,
                        "window_sent": window_sent,
                        "window_recv": window_recv,
                        "total_sent": rec.total_sent,
                        "total_recv": rec.total_recv,
                        "last_probe_ratio": round(rec.last_probe_ratio, 6),
                        "consecutive_healthy_windows":
                            rec.consecutive_healthy_windows,
                        "consecutive_loss_demote_windows":
                            rec.consecutive_loss_demote_windows,
                        "last_loss_ratio": round(rec.last_loss_ratio, 6),
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
        if self._lock is None:
            return _NullCtx()
        return self._lock

    def _try_demote_locked(
        self, tenant: str, plane: int, path: int,
    ) -> None:
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
        # On promote, clear healthy-window counter for the next round.
        if new_state is EVState.GOOD:
            rec.consecutive_healthy_windows = 0
        # On demote, reset the same counter so a recovery streak starts
        # fresh once the EV starts succeeding again.
        if new_state is EVState.ASSUMED_BAD:
            rec.consecutive_healthy_windows = 0
        self._rebuild_weights_locked(tenant)
        if self._on_transition is not None:
            self._on_transition(tenant, plane, path, old, new_state)

    def _rebuild_weights_locked(self, tenant: str) -> None:
        raw: list[list[float]] = [
            [
                _STATE_WEIGHT[self._evs[tenant][plane][path].state]
                for path in range(self._num_paths)
            ]
            for plane in range(self._num_planes)
        ]
        total = sum(w for row in raw for w in row)
        if total <= 0:
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
