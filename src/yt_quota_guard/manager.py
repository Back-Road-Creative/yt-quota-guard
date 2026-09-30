"""Global YouTube Data API quota accountant.

The YouTube Data API meters your *project*, not your credential. Every
channel, brand, and script that authenticates against the same Google Cloud
project draws from one shared daily allowance. A fleet of independent
scripts therefore has no way to know how close it is to the cap unless
something outside them keeps the books.

:class:`QuotaManager` is that book. It records every operation with its unit
cost in one JSON file per project, guards read-modify-write with a file lock
so concurrent processes on the same host cannot lose each other's updates,
and rolls the ledger over on a configurable daily boundary.

``check_quota`` followed by ``track_operation`` is two separate lock holds, so
two callers can both pass the check for the same remaining units.
:meth:`QuotaManager.reserve` is the atomic alternative: it checks the limit and
holds the units in one lock hold, and :meth:`QuotaManager.commit` or
:meth:`QuotaManager.release` settles the hold afterwards.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, tzinfo
from enum import IntEnum
from pathlib import Path
from typing import Any

from filelock import FileLock

__all__ = [
    "DEFAULT_COST_TABLE",
    "DEFAULT_DAILY_LIMIT",
    "ENV_STORAGE_PATH",
    "LOCK_FILE_MODE",
    "OperationCost",
    "QuotaExceededError",
    "QuotaManager",
    "QuotaOperation",
    "Reservation",
    "UnknownOperationError",
    "UnknownReservationError",
    "default_storage_path",
]

#: Units a project may spend per day on the shared pool, before any
#: extension Google has granted you. Override with ``daily_limit=``.
DEFAULT_DAILY_LIMIT = 10_000

#: Percentage of ``daily_limit`` at which :meth:`QuotaManager.get_warnings`
#: starts complaining, and the point at which it escalates to CRITICAL.
WARN_THRESHOLD_PCT = 80.0
CRITICAL_THRESHOLD_PCT = 90.0

#: Mode applied to the lock file. Group-writable (rather than filelock's
#: default 0o644) so two UNIX accounts in the same group can share one
#: ledger on a build host; whichever runs first would otherwise leave a
#: lock file the other cannot acquire.
LOCK_FILE_MODE = 0o664

#: Environment variable that overrides the default state directory.
ENV_STORAGE_PATH = "YT_QUOTA_GUARD_HOME"

_STATE_DIR_NAME = "yt-quota-guard"
_LEDGER_FILENAME = "quota_usage.json"
_LOCK_FILENAME = ".quota.lock"


class OperationCost(IntEnum):
    """Unit costs for the operations most callers meter.

    Values are Google's published costs; see ``DEFAULT_COST_TABLE`` for the
    full table and the README for the caveats. These are ``int`` subclasses,
    so ``OperationCost.VIDEO_UPLOAD == 1600`` is true.
    """

    VIDEO_UPLOAD = 1600
    THUMBNAIL_UPLOAD = 50
    VIDEO_SEARCH = 100
    VIDEO_LIST = 1
    CHANNEL_LIST = 1
    PLAYLIST_INSERT = 50
    PLAYLIST_LIST = 1


#: Google's published per-operation unit costs, keyed by API method name.
#:
#: These numbers are Google's, not this library's, and Google changes them.
#: Treat this table as a convenience default and check the current published
#: table before relying on it:
#: https://developers.google.com/youtube/v3/determine_quota_cost
#:
#: The trailing block is a set of friendlier aliases for the same costs.
DEFAULT_COST_TABLE: dict[str, int] = {
    # captions
    "captions.list": 50,
    "captions.insert": 400,
    "captions.update": 450,
    "captions.delete": 50,
    # channels
    "channels.list": 1,
    "channels.update": 50,
    # playlistItems
    "playlistItems.list": 1,
    "playlistItems.insert": 50,
    "playlistItems.update": 50,
    "playlistItems.delete": 50,
    # playlists
    "playlists.list": 1,
    "playlists.insert": 50,
    "playlists.update": 50,
    "playlists.delete": 50,
    # search
    "search.list": 100,
    # thumbnails
    "thumbnails.set": 50,
    # videos
    "videos.list": 1,
    "videos.insert": 1600,
    "videos.update": 50,
    "videos.rate": 50,
    "videos.getRating": 1,
    "videos.reportAbuse": 50,
    "videos.delete": 50,
    # aliases
    "video_upload": OperationCost.VIDEO_UPLOAD.value,
    "thumbnail_upload": OperationCost.THUMBNAIL_UPLOAD.value,
    "video_search": OperationCost.VIDEO_SEARCH.value,
    "video_list": OperationCost.VIDEO_LIST.value,
    "channel_list": OperationCost.CHANNEL_LIST.value,
    "playlist_insert": OperationCost.PLAYLIST_INSERT.value,
    "playlist_list": OperationCost.PLAYLIST_LIST.value,
}


class QuotaExceededError(Exception):
    """Raised when an enforced operation would push usage past the limit."""


class UnknownOperationError(KeyError):
    """Raised when an operation name has no entry in the cost table.

    Costing an unrecognised name as zero is how a quota guard silently stops
    guarding, so an unknown name is an error rather than a free operation.
    Pass ``default=`` to :meth:`QuotaManager.estimate_cost` if you want a
    fallback, or supply your own ``cost_table=``.
    """


class UnknownReservationError(KeyError):
    """Raised when committing a reservation the ledger has no record of.

    The hold is gone and no operation carries its id: it was released, or the
    ledger rolled over to a new day since it was made. Nothing is charged; the
    caller decides whether to record the spend with ``track_operation``.
    """


def default_storage_path() -> Path:
    """Return the directory the ledger lives in when none is supplied.

    Resolution order:

    1. ``$YT_QUOTA_GUARD_HOME`` if set,
    2. ``$XDG_STATE_HOME/yt-quota-guard`` if ``XDG_STATE_HOME`` is set,
    3. ``~/.local/state/yt-quota-guard``.

    Nothing is written relative to the current working directory, so the
    ledger does not depend on where a script happened to be launched from.
    """
    override = os.environ.get(ENV_STORAGE_PATH)
    if override:
        return Path(override).expanduser()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state).expanduser() / _STATE_DIR_NAME
    return Path.home() / ".local" / "state" / _STATE_DIR_NAME


def group_writable_filelock(lock_path: Path | str) -> FileLock:
    """Build a :class:`filelock.FileLock` with :data:`LOCK_FILE_MODE`.

    ``filelock`` removes and recreates the lock file on every acquisition, so
    the mode has to be supplied at construction rather than chmod'ed once.
    """
    return FileLock(str(lock_path), mode=LOCK_FILE_MODE)


@dataclass
class QuotaOperation:
    """One recorded quota-consuming call."""

    operation: str
    cost: int
    timestamp: str
    brand_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class Reservation:
    """Units held against the daily limit for a call not yet settled.

    Returned by :meth:`QuotaManager.reserve`. Settle it exactly once, with
    :meth:`QuotaManager.commit` if the request was (or may have been) sent, or
    :meth:`QuotaManager.release` if it certainly was not.
    """

    id: str
    operation: str
    cost: int
    timestamp: str
    brand_id: str | None = None
    metadata: dict[str, Any] | None = None


class QuotaManager:
    """Ledger for one Google Cloud project's daily API quota.

    Every read re-reads the ledger from disk under the lock, and every write
    re-reads, mutates and saves inside a single lock hold. Two processes
    sharing a storage path therefore see the same running total; neither can
    overwrite the other's operations with a stale copy.

    Units held by :meth:`reserve` count against the limit alongside recorded
    spend. A hold is never expired or refunded automatically: a process that
    died after sending its request may already have consumed the quota, so the
    hold stays until it is committed, released, or the day rolls over.

    Args:
        project_id: Google Cloud project the quota belongs to. This is the
            unit Google meters, so it is also the unit of the ledger file.
        brand_id: Free-form label recorded against each operation, so a
            report can attribute spend to a channel, brand or job. Purely
            descriptive: it never partitions the quota.
        storage_path: Directory holding per-project ledgers. Defaults to
            :func:`default_storage_path`.
        daily_limit: Units available per day. Defaults to
            :data:`DEFAULT_DAILY_LIMIT`.
        cost_table: Operation-name to unit-cost mapping used by
            :meth:`estimate_cost`. Defaults to :data:`DEFAULT_COST_TABLE`.
        reset_tz: Timezone whose midnight rolls the ledger over. Defaults to
            UTC. Google resets at midnight Pacific; pass
            ``ZoneInfo("America/Los_Angeles")`` to match it.
    """

    def __init__(
        self,
        project_id: str,
        brand_id: str | None = None,
        storage_path: Path | str | None = None,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        cost_table: dict[str, int] | None = None,
        reset_tz: tzinfo = UTC,
    ) -> None:
        if daily_limit <= 0:
            raise ValueError(f"daily_limit must be positive, got {daily_limit}")

        self.project_id = project_id
        self.brand_id = brand_id
        self.daily_limit = daily_limit
        self.cost_table = (
            dict(cost_table) if cost_table is not None else dict(DEFAULT_COST_TABLE)
        )
        self.reset_tz = reset_tz

        root = (
            Path(storage_path) if storage_path is not None else default_storage_path()
        )
        self.storage_path = root / project_id
        self.storage_path.mkdir(parents=True, exist_ok=True)

        self.quota_file = self.storage_path / _LEDGER_FILENAME
        self.lock_file = self.storage_path / _LOCK_FILENAME

        # One FileLock instance per manager: filelock counts re-entrant
        # acquisitions per instance and per thread, so nesting a locked
        # helper inside a locked public method is safe, while a second
        # thread or process still blocks.
        self._lock = group_writable_filelock(self.lock_file)

        self.operations: list[QuotaOperation] = []
        self.reservations: list[Reservation] = []
        self.total_used = 0
        self.last_reset = datetime.now(UTC)

        self.refresh()

    # ---------------------------------------------------------------- state

    def refresh(self) -> None:
        """Re-read the ledger from disk, rolling the day over if due."""
        with self._lock:
            self._load_locked()

    def _load_locked(self) -> None:
        """Load state. Caller must hold ``self._lock``."""
        if not self.quota_file.exists():
            self._reset_locked()
            return

        try:
            with open(self.quota_file, encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError):
            # A truncated ledger (killed mid-write, full disk) must not wedge
            # every future run. Start a fresh day rather than crash; the cost
            # is an undercount for the remainder of one day.
            self._reset_locked()
            return

        last_reset = datetime.fromisoformat(
            data.get("last_reset", "2000-01-01T00:00:00+00:00")
        )
        if last_reset.tzinfo is None:
            last_reset = last_reset.replace(tzinfo=UTC)

        now = datetime.now(UTC)
        if (
            now.astimezone(self.reset_tz).date()
            > last_reset.astimezone(self.reset_tz).date()
        ):
            self._reset_locked()
            return

        self.operations = [QuotaOperation(**op) for op in data.get("operations", [])]
        self.reservations = [Reservation(**res) for res in data.get("reservations", [])]
        self.total_used = data.get("total_used", 0)
        self.last_reset = last_reset

    def _reset_locked(self) -> None:
        """Zero the ledger and persist. Caller must hold ``self._lock``."""
        self.operations = []
        self.reservations = []
        self.total_used = 0
        self.last_reset = datetime.now(UTC)
        self._save_locked()

    def _save_locked(self) -> None:
        """Persist state. Caller must hold ``self._lock``."""
        data = {
            "project_id": self.project_id,
            "total_used": self.total_used,
            "daily_limit": self.daily_limit,
            "last_reset": self.last_reset.isoformat(),
            "operations": [asdict(op) for op in self.operations],
            "reservations": [asdict(res) for res in self.reservations],
        }
        tmp = self.quota_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        tmp.replace(self.quota_file)

    def reset_quota(self) -> None:
        """Zero the ledger now, without waiting for the daily rollover."""
        with self._lock:
            self._reset_locked()

    # ----------------------------------------------------------- accounting

    def track_operation(
        self,
        operation: str,
        cost: int,
        metadata: dict[str, Any] | None = None,
        enforce: bool = False,
    ) -> QuotaOperation:
        """Record an operation and charge its cost against the day's quota.

        The whole read-check-append-write cycle happens under one lock hold,
        so a concurrent process cannot slip an operation in between the check
        and the write.

        Args:
            operation: Name of the call, e.g. ``"videos.insert"``. Free-form:
                the cost is whatever you pass, not a table lookup.
            cost: Units consumed.
            metadata: Optional payload stored alongside the record.
            enforce: Raise instead of recording if the charge would exceed
                the daily limit. Defaults to False (account, do not police).

        Returns:
            The recorded :class:`QuotaOperation`.

        Raises:
            QuotaExceededError: If ``enforce`` and the charge would exceed
                ``daily_limit``.
        """
        if cost < 0:
            raise ValueError(f"cost must not be negative, got {cost}")

        with self._lock:
            self._load_locked()

            if enforce:
                self._admit_locked(operation, cost)

            record = self._record_locked(operation, cost, metadata)

        return record

    def _reserved_units(self) -> int:
        return sum(res.cost for res in self.reservations)

    def _admit_locked(self, operation: str, cost: int) -> None:
        """Raise if ``cost`` more units would pass the limit. Caller holds lock.

        Held reservations count, so an enforced charge cannot spend units
        another caller has already claimed.
        """
        held = self._reserved_units()
        committed = self.total_used + held
        if committed + cost > self.daily_limit:
            pct = (committed / self.daily_limit) * 100
            raise QuotaExceededError(
                f"{operation!r} would exceed the daily quota: "
                f"{committed + cost} > {self.daily_limit} units. "
                f"Current usage: {committed}/{self.daily_limit} "
                f"({pct:.1f}%, {held} held by reservations)"
            )

    def _record_locked(
        self, operation: str, cost: int, metadata: dict[str, Any] | None
    ) -> QuotaOperation:
        """Append a charge and persist. Caller must hold ``self._lock``."""
        record = QuotaOperation(
            operation=operation,
            cost=cost,
            timestamp=datetime.now(UTC).isoformat(),
            brand_id=self.brand_id,
            metadata=metadata,
        )
        self.operations.append(record)
        self.total_used += cost
        self._save_locked()
        return record

    def reserve(
        self,
        operation: str,
        cost: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Reservation:
        """Atomically check the limit and hold ``cost`` units for a call.

        Unlike :meth:`check_quota` followed by :meth:`track_operation`, the
        check and the hold happen in one lock hold, so two callers contending
        for the last units cannot both be admitted. Settle the returned
        :class:`Reservation` with :meth:`commit` once the request has been
        sent (even if it then failed: Google may still have charged it), or
        with :meth:`release` only if it certainly never was.

        Args:
            operation: Name of the call, e.g. ``"videos.insert"``.
            cost: Units to hold. Defaults to the cost-table price of
                ``operation``.
            metadata: Optional payload carried onto the recorded operation.

        Raises:
            QuotaExceededError: If the hold would exceed ``daily_limit``
                counting recorded spend and other callers' holds.
            UnknownOperationError: If ``cost`` is omitted and the operation
                is not in the cost table.
        """
        if cost is None:
            cost = self.estimate_cost(operation)
        if cost < 0:
            raise ValueError(f"cost must not be negative, got {cost}")

        with self._lock:
            self._load_locked()
            self._admit_locked(operation, cost)
            reservation = Reservation(
                id=uuid.uuid4().hex,
                operation=operation,
                cost=cost,
                timestamp=datetime.now(UTC).isoformat(),
                brand_id=self.brand_id,
                metadata=metadata,
            )
            self.reservations.append(reservation)
            self._save_locked()

        return reservation

    def commit(
        self,
        reservation: Reservation | str,
        cost: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> QuotaOperation:
        """Convert a hold into recorded spend. Repeating it charges nothing more.

        Args:
            reservation: The :class:`Reservation` (or its id) from
                :meth:`reserve`.
            cost: Units actually consumed, if different from the hold.
            metadata: Extra payload merged over the reservation's, e.g. an
                id only known once the call returned.

        Returns:
            The recorded :class:`QuotaOperation`; on a repeat, the record
            the first commit made.

        Raises:
            UnknownReservationError: If the hold is gone and no recorded
                operation carries its id.
        """
        res_id = reservation if isinstance(reservation, str) else reservation.id
        if cost is not None and cost < 0:
            raise ValueError(f"cost must not be negative, got {cost}")

        with self._lock:
            self._load_locked()
            held = next((r for r in self.reservations if r.id == res_id), None)
            if held is None:
                for op in self.operations:
                    if (op.metadata or {}).get("reservation_id") == res_id:
                        return op
                raise UnknownReservationError(
                    f"no reservation {res_id!r}: already released, or the "
                    f"ledger rolled over since it was made"
                )

            self.reservations = [r for r in self.reservations if r.id != res_id]
            merged = {
                **(held.metadata or {}),
                **(metadata or {}),
                "reservation_id": res_id,
            }
            return self._record_locked(
                held.operation, held.cost if cost is None else cost, merged
            )

    def release(self, reservation: Reservation | str) -> None:
        """Drop a hold for a request that was certainly never sent.

        Releasing an already settled or unknown hold does nothing, and never
        refunds a committed charge. If the request may have been sent, commit
        instead: an unrecorded charge is how a ledger undercounts.
        """
        res_id = reservation if isinstance(reservation, str) else reservation.id
        with self._lock:
            self._load_locked()
            remaining = [r for r in self.reservations if r.id != res_id]
            if len(remaining) != len(self.reservations):
                self.reservations = remaining
                self._save_locked()

    def estimate_cost(self, operation_type: str, default: int | None = None) -> int:
        """Look an operation's unit cost up in the cost table.

        Args:
            operation_type: Key into the cost table, e.g. ``"videos.insert"``
                or the alias ``"video_upload"``.
            default: Returned instead of raising when the name is unknown.

        Raises:
            UnknownOperationError: If the name is unknown and no ``default``
                was given.
        """
        if operation_type in self.cost_table:
            return self.cost_table[operation_type]
        if default is not None:
            return default
        raise UnknownOperationError(
            f"unknown operation {operation_type!r}; pass default= or supply a "
            f"cost_table containing it"
        )

    def check_quota(self, cost: int) -> bool:
        """Return True if spending ``cost`` more units stays within the limit.

        Informational only: the answer can be stale by the time you act on
        it, because another caller may spend the same units in between. Use
        :meth:`reserve` to claim units atomically.
        """
        self.refresh()
        return (self.total_used + self._reserved_units() + cost) <= self.daily_limit

    def can_safely_perform(self, operation_type: str) -> bool:
        """Return True if the table cost of ``operation_type`` still fits.

        Informational, like :meth:`check_quota`; use :meth:`reserve` to claim.

        Raises:
            UnknownOperationError: If the operation name is unknown.
        """
        return self.check_quota(self.estimate_cost(operation_type))

    # -------------------------------------------------------------- reports

    def get_usage(self) -> dict[str, Any]:
        """Return current usage counters for this project."""
        self.refresh()
        return self._usage_unlocked()

    def _usage_unlocked(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "total_used": self.total_used,
            "total_limit": self.daily_limit,
            "reserved": self._reserved_units(),
            "remaining": self.daily_limit - self.total_used - self._reserved_units(),
            "percentage_used": (self.total_used / self.daily_limit) * 100,
            "operations_count": len(self.operations),
            "last_reset": self.last_reset.isoformat(),
        }

    def get_warnings(self) -> list[str]:
        """Return at most one message, describing the highest tier reached.

        Empty below :data:`WARN_THRESHOLD_PCT`, one WARNING line at or above
        it, one CRITICAL line at or above :data:`CRITICAL_THRESHOLD_PCT`.
        """
        self.refresh()
        return self._warnings_unlocked()

    def _warnings_unlocked(self) -> list[str]:
        pct = (self.total_used / self.daily_limit) * 100
        remaining = self.daily_limit - self.total_used
        if pct >= CRITICAL_THRESHOLD_PCT:
            return [
                f"CRITICAL: {pct:.1f}% of quota used "
                f"({self.total_used}/{self.daily_limit} units). "
                f"Only {remaining} units remaining."
            ]
        if pct >= WARN_THRESHOLD_PCT:
            return [
                f"WARNING: {pct:.1f}% of quota used "
                f"({self.total_used}/{self.daily_limit} units)."
            ]
        return []

    def get_history(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` operations, oldest first."""
        self.refresh()
        return [asdict(op) for op in self.operations[-limit:]]

    def generate_report(self) -> dict[str, Any]:
        """Return usage plus per-operation and per-brand breakdowns."""
        with self._lock:
            self._load_locked()
            by_operation: dict[str, dict[str, int]] = {}
            by_brand: dict[str, dict[str, int]] = {}

            for op in self.operations:
                bucket = by_operation.setdefault(
                    op.operation, {"count": 0, "total_cost": 0}
                )
                bucket["count"] += 1
                bucket["total_cost"] += op.cost

                if op.brand_id:
                    brand = by_brand.setdefault(
                        op.brand_id, {"count": 0, "total_cost": 0}
                    )
                    brand["count"] += 1
                    brand["total_cost"] += op.cost

            return {
                **self._usage_unlocked(),
                "by_operation": by_operation,
                "by_brand": by_brand,
                "warnings": self._warnings_unlocked(),
            }

    # --------------------------------------------------------------- lookup

    @classmethod
    def get_project_usage(
        cls, project_id: str, storage_path: Path | str | None = None
    ) -> int:
        """Return a project's units used today without constructing a manager.

        Returns 0 if no ledger exists yet. Does not apply the daily rollover,
        so a stale ledger from a previous day reports that day's total.
        """
        root = (
            Path(storage_path) if storage_path is not None else default_storage_path()
        )
        project_dir = root / project_id
        quota_file = project_dir / _LEDGER_FILENAME
        if not quota_file.exists():
            return 0

        with group_writable_filelock(project_dir / _LOCK_FILENAME):
            try:
                with open(quota_file, encoding="utf-8") as handle:
                    data = json.load(handle)
            except (json.JSONDecodeError, OSError):
                return 0
        return int(data.get("total_used", 0))
