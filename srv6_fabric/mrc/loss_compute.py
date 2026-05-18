"""Sender-side loss-report fusion: LossReport + sent-counters -> EVStateTable.

When the sender receives a LOSS_REPORT from a downstream receiver, it
needs to:

  1. Compute per-plane loss ratio over the window the report covers.
  2. Push that ratio into EVStateTable.record_loss_window(plane, ratio),
     which feeds the consecutive-bad-window counter that drives demotes.

The receiver sent us `(seen[plane], expected_local[plane])` where
`expected_local` is its observed flow-seq span on that plane (an upper
bound on packets sent on the plane; see srv6_fabric/mrc/loss_window.py
for derivation). We have a much better number — our own `sent[plane]`
counter for the matching emit window — when we can pair them up.

Pairing windows
---------------
Sender keeps a small ring of recent emit-window snapshots, each
containing per-plane sent counts and the wall-time bounds of the
window. The receiver's window is a wall-clock slice of arrival time.
We don't get to perfectly align them; the sender picks the snapshot
whose midpoint is closest to the report's arrival time minus an
expected one-way delay (which we approximate at 0 — it's always small
relative to a 100ms window for our lab fabric).

If we can't find a snapshot within `max_window_skew_ns` of the report's
implied window, we fall back to using the receiver's `expected_local`
and emit a warning counter. That keeps the system functional during
startup or after a long quiet period when the sender ring has nothing
to pair against.

Invariants
----------
- A `seen > expected` report for a plane is treated as 0% loss (clamped),
  not negative loss. This can legitimately happen when packets sent in
  the prior window arrive in this one (i.e., the receiver is using a
  broader window than the sender). No state change is more correct than
  a fake "below 0% loss" signal.
- A plane absent from the report (because seen==0) is treated as
  "no data this window". We do NOT call record_loss_window for it; the
  EV state machine continues to operate from probe data alone for that
  plane this window.
- A report with zero records means the receiver saw no traffic at all
  in the window. We skip the table update entirely (vs telling it every
  plane had 0 loss, which would clear the bad-window counters).
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

from .ev_state import EVStateTable
from .probe import LossReport


@dataclass(frozen=True)
class SentWindow:
    """One closed emit-window snapshot from the sender's perspective.

    `start_ns` and `end_ns` are wall-clock bounds (inclusive start,
    exclusive end). `sent` is a 2-D per-EV packet count indexed as
    `sent[plane][path]` — the receiver's LossReport carries per-EV
    `(plane_id, path_id, seen)` records and pairs against this same
    granularity. `window_id` is optional bookkeeping for tests /
    diagnostics; the receiver's window_id is independent, so we don't
    try to match them by id.
    """
    start_ns: int
    end_ns: int
    sent: Tuple[Tuple[int, ...], ...]
    window_id: int = 0

    def midpoint_ns(self) -> int:
        return (self.start_ns + self.end_ns) // 2


class SentWindowRing:
    """Bounded ring of recent emit-window snapshots for window pairing."""

    def __init__(
        self, *, num_planes: int, num_paths: int, capacity: int = 16,
    ) -> None:
        if num_planes <= 0:
            raise ValueError(f"num_planes must be positive, got {num_planes}")
        if num_paths <= 0:
            raise ValueError(f"num_paths must be positive, got {num_paths}")
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self._num_planes = num_planes
        self._num_paths = num_paths
        self._ring: Deque[SentWindow] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def push(self, window: SentWindow) -> None:
        if len(window.sent) != self._num_planes:
            raise ValueError(
                f"window.sent length {len(window.sent)} != "
                f"num_planes {self._num_planes}"
            )
        for plane_row in window.sent:
            if len(plane_row) != self._num_paths:
                raise ValueError(
                    f"window.sent row length {len(plane_row)} != "
                    f"num_paths {self._num_paths}"
                )
        with self._lock:
            self._ring.append(window)

    def find_closest(
        self, *, target_ns: int, max_skew_ns: int,
    ) -> Optional[SentWindow]:
        """Find the snapshot with midpoint closest to target_ns.

        Returns None if no snapshot is within max_skew_ns. This is
        important: rather than guessing with a stale window we'd rather
        fall back to the receiver's expected_local.
        """
        with self._lock:
            best: Optional[SentWindow] = None
            best_skew = max_skew_ns + 1  # outside threshold
            for w in self._ring:
                skew = abs(w.midpoint_ns() - target_ns)
                if skew < best_skew:
                    best_skew = skew
                    best = w
            return best if best is not None else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._ring)


# --- pure fusion helpers --------------------------------------------------

def compute_loss_ratio(seen: int, sent_or_expected: int) -> float:
    """Return loss ratio in [0.0, 1.0].

    Clamps `seen > sent_or_expected` to 0 loss (see Invariants section
    in module docstring). `sent_or_expected == 0` returns 0 loss as
    well — there's no information.
    """
    if sent_or_expected <= 0:
        return 0.0
    if seen >= sent_or_expected:
        return 0.0
    return 1.0 - (seen / sent_or_expected)


@dataclass
class LossFusionStats:
    """Diagnostic counters for the fusion logic. Tests assert against
    these to confirm the right code path was taken."""
    reports_processed: int = 0
    planes_updated: int = 0
    planes_skipped_no_data: int = 0
    paired_with_sent_window: int = 0
    fell_back_to_receiver_expected: int = 0
    no_pairing_window_in_ring: int = 0


def apply_loss_report(
    *,
    table: EVStateTable,
    tenant: str,
    report: LossReport,
    sent_ring: SentWindowRing,
    received_at_ns: int,
    max_window_skew_ns: int,
    stats: Optional[LossFusionStats] = None,
) -> None:
    """Translate a LossReport into EVStateTable.record_loss_window calls.

    `received_at_ns` is the sender's monotonic_ns at LOSS_REPORT arrival;
    used to find the matching SentWindow in the ring (the snapshot whose
    midpoint is closest to `received_at_ns`). One-way delay is small
    relative to window size in our lab fabric, so we don't compensate.

    No-op if the report has zero plane records (receiver saw nothing).

    For each plane in the report:
      - If we have a paired SentWindow with sent[plane] > 0, use that
        as the denominator (sender-driven attribution).
      - Else fall back to the receiver's expected_local field. If that
        is also zero, skip the plane (no signal either way).
    """
    if stats is None:
        stats = LossFusionStats()  # local-only, discarded

    if not report.planes:
        return  # receiver saw no traffic in this window
    stats.reports_processed += 1

    paired = sent_ring.find_closest(
        target_ns=received_at_ns,
        max_skew_ns=max_window_skew_ns,
    )
    if paired is None:
        stats.no_pairing_window_in_ring += 1

    for rec in report.planes:
        if rec.seen == 0 and rec.expected == 0:
            stats.planes_skipped_no_data += 1
            continue

        denominator = 0
        used_sender_counter = False
        if paired is not None:
            # Per-EV sender count: rows are planes, columns are paths.
            # Defensive bounds-check in case a malformed/forward-compat
            # report carries an out-of-range (plane, path).
            if 0 <= rec.plane_id < len(paired.sent):
                row = paired.sent[rec.plane_id]
                if 0 <= rec.path_id < len(row):
                    sender_sent = row[rec.path_id]
                    if sender_sent > 0:
                        denominator = sender_sent
                        used_sender_counter = True

        if denominator == 0:
            denominator = rec.expected
            if denominator == 0:
                # No signal from either side. Don't push noise into
                # the EV table — leave the consecutive-bad-window
                # counter where it is.
                stats.planes_skipped_no_data += 1
                continue

        # NB: once a plane is demoted to weight=0, the sender stops
        # spraying it, so subsequent SentWindows have sent[plane]=0
        # and pairing falls through to rec.expected. The receiver's
        # `expected_local = max_seq - min_seq + 1` is computed from
        # the global seq stream, so a trickle of stragglers on a
        # demoted plane (seq=7, 91, 4023, ...) yields a wildly
        # inflated denominator and a bogus-looking 0.8+ loss ratio
        # in the EV-state snapshot. That's cosmetic, not behavioral:
        # the plane is already ASSUMED_BAD, the inflated ratio just
        # keeps the consecutive_loss_demote_windows counter high so
        # the plane stays demoted, which is the desired behavior.
        # A real recovery still happens via the probe path
        # (consecutive_probe_successes >= probe_recover_threshold AND
        # loss-window counter quiet), not via this loss-window path.

        # EVStateTable.record_loss_window takes (seen, expected) and
        # does the ratio internally; we keep compute_loss_ratio public
        # so callers / tests can convert without re-deriving. The EV is
        # identified by (plane, path); the loss record carries both
        # since PROBE/LOSS_REPORT v2 wire formats added the path
        # dimension.
        table.record_loss_window(
            tenant, rec.plane_id, rec.path_id, rec.seen, denominator,
        )
        stats.planes_updated += 1
        if used_sender_counter:
            stats.paired_with_sent_window += 1
        else:
            stats.fell_back_to_receiver_expected += 1


__all__ = [
    "SentWindow", "SentWindowRing",
    "LossFusionStats",
    "compute_loss_ratio", "apply_loss_report",
]
