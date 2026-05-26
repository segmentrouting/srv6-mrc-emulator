"""Unit tests for `MrcSnapshot` (snapshot-backed health-aware MRC policy).

Step (b) of the MRC daemon refactor: the policy is the read-only twin
of `HealthAwareMrc` used by data-sender processes once the daemon owns
the live `EVStateTable`. These tests cover the whole policy in
isolation -- they don't use a real daemon, just hand-written snapshot
files driven via `MrcSnapshot._refresh_once` and the start/stop
lifecycle. The "does the daemon actually feed it" coverage lives in
test_mrc_daemon.py and (forthcoming) the lab smoke.

Test areas:
    * cold-start uniform fallback when the file is missing
    * direct file load (synchronous via __init__)
    * weight grid reflects per-EV `weight` field accurately
    * demoted-EV behaviour: zero-weight EVs are never picked
    * mtime-driven refresh skips re-parses on unchanged files
    * background refresh thread picks up fresh snapshots
    * transient parse errors keep the previous grid (don't go uniform)
    * dimension and tenant validation reject bad snapshots
    * `parse_policy` integration for the `mrc_snapshot:<path>` CLI form
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from collections import Counter

from srv6_mrc import policy
from srv6_mrc.cli.spray import parse_policy
from srv6_mrc.mrc.ev_state import EVStateTable, EVStateConfig
from srv6_mrc.topo import FlowKey, NUM_PLANES, NUM_SPINES


F = FlowKey("2001:db8:bbbb:00::2", "2001:db8:bbbb:0f::2", 9999, 9999)


def _make_snapshot(
    path: str,
    table: EVStateTable | None = None,
    *,
    tenants: tuple[str, ...] = ("green",),
) -> EVStateTable:
    """Write a fresh EVStateTable.snapshot() JSON to `path`.

    If `table` is None, builds a default cold-start table for the
    given tenants. Returns the table so callers can mutate it and
    re-snapshot.
    """
    if table is None:
        table = EVStateTable(
            tenants=tenants, num_planes=NUM_PLANES,
            num_paths=NUM_SPINES,
        )
    snap = table.snapshot()
    # Atomic write: same convention as MrcDaemon's publisher (write tmp
    # + rename) so the file always contains a complete JSON document.
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f)
    os.rename(tmp, path)
    return table


class TestMrcSnapshotConstruction(unittest.TestCase):
    """Construction-time validation and cold-start behaviour."""

    def test_cold_start_uniform_when_file_missing(self):
        # Daemon hasn't written its first snapshot yet. Policy should
        # come up with uniform weights so the data sender's first picks
        # spread evenly across planes — same posture as HealthAwareMrc
        # on a brand-new EVStateTable (every EV UNKNOWN).
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "nope.json")
            self.assertFalse(os.path.exists(path))
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
            )
            try:
                counts = Counter(p.pick(i, F) for i in range(4096))
                for plane in range(NUM_PLANES):
                    self.assertGreater(counts[plane], 0)
                self.assertEqual(p.refresh_missing, 1)
                self.assertEqual(p.refresh_loaded, 0)
            finally:
                p.stop()

    def test_loads_existing_file_on_init(self):
        # Daemon already wrote a snapshot before the data sender came
        # up; the policy must consume it eagerly so the very first
        # pick is health-aware rather than 200 ms of uniform.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            _make_snapshot(path)
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
            )
            try:
                self.assertEqual(p.refresh_loaded, 1)
            finally:
                p.stop()

    def test_invalid_paths_per_plane_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            with self.assertRaises(ValueError):
                policy.MrcSnapshot(
                    snapshot_path=path, tenant="green",
                    paths_per_plane=0,
                )
            with self.assertRaises(ValueError):
                policy.MrcSnapshot(
                    snapshot_path=path, tenant="green",
                    paths_per_plane=NUM_SPINES + 1,
                )

    def test_invalid_refresh_interval_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            with self.assertRaises(ValueError):
                policy.MrcSnapshot(
                    snapshot_path=path, tenant="green",
                    refresh_interval_ms=0,
                )


class TestMrcSnapshotPicking(unittest.TestCase):
    """Pick behaviour faithfully reflects snapshot weights."""

    def test_demoted_plane_never_picked(self):
        # Drive plane 1 to all-ASSUMED_BAD via probe timeouts in a real
        # EVStateTable, snapshot it, point the policy at the file —
        # the policy must never pick plane 1.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            cfg = EVStateConfig(
                probe_window_ticks=2,
                probe_min_samples=3,
                min_active_evs=1,
            )
            table = EVStateTable(
                tenants=("green",), num_planes=NUM_PLANES,
                num_paths=NUM_SPINES, cfg=cfg,
            )
            for _ in range(2):
                for spath in range(NUM_SPINES):
                    for _ in range(3):
                        table.record_probe_sent("green", 1, spath)
                table.tick("green")
            _make_snapshot(path, table=table)
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
            )
            try:
                seen = {p.pick(i, F) for i in range(4096)}
                self.assertNotIn(1, seen)
            finally:
                p.stop()

    def test_uniform_when_all_unknown(self):
        # Cold-start snapshot from a virgin EVStateTable: every EV is
        # UNKNOWN with equal weight; pick distribution must cover all
        # planes.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            _make_snapshot(path)
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
            )
            try:
                counts = Counter(p.pick(i, F) for i in range(4096))
                for plane in range(NUM_PLANES):
                    self.assertGreater(counts[plane], 0)
            finally:
                p.stop()

    def test_deterministic_given_fixed_grid(self):
        # Same (seq, flow) on a frozen wgrid must give the same pick.
        # No refresh thread running, no concurrent mutation, so the
        # grid is genuinely fixed for the duration of the assertion.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            _make_snapshot(path)
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
            )
            try:
                first = [p.pick(i, F) for i in range(256)]
                second = [p.pick(i, F) for i in range(256)]
                self.assertEqual(first, second)
            finally:
                p.stop()


class TestMrcSnapshotRefresh(unittest.TestCase):
    """Refresh-thread / mtime-driven update semantics."""

    def test_refresh_picks_up_new_snapshot(self):
        # Start with a cold-start snapshot, then demote plane 0 in a
        # second snapshot and rewrite the file with a newer mtime; the
        # background refresh thread must pick up the change and the
        # next batch of picks must avoid plane 0.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            cfg = EVStateConfig(
                probe_window_ticks=2,
                probe_min_samples=3,
                min_active_evs=1,
            )
            table = EVStateTable(
                tenants=("green",), num_planes=NUM_PLANES,
                num_paths=NUM_SPINES, cfg=cfg,
            )
            _make_snapshot(path, table=table)
            # Short refresh interval to keep the test fast. Production
            # default is 200 ms; here we want the thread to tick a few
            # times within ~250 ms.
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
                refresh_interval_ms=20,
            )
            try:
                p.start()
                # Confirm initial state: cold-start uniform; plane 0
                # gets nonzero share.
                pre = Counter(p.pick(i, F) for i in range(2048))
                self.assertGreater(pre[0], 100)
                # Demote plane 0 fully and rewrite the snapshot file.
                # Bump mtime explicitly: on systems with second-level
                # mtime granularity, two consecutive writes can carry
                # the same mtime and the refresh would skip the second
                # file. os.utime with an explicit future timestamp
                # guarantees the refresh thread sees a change.
                for _ in range(2):
                    for spath in range(NUM_SPINES):
                        for _ in range(3):
                            table.record_probe_sent("green", 0, spath)
                    table.tick("green")
                _make_snapshot(path, table=table)
                future = time.time() + 1.0
                os.utime(path, (future, future))
                # Wait for the refresh thread to load the new file.
                deadline = time.time() + 1.0
                while p.refresh_loaded < 2 and time.time() < deadline:
                    time.sleep(0.01)
                self.assertGreaterEqual(p.refresh_loaded, 2)
                post = Counter(p.pick(i, F) for i in range(2048, 4096))
                self.assertEqual(post[0], 0)
            finally:
                p.stop()

    def test_refresh_skips_when_mtime_unchanged(self):
        # If the file's mtime hasn't advanced between two refresh
        # ticks, the policy must NOT reparse JSON. Verifies the mtime
        # short-circuit; otherwise refresh_loaded would tick every
        # interval forever.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            _make_snapshot(path)
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
                refresh_interval_ms=10,
            )
            try:
                p.start()
                time.sleep(0.1)  # ~10 ticks
                # Initial sync load + zero refresh-thread loads (file
                # hasn't been touched since __init__).
                self.assertEqual(p.refresh_loaded, 1)
                self.assertGreater(p.refresh_attempts, 1)
            finally:
                p.stop()

    def test_transient_error_keeps_previous_grid(self):
        # Once the policy has loaded a real snapshot, a subsequent
        # refresh that hits a parse error must NOT revert to uniform —
        # we want the data sender to keep using the last good
        # weights through transient /dev/shm hiccups rather than
        # blanket-trying all EVs (some of which the daemon already
        # marked bad).
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            cfg = EVStateConfig(
                probe_window_ticks=2,
                probe_min_samples=3,
                min_active_evs=1,
            )
            table = EVStateTable(
                tenants=("green",), num_planes=NUM_PLANES,
                num_paths=NUM_SPINES, cfg=cfg,
            )
            for _ in range(2):
                for spath in range(NUM_SPINES):
                    for _ in range(3):
                        table.record_probe_sent("green", 1, spath)
                table.tick("green")
            _make_snapshot(path, table=table)
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
            )
            try:
                # Sanity: plane 1 starts blocked.
                seen_pre = {p.pick(i, F) for i in range(2048)}
                self.assertNotIn(1, seen_pre)
                # Corrupt the file and bump mtime so the refresh
                # *attempts* to reload.
                with open(path, "w") as f:
                    f.write("{not valid json")
                future = time.time() + 1.0
                os.utime(path, (future, future))
                # Drive a refresh manually (deterministic, no thread
                # races).
                self.assertFalse(p._refresh_once())
                self.assertGreaterEqual(p.refresh_errors, 1)
                # Plane 1 must still be blocked because we kept the
                # previous grid.
                seen_post = {p.pick(i, F) for i in range(2048, 4096)}
                self.assertNotIn(1, seen_post)
            finally:
                p.stop()

    def test_refresh_thread_lifecycle_is_idempotent(self):
        # Defensive: start()/stop() called twice must be no-ops.
        # Useful because the data sender's teardown path may double-
        # call .stop() if a higher-level finally also runs.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            _make_snapshot(path)
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
                refresh_interval_ms=20,
            )
            try:
                p.start()
                p.start()  # idempotent
                self.assertIsNotNone(p._refresh_thread)
                p.stop()
                p.stop()  # idempotent
                self.assertIsNone(p._refresh_thread)
            finally:
                p.stop()


class TestMrcSnapshotValidation(unittest.TestCase):
    """Snapshot-content validation rejects malformed inputs."""

    def test_dimension_mismatch_rejected_on_load(self):
        # Snapshot dimensions must match the policy's topology. A
        # mismatch typically means the daemon and senders are running
        # against different topo.yaml files — a config error, not a
        # runtime one — so we'd rather see the refresh_errors counter
        # tick than silently distribute incorrectly.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            # Build a wrong-sized snapshot.
            wrong = {
                "config": {},
                "num_planes": NUM_PLANES + 1,
                "num_paths": NUM_SPINES,
                "tenants": {"green": []},
            }
            with open(path, "w") as f:
                json.dump(wrong, f)
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
            )
            try:
                # Construction's eager refresh should have errored.
                self.assertEqual(p.refresh_loaded, 0)
                self.assertGreaterEqual(p.refresh_errors, 1)
            finally:
                p.stop()

    def test_unknown_tenant_in_snapshot_rejected(self):
        # The snapshot might exist but be for a different tenant
        # (e.g. the daemon publishes per-tenant files but the data
        # sender pointed at the wrong one).
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            _make_snapshot(path, tenants=("yellow",))
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
            )
            try:
                self.assertEqual(p.refresh_loaded, 0)
                self.assertGreaterEqual(p.refresh_errors, 1)
            finally:
                p.stop()

    def test_daemon_wrapped_snapshot_accepted(self):
        # Regression rail: the MRC daemon (mrc/daemon.py:_publish_snapshot)
        # wraps EVStateTable.snapshot() under an "ev_state" key alongside
        # traceability metadata (src_host, dst_id, captured_ns, etc).
        # Pre-fix, MrcSnapshot._wgrid_from_snapshot read num_planes off
        # the top-level dict and raised KeyError, swallowed at the
        # caller as refresh_errors -- the policy then kept its uniform
        # cold-start grid forever and never honored demotions. This
        # test pins the wrapper-shape acceptance.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            table = EVStateTable(
                tenants=("green",), num_planes=NUM_PLANES,
                num_paths=NUM_SPINES,
                cfg=EVStateConfig(
                    probe_window_ticks=2,
                    probe_min_samples=3,
                    min_active_evs=1,
                ),
            )
            # Demote a specific EV so we can prove the policy reads it.
            for _ in range(2):
                for _ in range(3):
                    table.record_probe_sent("green", plane=0, path=0)
                table.tick("green")
            inner = table.snapshot()
            # Daemon's exact wrapper shape (see daemon.py:488-495).
            wrapped = {
                "src_host": "green-host00",
                "src_id": 0,
                "tenant": "green",
                "dst_id": 7,
                "captured_ns": 123_456_789,
                "ev_state": inner,
            }
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(wrapped, f)
            os.rename(tmp, path)
            p = policy.MrcSnapshot(
                snapshot_path=path,
                tenant="green",
                paths_per_plane=NUM_SPINES,
            )
            try:
                # Refresh should LOAD, not error. The pre-fix bug was
                # refresh_errors += 1 here.
                self.assertEqual(p.refresh_errors, 0,
                                 f"refresh_errors={p.refresh_errors} — "
                                 f"policy rejected daemon's wrapper shape")
                self.assertGreaterEqual(p.refresh_loaded, 1)
                # EV (0,0) demoted -> weight 0; sibling EVs non-zero.
                self.assertEqual(p._wgrid[0][0], 0.0,
                                 "demoted EV(0,0) should have weight 0 "
                                 "in the loaded grid")
                self.assertGreater(p._wgrid[0][1], 0.0,
                                   "sibling EV(0,1) should still carry "
                                   "weight")
            finally:
                p.stop()


class TestParsePolicyIntegration(unittest.TestCase):
    """parse_policy() builds an MrcSnapshot from the CLI form."""

    def test_basic_form(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            _make_snapshot(path)
            p = parse_policy(
                f"mrc_snapshot:{path}",
                tenant="green",
            )
            try:
                self.assertIsInstance(p, policy.MrcSnapshot)
                self.assertEqual(p.snapshot_path, path)
                self.assertEqual(p.tenant, "green")
                # parse_policy starts the refresh thread eagerly.
                self.assertIsNotNone(p._refresh_thread)
            finally:
                p.stop()

    def test_with_paths_per_plane(self):
        # mrc_snapshot:<path>:<N> form pins paths_per_plane to N.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            _make_snapshot(path)
            p = parse_policy(
                f"mrc_snapshot:{path}:2",
                tenant="green",
            )
            try:
                self.assertEqual(p.paths_per_plane, 2)
            finally:
                p.stop()


class TestCDFCache(unittest.TestCase):
    """CDF cache eliminates per-packet flat-list + tuple allocation."""

    def test_cdf_cached_per_wgrid_and_spines(self):
        """pick_ev builds CDF once per (wgrid, spines) pair, not per packet."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            table = _make_snapshot(path)
            p = policy.MrcSnapshot(path, "green", paths_per_plane=2)
            try:
                # First pick on this flow: cache miss, CDF built.
                plane0, spine0 = p.pick_ev(0, F)
                self.assertEqual(len(p._cdf_cache), 1,
                                 "first pick should populate cache with 1 entry")
                # Second pick, same flow: cache hit, no rebuild.
                plane1, spine1 = p.pick_ev(1, F)
                self.assertEqual(len(p._cdf_cache), 1,
                                 "cache size should still be 1 after second pick")
                # Different flow (different spines): new cache entry.
                F2 = FlowKey("2001:db8:bbbb:01::2", "2001:db8:bbbb:0e::2",
                             9999, 9999)
                p.pick_ev(0, F2)
                self.assertEqual(len(p._cdf_cache), 2,
                                 "different spines should add a second cache entry")
            finally:
                p.stop()

    def test_cache_cleared_on_wgrid_swap(self):
        """Cache is invalidated when _wgrid is swapped by refresh."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ev.json")
            table = _make_snapshot(path)
            p = policy.MrcSnapshot(path, "green")
            try:
                # Warm the cache with one pick.
                p.pick_ev(0, F)
                self.assertEqual(len(p._cdf_cache), 1, "cache should have 1 entry")
                # Modify snapshot, touch the file, trigger refresh.
                # Bump recv count on EV (0,0) so weights change.
                table.record_probe_recv("green", 0, 0)
                _make_snapshot(path, table)  # writes with new mtime
                time.sleep(0.01)  # ensure mtime advances
                loaded = p._refresh_once()
                self.assertTrue(loaded, "refresh should have loaded new snapshot")
                # Cache should be cleared.
                self.assertEqual(len(p._cdf_cache), 0,
                                 "cache should be empty after wgrid swap")
                # Next pick rebuilds for new weights.
                p.pick_ev(0, F)
                self.assertEqual(len(p._cdf_cache), 1,
                                 "cache repopulated after wgrid swap")
            finally:
                p.stop()


if __name__ == "__main__":
    unittest.main()
