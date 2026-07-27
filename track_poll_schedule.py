"""Shared JSON poll schedule for staggered multi-process mempool polling."""

from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any


def default_schedule(*, process_count: int, poll_interval_ms: int) -> dict[str, Any]:
    process_count = max(1, int(process_count))
    poll_interval_ms = max(1, int(poll_interval_ms))
    slot_gap_ms = max(1, poll_interval_ms // process_count)
    return {
        "version": 1,
        "poll_interval_ms": poll_interval_ms,
        "process_count": process_count,
        "slot_gap_ms": slot_gap_ms,
        "last_poll_ms": 0,
        "last_poll_pid": -1,
        "next_pid": 0,
        "seq": 0,
    }


def _read_json_file(f) -> dict[str, Any]:
    f.seek(0)
    raw = f.read()
    if not raw.strip():
        raise ValueError("empty schedule file")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("poll schedule root must be an object")
    return data


def _write_json_file(f, data: dict[str, Any]) -> None:
    f.seek(0)
    f.truncate()
    json.dump(data, f, indent=2)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())


def init_poll_schedule(
    path: Path,
    *,
    process_count: int,
    poll_interval_ms: int,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = default_schedule(process_count=process_count, poll_interval_ms=poll_interval_ms)
    with path.open("w", encoding="utf-8") as f:
        _write_json_file(f, data)
    return data


def try_acquire_poll_slot(
    process_index: int,
    path: Path,
) -> tuple[bool, int | None, dict[str, Any] | None]:
    """Try to take the next poll slot under an exclusive file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    process_index = int(process_index)

    with path.open("a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            try:
                data = _read_json_file(f)
            except (json.JSONDecodeError, ValueError, OSError):
                data = default_schedule(
                    process_count=max(process_index + 1, 1),
                    poll_interval_ms=40,
                )

            process_count = max(1, int(data.get("process_count", 1)))
            if process_index < 0 or process_index >= process_count:
                return False, None, dict(data)

            now_ms = int(time.time() * 1000)
            next_pid = int(data.get("next_pid", 0))
            last_poll_ms = int(data.get("last_poll_ms", 0))
            slot_gap_ms = max(1, int(data.get("slot_gap_ms", 1)))

            if next_pid != process_index:
                return False, None, dict(data)

            earliest_ms = last_poll_ms + slot_gap_ms
            if last_poll_ms > 0 and now_ms < earliest_ms:
                return False, None, dict(data)

            send_ms = now_ms
            data["last_poll_ms"] = send_ms
            data["last_poll_pid"] = process_index
            data["next_pid"] = (process_index + 1) % process_count
            data["seq"] = int(data.get("seq", 0)) + 1
            _write_json_file(f, data)
            return True, send_ms, dict(data)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
