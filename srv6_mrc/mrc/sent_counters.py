"""Cross-process per-EV sent counters: data sender -> MRC daemon.

In the daemon split (see docs/mrc-daemon-design.md) the data sender
runs the passive `mrc_snapshot` policy in its own process, while the
per-flow `SenderMrcAgent` that fuses LOSS_REPORTs lives in the
`MrcDaemon`. Loss fusion needs the sender's own per-EV sent count as
the denominator (see loss_compute.py); without it every report is
skipped and the loss signal is dead. This module carries those counts
across the process boundary.

  SentCounterWriter  (data sender)
      Keeps cumulative per-(plane, path) sent counts, fed from the
      runner's progress_cb, and atomically publishes them to
      `<snapshot_dir>/<src_host>/<tenant>_<dd>.sent` every
      `publish_interval_ms`.

  SentCounterReader  (daemon)
      Reads that file and returns the per-EV delta since its previous
      read. The agent's window-rotate thread folds the delta into the
      current sent window, so the SentWindowRing looks exactly as it
      does when the runner calls `SenderMrcAgent.record_sent` in
      process.

Counts are cumulative on the wire so a missed or torn read loses
nothing: the next successful read carries the whole difference. The
`.sent` suffix keeps the file out of scrapers that glob `*.json`
snapshots.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

SENT_COUNTERS_SUFFIX = ".sent"
DEFAULT_PUBLISH_INTERVAL_MS = 50

Grid = Tuple[Tuple[int, ...], ...]


def sent_counters_path(snapshot_path: str | Path) -> Path:
    """Sidecar path for a flow's snapshot: `<tenant>_<dd>.json` -> `.sent`."""
    return Path(snapshot_path).with_suffix(SENT_COUNTERS_SUFFIX)


class SentCounterWriter:
    """Data-sender side. `record()` is the runner hot path."""

    def __init__(
        self,
        path: str | Path,
        *,
        num_planes: int,
        num_paths: int,
        publish_interval_ms: int = DEFAULT_PUBLISH_INTERVAL_MS,
    ) -> None:
        if publish_interval_ms <= 0:
            raise ValueError(
                f"publish_interval_ms must be > 0, got {publish_interval_ms}"
            )
        self.path = Path(path)
        self.num_planes = num_planes
        self.num_paths = num_paths
        self.publish_interval_ms = publish_interval_ms
        # Single writer (the sender loop); the publisher thread only
        # reads. Per-cell ints are monotonic, so a read racing a write
        # is at worst one packet stale and caught up next publish.
        self._sent: List[List[int]] = [
            [0] * num_paths for _ in range(num_planes)
        ]
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.publish_errors = 0

    def record(self, plane: int, path: int) -> None:
        if 0 <= plane < self.num_planes and 0 <= path < self.num_paths:
            self._sent[plane][path] += 1

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="mrc-sent-publish", daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_s: float = 1.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
            self._thread = None
        self.publish()  # final counts

    def publish(self) -> None:
        payload = {
            "written_ns": time.monotonic_ns(),
            "sent": [list(row) for row in self._sent],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self.path)
        except OSError:
            self.publish_errors += 1

    def _loop(self) -> None:
        interval_s = self.publish_interval_ms / 1000.0
        self.publish()
        while not self._stop.wait(interval_s):
            self.publish()


class SentCounterReader:
    """Daemon side. `read_delta()` is called once per loss window."""

    def __init__(
        self, path: str | Path, *, num_planes: int, num_paths: int,
    ) -> None:
        self.path = Path(path)
        self.num_planes = num_planes
        self.num_paths = num_paths
        self._last: Optional[Grid] = None
        self.read_errors = 0

    def read_delta(self) -> Optional[Grid]:
        """Per-EV sent since the previous successful read, or None.

        A cell that went backwards means the data sender restarted with
        fresh counters; its new cumulative value is the delta.
        """
        current = self._read()
        if current is None:
            return None
        last = self._last
        self._last = current
        if last is None:
            return current
        return tuple(
            tuple(
                c - l if c >= l else c
                for c, l in zip(cur_row, last_row)
            )
            for cur_row, last_row in zip(current, last)
        )

    def _read(self) -> Optional[Grid]:
        try:
            with open(self.path) as f:
                payload = json.load(f)
        except FileNotFoundError:
            return None  # sender not started yet
        except (OSError, ValueError):
            self.read_errors += 1
            return None
        sent = payload.get("sent") if isinstance(payload, dict) else None
        if (
            not isinstance(sent, list)
            or len(sent) != self.num_planes
            or any(
                not isinstance(row, list) or len(row) != self.num_paths
                for row in sent
            )
        ):
            self.read_errors += 1
            return None
        return tuple(tuple(int(v) for v in row) for row in sent)


__all__ = [
    "SENT_COUNTERS_SUFFIX",
    "DEFAULT_PUBLISH_INTERVAL_MS",
    "sent_counters_path",
    "SentCounterWriter",
    "SentCounterReader",
]
