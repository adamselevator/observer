"""
Health monitor.
Tracks connection health, logs events, writes health JSONL.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class HealthMonitor:
    """Tracks and logs connection health events."""

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._fh = None
        self._current_date: str = ""
        self._stats: dict[str, dict] = {}

    def log_event(self, source: str, event: str, details: dict | None = None):
        """Log a health event."""
        now = time.time()
        date_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

        # Rotate file if date changed
        if date_str != self._current_date:
            self._rotate(date_str)

        record = {
            "timestamp": int(now),
            "timestamp_iso": datetime.fromtimestamp(now, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "source": source,
            "event": event,
            "details": details or {},
        }

        if self._fh:
            line = json.dumps(record, separators=(",", ":"))
            self._fh.write(line + "\n")
            self._fh.flush()

        # Update in-memory stats
        if source not in self._stats:
            self._stats[source] = {
                "last_event": event,
                "last_event_at": now,
                "event_counts": {},
            }

        stats = self._stats[source]
        stats["last_event"] = event
        stats["last_event_at"] = now
        stats["event_counts"][event] = stats["event_counts"].get(event, 0) + 1

    def get_stats(self) -> dict:
        """Return current health stats for all sources."""
        return dict(self._stats)

    def get_summary(self) -> str:
        """Human-readable health summary."""
        lines = []
        for source, stats in self._stats.items():
            age = time.time() - stats["last_event_at"]
            lines.append(
                f"  {source}: last={stats['last_event']} ({age:.0f}s ago) "
                f"events={stats['event_counts']}"
            )
        return "\n".join(lines) if lines else "  No events yet"

    def _rotate(self, date_str: str):
        """Open a new health log file for the given date."""
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass

        dir_path = self._data_dir / "health"
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{date_str}.jsonl"

        self._fh = open(file_path, "a", buffering=1)
        self._current_date = date_str
        logger.info(f"[health] Opened {file_path}")

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
