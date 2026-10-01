"""Atomic admission: ``reserve`` closes the check-then-act gap.

``check_quota`` followed by ``track_operation`` is two lock holds, so two
callers can both pass the check for the same remaining units. ``reserve``
checks and holds the units in one lock hold.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from yt_quota_guard import (
    QuotaExceededError,
    QuotaManager,
    Reservation,
    UnknownReservationError,
)

CHILD = """
import sys
from pathlib import Path
from yt_quota_guard import QuotaExceededError, QuotaManager

manager = QuotaManager(
    project_id="race", storage_path=Path(sys.argv[1]), daily_limit=100
)
try:
    manager.reserve("videos.insert", cost=80)
except QuotaExceededError:
    print("denied")
else:
    print("granted")
"""


def make(tmp_path: Path, **kwargs) -> QuotaManager:
    kwargs.setdefault("project_id", "p")
    kwargs.setdefault("storage_path", tmp_path)
    kwargs.setdefault("daily_limit", 100)
    return QuotaManager(**kwargs)


class TestCheckThenActGap:
    def test_check_then_track_is_still_racy_by_design(self, tmp_path: Path) -> None:
        # Documents why reserve exists: both callers pass the informational
        # check, so a check/track pair cannot arbitrate between them.
        a, b = make(tmp_path), make(tmp_path)
        assert a.check_quota(80) is True
        assert b.check_quota(80) is True

    def test_second_80_unit_contender_cannot_reserve(self, tmp_path: Path) -> None:
        a, b = make(tmp_path), make(tmp_path)
        assert a.check_quota(80) and b.check_quota(80)

        a.reserve("videos.insert", cost=80)
        with pytest.raises(QuotaExceededError, match="exceed the daily quota"):
            b.reserve("videos.insert", cost=80)

    def test_threaded_contenders_admit_exactly_one(self, tmp_path: Path) -> None:
        managers = [make(tmp_path) for _ in range(8)]
        barrier = threading.Barrier(len(managers))
        granted: list[Reservation] = []
        denied: list[QuotaExceededError] = []

        def contend(manager: QuotaManager) -> None:
            barrier.wait()
            try:
                granted.append(manager.reserve("videos.insert", cost=80))
            except QuotaExceededError as exc:
                denied.append(exc)

        threads = [threading.Thread(target=contend, args=(m,)) for m in managers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(granted) == 1
        assert len(denied) == len(managers) - 1

    def test_process_contenders_admit_exactly_one(self, tmp_path: Path) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        children = [
            subprocess.Popen(
                [sys.executable, "-c", CHILD, str(tmp_path)],
                env=env,
                stdout=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        outcomes = []
        for child in children:
            out, _ = child.communicate(timeout=120)
            assert child.returncode == 0
            outcomes.append(out.strip())

        assert sorted(outcomes) == ["denied", "denied", "denied", "granted"]

    def test_reserve_uses_the_cost_table_when_cost_is_omitted(
        self, tmp_path: Path
    ) -> None:
        manager = make(tmp_path, daily_limit=10_000)
        assert manager.reserve("videos.insert").cost == 1600


class TestHeldUnitsAreVisible:
    def test_check_quota_counts_reserved_units(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.reserve("x", cost=80)
        assert make(tmp_path).check_quota(21) is False
        assert make(tmp_path).check_quota(20) is True

    def test_enforced_track_operation_counts_reserved_units(
        self, tmp_path: Path
    ) -> None:
        make(tmp_path).reserve("x", cost=80)
        with pytest.raises(QuotaExceededError):
            make(tmp_path).track_operation("y", cost=30, enforce=True)

    def test_usage_reports_reserved_and_remaining(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.track_operation("a", cost=10)
        manager.reserve("b", cost=30)
        usage = manager.get_usage()
        assert usage["total_used"] == 10
        assert usage["reserved"] == 30
        assert usage["remaining"] == 60


class TestCommitAndRelease:
    def test_commit_converts_the_hold_into_spend(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        reservation = manager.reserve("videos.insert", cost=80, metadata={"v": 1})
        record = manager.commit(reservation)

        usage = manager.get_usage()
        assert (usage["total_used"], usage["reserved"]) == (80, 0)
        assert record.operation == "videos.insert"
        assert record.metadata is not None
        assert record.metadata["v"] == 1
        assert record.metadata["reservation_id"] == reservation.id

    def test_commit_merges_late_metadata(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        reservation = manager.reserve("x", cost=5, metadata={"a": 1})
        record = manager.commit(reservation, metadata={"video_id": "abc"})
        assert record.metadata == {
            "a": 1,
            "video_id": "abc",
            "reservation_id": reservation.id,
        }

    def test_commit_can_charge_the_actual_cost(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        reservation = manager.reserve("x", cost=80)
        manager.commit(reservation, cost=50)
        assert manager.get_usage()["total_used"] == 50

    def test_repeated_commit_charges_once(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        reservation = manager.reserve("x", cost=80)
        first = manager.commit(reservation)
        again = make(tmp_path).commit(reservation)

        assert again == first
        usage = manager.get_usage()
        assert usage["total_used"] == 80
        assert usage["operations_count"] == 1

    def test_release_frees_the_hold(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        reservation = manager.reserve("x", cost=80)
        manager.release(reservation)

        assert manager.get_usage()["reserved"] == 0
        manager.reserve("y", cost=80)  # fits again

    def test_repeated_release_is_a_no_op(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        reservation = manager.reserve("x", cost=40)
        other = manager.reserve("y", cost=40)
        manager.release(reservation)
        manager.release(reservation)
        assert manager.get_usage()["reserved"] == 40
        manager.commit(other)

    def test_release_after_commit_never_refunds(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        reservation = manager.reserve("x", cost=80)
        manager.commit(reservation)
        manager.release(reservation)
        assert manager.get_usage()["total_used"] == 80

    def test_commit_of_an_unknown_reservation_raises(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        ghost = Reservation(
            id="nope", operation="x", cost=1, timestamp="2000-01-01T00:00:00+00:00"
        )
        with pytest.raises(UnknownReservationError):
            manager.commit(ghost)

    def test_negative_cost_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cost"):
            make(tmp_path).reserve("x", cost=-1)


class TestCrashAccounting:
    def test_unsettled_reservation_survives_and_is_not_refunded(
        self, tmp_path: Path
    ) -> None:
        # A process that dies between reserve and commit may or may not have
        # sent its request. The units stay held rather than being refunded.
        make(tmp_path).reserve("videos.insert", cost=80)
        survivor = make(tmp_path)
        assert survivor.get_usage()["reserved"] == 80
        assert survivor.check_quota(21) is False

    def test_reservations_clear_on_daily_rollover(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.reserve("x", cost=80)
        manager.reset_quota()
        usage = manager.get_usage()
        assert (usage["total_used"], usage["reserved"]) == (0, 0)

    def test_ledger_without_a_reservations_key_loads(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.track_operation("a", cost=5)
        assert make(tmp_path).get_usage()["reserved"] == 0
