"""Per-flow per-plane data-loss accounting (receiver side).

Tracks counts of received data packets per (FlowKey, plane) over a
rolling time window. On window close, the receiver-side I/O layer
calls `snapshot_and_reset()` to get a `LossReport` ready to wire-encode
and unicast back to the sender.

Sender-driven attribution
-------------------------
Per-plane loss can't be cleanly computed receiver-side without knowing
how many packets the sender intended for each plane in this window.
Instead the wire model is:

    receiver -> sender:  per-plane (seen, expected_local, max_gap)
    sender computes:     loss_ratio[p] = 1 - seen[p] / sent[p]

where `sent[p]` comes from the sender's own per-window emit counters.
The receiver still fills in `expected_local = max_seq_p - min_seq_p + 1`
(the flow-seq span observed on plane p) so the sender has a usable
fallback when its own counters aren't aligned with the receiver's
window — e.g. during the first window of a flow.

Because seq numbers are monotonically increasing across a flow but
sprayed across planes, max_seq_p - min_seq_p + 1 is a STRICT UPPER
BOUND on how many packets sender sent on plane p in the time window.
The actual number is between `seen[p]` and that upper bound. This is
not great in low-loss conditions (we'd never demote) but the sender
prefers its own `sent[p]` counter when present.

`max_gap` is the largest jump between consecutive seqs received on a
plane, useful as an out-of-order / burst-loss signal that's independent
of the loss-ratio computation.

Windowing
---------
Per the agreed design (see commit message for srv6_fabric/mrc/probe.py):

    - Receiver opens a window at recv_first_packet_in_window_at_ns.
    - On each data packet, the accountant increments seen[plane] and
      updates min/max/max_gap.
    - Window length is wall-clock; the I/O layer calls
      snapshot_and_reset() every loss_window_ms.
    - window_id increments monotonically per (sender, receiver) pair —
      we use a per-FlowKey u16 counter that wraps. The sender uses
      consecutive-bad-window detection (EVStateConfig.loss_demote_
      consecutive), so a wraparound is at worst a one-window glitch.

Thread-safety
-------------
LossWindowTable holds one Lock; both the data-RX path (record) and the
window-close path (snapshot_and_reset) take it. snapshot_and_reset is
O(planes) per flow, so even with hundreds of flows the wall-clock cost
is microseconds.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .probe import LossReport, PlaneLossRecord


@dataclass
class _PlaneCounters:
    """Per-plane counters for one in-flight window of one flow."""
    seen: int = 0
    min_seq: int = -1   # -1 sentinel = no packet seen yet
    max_seq: int = -1
    max_gap: int = 0
    last_seq: int = -1  # for max_gap computation


@dataclass
class _FlowWindow:
    """One flow's currently-accumulating window."""
    next_window_id: int = 0
    planes: List[_PlaneCounters] = field(default_factory=list)

    def reset(self, num_planes: int) -> None:
        # Keep next_window_id; reset everything else.
        self.planes = [_PlaneCounters() for _ in range(num_planes)]


