"""Tests for srv6_mrc.mrc.sent_counters and its data-sender wiring.

The daemon side (counts folded into the agent's SentWindowRing and
used for loss fusion) is covered in
tests/test_mrc_daemon.py::MrcDaemonCrossProcessLossFusionTests.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from srv6_mrc.mrc.sent_counters import (
    SentCounterReader,
    SentCounterWriter,
    sent_counters_path,
)
from srv6_mrc.topo import NUM_PLANES, NUM_SPINES


class SentCountersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mrc-sent-test-")
        self.path = Path(self.tmpdir) / "host" / "green_05.sent"
        self.writer = SentCounterWriter(self.path, num_planes=2, num_paths=2)
        self.reader = SentCounterReader(self.path, num_planes=2, num_paths=2)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_sidecar_path_sits_next_to_snapshot_but_is_not_json(self) -> None:
        self.assertEqual(
            sent_counters_path("/dev/shm/srv6-mrc/h/green_05.json"),
            Path("/dev/shm/srv6-mrc/h/green_05.sent"),
        )

    def test_missing_file_reads_none(self) -> None:
        self.assertIsNone(self.reader.read_delta())
        self.assertEqual(self.reader.read_errors, 0)

    def test_read_delta_returns_increment_since_last_read(self) -> None:
        for _ in range(3):
            self.writer.record(1, 0)
        self.writer.publish()
        self.assertEqual(self.reader.read_delta(), ((0, 0), (3, 0)))
        self.writer.record(1, 0)
        self.writer.record(0, 1)
        self.writer.publish()
        self.assertEqual(self.reader.read_delta(), ((0, 1), (1, 0)))
        # Nothing new published: zero delta, not a repeat.
        self.assertEqual(self.reader.read_delta(), ((0, 0), (0, 0)))

    def test_sender_restart_counts_from_new_cumulative(self) -> None:
        for _ in range(5):
            self.writer.record(0, 0)
        self.writer.publish()
        self.reader.read_delta()
        fresh = SentCounterWriter(self.path, num_planes=2, num_paths=2)
        fresh.record(0, 0)
        fresh.publish()
        self.assertEqual(self.reader.read_delta(), ((1, 0), (0, 0)))

    def test_out_of_range_ev_ignored(self) -> None:
        self.writer.record(9, 0)
        self.writer.record(0, 9)
        self.writer.publish()
        self.assertEqual(self.reader.read_delta(), ((0, 0), (0, 0)))

    def test_shape_mismatch_is_read_error(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"sent": [[1, 2, 3]]}))
        self.assertIsNone(self.reader.read_delta())
        self.assertEqual(self.reader.read_errors, 1)

    def test_stop_flushes_final_counts(self) -> None:
        self.writer.start()
        self.writer.record(1, 1)
        self.writer.stop()
        self.assertEqual(self.reader.read_delta(), ((0, 0), (0, 1)))


class CmdSendPublishesSentCountsTests(unittest.TestCase):
    """A data sender on `mrc_snapshot:<path>` must publish per-EV sent
    counts to the daemon's sidecar path."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp(prefix="mrc-sent-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_mrc_snapshot_sender_writes_sidecar(self) -> None:
        from srv6_mrc.cli import spray

        snap = Path(self.tmpdir) / "green-host00" / "green_05.json"
        args = argparse.Namespace(
            dst_id=5, policy=f"mrc_snapshot:{snap}", rate=100,
            duration=1.0, json=True, paths_per_plane=None, sid=None,
        )

        def fake_run_sender(flow, policy, rate, duration, *,
                            progress_cb=None, **_kwargs):
            for seq, ev in enumerate([(0, 0), (2, 1), (2, 1)]):
                progress_cb(seq, *ev)
            return mock.Mock(to_dict=lambda: {"sent": 3})

        with mock.patch.object(spray, "run_sender", fake_run_sender), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = spray.cmd_send(args, "green", 0)
        self.assertEqual(rc, 0)

        reader = SentCounterReader(
            sent_counters_path(snap),
            num_planes=NUM_PLANES, num_paths=NUM_SPINES,
        )
        delta = reader.read_delta()
        self.assertIsNotNone(delta, "data sender wrote no sidecar")
        self.assertEqual(delta[0][0], 1)
        self.assertEqual(delta[2][1], 2)
        self.assertEqual(sum(map(sum, delta)), 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
