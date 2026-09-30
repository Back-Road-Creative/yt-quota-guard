"""Global YouTube Data API quota accountant.

Keeps one shared ledger of API unit spend per Google Cloud project, so a
fleet of independent scripts and channels cannot exhaust the daily quota
without noticing.
"""

from .manager import (
    CRITICAL_THRESHOLD_PCT,
    DEFAULT_COST_TABLE,
    DEFAULT_DAILY_LIMIT,
    ENV_STORAGE_PATH,
    LOCK_FILE_MODE,
    WARN_THRESHOLD_PCT,
    OperationCost,
    QuotaExceededError,
    QuotaManager,
    QuotaOperation,
    Reservation,
    UnknownOperationError,
    UnknownReservationError,
    default_storage_path,
)

__version__ = "0.1.0"

__all__ = [
    "CRITICAL_THRESHOLD_PCT",
    "DEFAULT_COST_TABLE",
    "DEFAULT_DAILY_LIMIT",
    "ENV_STORAGE_PATH",
    "LOCK_FILE_MODE",
    "WARN_THRESHOLD_PCT",
    "OperationCost",
    "QuotaExceededError",
    "QuotaManager",
    "QuotaOperation",
    "Reservation",
    "UnknownOperationError",
    "UnknownReservationError",
    "__version__",
    "default_storage_path",
]
