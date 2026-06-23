from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import settings


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
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

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
