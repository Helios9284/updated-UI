#!/usr/bin/env python3
"""Load/save per-netuid top validator hotkeys (shared by updater + stake backend)."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_hotkeys_path() -> Path:
    override = (os.getenv("VALIDATOR_HOTKEYS_PATH") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / "data" / "validator_hotkeys.json"


def parse_refresh_blocks(value: str | None, default: int = 100) -> int:
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except Exception:
        return default
    return max(1, n)


def parse_netuid_filter(value: str | None) -> set[int] | None:
    raw = (value or "").strip()
    if not raw:
        return None
    out: set[int] = set()
    for part in raw.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except Exception:
            continue
    return out or None


def load_validator_hotkeys(path: Path | None = None) -> tuple[dict[int, str], dict]:
    """Return ({netuid: hotkey_ss58}, metadata dict). Missing file -> empty."""
    p = path or default_hotkeys_path()
    if not p.is_file():
        return {}, {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    if not isinstance(raw, dict):
        return {}, {}
    meta = {
        "updated_at_block": raw.get("updated_at_block"),
        "updated_at": raw.get("updated_at"),
        "path": str(p),
    }
    src = raw.get("hotkeys")
    if not isinstance(src, dict):
        return {}, meta
    out: dict[int, str] = {}
    for k, v in src.items():
        try:
            netuid = int(k)
        except Exception:
            continue
        hk = str(v or "").strip()
        if hk:
            out[netuid] = hk
    return out, meta


def save_validator_hotkeys(
    hotkeys: dict[int, str],
    *,
    path: Path | None = None,
    updated_at_block: int | None = None,
    extra: dict | None = None,
) -> Path:
    p = path or default_hotkeys_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": utc_now_iso(),
        "updated_at_block": updated_at_block,
        "hotkeys": {str(int(k)): str(v) for k, v in sorted(hotkeys.items())},
    }
    if extra:
        payload.update(extra)
    text = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".validator_hotkeys.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, p)
    except Exception:
        with contextlib.suppress(Exception):
            os.unlink(tmp_name)
        raise
    return p


def pick_top_emission_validator(
    hotkeys,
    validator_permit,
    stake,
    emission,
) -> tuple[str | None, float]:
    """Pick validator hotkey with highest emission among permitted validators with stake."""
    best_hk = None
    best_em = -1.0
    n = min(len(hotkeys), len(validator_permit), len(stake), len(emission))
    for i in range(n):
        if not bool(validator_permit[i]):
            continue
        try:
            st = float(stake[i] or 0)
        except Exception:
            st = 0.0
        if st <= 0:
            continue
        try:
            em = float(emission[i] or 0)
        except Exception:
            em = 0.0
        if em > best_em:
            best_em = em
            best_hk = str(hotkeys[i])
    if best_hk is None:
        return None, 0.0
    return best_hk, best_em
