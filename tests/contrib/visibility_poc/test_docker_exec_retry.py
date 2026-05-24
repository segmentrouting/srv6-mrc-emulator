"""Tests for docker_exec_with_retry — the AGENTS.md SO_REUSEPORT cure."""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from . import _path_shim  # noqa: F401
import scraper  # type: ignore[import-not-found]


def _proc(rc: int, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["docker", "exec", "h", "true"],
        returncode=rc, stdout=stdout, stderr=stderr,
    )


class DockerExecRetryTests(unittest.TestCase):
    def test_success_on_first_try(self) -> None:
        with mock.patch("scraper.subprocess.run",
                        return_value=_proc(0, b"ok")) as run, \
             mock.patch("scraper.time.sleep") as sleep:
            proc = scraper.docker_exec_with_retry("h", ["true"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def test_retries_on_rc1_then_succeeds(self) -> None:
        results = [_proc(1, stderr=b"transient"),
                   _proc(1, stderr=b"transient"),
                   _proc(0, b"ok")]
        with mock.patch("scraper.subprocess.run", side_effect=results) as run, \
             mock.patch("scraper.time.sleep") as sleep:
            proc = scraper.docker_exec_with_retry(
                "h", ["cat", "/x"], attempts=3, backoff_s=0.01,
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_no_retry_on_non_rc1_failure(self) -> None:
        with mock.patch("scraper.subprocess.run",
                        return_value=_proc(2, stderr=b"boom")) as run, \
             mock.patch("scraper.time.sleep") as sleep:
            with self.assertRaises(scraper.DockerExecError) as cm:
                scraper.docker_exec_with_retry("h", ["x"], attempts=3)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("rc=2", str(cm.exception))
        self.assertIn("boom", str(cm.exception))

    def test_exhausts_attempts_on_rc1(self) -> None:
        with mock.patch("scraper.subprocess.run",
                        return_value=_proc(1, stderr=b"still missing")) as run, \
             mock.patch("scraper.time.sleep") as sleep:
            with self.assertRaises(scraper.DockerExecError) as cm:
                scraper.docker_exec_with_retry(
                    "h", ["cat", "/x"], attempts=3, backoff_s=0.01,
                )
        self.assertEqual(run.call_count, 3)
        # sleep between attempts: N-1 backoffs.
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("3 attempts", str(cm.exception))
        self.assertIn("still missing", str(cm.exception))

    def test_timeout_raises_immediately(self) -> None:
        with mock.patch(
            "scraper.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=5.0),
        ) as run, mock.patch("scraper.time.sleep") as sleep:
            with self.assertRaises(scraper.DockerExecError) as cm:
                scraper.docker_exec_with_retry("h", ["cat", "/x"])
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("timed out", str(cm.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
