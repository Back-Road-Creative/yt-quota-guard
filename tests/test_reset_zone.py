"""Reset zone: Google's quota day ends at midnight Pacific, so that is the default.

The clock is injected through ``yt_quota_guard.manager._utcnow`` so the
midnight and DST fixtures are exact instants, not whatever time the suite
happens to run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

import yt_quota_guard.manager as manager_module
from yt_quota_guard import QuotaManager, ResetZoneMismatchError


def at(monkeypatch: pytest.MonkeyPatch, *args: int) -> None:
    instant = datetime(*args, tzinfo=UTC)
    monkeypatch.setattr(manager_module, "_utcnow", lambda: instant)


def write_ledger(
    tmp_path: Path, last_reset: datetime, used: int, reset_tz: str | None = None
) -> None:
    project_dir = tmp_path / "p"
    project_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "project_id": "p",
        "total_used": used,
        "daily_limit": 10_000,
        "last_reset": last_reset.isoformat(),
        "operations": [],
    }
    if reset_tz is not None:
        data["reset_tz"] = reset_tz
    (project_dir / "quota_usage.json").write_text(json.dumps(data), encoding="utf-8")


def make(tmp_path: Path, **kwargs) -> QuotaManager:
    return QuotaManager(project_id="p", storage_path=tmp_path, **kwargs)


def used(tmp_path: Path, **kwargs) -> int:
    return make(tmp_path, **kwargs).get_usage()["total_used"]


class TestDefaultZone:
    def test_defaults_to_pacific(self, tmp_path: Path) -> None:
        assert make(tmp_path).reset_tz == ZoneInfo("America/Los_Angeles")

    def test_accepts_an_iana_name(self, tmp_path: Path) -> None:
        assert make(tmp_path, reset_tz="America/New_York").reset_tz == ZoneInfo(
            "America/New_York"
        )

    def test_missing_tz_database_is_a_clear_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(key: str):
            raise ZoneInfoNotFoundError(key)

        monkeypatch.setattr(manager_module, "ZoneInfo", boom)
        with pytest.raises(RuntimeError, match="tzdata"):
            make(tmp_path)


class TestPacificBoundary:
    def test_ledger_rolls_at_pacific_not_utc_midnight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stored 01:00Z Sep 30 = 18:00 PDT Sep 29; now 12:00Z = 05:00 PDT
        # Sep 30. Both fall on Sep 30 in UTC, but on different Pacific days.
        write_ledger(tmp_path, datetime(2026, 9, 30, 1, 0, tzinfo=UTC), used=9000)
        at(monkeypatch, 2026, 9, 30, 12, 0)
        assert used(tmp_path) == 0

    def test_ledger_survives_until_pacific_midnight_summer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PDT is UTC-7: Pacific midnight is 07:00Z.
        write_ledger(tmp_path, datetime(2026, 9, 30, 1, 0, tzinfo=UTC), used=9000)
        at(monkeypatch, 2026, 9, 30, 6, 59)
        assert used(tmp_path) == 9000
        at(monkeypatch, 2026, 9, 30, 7, 0)
        assert used(tmp_path) == 0

    def test_ledger_survives_until_pacific_midnight_winter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PST is UTC-8: Pacific midnight is 08:00Z.
        write_ledger(tmp_path, datetime(2026, 12, 15, 0, 30, tzinfo=UTC), used=9000)
        at(monkeypatch, 2026, 12, 15, 7, 59)
        assert used(tmp_path) == 9000
        at(monkeypatch, 2026, 12, 15, 8, 0)
        assert used(tmp_path) == 0

    def test_spring_forward_day_uses_the_real_offset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DST began 2026-03-08. Mar 9 Pacific midnight is 07:00Z (PDT), not
        # 08:00Z: a fixed UTC-8 offset would roll an hour late.
        write_ledger(tmp_path, datetime(2026, 3, 8, 12, 0, tzinfo=UTC), used=9000)
        at(monkeypatch, 2026, 3, 9, 6, 59)
        assert used(tmp_path) == 9000
        at(monkeypatch, 2026, 3, 9, 7, 30)
        assert used(tmp_path) == 0

    def test_fall_back_day_uses_the_real_offset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # DST ended 2026-11-01. Nov 2 Pacific midnight is 08:00Z (PST).
        write_ledger(tmp_path, datetime(2026, 11, 1, 12, 0, tzinfo=UTC), used=9000)
        at(monkeypatch, 2026, 11, 2, 7, 59)
        assert used(tmp_path) == 9000
        at(monkeypatch, 2026, 11, 2, 8, 0)
        assert used(tmp_path) == 0


class TestLedgerOwnsTheZone:
    def test_zone_is_persisted_with_the_ledger(self, tmp_path: Path) -> None:
        make(tmp_path).track_operation("x", cost=1)
        data = json.loads((tmp_path / "p" / "quota_usage.json").read_text())
        assert data["reset_tz"] == "America/Los_Angeles"

    def test_a_different_zone_cannot_share_the_ledger(self, tmp_path: Path) -> None:
        make(tmp_path).track_operation("x", cost=5)
        with pytest.raises(ResetZoneMismatchError, match="America/Los_Angeles"):
            make(tmp_path, reset_tz="UTC")
        # The refused caller changed nothing.
        assert used(tmp_path) == 5

    def test_same_zone_under_any_spelling_is_accepted(self, tmp_path: Path) -> None:
        make(tmp_path, reset_tz="America/Los_Angeles").track_operation("x", cost=5)
        assert used(tmp_path, reset_tz=ZoneInfo("America/Los_Angeles")) == 5

    def test_concurrent_rollover_resets_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_ledger(tmp_path, datetime(2026, 9, 29, 12, 0, tzinfo=UTC), used=9000)
        at(monkeypatch, 2026, 9, 30, 12, 0)
        first, second = make(tmp_path), make(tmp_path)
        first.track_operation("x", cost=10)
        second.refresh()  # must not roll the day a second time
        assert second.get_usage()["total_used"] == 10


class TestMigrationFromTheUtcDefault:
    def test_legacy_ledger_keeps_its_spend_and_gains_a_zone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 0.1.0 wrote no zone. A ledger rolled at UTC midnight (17:30 PDT the
        # evening before) keeps its units, and is not re-granted an allowance
        # until the next Pacific midnight.
        write_ledger(tmp_path, datetime(2026, 9, 30, 0, 30, tzinfo=UTC), used=9000)
        at(monkeypatch, 2026, 9, 30, 1, 0)
        manager = make(tmp_path)
        assert manager.get_usage()["total_used"] == 9000
        manager.track_operation("x", cost=1)

        data = json.loads((tmp_path / "p" / "quota_usage.json").read_text())
        assert data["total_used"] == 9001
        assert data["reset_tz"] == "America/Los_Angeles"

    def test_legacy_utc_ledger_can_be_adopted_explicitly_as_utc(
        self, tmp_path: Path
    ) -> None:
        write_ledger(tmp_path, datetime.now(UTC), used=7)
        manager = make(tmp_path, reset_tz=UTC)
        manager.track_operation("x", cost=1)
        data = json.loads((tmp_path / "p" / "quota_usage.json").read_text())
        assert data["reset_tz"] == "UTC"
