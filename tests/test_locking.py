"""Shared-ledger behaviour: the reason this library uses a file lock.

The whole point of a global accountant is that independent callers agree on
one running total. These tests exercise the three ways that can break: two
manager objects in one process, two threads, and two OS processes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from yt_quota_guard import QuotaExceededError, QuotaManager
from yt_quota_guard.manager import LOCK_FILE_MODE, group_writable_filelock

CHILD = """
import sys
from pathlib import Path
from yt_quota_guard import QuotaManager

storage, project, count, cost = sys.argv[1:5]
manager = QuotaManager(project_id=project, storage_path=Path(storage))
for _ in range(int(count)):
    manager.track_operation("videos.list", cost=int(cost))
"""


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class TestLockFile:
    def test_lock_file_is_group_writable(self, tmp_path: Path) -> None:
        # A lock file left at 0o644 by whichever account ran first is
        # unacquirable by the next account in the same group.
        lock_path = tmp_path / "a.lock"
        with group_writable_filelock(lock_path):
            assert lock_path.exists()
            assert _mode(lock_path) == LOCK_FILE_MODE, oct(_mode(lock_path))

    def test_mode_survives_filelock_recreating_the_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "b.lock"
        with group_writable_filelock(lock_path):
            assert _mode(lock_path) == LOCK_FILE_MODE
        with group_writable_filelock(lock_path):
            assert _mode(lock_path) == LOCK_FILE_MODE

    def test_manager_creates_its_lock_group_writable(self, tmp_path: Path) -> None:
        manager = QuotaManager(project_id="p", storage_path=tmp_path)
        manager.track_operation("videos.list", cost=1)
        assert _mode(manager.lock_file) == LOCK_FILE_MODE


class TestTwoManagersInOneProcess:
    def test_second_manager_sees_the_first_managers_spend(self, tmp_path: Path) -> None:
        first = QuotaManager(project_id="shared", storage_path=tmp_path)
        second = QuotaManager(project_id="shared", storage_path=tmp_path)

        first.track_operation("videos.insert", cost=9000)

        assert second.get_usage()["total_used"] == 9000
        assert second.check_quota(1600) is False

    def test_neither_manager_overwrites_the_other(self, tmp_path: Path) -> None:
        # Both managers loaded a zeroed ledger before either wrote. A
        # read-modify-write that skipped the reload would leave 100, not 1700.
        brand_a = QuotaManager(project_id="shared", brand_id="a", storage_path=tmp_path)
        brand_b = QuotaManager(project_id="shared", brand_id="b", storage_path=tmp_path)

        brand_a.track_operation("videos.insert", cost=1600)
        assert QuotaManager.get_project_usage("shared", storage_path=tmp_path) == 1600

        brand_b.track_operation("search.list", cost=100)
        assert QuotaManager.get_project_usage("shared", storage_path=tmp_path) == 1700

    def test_enforcement_uses_the_other_managers_spend(self, tmp_path: Path) -> None:
        first = QuotaManager(
            project_id="shared", storage_path=tmp_path, daily_limit=10_000
        )
        second = QuotaManager(
            project_id="shared", storage_path=tmp_path, daily_limit=10_000
        )

        first.track_operation("bulk", cost=9500)

        with pytest.raises(QuotaExceededError, match="exceed the daily quota"):
            second.track_operation("videos.insert", cost=1600, enforce=True)


class TestThreads:
    def test_concurrent_threads_lose_no_operations(self, tmp_path: Path) -> None:
        manager = QuotaManager(project_id="threaded", storage_path=tmp_path)

        def worker() -> None:
            for _ in range(5):
                manager.track_operation("videos.list", cost=1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert manager.get_usage()["total_used"] == 40
        assert manager.get_usage()["operations_count"] == 40

    def test_separate_managers_per_thread_also_agree(self, tmp_path: Path) -> None:
        def worker() -> None:
            manager = QuotaManager(project_id="threaded2", storage_path=tmp_path)
            for _ in range(5):
                manager.track_operation("videos.list", cost=1)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert QuotaManager.get_project_usage("threaded2", storage_path=tmp_path) == 20


class TestProcesses:
    def test_concurrent_processes_lose_no_operations(self, tmp_path: Path) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(sys.path)

        children = [
            subprocess.Popen(
                [sys.executable, "-c", CHILD, str(tmp_path), "fleet", "10", "1"],
                env=env,
            )
            for _ in range(4)
        ]
        for child in children:
            assert child.wait(timeout=120) == 0

        assert QuotaManager.get_project_usage("fleet", storage_path=tmp_path) == 40
