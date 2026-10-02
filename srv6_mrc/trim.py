"""Opt-in packet trimming, NACK and fast retransmit (sender side).

Phase 1 of trimming support: the trim itself is synthetic. A scenario's
`trim:` block reaches each data sender as `SRV6_TRIM_JSON`; the sender's
`TrimOracle` then plays the congested switch, turning a fraction of the
packets on chosen EVs into their trimmed form (headers + the 10-byte MRC
header, no padding) or, in `drop` mode, not sending them at all. Phase 2
moves the trim into the fabric; the wire format stays the same.

The receiver detects a trim by length (see runner.run_receiver) and
NACKs it to the data sender's own port, `topo.nack_port(dst_id)`.
`NackListener` collects those NACKs and hands the sender bounded
retransmit requests, which it resends full size on a different EV.

Nothing here runs unless a trim spec is present: without one the sender,
receiver and MRC agents behave exactly as before.
"""

from __future__ import annotations

import json
import logging
import os
import random
import socket
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

from .mrc.probe import ProbeDecodeError, decode_nack
from .topo import nack_port

log = logging.getLogger(__name__)

TRIM_ENV = "SRV6_TRIM_JSON"
TRIM_MODES = ("trim", "drop")
DEFAULT_MAX_RETRANSMITS = 3
# Recent sends remembered for NACK validation; a NACK for a seq older
# than this is ignored (the packet would be long stale).
RECENT_SENDS = 4096


@dataclass(frozen=True)
class TrimSpec:
    """What the oracle trims. `planes` and `evs` both empty = every EV."""
    rate: float
    mode: str = "trim"
    planes: Tuple[int, ...] = ()
    evs: Tuple[Tuple[int, int], ...] = ()
    seed: int = 0
    max_retransmits: int = DEFAULT_MAX_RETRANSMITS

    def __post_init__(self) -> None:
        if not 0.0 <= self.rate <= 1.0:
            raise ValueError(f"trim rate must be in [0, 1], got {self.rate}")
        if self.mode not in TRIM_MODES:
            raise ValueError(f"trim mode must be one of {TRIM_MODES}, "
                             f"got {self.mode!r}")
        if self.max_retransmits < 0:
            raise ValueError("max_retransmits must be >= 0")

    def matches(self, plane: int, path: int) -> bool:
        if not self.planes and not self.evs:
            return True
        return plane in self.planes or (plane, path) in self.evs

    def to_env_json(self) -> str:
        return json.dumps({
            "rate": self.rate, "mode": self.mode,
            "planes": list(self.planes),
            "evs": [list(ev) for ev in self.evs],
            "seed": self.seed, "max_retransmits": self.max_retransmits,
        }, sort_keys=True)

    @classmethod
    def from_env(cls, value: Optional[str] = None) -> Optional["TrimSpec"]:
        """Parse `SRV6_TRIM_JSON`; None when unset (trimming off)."""
        if value is None:
            value = os.environ.get(TRIM_ENV)
        if not value:
            return None
        d = json.loads(value)
        return cls(
            rate=float(d["rate"]),
            mode=d.get("mode", "trim"),
            planes=tuple(int(p) for p in d.get("planes", ())),
            evs=tuple((int(p), int(q)) for p, q in d.get("evs", ())),
            seed=int(d.get("seed", 0)),
            max_retransmits=int(d.get("max_retransmits",
                                      DEFAULT_MAX_RETRANSMITS)),
        )


class TrimOracle:
    """Deterministic stand-in for a congested switch.

    One draw per packet on a matching EV, from a PRNG seeded by the
    spec, so a scenario replays the same trims every run.
    """

    def __init__(self, spec: TrimSpec) -> None:
        self.spec = spec
        self._rng = random.Random(spec.seed)

    def hits(self, plane: int, path: int) -> bool:
        if self.spec.rate <= 0.0 or not self.spec.matches(plane, path):
            return False
        return self._rng.random() < self.spec.rate


@dataclass
class TrimStats:
    """Sender-side counters, reported only when trimming is on."""
    trimmed: int = 0          # oracle sent the trimmed form
    dropped: int = 0          # oracle dropped (mode: drop)
    nacks_received: int = 0
    nacks_ignored: int = 0    # unknown/stale seq or retransmit limit hit
    retransmits: int = 0

    def to_dict(self) -> dict:
        return {
            "trimmed": self.trimmed, "dropped": self.dropped,
            "nacks_received": self.nacks_received,
            "nacks_ignored": self.nacks_ignored,
            "retransmits": self.retransmits,
        }


class NackListener:
    """Receives NACKs on the flow's port and queues bounded retransmits.

    The sender loop calls `note_sent()` for every packet and drains
    `take()` before each new send. A seq is retransmitted at most
    `max_retransmits` times.
    """

    def __init__(self, *, dst_id: int, max_retransmits: int,
                 stats: TrimStats, sock: Optional[socket.socket] = None
                 ) -> None:
        self.max_retransmits = max_retransmits
        self.stats = stats
        self._recent: "OrderedDict[int, int]" = OrderedDict()  # seq -> retx
        # (seq, plane, path of the trimmed arrival)
        self._queue: Deque[Tuple[int, int, int]] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        if sock is None:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("::", nack_port(dst_id)))
        sock.settimeout(0.1)
        self._sock = sock
        self._thread = threading.Thread(
            target=self._rx_loop, name="trim-nack-rx", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        try:
            self._sock.close()
        except OSError:
            pass

    def note_sent(self, seq: int) -> None:
        with self._lock:
            if seq not in self._recent:
                self._recent[seq] = 0
                if len(self._recent) > RECENT_SENDS:
                    self._recent.popitem(last=False)

    def on_nack(self, seq: int, plane: int, path: int) -> None:
        with self._lock:
            self.stats.nacks_received += 1
            count = self._recent.get(seq)
            if count is None or count >= self.max_retransmits:
                self.stats.nacks_ignored += 1
                return
            self._recent[seq] = count + 1
            self._queue.append((seq, plane, path))

    def take(self) -> list:
        with self._lock:
            out = list(self._queue)
            self._queue.clear()
        return out

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                payload, _peer = self._sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                nack = decode_nack(payload)
            except ProbeDecodeError as e:
                log.debug("trim: bad nack: %s", e)
                continue
            self.on_nack(nack.seq, nack.plane_id, nack.path_id)


__all__ = [
    "TRIM_ENV", "TRIM_MODES", "TrimSpec", "TrimOracle", "TrimStats",
    "NackListener",
]
