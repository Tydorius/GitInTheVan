from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings
from app.services.timezones import get_zoneinfo

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_active_log_timezone: str = ""


class TimezoneFormatter(logging.Formatter):
    """Log formatter that renders ``%(asctime)s`` in a configured IANA timezone.

    When ``tz_name`` is empty or invalid, falls back to standard
    ``logging.Formatter.formatTime`` (server local time).
    """

    def __init__(
        self,
        fmt: str | None = LOG_FORMAT,
        datefmt: str | None = None,
        tz_name: str = "",
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.tz_name = (tz_name or "").strip()
        self._zi = get_zoneinfo(self.tz_name)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        if self._zi is None:
            return super().formatTime(record, datefmt)
        dt = datetime.fromtimestamp(record.created, tz=self._zi)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def apply_log_timezone(tz_name: str) -> None:
    """Update all root logger handlers to format timestamps in ``tz_name`` live.

    Passing an empty string resets handlers to standard server local time.
    """
    global _active_log_timezone
    cleaned = (tz_name or "").strip()
    _active_log_timezone = cleaned if get_zoneinfo(cleaned) is not None else ""

    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        existing = handler.formatter
        fmt = existing._fmt if existing is not None and getattr(existing, "_fmt", None) else LOG_FORMAT
        datefmt = existing.datefmt if existing is not None else None
        handler.setFormatter(TimezoneFormatter(fmt=fmt, datefmt=datefmt, tz_name=_active_log_timezone))


def setup_file_logging() -> Path | None:
    """Set up rotating file logging. Returns the log directory path or None."""
    log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = Path(settings.log_file) if settings.log_file else log_dir / "gitinthevan.log"

    max_bytes = settings.log_max_size_mb * 1024 * 1024

    handler = RotatingFileHandler(
        str(log_file),
        maxBytes=max_bytes,
        backupCount=99,
        encoding="utf-8",
    )
    handler.setFormatter(TimezoneFormatter(LOG_FORMAT, tz_name=_active_log_timezone))

    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler.setLevel(level)

    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    _cleanup_old_logs(log_dir, settings.log_retention_days)

    return log_dir


def _cleanup_old_logs(log_dir: Path, retention_days: int) -> None:
    """Delete log files older than retention_days."""
    if retention_days <= 0:
        return

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    cutoff_timestamp = cutoff.timestamp()

    for log_file in log_dir.glob("gitinthevan*.log*"):
        try:
            if log_file.stat().st_mtime < cutoff_timestamp:
                log_file.unlink()
        except Exception:
            pass


def get_log_files() -> list[Path]:
    """Return all log files in the log directory, newest first."""
    log_dir = Path("data/logs")
    if not log_dir.exists():
        return []

    files = list(log_dir.glob("gitinthevan*.log*"))
    files.sort(key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True)
    return files


def read_recent_logs(lines: int = 200) -> list[str]:
    """Read the most recent log lines across all log files."""
    log_files = get_log_files()
    if not log_files:
        return [
            "No log files found. Logs are written to data/logs/gitinthevan.log automatically.",
            f"Current log level: {settings.log_level.upper()}",
        ]

    all_lines: list[str] = []
    target = max(1, min(lines, 1000))

    for log_file in log_files:
        try:
            content = log_file.read_text(encoding="utf-8", errors="replace")
            file_lines = content.splitlines()
            all_lines = file_lines + all_lines
            if len(all_lines) >= target:
                break
        except Exception:
            continue

    return all_lines[-target:] if len(all_lines) > target else all_lines
