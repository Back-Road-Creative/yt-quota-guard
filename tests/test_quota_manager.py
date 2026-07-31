"""Accounting behaviour: costs, limits, rollover, reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from yt_quota_guard import (
    DEFAULT_COST_TABLE,
    DEFAULT_DAILY_LIMIT,
    ENV_STORAGE_PATH,
    OperationCost,
    QuotaExceededError,
    QuotaManager,
    UnknownOperationError,
    default_storage_path,
)


def make(tmp_path: Path, **kwargs) -> QuotaManager:
    kwargs.setdefault("project_id", "test-project")
    kwargs.setdefault("storage_path", tmp_path)
    return QuotaManager(**kwargs)


class TestConstruction:
    def test_records_project_id(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        assert manager.project_id == "test-project"
        assert manager.daily_limit == DEFAULT_DAILY_LIMIT

    def test_ledger_lives_under_a_per_project_directory(self, tmp_path: Path) -> None:
        make(tmp_path).track_operation("videos.list", cost=1)
        assert (tmp_path / "test-project" / "quota_usage.json").exists()

    def test_rejects_non_positive_daily_limit(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="daily_limit"):
            make(tmp_path, daily_limit=0)

    def test_rejects_negative_cost(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cost"):
            make(tmp_path).track_operation("videos.list", cost=-1)


class TestDefaultStoragePath:
    def test_env_override_wins(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(ENV_STORAGE_PATH, str(tmp_path / "explicit"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        assert default_storage_path() == tmp_path / "explicit"

    def test_falls_back_to_xdg_state_home(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv(ENV_STORAGE_PATH, raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        assert default_storage_path() == tmp_path / "xdg" / "yt-quota-guard"

    def test_falls_back_to_home_state_dir(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_STORAGE_PATH, raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert (
            default_storage_path()
            == Path.home() / ".local" / "state" / "yt-quota-guard"
        )

    def test_default_is_never_relative_to_cwd(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_STORAGE_PATH, raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        assert default_storage_path().is_absolute()

    def test_manager_uses_the_default_when_none_given(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv(ENV_STORAGE_PATH, str(tmp_path / "state"))
        manager = QuotaManager(project_id="p")
        assert manager.storage_path == tmp_path / "state" / "p"


class TestTracking:
    def test_single_operation_is_charged(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.track_operation("videos.insert", cost=1600)

        usage = manager.get_usage()
        assert usage["total_used"] == 1600
        assert usage["remaining"] == DEFAULT_DAILY_LIMIT - 1600

    def test_operations_accumulate(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.track_operation("videos.insert", cost=1600)
        manager.track_operation("thumbnails.set", cost=50)
        manager.track_operation("search.list", cost=100)

        assert manager.get_usage()["total_used"] == 1750

    def test_returns_the_recorded_operation(self, tmp_path: Path) -> None:
        record = make(tmp_path).track_operation(
            "videos.insert", cost=1600, metadata={"video_id": "abc123"}
        )
        assert record.operation == "videos.insert"
        assert record.cost == 1600
        assert record.metadata == {"video_id": "abc123"}

    def test_brand_id_is_stamped_on_each_record(self, tmp_path: Path) -> None:
        manager = make(tmp_path, brand_id="north-channel")
        manager.track_operation("videos.list", cost=1)
        assert manager.get_history()[0]["brand_id"] == "north-channel"

    def test_history_is_oldest_first_and_capped(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        for i in range(5):
            manager.track_operation(f"op-{i}", cost=1)

        assert [op["operation"] for op in manager.get_history()] == [
            "op-0",
            "op-1",
            "op-2",
            "op-3",
            "op-4",
        ]
        assert [op["operation"] for op in manager.get_history(limit=2)] == [
            "op-3",
            "op-4",
        ]

    def test_state_survives_a_new_manager(self, tmp_path: Path) -> None:
        make(tmp_path).track_operation("videos.insert", cost=1600)
        assert make(tmp_path).get_usage()["total_used"] == 1600


class TestLimits:
    def test_check_quota_draws_the_line_at_the_limit(self, tmp_path: Path) -> None:
        manager = make(tmp_path, daily_limit=10_000)
        manager.track_operation("bulk", cost=9500)

        assert manager.check_quota(500) is True  # exactly at the limit
        assert manager.check_quota(501) is False

    def test_enforce_raises_instead_of_recording(self, tmp_path: Path) -> None:
        manager = make(tmp_path, daily_limit=10_000)
        manager.track_operation("bulk", cost=9500)

        with pytest.raises(QuotaExceededError, match="exceed the daily quota"):
            manager.track_operation("videos.insert", cost=1600, enforce=True)

        assert manager.get_usage()["total_used"] == 9500

    def test_without_enforce_an_overspend_is_still_recorded(
        self, tmp_path: Path
    ) -> None:
        # The manager is an accountant by default, not a policeman: it must
        # record what actually happened even when that busts the limit.
        manager = make(tmp_path, daily_limit=1000)
        manager.track_operation("videos.insert", cost=1600)
        assert manager.get_usage()["total_used"] == 1600
        assert manager.get_usage()["remaining"] == -600

    def test_can_safely_perform_uses_the_cost_table(self, tmp_path: Path) -> None:
        manager = make(tmp_path, daily_limit=10_000)
        manager.track_operation("bulk", cost=9000)

        assert manager.can_safely_perform("videos.insert") is False  # 1600
        assert manager.can_safely_perform("search.list") is True  # 100

    def test_projects_do_not_share_a_ledger(self, tmp_path: Path) -> None:
        make(tmp_path, project_id="project-a").track_operation("upload", cost=1600)
        other = make(tmp_path, project_id="project-b")
        assert other.get_usage()["total_used"] == 0


class TestCostTable:
    def test_published_costs(self) -> None:
        assert DEFAULT_COST_TABLE["videos.insert"] == 1600
        assert DEFAULT_COST_TABLE["search.list"] == 100
        assert DEFAULT_COST_TABLE["thumbnails.set"] == 50
        assert DEFAULT_COST_TABLE["captions.insert"] == 400
        assert DEFAULT_COST_TABLE["videos.list"] == 1
        assert DEFAULT_COST_TABLE["channels.list"] == 1
        assert DEFAULT_COST_TABLE["playlists.list"] == 1

    @pytest.mark.parametrize(
        ("alias", "member"),
        [
            ("video_upload", OperationCost.VIDEO_UPLOAD),
            ("thumbnail_upload", OperationCost.THUMBNAIL_UPLOAD),
            ("video_search", OperationCost.VIDEO_SEARCH),
            ("video_list", OperationCost.VIDEO_LIST),
            ("channel_list", OperationCost.CHANNEL_LIST),
            ("playlist_insert", OperationCost.PLAYLIST_INSERT),
            ("playlist_list", OperationCost.PLAYLIST_LIST),
        ],
    )
    def test_every_enum_member_has_a_matching_alias(
        self, alias: str, member: OperationCost
    ) -> None:
        # The enum and the table drifting apart is exactly how an operation
        # ends up costing zero, so pin them together.
        assert DEFAULT_COST_TABLE[alias] == member.value

    def test_estimate_cost_accepts_method_names_and_aliases(
        self, tmp_path: Path
    ) -> None:
        manager = make(tmp_path)
        assert manager.estimate_cost("videos.insert") == 1600
        assert manager.estimate_cost("video_upload") == 1600
        assert manager.estimate_cost("thumbnail_upload") == 50
        assert manager.estimate_cost("video_search") == 100

    def test_unknown_operation_is_an_error_not_a_free_ride(
        self, tmp_path: Path
    ) -> None:
        manager = make(tmp_path)
        with pytest.raises(UnknownOperationError):
            manager.estimate_cost("vidoes.insert")  # typo
        with pytest.raises(UnknownOperationError):
            manager.can_safely_perform("vidoes.insert")

    def test_default_suppresses_the_error(self, tmp_path: Path) -> None:
        assert make(tmp_path).estimate_cost("captions.download", default=0) == 0

    def test_caller_supplied_table_overrides_the_defaults(self, tmp_path: Path) -> None:
        manager = make(tmp_path, cost_table={"videos.insert": 1})
        assert manager.estimate_cost("videos.insert") == 1
        with pytest.raises(UnknownOperationError):
            manager.estimate_cost("search.list")

    def test_table_is_copied_not_aliased(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.cost_table["videos.insert"] = 7
        assert DEFAULT_COST_TABLE["videos.insert"] == 1600


class TestWarnings:
    def test_quiet_below_the_warn_threshold(self, tmp_path: Path) -> None:
        manager = make(tmp_path, daily_limit=10_000)
        manager.track_operation("op", cost=7000)
        assert manager.get_warnings() == []

    def test_warns_at_eighty_percent(self, tmp_path: Path) -> None:
        manager = make(tmp_path, daily_limit=10_000)
        manager.track_operation("op", cost=8500)

        warnings = manager.get_warnings()
        assert len(warnings) == 1
        assert warnings[0].startswith("WARNING")
        assert "85.0%" in warnings[0]

    def test_escalates_to_critical_at_ninety_percent(self, tmp_path: Path) -> None:
        manager = make(tmp_path, daily_limit=10_000)
        manager.track_operation("op", cost=9500)

        warnings = manager.get_warnings()
        assert len(warnings) == 1
        assert warnings[0].startswith("CRITICAL")
        assert "500 units remaining" in warnings[0]

    def test_warning_text_is_plain_ascii(self, tmp_path: Path) -> None:
        # Warnings get printed into logs and CI consoles of unknown encoding.
        manager = make(tmp_path, daily_limit=10_000)
        manager.track_operation("op", cost=9500)
        manager.get_warnings()[0].encode("ascii")


class TestDailyRollover:
    def _write_ledger(self, tmp_path: Path, last_reset: datetime, used: int) -> None:
        project_dir = tmp_path / "test-project"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "quota_usage.json").write_text(
            json.dumps(
                {
                    "project_id": "test-project",
                    "total_used": used,
                    "daily_limit": 10_000,
                    "last_reset": last_reset.isoformat(),
                    "operations": [
                        {
                            "operation": "videos.insert",
                            "cost": used,
                            "timestamp": last_reset.isoformat(),
                            "brand_id": None,
                            "metadata": None,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_yesterdays_ledger_is_rolled_over(self, tmp_path: Path) -> None:
        yesterday = datetime.now(UTC) - timedelta(days=2)
        self._write_ledger(tmp_path, yesterday, used=9000)

        usage = make(tmp_path).get_usage()
        assert usage["total_used"] == 0
        assert usage["operations_count"] == 0

    def test_todays_ledger_is_kept(self, tmp_path: Path) -> None:
        self._write_ledger(tmp_path, datetime.now(UTC), used=9000)
        assert make(tmp_path).get_usage()["total_used"] == 9000

    def test_reset_tz_decides_where_the_day_boundary_falls(
        self, tmp_path: Path
    ) -> None:
        # One stored timestamp, two timezones, two answers. Build the pair of
        # zones from the current clock so the test holds at any hour: in
        # `tz_midnight` it is just past local midnight, so a timestamp from
        # ~half an hour ago fell on the previous local day; in `tz_midday`
        # the same instant is late morning, comfortably the same day.
        now = datetime.now(UTC)
        tz_midnight = timezone(timedelta(hours=-now.hour))
        tz_midday = timezone(timedelta(hours=-now.hour + 12))
        stored = now - timedelta(minutes=now.minute + 31)

        self._write_ledger(tmp_path, stored, used=9000)
        rolled = make(tmp_path, reset_tz=tz_midnight).get_usage()["total_used"]
        assert rolled == 0

        self._write_ledger(tmp_path, stored, used=9000)
        kept = make(tmp_path, reset_tz=tz_midday).get_usage()["total_used"]
        assert kept == 9000

    def test_naive_timestamp_is_read_as_utc(self, tmp_path: Path) -> None:
        naive_today = datetime.now(UTC).replace(tzinfo=None)
        project_dir = tmp_path / "test-project"
        project_dir.mkdir(parents=True)
        (project_dir / "quota_usage.json").write_text(
            json.dumps(
                {
                    "total_used": 42,
                    "last_reset": naive_today.isoformat(),
                    "operations": [],
                }
            ),
            encoding="utf-8",
        )
        assert make(tmp_path).get_usage()["total_used"] == 42

    def test_manual_reset(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.track_operation("videos.insert", cost=1600)
        manager.reset_quota()

        usage = manager.get_usage()
        assert usage["total_used"] == 0
        assert usage["remaining"] == DEFAULT_DAILY_LIMIT


class TestCorruptLedger:
    def test_truncated_json_does_not_wedge_the_manager(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "test-project"
        project_dir.mkdir(parents=True)
        (project_dir / "quota_usage.json").write_text(
            '{"total_used": 4', encoding="utf-8"
        )

        manager = make(tmp_path)
        assert manager.get_usage()["total_used"] == 0
        manager.track_operation("videos.list", cost=1)
        assert manager.get_usage()["total_used"] == 1

    def test_writes_are_atomic(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.track_operation("videos.list", cost=1)
        leftovers = list((tmp_path / "test-project").glob("*.tmp"))
        assert leftovers == []


class TestReports:
    def test_usage_shape(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.track_operation("videos.insert", cost=1600)

        usage = manager.get_usage()
        assert usage["project_id"] == "test-project"
        assert usage["total_limit"] == DEFAULT_DAILY_LIMIT
        assert usage["percentage_used"] == pytest.approx(16.0)
        assert usage["operations_count"] == 1

    def test_report_groups_by_operation(self, tmp_path: Path) -> None:
        manager = make(tmp_path)
        manager.track_operation("videos.insert", cost=1600)
        manager.track_operation("thumbnails.set", cost=50)
        manager.track_operation("search.list", cost=100)

        report = manager.generate_report()
        assert report["operations_count"] == 3
        assert report["percentage_used"] == pytest.approx(17.5)
        assert report["by_operation"]["videos.insert"] == {
            "count": 1,
            "total_cost": 1600,
        }

    def test_report_groups_by_brand(self, tmp_path: Path) -> None:
        north = make(tmp_path, brand_id="north")
        south = make(tmp_path, brand_id="south")
        north.track_operation("videos.insert", cost=1600)
        south.track_operation("search.list", cost=100)
        south.track_operation("search.list", cost=100)

        report = north.generate_report()
        assert report["by_brand"]["north"] == {"count": 1, "total_cost": 1600}
        assert report["by_brand"]["south"] == {"count": 2, "total_cost": 200}

    def test_report_carries_warnings(self, tmp_path: Path) -> None:
        manager = make(tmp_path, daily_limit=10_000)
        manager.track_operation("bulk", cost=9500)
        assert manager.generate_report()["warnings"][0].startswith("CRITICAL")


class TestGetProjectUsage:
    def test_reads_without_constructing_a_manager(self, tmp_path: Path) -> None:
        make(tmp_path).track_operation("videos.insert", cost=1600)
        assert (
            QuotaManager.get_project_usage("test-project", storage_path=tmp_path)
            == 1600
        )

    def test_unknown_project_is_zero(self, tmp_path: Path) -> None:
        assert QuotaManager.get_project_usage("nobody", storage_path=tmp_path) == 0