class LossWindowTable:
    """Per-(FlowKey, plane) data-packet counters, sliced into windows.

    The receiver hooks `record(flow_key, plane, seq)` into its data RX
    loop. A separate thread (or timer) calls `snapshot_and_reset(flow_key)`
    at the end of each window to emit a LossReport.

    `flow_key` here is whatever the receiver uses to uniquely identify a
    sender flow — typically a (tenant, src_id, dst_id) tuple. We accept
    any hashable; nothing in this module interprets it.
    """

    def __init__(self, *, num_planes: int) -> None:
        if num_planes <= 0:
            raise ValueError(f"num_planes must be positive, got {num_planes}")
        self._num_planes = num_planes
        self._flows: Dict[object, _FlowWindow] = {}
        self._lock = threading.Lock()

    @property
    def num_planes(self) -> int:
        return self._num_planes

    def record(self, flow_key, plane: int, seq: int) -> None:
        """Account for one received data packet.

        `flow_key` may be anything hashable (the receiver chooses).
        `plane` is the plane the packet arrived on (from the data
        payload's plane field, NOT the local socket binding — for
        defense in depth, mismatches between the two are a separate
        concern handled in the I/O layer).

        `seq` is the flow-global monotonic sequence number from the
        data payload. We assume sender-side seq is non-decreasing per
        flow; out-of-order RX (which happens with multi-plane spray) is
        fine and counted as a normal arrival, but max_gap is computed
        on the wire-arrival order, not sorted-seq order, so it captures
        REAL reorder gaps the sender cares about.

        Idempotency: duplicate seqs (rare; would imply NIC retransmit
        or similar) are double-counted. We accept this rather than
        track a per-flow seen-seq set; it would dwarf the rest of the
        receiver's memory under normal flow rates.
        """
        if not 0 <= plane < self._num_planes:
            raise ValueError(
                f"plane {plane} out of range [0, {self._num_planes})"
            )
        if seq < 0 or seq > 0xFFFFFFFF:
            raise ValueError(f"seq must be uint32, got {seq}")
        with self._lock:
            flow = self._flows.get(flow_key)
            if flow is None:
                flow = _FlowWindow()
                flow.reset(self._num_planes)
                self._flows[flow_key] = flow
            counters = flow.planes[plane]
            counters.seen += 1
            if counters.min_seq < 0 or seq < counters.min_seq:
                counters.min_seq = seq
            if seq > counters.max_seq:
                counters.max_seq = seq
            if counters.last_seq >= 0:
                gap = seq - counters.last_seq
                # gap can be negative if reorder pulls an old seq in;
                # we only track positive forward jumps, since negative
                # gaps don't represent a "missed packet" signal.
                if gap > counters.max_gap:
                    counters.max_gap = gap
            counters.last_seq = seq

    def snapshot_and_reset(self, flow_key) -> LossReport:
        """Close the current window for `flow_key`; return + reset.

        Returns a LossReport with the per-plane records already in
        plane-id order. If the flow has never been seen, returns an
        empty report with window_id=0 (caller may choose to skip
        emission).

        After the call, the flow's counters are zeroed but its
        window_id counter persists, so the next snapshot increments it.
        """
        with self._lock:
            flow = self._flows.get(flow_key)
            if flow is None:
                # Touch the flow so subsequent record() calls see a
                # consistent next_window_id if any of them race.
                flow = _FlowWindow()
                flow.reset(self._num_planes)
                self._flows[flow_key] = flow
            window_id = flow.next_window_id
            flow.next_window_id = (window_id + 1) & 0xFFFF

            records: List[PlaneLossRecord] = []
            for plane, counters in enumerate(flow.planes):
                # Skip planes with zero activity to keep the wire
                # message small; the sender treats absence as "no
                # data this window for this plane" (which means we
                # can't tell loss vs not-spraying-this-plane, but the
                # state machine handles UNKNOWN naturally).
                if counters.seen == 0:
                    continue
                if counters.min_seq < 0:
                    expected_local = 0
                else:
                    expected_local = (
                        counters.max_seq - counters.min_seq + 1
                    )
                # Cap at u32; a runaway seq stream shouldn't crash us.
                expected_local = min(expected_local, 0xFFFFFFFF)
                seen_capped = min(counters.seen, 0xFFFFFFFF)
                max_gap_capped = min(counters.max_gap, 0xFFFFFFFF)
                records.append(PlaneLossRecord(
                    plane_id=plane,
                    path_id=0,  # TODO(phase1b/step2): per-EV counters
                    seen=seen_capped,
                    expected=expected_local,
                    max_gap=max_gap_capped,
                ))
            flow.reset(self._num_planes)
            return LossReport(window_id=window_id, planes=tuple(records))

    def known_flows(self) -> Tuple[object, ...]:
        """Snapshot of currently-tracked flow keys (for window-emit loops)."""
        with self._lock:
            return tuple(self._flows.keys())

    def forget(self, flow_key) -> None:
        """Drop a flow from the table.

        Called by the I/O layer when a flow goes idle (no data for N
        windows) to reclaim memory. Safe to call on unknown keys.
        """
        with self._lock:
            self._flows.pop(flow_key, None)


__all__ = ["LossWindowTable"]
