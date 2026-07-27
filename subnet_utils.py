"""Shared subnet dynamic-info normalization (no substrate/bittensor imports)."""

import json
from datetime import datetime, timezone
from pathlib import Path


def to_int(value):
    try:
        if hasattr(value, "value"):
            value = value.value
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def bytes_field_to_str(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        try:
            return bytes(int(b) for b in value).decode("utf-8", errors="replace").strip()
        except Exception:
            return ""
    return str(value).strip()


def normalize_subnet_dynamic_row(row):
    if not isinstance(row, dict):
        return None
    netuid = to_int(row.get("netuid"))
    if netuid is None:
        return None
    identity = row.get("subnet_identity") if isinstance(row.get("subnet_identity"), dict) else {}
    tao_in_rao = to_int(row.get("tao_in")) or 0
    alpha_in_rao = to_int(row.get("alpha_in")) or 0
    alpha_out_rao = to_int(row.get("alpha_out")) or 0
    if alpha_in_rao:
        price_tao = tao_in_rao / alpha_in_rao
    else:
        price_tao = 1.0 if netuid == 0 else 0.0
    name = str(identity.get("subnet_name") or "").strip()
    if not name:
        name = bytes_field_to_str(row.get("subnet_name"))
    return {
        "netuid": int(netuid),
        "subnet_name": name or f"subnet-{netuid}",
        "symbol": bytes_field_to_str(row.get("token_symbol")),
        "owner_coldkey": str(row.get("owner_coldkey") or ""),
        "owner_hotkey": str(row.get("owner_hotkey") or ""),
        "tao_in": round(tao_in_rao / 1_000_000_000, 4),
        "alpha_in": round(alpha_in_rao / 1_000_000_000, 4),
        "alpha_out": round(alpha_out_rao / 1_000_000_000, 4),
        "price_tao": round(price_tao, 9),
        "tempo": to_int(row.get("tempo")),
        "emission_tao": round((to_int(row.get("emission")) or 0) / 1_000_000_000, 6),
        "logo_url": str(identity.get("logo_url") or "").strip(),
        "subnet_url": str(identity.get("subnet_url") or "").strip(),
        "description": str(identity.get("description") or "").strip(),
        "github_repo": str(identity.get("github_repo") or "").strip(),
        "discord": str(identity.get("discord") or "").strip(),
    }


def subnet_emission_tao(subnet_row):
    """Per-block subnet emission in TAO from dynamic info (0 if none)."""
    if not isinstance(subnet_row, dict):
        return 0.0
    try:
        return float(subnet_row.get("emission_tao") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def subnet_has_emission(subnet_row, *, min_tao=0.0):
    """True when a subnet (netuid > 0) receives non-zero block emission."""
    if not isinstance(subnet_row, dict):
        return False
    netuid = to_int(subnet_row.get("netuid"))
    if netuid is None or int(netuid) <= 0:
        return False
    return subnet_emission_tao(subnet_row) > float(min_tao)


def active_emission_subnet_rows(subnets, *, min_tao=0.0):
    """Return normalized subnet rows that currently receive emission."""
    out = []
    for row in subnets or []:
        if isinstance(row, dict) and subnet_has_emission(row, min_tao=min_tao):
            out.append(row)
    out.sort(key=lambda r: int(r.get("netuid") or 0))
    return out


def build_subnet_emission_notification_row(subnet_row, block_number):
    """Notification payload when a subnet newly starts receiving emission."""
    netuid = int(subnet_row.get("netuid") or 0)
    return {
        "call": "SubtensorModule.subnet_emission_started",
        "signer": "-",
        "real_address": str(subnet_row.get("owner_coldkey") or "-"),
        "hotkey": str(subnet_row.get("owner_hotkey") or "-"),
        "netuid": str(netuid),
        "subnet_name": str(subnet_row.get("subnet_name") or f"subnet-{netuid}"),
        "emission_tao": str(subnet_row.get("emission_tao") or "0"),
        "price_tao": str(subnet_row.get("price_tao") or "-"),
        "symbol": str(subnet_row.get("symbol") or ""),
        "block_number": int(block_number) if block_number is not None else None,
        "status": "confirmed",
    }


def subnet_owner_label(netuid, subnet_name, role):
    name = (subnet_name or f"subnet-{netuid}").strip()
    if role == "owner_coldkey":
        return f"<== SN{netuid} {name} OWNER ==>"
    if role == "owner_hotkey":
        return f"<== SN{netuid} {name} HK ==>"
    return f"<== SN{netuid} {name} ==>"


def build_subnet_owners_snapshot(rows, block_number=None):
    subnets = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        netuid = to_int(row.get("netuid"))
        if netuid is None:
            continue
        subnet_name = str(row.get("subnet_name") or "").strip() or f"subnet-{netuid}"
        subnets[str(netuid)] = {
            "netuid": int(netuid),
            "subnet_name": subnet_name,
            "owner_coldkey": str(row.get("owner_coldkey") or "").strip(),
            "owner_hotkey": str(row.get("owner_hotkey") or "").strip(),
        }
    return {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "block_number": int(block_number) if block_number is not None else None,
        "subnets": subnets,
    }


def _owner_pairs_by_netuid(snapshot):
    pairs = {}
    for key, rec in (snapshot or {}).get("subnets", {}).items():
        if not isinstance(rec, dict):
            continue
        pairs[str(key)] = (
            str(rec.get("owner_coldkey") or "").strip(),
            str(rec.get("owner_hotkey") or "").strip(),
        )
    return pairs


def subnet_owners_changed(previous, current):
    return _owner_pairs_by_netuid(previous) != _owner_pairs_by_netuid(current)


def load_subnet_owners(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "updated_at": None, "block_number": None, "subnets": {}}
    if not isinstance(raw, dict):
        return {"version": 1, "updated_at": None, "block_number": None, "subnets": {}}
    subnets = raw.get("subnets") if isinstance(raw.get("subnets"), dict) else {}
    return {
        "version": int(raw.get("version") or 1),
        "updated_at": raw.get("updated_at"),
        "block_number": raw.get("block_number"),
        "subnets": subnets,
    }


def subnet_owner_labels_from_snapshot(snapshot):
    labels = {}
    for rec in (snapshot or {}).get("subnets", {}).values():
        if not isinstance(rec, dict):
            continue
        netuid = to_int(rec.get("netuid"))
        if netuid is None:
            continue
        subnet_name = str(rec.get("subnet_name") or "").strip() or f"subnet-{netuid}"
        cold = str(rec.get("owner_coldkey") or "").strip()
        hot = str(rec.get("owner_hotkey") or "").strip()
        if cold:
            labels[cold] = subnet_owner_label(netuid, subnet_name, "owner_coldkey")
        if hot:
            labels[hot] = subnet_owner_label(netuid, subnet_name, "owner_hotkey")
    return labels


def persist_subnet_owners(rows, path, block_number=None):
    """Save subnet owner addresses when any owner coldkey/hotkey changes."""
    path = Path(path)
    current = build_subnet_owners_snapshot(rows, block_number=block_number)
    previous = load_subnet_owners(path)
    if not subnet_owners_changed(previous, current):
        return False
    path.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def merge_address_book_labels(manual_book, subnet_owner_snapshot):
    merged = subnet_owner_labels_from_snapshot(subnet_owner_snapshot)
    if isinstance(manual_book, dict):
        merged.update({str(k): str(v) for k, v in manual_book.items()})
    return merged
