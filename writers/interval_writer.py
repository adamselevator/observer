"""
Interval writer.
Writes per-interval summary records and resolution updates to JSONL files.
One file per asset per timeframe per day.
"""

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from state.interval_tracker import IntervalRecord

logger = logging.getLogger(__name__)


class IntervalWriter:
    """
    Writes interval summary and resolution records to JSONL files.
    Append-only, no in-place edits.
    """

    def __init__(self, data_dir: Path):
        self._data_dir = data_dir
        self._files: dict[tuple[str, str, str], object] = {}

    def write_summary(self, record: IntervalRecord):
        """Write the interval summary when an interval completes."""
        date_str = datetime.fromtimestamp(
            record.start_ts, tz=timezone.utc
        ).strftime("%Y-%m-%d")

        key = (record.asset, record.timeframe, date_str)
        fh = self._get_file(key)

        line = json.dumps(record.to_dict(), separators=(",", ":"))
        fh.write(line + "\n")
        fh.flush()

        logger.info(f"[interval] Wrote summary for {record.interval_id}")

    def write_resolution(self, record: IntervalRecord):
        """Write a resolution update when outcome is determined."""
        date_str = datetime.fromtimestamp(
            record.start_ts, tz=timezone.utc
        ).strftime("%Y-%m-%d")

        key = (record.asset, record.timeframe, date_str)
        fh = self._get_file(key)

        line = json.dumps(record.resolution_dict(), separators=(",", ":"))
        fh.write(line + "\n")
        fh.flush()

        logger.info(
            f"[interval] Wrote resolution for {record.interval_id}: "
            f"{record.resolution}"
        )

    def _get_file(self, key: tuple[str, str, str]):
        """Get or create a JSONL file handle."""
        if key in self._files:
            return self._files[key]

        asset, timeframe, date_str = key
        dir_path = self._data_dir / timeframe / "intervals"
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{asset}_{date_str}.jsonl"

        fh = open(file_path, "a", buffering=1)
        self._files[key] = fh

        logger.info(f"[interval] Opened {file_path}")
        return fh

    def close(self):
        """Close all open file handles."""
        for fh in self._files.values():
            try:
                fh.close()
            except Exception:
                pass
        self._files.clear()

    def rotate_date(self, new_date_str: str):
        """Close files from previous date."""
        old_keys = [
            k for k in self._files if k[2] != new_date_str
        ]
        for key in old_keys:
            fh = self._files.pop(key, None)
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
        if old_keys:
            logger.info(f"[interval] Rotated {len(old_keys)} files for new date")
