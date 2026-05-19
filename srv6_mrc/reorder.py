"""Per-flow reorder distance histograms.

Metric definition (matches the OpenAI MRC paper closely enough for
comparative analysis):

  For each flow, maintain `max_seq_seen`. On arrival of a packet with
  sequence number `s`:
      delta = max_seq_seen - s        if s <= max_seq_seen   # late
      delta = 0                        if s >  max_seq_seen   # in-order; update max

  The histogram bins `delta`. Bin 0 dominates in a healthy lab; tail mass
  at high `delta` indicates reordering depth.

Notes:
  - This conflates "late" with "duplicate"; duplicates are reported in a
    separate counter.
  - Loss is computed at flow-finalization time from (first_seq, last_seq,
    received_count), not in the histogram.
  - The histogram is a dict (sparse) — a 1000pps × 30s burst with mild
    reorder has well under 100 distinct bins.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .topo import FlowKey


@dataclass
class FlowStats:
    """Per-flow rolling state. One instance per (src, dst, sport, dport)."""

    flow: FlowKey
    first_seq: int | None = None
    last_seq_seen: int = -1     # max seq ever observed
    received: int = 0
    duplicates: int = 0
    # Per-plane counts; populated from the payload's plane byte.
    per_plane: dict[int, int] = field(default_factory=dict)
    # delta -> count
    reorder_hist: dict[int, int] = field(default_factory=dict)
    # Seen seqs in a bounded window, for duplicate detection. The window
    # size caps memory; reorder beyond it is still counted, duplicates
    # outside it are missed (acceptable for our rates).
    _window: set[int] = field(default_factory=set, repr=False)
    _window_cap: int = 4096

    def observe(self, seq: int, plane: int | None = None) -> None:
        if self.first_seq is None:
            self.first_seq = seq

        if seq in self._window:
            self.duplicates += 1
            return
        self._window.add(seq)
        if len(self._window) > self._window_cap:
            # Drop the smallest. Cheap: O(window_cap) once per overflow,
            # amortized O(1).
            self._window.discard(min(self._window))

        self.received += 1
        if plane is not None:
            self.per_plane[plane] = self.per_plane.get(plane, 0) + 1

        if seq > self.last_seq_seen:
            self.last_seq_seen = seq
            self.reorder_hist[0] = self.reorder_hist.get(0, 0) + 1
        else:
            delta = self.last_seq_seen - seq
            self.reorder_hist[delta] = self.reorder_hist.get(delta, 0) + 1

    # --- derived metrics ----------------------------------------------------

    @property
    def expected(self) -> int:
        """seq range covered: last - first + 1, or 0 if nothing received."""
        if self.first_seq is None:
            return 0
        return self.last_seq_seen - self.first_seq + 1

    @property
    def loss(self) -> int:
        """Packets in the seq range we never saw."""
        return max(0, self.expected - self.received)

    @property
    def reorder_max(self) -> int:
        return max(self.reorder_hist) if self.reorder_hist else 0

    @property
    def reorder_mean(self) -> float:
        if not self.reorder_hist:
            return 0.0
        total = sum(self.reorder_hist.values())
        weighted = sum(d * c for d, c in self.reorder_hist.items())
        return weighted / total

    def reorder_percentile(self, pct: float) -> int:
        """Return the smallest delta D such that >= pct% of packets had
        reorder <= D. pct in (0, 100]."""
        if not self.reorder_hist:
            return 0
        if not 0 < pct <= 100:
            raise ValueError("pct must be in (0, 100]")
        total = sum(self.reorder_hist.values())
        target = total * pct / 100.0
        cum = 0
        for d in sorted(self.reorder_hist):
            cum += self.reorder_hist[d]
            if cum >= target:
                return d
        return max(self.reorder_hist)

    # --- serialization ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "src": self.flow.src_addr,
            "dst": self.flow.dst_addr,
            "sport": self.flow.src_port,
            "dport": self.flow.dst_port,
            "received": self.received,
            "duplicates": self.duplicates,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq_seen if self.first_seq is not None else None,
            "expected": self.expected,
            "loss": self.loss,
            "per_plane_recv": dict(sorted(self.per_plane.items())),
            "reorder_hist": dict(sorted(self.reorder_hist.items())),
            "reorder_max": self.reorder_max,
            "reorder_mean": round(self.reorder_mean, 3),
            "reorder_p99": self.reorder_percentile(99),
        }


class ReorderTracker:
    """Per-flow demultiplexer. One global tracker per receiver process."""

    def __init__(self) -> None:
        self._flows: dict[FlowKey, FlowStats] = {}

    def observe(self, flow: FlowKey, seq: int, plane: int | None = None) -> None:
        st = self._flows.get(flow)
        if st is None:
            st = FlowStats(flow=flow)
            self._flows[flow] = st
        st.observe(seq, plane)

    def flows(self) -> list[FlowStats]:
        return list(self._flows.values())

    def to_dict(self) -> dict:
        return {"flows": [f.to_dict() for f in self._flows.values()]}
