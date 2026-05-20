"""Sender-side loss-report fusion: LossReport + sent-counters -> EVStateTable.

When the sender receives a LOSS_REPORT from a downstream receiver, it
needs to:

  1. Compute per-plane loss ratio over the window the report covers.
  2. Push that ratio into EVStateTable.record_loss_window(plane, ratio),
     which feeds the consecutive-bad-window counter that drives demotes.

The receiver sent us `(seen[plane], expected_local[plane])` where
`expected_local` is its observed flow-seq span on that plane (an upper
bound on packets sent on the plane; see srv6_mrc/mrc/loss_window.py
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
implied window, we SKIP the plane entirely and increment a warning
counter. We do NOT fall back to the receiver's `expected_local`: with
packet-level EV spray, that field is `max_seq - min_seq + 1` over a
flow-global seq stream sprayed across many EVs, which is a strict and
typically wildly inflated upper bound (e.g. seen=10 on seqs spanning
0..400 yields a 97% phantom loss ratio on a healthy fabric). Two such
windows in a row would cross `loss_demote_consecutive` and demote a
healthy EV. The startup / skew-burst windows where pairing fails just
have no loss signal — probes still detect EV failures, and as soon as
pairing succeeds again we resume normal demotion behaviour.

Historical note: an earlier version of this module DID fall back to
`rec.expected` here. It produced phantom EV demotions on the 4p-4x8
baselines (multiple flows showed 5+/16 EVs demoted with `loss: 0` and
`consecutive_probe_timeouts: 0`). The fallback was removed; the
`fell_back_to_receiver_expected` counter is kept for diagnostics so
existing dashboards / log scrapers don't break, but it now means
"plane was skipped because we couldn't pair" rather than "plane was
attributed using receiver's seq-span estimate".

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
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

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
    these to confirm the right code path was taken.

    Counter semantics:
      - reports_processed: LossReports with at least one plane record.
      - planes_updated: EV state machine was fed a (seen, expected).
      - planes_skipped_no_data: report record had seen==0 AND expected==0.
      - paired_with_sent_window: an EV was attributed using the sender's
        own sent[plane][path] counter. Always equals planes_updated
        in the current implementation; kept distinct for forward-compat
        if we ever re-introduce alternate denominators.
      - fell_back_to_receiver_expected: an EV was SKIPPED because we
        had no usable sender-side denominator (no paired SentWindow,
        or paired but sent[plane][path]==0). Despite the historical
        name, this counter no longer means "we used rec.expected" —
        see module docstring for the rationale.
      - no_pairing_window_in_ring: per-report counter (not per-plane);
        increments once per LossReport that couldn't pair against any
        SentWindow within max_window_skew_ns.
    """
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
      - If we have a paired SentWindow with sent[plane][path] > 0, use
        that as the denominator (sender-driven attribution).
      - Else skip the plane: leave the EV's bad-window counter where
        it is. We deliberately do NOT use `rec.expected` as a fallback
        denominator — see module docstring for why.
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

        if denominator == 0:
            # No sender-side counter for this EV. Either:
            #   (a) we couldn't pair against any SentWindow (paired is
            #       None), or
            #   (b) we paired but the snapshot's sent[plane][path] was
            #       0 (sender didn't emit on this EV in the matched
            #       window — receiver's record is from straggler /
            #       skew packets that landed in this window).
            # In both cases we have no trustworthy denominator. The
            # receiver's `rec.expected` is `max_seq - min_seq + 1`
            # computed over the flow-global seq stream sprayed across
            # all EVs, which is a wildly inflated upper bound and
            # produces phantom loss demotions on healthy EVs. Skip
            # the plane; leave the bad-window counter where it is.
            stats.fell_back_to_receiver_expected += 1
            continue

        # NB: once a plane is demoted to weight=0, the sender stops
        # spraying it, so subsequent SentWindows have sent[plane]=0
        # and we hit the `denominator == 0` skip above. The plane
        # stays demoted via the consecutive-counter ratchet from when
        # it was first demoted; recovery happens via the probe path
        # (consecutive_probe_successes >= probe_recover_threshold),
        # not via this loss-window path.

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
        stats.paired_with_sent_window += 1


__all__ = [
    "SentWindow", "SentWindowRing",
    "LossFusionStats",
    "compute_loss_ratio", "apply_loss_report",
]
