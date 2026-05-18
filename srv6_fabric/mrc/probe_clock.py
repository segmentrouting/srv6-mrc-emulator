"""Per-EV outstanding-probe tracking + timeout sweep (sender side).

The sender's probe loop emits one PROBE per EV (each (plane, spine)
pair) every probe_interval_ms and wants to know which EVs' probes
never came back. This module holds the bookkeeping for that question,
separated from the I/O so the state-machine logic is unit-testable
without sockets.

Phase 1b step 2 commit 2/5: pre-Phase-1b this was per-plane only.
spine_id is now a required dimension on emit / match_reply / the
timeout sweep result. Internal storage is keyed (plane, spine);
public methods take both. The agent currently passes spine=0 while
the rest of the per-EV stack lands — wire format and storage are
ready, the actual fan-out per spine moves in commit 5.

Lifecycle from the caller's view:

    clock = ProbeClock(num_planes=4, num_spines=8,
                      probe_timeout_ns=50_000_000)

    # Each emit:
    req_id, tx_ns = clock.emit(plane=2, spine=3,
                               now_ns=time.monotonic_ns())
    # ... encode + sendto

    # Each reply received:
    matched = clock.match_reply(
        req_id=req_id, plane=2, spine=3,
        reply_tx_ns=reply.tx_ns, now_ns=time.monotonic_ns(),
    )
    # matched is None (unknown / late / wrong-EV) or an RTT in ns.

    # Periodic timeout sweep (e.g. every probe_interval_ms):
    timeouts = clock.sweep_timeouts(now_ns=time.monotonic_ns())
    for plane, spine, _req_id in timeouts:
        ev_table.record_probe_result(tenant, plane, spine, success=False)

`req_id` is a u16 that wraps, allocated PER EV. Different EVs have
independent number spaces; the (plane, spine, req_id) triple is the
match key. The tracker keeps at most `max_outstanding_per_ev`
entries per EV (default 256, fits in u16 wrap window for typical
cadences). Older outstanding entries are silently evicted — at the
cadences we run (10–100 Hz per EV) this is not reachable in
practice; the cap exists to bound memory in adversarial /
runaway-emit failure modes.

Thread-safety: the tracker uses a single threading.Lock around all
state. Expected callers: one emit thread + one reply RX thread + one
sweep thread. Hot path is short — match_reply is O(1), sweep is O(N)
in outstanding entries. Holding the lock during EV table updates is
NOT ok (those would re-enter the table's lock); the sweep returns a
plain list and the caller updates EVStateTable outside the lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class _OutstandingProbe:
    """Bookkeeping for one in-flight probe.

    `req_id` is also the dict key in `_outstanding[plane][spine]`; we
    keep it in the value too so the sweep can build clean
    (plane, spine, req_id) tuples without re-iterating dict items.
    """
    req_id: int
    plane: int
    spine: int
    tx_ns: int


class ProbeClock:
    """Per-EV req-id allocator + outstanding-probe registry."""

    def __init__(
        self,
        *,
        num_planes: int,
        num_spines: int,
        probe_timeout_ns: int,
        max_outstanding_per_ev: int = 256,
    ) -> None:
        if num_planes <= 0:
            raise ValueError(f"num_planes must be positive, got {num_planes}")
        if num_spines <= 0:
            raise ValueError(f"num_spines must be positive, got {num_spines}")
        if probe_timeout_ns <= 0:
            raise ValueError(
                f"probe_timeout_ns must be positive, got {probe_timeout_ns}"
            )
        if max_outstanding_per_ev <= 0:
            raise ValueError(
                "max_outstanding_per_ev must be positive, got "
                f"{max_outstanding_per_ev}"
            )

        self._num_planes = num_planes
        self._num_spines = num_spines
        self._probe_timeout_ns = probe_timeout_ns
        self._max_outstanding = max_outstanding_per_ev

        # Per-EV next req_id (u16, wraps). 2-D: [plane][spine]. Per-EV
        # keeps the number-space dense and lets us reason about wrap
        # independently per EV; req_ids ARE NOT globally unique across
        # EVs.
        self._next_req_id: List[List[int]] = [
            [0] * num_spines for _ in range(num_planes)
        ]

        # Per-EV outstanding probes: [plane][spine] -> {req_id -> entry}.
        self._outstanding: List[List[Dict[int, _OutstandingProbe]]] = [
            [{} for _ in range(num_spines)] for _ in range(num_planes)
        ]

        # Counters: how many probes have we ever emitted vs replied to
        # vs timed out per EV. Diagnostic only; useful for tests
        # asserting the I/O layer is calling us correctly. Stored as
        # flat dicts keyed by (plane, spine) so callers iterating over
        # stats don't pay for a 4×N dense list when most EVs are quiet.
        self._emit_count: Dict[Tuple[int, int], int] = {}
        self._reply_count: Dict[Tuple[int, int], int] = {}
        self._timeout_count: Dict[Tuple[int, int], int] = {}
        # Replies that didn't match anything outstanding. Either too
        # late (already swept as timeout) or duplicate / wrong EV.
        self._stale_replies: int = 0

        self._lock = threading.Lock()

    @property
    def num_planes(self) -> int:
        return self._num_planes

    @property
    def num_spines(self) -> int:
        return self._num_spines

    def emit(self, plane: int, spine: int, now_ns: int) -> Tuple[int, int]:
        """Allocate a fresh req_id for the (plane, spine) EV.

        Returns (req_id, tx_ns) — caller passes both to encode_probe.
        tx_ns is just `now_ns` echoed back (we accept the clock as a
        parameter so tests don't depend on time.monotonic_ns).

        If the EV already has `max_outstanding_per_ev` outstanding
        probes, the OLDEST entry is dropped (LRU eviction). This
        shouldn't happen at sane cadences and is treated as a silent
        timeout — the evicted entry won't appear in any sweep.
        """
        self._check_ev(plane, spine)
        key = (plane, spine)
        with self._lock:
            req_id = self._next_req_id[plane][spine]
            self._next_req_id[plane][spine] = (req_id + 1) & 0xFFFF
            outstanding = self._outstanding[plane][spine]
            if len(outstanding) >= self._max_outstanding:
                # Drop oldest. dicts preserve insertion order.
                oldest_key = next(iter(outstanding))
                del outstanding[oldest_key]
            outstanding[req_id] = _OutstandingProbe(
                req_id=req_id, plane=plane, spine=spine, tx_ns=now_ns,
            )
            self._emit_count[key] = self._emit_count.get(key, 0) + 1
            return req_id, now_ns

    def match_reply(
        self,
        *,
        req_id: int,
        plane: int,
        spine: int,
        reply_tx_ns: int,
        now_ns: int,
    ) -> Optional[int]:
        """Match an incoming PROBE_REPLY against an outstanding probe.

        Returns the RTT in ns if matched (and removes the entry), or
        None if no match (stale / duplicate / wrong EV).

        We require the (plane, spine, req_id) triple to match — a
        reply that arrives attributing itself to a different EV than
        the probe was sent on is treated as stale. This catches the
        rare case where a reply traverses an unexpected EV due to a
        misconfigured route or a buggy receiver echoing the wrong
        spine_id, which would otherwise silently be counted against
        the wrong EV.

        We also cross-check `reply_tx_ns` against the recorded tx_ns:
        if they don't match exactly the reply is also stale (a
        different probe with the same req_id, e.g. after wrap).
        """
        self._check_ev(plane, spine)
        key = (plane, spine)
        with self._lock:
            outstanding = self._outstanding[plane][spine]
            entry = outstanding.get(req_id)
            if entry is None or entry.tx_ns != reply_tx_ns:
                self._stale_replies += 1
                return None
            del outstanding[req_id]
            self._reply_count[key] = self._reply_count.get(key, 0) + 1
            # RTT is wall-time-from-emit-to-now. We don't subtract
            # svc_time_ns here; that's a sender-side policy decision
            # left to the caller (e.g. for the OCP adj_svc_time bit).
            return now_ns - entry.tx_ns

    def sweep_timeouts(self, now_ns: int) -> List[Tuple[int, int, int]]:
        """Remove + return any outstanding probes older than the timeout.

        Returns a list of (plane, spine, req_id) for each timed-out
        probe. Caller is responsible for translating each into a
        EVStateTable.record_probe_result(success=False) call (which we
        don't do directly to avoid coupling this module to the table).
        """
        deadline_ns = now_ns - self._probe_timeout_ns
        timed_out: List[Tuple[int, int, int]] = []
        with self._lock:
            for plane in range(self._num_planes):
                for spine in range(self._num_spines):
                    outstanding = self._outstanding[plane][spine]
                    # Iterate over a copy of items because we mutate
                    # the dict. At sane cadences this is small (a
                    # handful of entries per EV).
                    for req_id, entry in list(outstanding.items()):
                        if entry.tx_ns <= deadline_ns:
                            del outstanding[req_id]
                            timed_out.append((plane, spine, req_id))
                            key = (plane, spine)
                            self._timeout_count[key] = (
                                self._timeout_count.get(key, 0) + 1
                            )
        return timed_out

    def stats(self) -> dict:
        """Snapshot of per-EV counters for tests / diagnostics.

        Counters are keyed by (plane, spine) tuple. EVs that have
        never had any activity are absent from the dicts (callers
        should treat missing keys as zero).
        """
        with self._lock:
            return {
                "emit": dict(self._emit_count),
                "reply": dict(self._reply_count),
                "timeout": dict(self._timeout_count),
                "stale_replies": self._stale_replies,
                "outstanding": {
                    (p, s): len(self._outstanding[p][s])
                    for p in range(self._num_planes)
                    for s in range(self._num_spines)
                    if self._outstanding[p][s]
                },
            }

    def outstanding(self, plane: int, spine: int) -> int:
        """Number of probes currently in-flight on the (plane, spine) EV."""
        self._check_ev(plane, spine)
        with self._lock:
            return len(self._outstanding[plane][spine])

    # --- internal -----------------------------------------------------

    def _check_ev(self, plane: int, spine: int) -> None:
        if not 0 <= plane < self._num_planes:
            raise ValueError(
                f"plane {plane} out of range [0, {self._num_planes})"
            )
        if not 0 <= spine < self._num_spines:
            raise ValueError(
                f"spine {spine} out of range [0, {self._num_spines})"
            )


__all__ = ["ProbeClock"]
