#!/usr/bin/env python3
import argparse
import contextlib
import json
import signal
import time
from datetime import datetime, timezone

from substrateinterface import SubstrateInterface

from mempool_realtime import (
    SPLIT_STAKE_FUNCTIONS,
    _canonical_proxy_ss58,
    _format_real_address_cell,
    annotate_mempool_proxy_fake,
    build_alpha_prices_tao_from_reserves_map,
    call_args_to_dict,
    clear_terminal,
    decode_extrinsic,
    enrich_alpha_prices_tao_by_netuid,
    format_cell,
    format_tao,
    get_first_value,
    netuids_needing_alpha_price_from_decoded,
    notification_rows_from_decoded_ext,
    rao_to_tao,
    render_custom_table,
    stake_amount_arg_candidates,
    stake_amount_to_tao,
    to_int,
)


RUNNING = True


def handle_stop_signal(signum, frame):
    del signum, frame
    global RUNNING
    RUNNING = False


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _ms_to_iso(ms):
    ms = to_int(ms)
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return None


def extract_block_timestamp_ms(decoded_ext):
    """Return the on-chain block time (epoch ms) from the Timestamp.set inherent."""
    if str(decoded_ext.get("call_module") or "").lower() != "timestamp":
        return None
    if str(decoded_ext.get("call_function") or "").lower() != "set":
        return None
    for arg in decoded_ext.get("call_args") or []:
        if isinstance(arg, dict) and arg.get("name") == "now":
            return to_int(arg.get("value"))
    return None


def parse_block_number(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return int(value, 16) if value.startswith("0x") else int(value)
        except Exception:
            return None
    try:
        return int(value)
    except Exception:
        return None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Realtime Bittensor block fetcher with stake/transfer/start_call tables"
    )
    parser.add_argument(
        "--endpoint",
        default="wss://entrypoint-finney.opentensor.ai:443",
        help="WebSocket RPC endpoint",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.2,
        help="Polling interval in seconds",
    )
    parser.add_argument(
        "--last-n",
        type=int,
        default=10,
        help="How many recent blocks to fetch in oneshot mode",
    )
    parser.add_argument(
        "--oneshot",
        action="store_true",
        help="Fetch last N blocks once and exit",
    )
    parser.add_argument(
        "--output",
        choices=("pretty", "json"),
        default="pretty",
        help="Output format",
    )
    parser.add_argument(
        "--current-only-screen",
        action="store_true",
        help="Clear screen before printing each processed block",
    )
    return parser.parse_args()


def _compact_json(value, *, limit=240):
    try:
        text = json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=True)
    except Exception:
        text = str(value)
    text = " ".join(str(text).split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def module_error_index(error_field):
    """Extract u8 error index from Module.error ([u8;4] / hex / int)."""
    if error_field is None:
        return None
    if isinstance(error_field, bool):
        return int(error_field)
    if isinstance(error_field, int):
        return int(error_field)
    if isinstance(error_field, str):
        text = error_field.strip()
        if text.startswith("0x") or text.startswith("0X"):
            try:
                raw = bytes.fromhex(text[2:])
            except Exception:
                return None
            return int(raw[0]) if raw else None
        with contextlib.suppress(Exception):
            return int(text)
        return None
    if isinstance(error_field, (bytes, bytearray)):
        return int(error_field[0]) if error_field else None
    if isinstance(error_field, (list, tuple)) and error_field:
        with contextlib.suppress(Exception):
            return int(error_field[0])
    return None


def summarize_dispatch_error(dispatch_error, *, substrate=None):
    """Human-readable DispatchError (Module / Token / Arithmetic / …)."""
    if dispatch_error is None:
        return ""
    if isinstance(dispatch_error, str):
        return dispatch_error.strip()
    if not isinstance(dispatch_error, dict):
        return _compact_json(dispatch_error)

    local = ""
    module = dispatch_error.get("Module")
    if isinstance(module, dict):
        err_name = module.get("error") or module.get("name") or module.get("Error")
        pallet = module.get("pallet") or module.get("name")
        idx = module.get("index")
        err = module.get("error")
        err_idx = module_error_index(err)
        if err_name and isinstance(err_name, str) and not err_name.startswith("0x"):
            if pallet is not None and not str(pallet).isdigit():
                local = f"{pallet}.{err_name}"
            else:
                local = str(err_name)
        else:
            # Resolve via live chain metadata when possible.
            meta_name = ""
            if substrate is not None and idx is not None and err_idx is not None:
                with contextlib.suppress(Exception):
                    meta_name = lookup_module_error_name(substrate, int(idx), int(err_idx)) or ""
            if meta_name:
                local = meta_name
            elif idx is not None and err is not None:
                local = f"Module(index={idx}, error={err}"
                if err_idx is not None:
                    local += f", error_index={err_idx}"
                local += ")"

    if not local:
        for variant in (
            "Token",
            "Arithmetic",
            "Transactional",
            "BadOrigin",
            "CannotLookup",
            "Other",
            "ConsumerRemaining",
            "NoProviders",
            "TooManyConsumers",
            "Unavailable",
            "Exhausted",
            "Corruption",
            "Root",
        ):
            if variant in dispatch_error:
                val = dispatch_error.get(variant)
                if val in (None, "", "None", {}):
                    local = variant
                else:
                    local = f"{variant}:{_compact_json(val, limit=120)}"
                break
    if not local:
        local = _compact_json(dispatch_error)

    # bittensor helper often returns UnknownError(UnknownType) on stale metadata —
    # only keep it when it adds a real name.
    with contextlib.suppress(Exception):
        from bittensor.utils import format_error_message

        text = str(format_error_message(dispatch_error) or "").strip()
        if text and "UnknownError" not in text and "UnknownType" not in text:
            if "Unknown Description" not in text:
                return text
    return local


def lookup_module_error_name(substrate, module_index, error_index):
    """Resolve Module error to Pallet.ErrorName via substrate metadata."""
    module_index = int(module_index)
    error_index = int(error_index)
    meta = getattr(substrate, "metadata", None)
    if meta is None:
        return None
    pallets = None
    for attr in ("pallets", "value", "pallets_v14"):
        with contextlib.suppress(Exception):
            cand = getattr(meta, attr, None)
            if cand:
                pallets = list(cand)
                break
    if pallets is None:
        with contextlib.suppress(Exception):
            pallets = list(meta)
    if not pallets:
        return None
    for pallet in pallets:
        p_idx = None
        for getter in ("index", "value", "pallet_index"):
            with contextlib.suppress(Exception):
                val = getattr(pallet, getter, None)
                if callable(val):
                    continue
                if isinstance(val, dict) and "index" in val:
                    p_idx = int(val["index"])
                    break
                if val is not None and not isinstance(val, (dict, list)):
                    p_idx = int(val)
                    break
        if p_idx is None:
            with contextlib.suppress(Exception):
                p_idx = int(pallet["index"])
        if p_idx != module_index:
            continue
        pallet_name = (
            getattr(pallet, "name", None)
            or getattr(pallet, "pallet_name", None)
            or (pallet.get("name") if isinstance(pallet, dict) else None)
            or f"Pallet{module_index}"
        )
        errors = (
            getattr(pallet, "errors", None)
            or getattr(pallet, "Errors", None)
            or (pallet.get("errors") if isinstance(pallet, dict) else None)
            or []
        )
        for err in errors or []:
            e_idx = None
            with contextlib.suppress(Exception):
                e_idx = int(getattr(err, "index", None))
            if e_idx is None and isinstance(err, dict):
                with contextlib.suppress(Exception):
                    e_idx = int(err.get("index"))
            if e_idx != error_index:
                continue
            e_name = (
                getattr(err, "name", None)
                or (err.get("name") if isinstance(err, dict) else None)
                or f"Error{error_index}"
            )
            return f"{pallet_name}.{e_name}"
    return None


def _dispatch_error_from_attrs(attrs):
    if attrs is None:
        return None
    if isinstance(attrs, dict):
        if "dispatch_error" in attrs:
            return attrs.get("dispatch_error")
        if "error" in attrs and any(
            k in attrs for k in ("dispatch_info", "DispatchInfo", "info")
        ):
            return attrs.get("error")
        # Bare DispatchError enum object.
        if any(
            k in attrs
            for k in ("Module", "Token", "Arithmetic", "BadOrigin", "Other")
        ):
            return attrs
        return None
    if isinstance(attrs, (list, tuple)) and attrs:
        first = attrs[0]
        if isinstance(first, dict):
            return _dispatch_error_from_attrs(first) or first
        return first
    return attrs


def extract_extrinsic_failure_detail(events, extrinsic_idx, *, substrate=None):
    """Return a short on-chain failure reason for extrinsic_idx, or None."""
    if extrinsic_idx is None:
        return None
    target = int(extrinsic_idx)
    reasons = []
    for ev in events or []:
        idx = ev.get("extrinsic_idx")
        if idx is None:
            continue
        try:
            if int(idx) != target:
                continue
        except Exception:
            continue
        mod = str(ev.get("module_id") or "")
        eid = str(ev.get("event_id") or "")
        mod_l = mod.lower()
        eid_l = eid.lower()
        attrs = _event_attrs(ev)

        if mod_l == "system" and eid_l == "extrinsicfailed":
            detail = summarize_dispatch_error(
                _dispatch_error_from_attrs(attrs), substrate=substrate
            )
            reasons.append(detail or "ExtrinsicFailed")
            continue

        if mod_l == "proxy" and eid_l == "proxyexecuted" and isinstance(attrs, dict):
            result = attrs.get("result")
            if isinstance(result, dict) and "Err" in result:
                detail = summarize_dispatch_error(result.get("Err"), substrate=substrate)
                reasons.append(f"ProxyExecuted.Err:{detail or _compact_json(result.get('Err'))}")
            continue

        if mod_l == "utility" and eid_l == "batchinterrupted":
            detail = ""
            if isinstance(attrs, dict):
                detail = summarize_dispatch_error(
                    attrs.get("error") or attrs.get("dispatch_error"),
                    substrate=substrate,
                )
            elif isinstance(attrs, (list, tuple)) and len(attrs) >= 2:
                detail = summarize_dispatch_error(attrs[1], substrate=substrate)
            reasons.append(
                f"BatchInterrupted:{detail}" if detail else "BatchInterrupted"
            )
            continue

        if mod_l == "utility" and eid_l == "itemfailed":
            detail = summarize_dispatch_error(
                _dispatch_error_from_attrs(attrs) or attrs, substrate=substrate
            )
            reasons.append(f"ItemFailed:{detail}" if detail else "ItemFailed")
            continue

        if mod_l == "utility" and eid_l == "batchcompletedwitherrors":
            reasons.append("BatchCompletedWithErrors")

    if not reasons:
        return None
    # Prefer System.ExtrinsicFailed / first concrete reason.
    return "; ".join(dict.fromkeys(r for r in reasons if r))


def build_extrinsic_status_map(events):
    status = {}
    for ev in events or []:
        idx = ev.get("extrinsic_idx")
        if idx is None:
            continue
        mod = str(ev.get("module_id") or "").lower()
        eid = str(ev.get("event_id") or "").lower()
        if mod == "system" and eid == "extrinsicsuccess":
            status.setdefault(idx, "success")
    for ev in events or []:
        idx = ev.get("extrinsic_idx")
        if idx is None:
            continue
        mod = str(ev.get("module_id") or "").lower()
        eid = str(ev.get("event_id") or "").lower()
        if mod == "system" and eid == "extrinsicfailed":
            status[idx] = "failed"

    # Some wrapped calls can fail internally while the outer extrinsic succeeds.
    # Treat these as failed for table purposes so proxy/batch rows reflect reality.
    for ev in events or []:
        idx = ev.get("extrinsic_idx")
        if idx is None:
            continue
        mod = str(ev.get("module_id") or "").lower()
        eid = str(ev.get("event_id") or "").lower()
        attrs = _event_attrs(ev)

        # Proxy.proxy inner dispatch result.
        if mod == "proxy" and eid == "proxyexecuted" and isinstance(attrs, dict):
            result = attrs.get("result")
            if isinstance(result, dict) and "Err" in result:
                status[idx] = "failed"

        # Utility.batch / batch_all interrupted before completing all calls.
        if mod == "utility" and eid == "batchinterrupted":
            status[idx] = "failed"

        # Utility.force_batch runs every item and, on a failed item, emits
        # ItemFailed + BatchCompletedWithErrors while the OUTER extrinsic still
        # reports ExtrinsicSuccess. Without this, a force_batch whose inner
        # stake failed (e.g. SlippageTooHigh) is mislabeled as a successful
        # stake. Treat any item failure as a failed extrinsic so batch rows
        # reflect on-chain reality.
        if mod == "utility" and eid in ("itemfailed", "batchcompletedwitherrors"):
            status[idx] = "failed"
    return status


def build_force_batch_item_status(events):
    """Per-extrinsic ordered list of force_batch item outcomes.

    Utility (force_)batch emits one Utility.ItemCompleted (ok) or
    Utility.ItemFailed (err) per top-level item, in call order. Returns
    {extrinsic_idx: [True, False, ...]} where True == item succeeded.
    """
    out = {}
    for ev in events or []:
        idx = ev.get("extrinsic_idx")
        if idx is None:
            continue
        mod = str(ev.get("module_id") or "").lower()
        eid = str(ev.get("event_id") or "").lower()
        if mod != "utility":
            continue
        if eid == "itemcompleted":
            out.setdefault(idx, []).append(True)
        elif eid == "itemfailed":
            out.setdefault(idx, []).append(False)
    return out


def _force_batch_item_count(ext):
    """Number of top-level calls inside this extrinsic's Utility.force_batch
    (None if there is no force_batch)."""
    for c in ext.get("all_calls", []) or []:
        if str(c.get("function") or "").lower() == "force_batch":
            for arg in c.get("call_args") or []:
                if isinstance(arg, dict) and arg.get("name") == "calls":
                    val = arg.get("value")
                    if isinstance(val, list):
                        return len(val)
    return None


def top_event_counts(events, limit=12):
    counts = {}
    for ev in events or []:
        key = f"{ev.get('module_id', '?')}.{ev.get('event_id', '?')}"
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def get_alpha_prices_tao_by_netuid(sub, block_hash):
    """
    Return {netuid: alpha_price_in_tao} at the given block.

    Instantaneous AMM spot price from the subnet reserves
    (price_tao = SubnetTAO / SubnetAlphaIn).
    """
    return build_alpha_prices_tao_from_reserves_map(sub, block_hash)


def _event_attrs(ev):
    attrs = ev.get("attributes")
    if attrs is None and isinstance(ev.get("event"), dict):
        attrs = ev["event"].get("attributes")
    return attrs


def _fmt_tao_from_rao(value):
    return format_tao(rao_to_tao(value))


def _summarize_chain_event(ev):
    mod = ev.get("module_id")
    eid = ev.get("event_id")
    attrs = _event_attrs(ev)

    if mod == "Balances" and eid == "Transfer" and isinstance(attrs, dict):
        src = format_cell(attrs.get("from"))
        dst = format_cell(attrs.get("to"))
        amt = _fmt_tao_from_rao(attrs.get("amount"))
        return f"Transfer {src} -> {dst} tao={amt}"

    if mod == "SubtensorModule" and eid in ("StakeRemoved", "StakeAdded") and isinstance(attrs, (list, tuple)):
        coldkey = format_cell(attrs[0] if len(attrs) >= 1 else None)
        hotkey = format_cell(attrs[1] if len(attrs) >= 2 else None)
        tao = _fmt_tao_from_rao(attrs[2] if len(attrs) >= 3 else None)
        netuid = format_cell(attrs[4] if len(attrs) >= 5 else None)
        return f"{eid} coldkey={coldkey} hotkey={hotkey} netuid={netuid} tao={tao}"

    if mod == "SubtensorModule" and eid == "StakeTransferred" and isinstance(attrs, (list, tuple)):
        coldkey = format_cell(attrs[0] if len(attrs) >= 1 else None)
        hotkey = format_cell(attrs[2] if len(attrs) >= 3 else None)
        origin = format_cell(attrs[3] if len(attrs) >= 4 else None)
        dest = format_cell(attrs[4] if len(attrs) >= 5 else None)
        tao = _fmt_tao_from_rao(attrs[5] if len(attrs) >= 6 else None)
        return f"StakeTransferred coldkey={coldkey} hotkey={hotkey} {origin}->{dest} tao={tao}"

    return None


def extract_ethereum_stake_transfer_chains(events, extrinsic_meta):
    rows = []
    events_by_idx = {}
    for ev in events or []:
        idx = ev.get("extrinsic_idx")
        if idx is None:
            continue
        events_by_idx.setdefault(idx, []).append(ev)

    for idx, meta in extrinsic_meta.items():
        if meta.get("root_call") != "Ethereum.transact":
            continue
        evs = events_by_idx.get(idx, [])
        interesting = [
            ev
            for ev in evs
            if (ev.get("module_id"), ev.get("event_id"))
            in {
                ("Balances", "Transfer"),
                ("SubtensorModule", "StakeRemoved"),
                ("SubtensorModule", "StakeAdded"),
                ("SubtensorModule", "StakeTransferred"),
            }
        ]
        if not interesting:
            continue

        has_transferred = any(
            ev.get("module_id") == "SubtensorModule" and ev.get("event_id") == "StakeTransferred"
            for ev in interesting
        )
        if not has_transferred:
            continue

        flow = " -> ".join(f"{ev.get('module_id')}.{ev.get('event_id')}" for ev in interesting)
        details_parts = []
        for ev in interesting:
            s = _summarize_chain_event(ev)
            if s:
                details_parts.append(s)

        rows.append(
            {
                "idx": idx,
                "status": meta.get("status", "unknown"),
                "signer": format_cell(meta.get("signer")),
                "hash": format_cell(meta.get("hash")),
                "flow": flow,
                "details": " | ".join(details_parts),
            }
        )

    return rows


def extract_ethereum_stake_rows_from_events(events, extrinsic_meta):
    rows = []
    for ev in events or []:
        idx = ev.get("extrinsic_idx")
        if idx is None:
            continue
        meta = extrinsic_meta.get(idx) or {}
        if meta.get("root_call") != "Ethereum.transact":
            continue

        mod = ev.get("module_id")
        eid = ev.get("event_id")
        attrs = _event_attrs(ev)
        if mod != "SubtensorModule" or eid not in ("StakeAdded", "StakeRemoved", "StakeTransferred"):
            continue

        signer = format_cell(meta.get("signer"))
        status = meta.get("status", "unknown")

        if eid in ("StakeAdded", "StakeRemoved") and isinstance(attrs, (list, tuple)):
            coldkey = format_cell(attrs[0] if len(attrs) >= 1 else None)
            hotkey = format_cell(attrs[1] if len(attrs) >= 2 else None)
            amount = _fmt_tao_from_rao(attrs[2] if len(attrs) >= 3 else None)
            netuid = format_cell(attrs[4] if len(attrs) >= 5 else None)
            rows.append(
                {
                    "idx": idx,
                    "status": status,
                    "call": f"SubtensorModule.{eid} [event]",
                    "signer": signer,
                    "real_address": coldkey,
                    "amount": amount,
                    "netuid": netuid,
                    "path": f"Ethereum.transact > event:{eid} hotkey={hotkey}",
                }
            )
            continue

        if eid == "StakeTransferred" and isinstance(attrs, (list, tuple)):
            coldkey = format_cell(attrs[0] if len(attrs) >= 1 else None)
            hotkey = format_cell(attrs[2] if len(attrs) >= 3 else None)
            origin = format_cell(attrs[3] if len(attrs) >= 4 else None)
            dest = format_cell(attrs[4] if len(attrs) >= 5 else None)
            amount = _fmt_tao_from_rao(attrs[5] if len(attrs) >= 6 else None)
            rows.append(
                {
                    "idx": idx,
                    "status": status,
                    "call": "SubtensorModule.StakeTransferred [event]",
                    "signer": signer,
                    "real_address": coldkey,
                    "amount": amount,
                    "netuid": f"{origin}->{dest}",
                    "path": f"Ethereum.transact > event:StakeTransferred hotkey={hotkey}",
                }
            )

    return rows


def build_stake_event_amount_index(events):
    """
    Build index for filling missing stake amounts from confirmed events.
    key: extrinsic_idx -> list of {'event_id','amount','netuid'}
    """
    out = {}
    for ev in events or []:
        idx = ev.get("extrinsic_idx")
        if idx is None:
            continue
        if ev.get("module_id") != "SubtensorModule":
            continue
        eid = ev.get("event_id")
        attrs = _event_attrs(ev)
        if eid in ("StakeAdded", "StakeRemoved") and isinstance(attrs, (list, tuple)):
            amount = _fmt_tao_from_rao(attrs[2] if len(attrs) >= 3 else None)
            netuid = format_cell(attrs[4] if len(attrs) >= 5 else None)
            out.setdefault(idx, []).append(
                {"event_id": eid, "amount": amount, "netuid": netuid}
            )
        elif eid == "StakeTransferred" and isinstance(attrs, (list, tuple)):
            amount = _fmt_tao_from_rao(attrs[5] if len(attrs) >= 6 else None)
            origin = format_cell(attrs[3] if len(attrs) >= 4 else None)
            dest = format_cell(attrs[4] if len(attrs) >= 5 else None)
            out.setdefault(idx, []).append(
                {"event_id": eid, "amount": amount, "netuid": f"{origin}->{dest}"}
            )
    return out


def _preferred_event_ids_for_call(call_lower):
    if "transfer_stake" in call_lower or "move_stake" in call_lower or "swap_stake" in call_lower:
        return ("StakeTransferred", "StakeRemoved", "StakeAdded")
    if "remove_stake" in call_lower or "unstake" in call_lower:
        return ("StakeRemoved", "StakeTransferred")
    if "add_stake" in call_lower:
        return ("StakeAdded", "StakeTransferred")
    return ("StakeAdded", "StakeRemoved", "StakeTransferred")


def _is_missing_field(value):
    return value in (None, "", "-")


def _amount_to_float(value):
    if _is_missing_field(value):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_split_netuid(netuid):
    text = format_cell(netuid)
    if "->" not in text:
        return None, None
    origin, dest = text.split("->", 1)
    origin = origin.strip()
    dest = dest.strip()
    if not origin or not dest:
        return None, None
    return origin, dest


def _is_split_stake_call(call_lower):
    return any(
        token in call_lower
        for token in ("transfer_stake", "move_stake", "swap_stake")
    )


def _pick_largest_candidate(candidates, used_indices, event_id=None, netuid=None):
    best_i = None
    best_amt = -1.0
    for i, candidate in enumerate(candidates):
        if i in used_indices:
            continue
        if event_id is not None and candidate.get("event_id") != event_id:
            continue
        if netuid is not None and format_cell(candidate.get("netuid")) != netuid:
            continue
        amt = _amount_to_float(candidate.get("amount")) or 0.0
        if amt > best_amt:
            best_amt = amt
            best_i = i
    return best_i


def _pick_stake_event_candidate(candidates, call_lower, target_netuid, used_indices):
    """
    Match stake events to extrinsic rows.
    Swap/move/transfer emit fee-sized StakeRemoved before the main remove/add pair,
    so split-netuid rows must match origin/dest and prefer the largest amount.
    """
    preferred_ids = _preferred_event_ids_for_call(call_lower)
    origin, dest = _parse_split_netuid(target_netuid)
    is_split = _is_split_stake_call(call_lower) and origin is not None and dest is not None

    if is_split:
        picked_i = _pick_largest_candidate(
            candidates,
            used_indices,
            event_id="StakeTransferred",
            netuid=f"{origin}->{dest}",
        )
        if picked_i is not None:
            return picked_i

        for event_id, netuid in (("StakeRemoved", origin), ("StakeAdded", dest)):
            picked_i = _pick_largest_candidate(
                candidates,
                used_indices,
                event_id=event_id,
                netuid=netuid,
            )
            if picked_i is not None:
                return picked_i

    for event_id in preferred_ids:
        picked_i = _pick_largest_candidate(
            candidates,
            used_indices,
            event_id=event_id,
            netuid=target_netuid,
        )
        if picked_i is not None:
            return picked_i

    for event_id in preferred_ids:
        picked_i = _pick_largest_candidate(
            candidates,
            used_indices,
            event_id=event_id,
        )
        if picked_i is not None:
            return picked_i

    picked_i = _pick_largest_candidate(
        candidates,
        used_indices,
        netuid=target_netuid,
    )
    if picked_i is not None:
        return picked_i

    for i, _ in enumerate(candidates):
        if i not in used_indices:
            return i
    return None


def apply_exact_amounts_from_events(stake_rows, stake_event_amount_index):
    """
    For confirmed blocks, event amounts are source-of-truth.
    Override call-derived amounts with matched event amounts when possible.
    """
    used_by_idx = {idx: set() for idx in stake_event_amount_index.keys()}

    for row in stake_rows:
        call = str(row.get("call") or "")
        if "[event]" in call:
            continue
        idx = row.get("idx")
        if idx is None:
            continue
        candidates = stake_event_amount_index.get(idx, [])
        if not candidates:
            continue

        call_lower = call.lower()
        target_netuid = format_cell(row.get("netuid"))
        used_indices = used_by_idx.setdefault(idx, set())
        picked_i = _pick_stake_event_candidate(
            candidates,
            call_lower,
            target_netuid,
            used_indices,
        )

        if picked_i is not None:
            picked = candidates[picked_i]
            row["amount"] = picked.get("amount", row.get("amount"))
            if _is_missing_field(row.get("netuid")):
                picked_netuid = picked.get("netuid")
                if not _is_missing_field(picked_netuid):
                    row["netuid"] = picked_netuid
            used_indices.add(picked_i)


def enrich_missing_amounts_from_events(stake_rows, stake_event_amount_index):
    for row in stake_rows:
        amount = row.get("amount")
        if amount not in (None, "-", ""):
            continue

        idx = row.get("idx")
        candidates = stake_event_amount_index.get(idx, [])
        if not candidates:
            continue

        call = str(row.get("call") or "").lower()
        netuid = format_cell(row.get("netuid"))
        picked_i = _pick_stake_event_candidate(candidates, call, netuid, set())
        if picked_i is None:
            continue

        picked = candidates[picked_i]
        row["amount"] = picked.get("amount", amount)
        if _is_missing_field(row.get("netuid")):
            picked_netuid = picked.get("netuid")
            if not _is_missing_field(picked_netuid):
                row["netuid"] = picked_netuid


def apply_full_unstake_amount_hint(stake_rows):
    """
    Some calls intentionally omit explicit amount (full-exit semantics).
    If event-based enrichment could not recover a numeric value, show FULL hint.
    """
    for row in stake_rows:
        amount = row.get("amount")
        if amount not in (None, "-", ""):
            continue
        call = str(row.get("call") or "").lower()
        if any(
            token in call
            for token in (
                "remove_stake_full",
                "remove_stake_full_limit",
                "unstake_all",
                "unstake_all_alpha",
            )
        ):
            row["amount"] = "FULL"


FULL_UNSTAKE_CALL_FUNCTIONS = {
    "unstake_all",
    "unstake_all_alpha",
}

_SPLIT_STAKE_CALL_TOKENS = frozenset(
    {
        "transfer_stake",
        "transfer_stake_limit",
        "move_stake",
        "move_stake_limit",
        "swap_stake",
        "swap_stake_limit",
        "staketransferred",
        "stakemoved",
        "stakeswapped",
    }
)


def _row_display_signer(row):
    real = format_cell(row.get("real_address"))
    if real and real != "-":
        return real
    return format_cell(row.get("signer"))


def _stake_call_function_token(call_value):
    call = str(call_value or "").lower()
    if "." in call:
        call = call.rsplit(".", 1)[-1]
    return call.split(" ", 1)[0].strip()


def _get_stake_call_side(row):
    call = str(row.get("call") or "").lower()
    if "stakeremoved" in call or "remove_stake" in call or "unstake" in call:
        return "remove"
    if "stakeadded" in call or "add_stake" in call:
        return "add"
    if "remove" in call:
        return "remove"
    if "add" in call:
        return "add"
    return None


def _split_stake_dedup_key(row, normalized_call, netuid_value):
    return (
        format_cell(row.get("idx")),
        normalized_call,
        format_cell(netuid_value),
        format_cell(row.get("amount")),
        _row_display_signer(row),
    )


def attribute_ethereum_proxy_stake_added(stake_rows):
    """
    EVM routers often emit StakeAdded on a proxy coldkey while the user coldkey
    gets StakeRemoved(origin) + StakeTransferred(origin->dest). Attribute only
    that proxy StakeAdded to the user (not unwind legs that StakeAdded on user).
    """
    by_idx = {}
    for row in stake_rows:
        idx = row.get("idx")
        if idx is None:
            continue
        by_idx.setdefault(idx, []).append(row)

    for rows in by_idx.values():
        removed_keys = set()
        transfers = []
        for row in rows:
            call = str(row.get("call") or "").lower()
            coldkey = format_cell(row.get("real_address"))
            if _is_missing_field(coldkey):
                continue
            if "stakeremoved" in call and "[event]" in call:
                removed_keys.add((coldkey, format_cell(row.get("netuid"))))
            netuid = format_cell(row.get("netuid"))
            if "staketransferred" in call and "->" in netuid:
                origin, dest = _parse_split_netuid(netuid)
                if origin is None:
                    continue
                transfers.append(
                    (coldkey, origin, dest, format_cell(row.get("amount")))
                )

        if not transfers:
            continue

        for row in rows:
            call = str(row.get("call") or "").lower()
            if "stakeadded" not in call or "[event]" not in call:
                continue
            netuid = format_cell(row.get("netuid"))
            added_coldkey = format_cell(row.get("real_address"))
            for beneficiary, origin, dest, amount in transfers:
                if added_coldkey == beneficiary:
                    continue
                if netuid != dest:
                    continue
                if dest == "0":
                    continue
                if (beneficiary, origin) not in removed_keys:
                    continue
                row_amount = format_cell(row.get("amount"))
                if not _is_missing_field(amount) and row_amount not in ("-", amount):
                    continue
                row["real_address"] = beneficiary
                path = format_cell(row.get("path"))
                if path and path != "-":
                    row["path"] = f"{path} (proxy→user)"
                break


def expand_split_stake_rows(stake_rows):
    """Mirror BlockPanel.expandSplitStakeRows: origin->dest into remove + add rows."""
    if not stake_rows:
        return stake_rows

    passthrough = []
    synthetic = []
    split_derived_keys = set()

    for row in stake_rows:
        token = _stake_call_function_token(row.get("call"))
        netuid = format_cell(row.get("netuid"))
        if token not in _SPLIT_STAKE_CALL_TOKENS or "->" not in netuid:
            passthrough.append(row)
            continue

        origin, dest = _parse_split_netuid(netuid)
        if origin is None:
            continue

        origin_netuid = origin or "-"
        dest_netuid = dest or "-"
        synthetic.append({**row, "call": "remove_stake", "netuid": origin_netuid, "_splitOrder": 0})
        synthetic.append({**row, "call": "add_stake", "netuid": dest_netuid, "_splitOrder": 1})
        split_derived_keys.add(_split_stake_dedup_key(row, "remove_stake", origin_netuid))
        split_derived_keys.add(_split_stake_dedup_key(row, "add_stake", dest_netuid))

    filtered = []
    for row in passthrough:
        call = str(row.get("call") or "").lower()
        if "stakeremoved [event]" in call:
            normalized = "remove_stake"
            netuid = format_cell(row.get("netuid"))
        elif "stakeadded [event]" in call:
            normalized = "add_stake"
            netuid = format_cell(row.get("netuid"))
        else:
            filtered.append(row)
            continue
        if _split_stake_dedup_key(row, normalized, netuid) in split_derived_keys:
            continue
        filtered.append(row)

    return filtered + synthetic


def consolidate_stake_display_rows(stake_rows):
    """Mirror BlockPanel.consolidateBlockDisplayRows for backend/WebSocket rows."""
    if not stake_rows:
        return stake_rows

    passthrough = []
    groups = {}
    group_order = []

    for row in stake_rows:
        if row.get("_same_origin_destination"):
            continue

        netuid = format_cell(row.get("netuid"))
        if netuid == "-" or "->" in netuid:
            passthrough.append(row)
            continue

        side = _get_stake_call_side(row)
        amount = _amount_to_float(row.get("amount"))
        if side is None or amount is None:
            passthrough.append(row)
            continue

        key = (
            format_cell(row.get("idx")),
            _row_display_signer(row),
            netuid,
        )
        if key not in groups:
            groups[key] = {"template": row, "remove": 0.0, "add": 0.0}
            group_order.append(key)
        bucket = groups[key]
        if side == "remove":
            bucket["remove"] += amount
        else:
            bucket["add"] += amount

    consolidated = []
    for key in group_order:
        bucket = groups[key]
        net_remove = bucket["remove"] - bucket["add"]
        if abs(net_remove) < 1e-9:
            continue
        template = dict(bucket["template"])
        if net_remove > 0:
            template["call"] = "remove_stake"
            amt = net_remove
        else:
            template["call"] = "add_stake"
            amt = -net_remove
        formatted = format_tao(amt)
        template["amount"] = formatted
        template["amount_tao"] = formatted
        consolidated.append(template)

    return passthrough + consolidated


def _call_function_name(call_value):
    call = str(call_value or "").lower()
    if "." in call:
        return call.rsplit(".", 1)[-1]
    return call


def expand_full_unstake_rows(stake_rows, stake_event_amount_index):
    """
    unstake_all / unstake_all_alpha remove stake across many netuids inside a
    single extrinsic. The call itself has no netuid; the chain emits one
    StakeRemoved event per netuid. Expand to one table row per event.

    Batches emit one stake_row per inner call; expansion runs once per extrinsic
    idx so N calls × M events does not produce N×M duplicate rows.
    """
    if not stake_rows:
        return stake_rows

    remove_indices = set()
    extra_rows = []
    expanded_idxs = set()

    for i, row in enumerate(stake_rows):
        fn = _call_function_name(row.get("call"))
        if fn not in FULL_UNSTAKE_CALL_FUNCTIONS:
            continue
        idx = row.get("idx")
        if idx is None:
            continue

        if idx in expanded_idxs:
            remove_indices.add(i)
            continue

        removed_events = [
            c
            for c in stake_event_amount_index.get(idx, [])
            if c.get("event_id") == "StakeRemoved"
        ]
        if len(removed_events) <= 1:
            continue

        expanded_idxs.add(idx)
        remove_indices.add(i)
        for event in removed_events:
            extra_rows.append(
                {
                    **row,
                    "amount": event.get("amount", row.get("amount")),
                    "netuid": event.get("netuid", row.get("netuid")),
                }
            )

    if not remove_indices:
        return stake_rows

    return [row for i, row in enumerate(stake_rows) if i not in remove_indices] + extra_rows


def parse_block(sub, block_number, cached_alpha_prices_tao_by_netuid=None):
    block_hash = sub.get_block_hash(block_number)
    raw_block = sub.rpc_request("chain_getBlock", [block_hash])["result"]["block"]
    extrinsics = raw_block.get("extrinsics", [])
    events_scale = sub.query(module="System", storage_function="Events", block_hash=block_hash)
    events = getattr(events_scale, "value", events_scale) or []
    status_by_idx = build_extrinsic_status_map(events)
    force_batch_item_status_by_idx = build_force_batch_item_status(events)
    alpha_prices_tao_by_netuid = (
        cached_alpha_prices_tao_by_netuid
        if cached_alpha_prices_tao_by_netuid is not None
        else get_alpha_prices_tao_by_netuid(sub, block_hash)
    )

    stake_rows = []
    transfer_rows = []
    other_notification_rows = []
    extrinsic_meta = {}
    block_ts_ms = None

    decoded_by_idx = [decode_extrinsic(sub, ext_hex) for ext_hex in extrinsics]
    needed_netuids = netuids_needing_alpha_price_from_decoded(decoded_by_idx)
    alpha_prices_tao_by_netuid = enrich_alpha_prices_tao_by_netuid(
        alpha_prices_tao_by_netuid, needed_netuids, sub, block_hash
    )

    for idx, ext in enumerate(decoded_by_idx):
        signer = format_cell(_canonical_proxy_ss58(ext.get("signer")))
        ext_status = status_by_idx.get(idx, "unknown")

        extrinsic_meta[idx] = {
            "status": ext_status,
            "signer": ext.get("signer"),
            "hash": ext.get("hash"),
            "root_call": f"{ext.get('call_module')}.{ext.get('call_function')}",
        }

        if "decode_error" in ext:
            continue

        if block_ts_ms is None:
            block_ts_ms = extract_block_timestamp_ms(ext)

        other_notification_rows.extend(
            notification_rows_from_decoded_ext(
                ext,
                status=ext_status,
                block_number=int(block_number),
            )
        )

        # Per-item status only applies when every force_batch item is a stake
        # call (our own batches): then the k-th stake match lines up with the
        # k-th ItemCompleted/ItemFailed. For mixed/third-party batches the
        # alignment is ambiguous, so we fall back to the extrinsic-level status.
        ext_stake_matches = ext.get("stake_matches", [])
        fb_item_status = force_batch_item_status_by_idx.get(idx)
        fb_item_count = _force_batch_item_count(ext)
        per_item_status_ok = (
            fb_item_status is not None
            and fb_item_count is not None
            and len(ext_stake_matches) == fb_item_count == len(fb_item_status)
        )

        for item_index, match in enumerate(ext_stake_matches):
            values_by_name = call_args_to_dict(match.get("call_args"))
            function_name = str(match.get("function") or "").lower()
            amount_rao = get_first_value(
                values_by_name, stake_amount_arg_candidates(function_name)
            )
            limit_price_rao = get_first_value(
                values_by_name,
                ["limit_price", "price_limit", "max_price", "min_price"],
            )
            amount_tao = stake_amount_to_tao(
                amount_rao,
                function_name,
                values_by_name,
                alpha_prices_tao_by_netuid,
                limit_price_rao=limit_price_rao,
            )

            netuid = values_by_name.get("netuid")
            origin_netuid = to_int(
                values_by_name.get(
                    "origin_netuid",
                    values_by_name.get("src_netuid", values_by_name.get("netuid")),
                )
            )
            destination_netuid = to_int(
                values_by_name.get("destination_netuid", values_by_name.get("dest_netuid"))
            )
            if netuid is None:
                src = values_by_name.get("origin_netuid", values_by_name.get("src_netuid"))
                dst = values_by_name.get("destination_netuid", values_by_name.get("dest_netuid"))
                if src is not None or dst is not None:
                    netuid = f"{format_cell(src)}->{format_cell(dst)}"

            same_origin_destination = False
            if function_name in SPLIT_STAKE_FUNCTIONS:
                if (
                    origin_netuid is not None
                    and destination_netuid is not None
                    and int(origin_netuid) == int(destination_netuid)
                ):
                    same_origin_destination = True

            row_status = ext_status
            if per_item_status_ok:
                row_status = "success" if fb_item_status[item_index] else "failed"

            stake_row = {
                "idx": idx,
                "status": row_status,
                "call": f"{match.get('module')}.{match.get('function')}",
                "signer": signer,
                "real_address": _format_real_address_cell(match.get("real_address")),
                "amount": format_tao(amount_tao),
                "netuid": format_cell(netuid),
                "path": format_cell(match.get("path")),
            }
            if per_item_status_ok:
                stake_row["item_index"] = item_index
            if same_origin_destination:
                stake_row["_same_origin_destination"] = True
            stake_rows.append(stake_row)

        for match in ext.get("transfer_matches", []):
            values_by_name = call_args_to_dict(match.get("call_args"))
            amount_rao = values_by_name.get("value", values_by_name.get("amount"))
            transfer_rows.append(
                {
                    "idx": idx,
                    "status": ext_status,
                    "hash": format_cell(ext.get("hash")),
                    "call": f"{match.get('module')}.{match.get('function')}",
                    "signer": signer,
                    "real_address": format_cell(match.get("real_address")),
                    "to": format_cell(values_by_name.get("dest")),
                    "amount": format_tao(rao_to_tao(amount_rao)),
                    "path": format_cell(match.get("path")),
                }
            )
        for match in ext.get("stake_matches", []):
            function_name = (match.get("function") or "").lower()
            if function_name != "lock_stake":
                continue
            values_by_name = call_args_to_dict(match.get("call_args"))
            amount_rao = get_first_value(
                values_by_name, stake_amount_arg_candidates("lock_stake")
            )
            amount_tao = stake_amount_to_tao(
                amount_rao,
                "lock_stake",
                values_by_name,
                alpha_prices_tao_by_netuid,
            )
            transfer_rows.append(
                {
                    "idx": idx,
                    "status": ext_status,
                    "hash": format_cell(ext.get("hash")),
                    "call": f"{match.get('module')}.{match.get('function')}",
                    "signer": signer,
                    "real_address": format_cell(match.get("real_address")),
                    "to": format_cell(values_by_name.get("hotkey", values_by_name.get("dest"))),
                    "amount": format_tao(amount_tao),
                    "path": format_cell(match.get("path")),
                }
            )

    stake_event_amount_index = build_stake_event_amount_index(events)
    stake_rows = expand_full_unstake_rows(stake_rows, stake_event_amount_index)
    apply_exact_amounts_from_events(stake_rows, stake_event_amount_index)
    enrich_missing_amounts_from_events(stake_rows, stake_event_amount_index)
    apply_full_unstake_amount_hint(stake_rows)

    eth_stake_rows = extract_ethereum_stake_rows_from_events(events, extrinsic_meta)
    existing_stake_keys = {
        (
            r.get("idx"),
            r.get("call"),
            r.get("amount"),
            r.get("netuid"),
            r.get("real_address"),
        )
        for r in stake_rows
    }
    for r in eth_stake_rows:
        key = (
            r.get("idx"),
            r.get("call"),
            r.get("amount"),
            r.get("netuid"),
            r.get("real_address"),
        )
        if key not in existing_stake_keys:
            stake_rows.append(r)
            existing_stake_keys.add(key)

    attribute_ethereum_proxy_stake_added(stake_rows)
    stake_rows = consolidate_stake_display_rows(expand_split_stake_rows(stake_rows))
    annotate_mempool_proxy_fake(stake_rows, sub)

    eth_stake_transfer_chains = extract_ethereum_stake_transfer_chains(events, extrinsic_meta)

    return {
        "block_number": int(block_number),
        "block_hash": block_hash,
        "extrinsics_count": len(extrinsics),
        "events_total": len(events),
        "events_top": top_event_counts(events),
        "stake_rows": stake_rows,
        "transfer_rows": transfer_rows,
        "other_notification_rows": other_notification_rows,
        "ethereum_stake_transfer_chains": eth_stake_transfer_chains,
        # On-chain block production time (from the Timestamp.set inherent).
        "block_ts_ms": block_ts_ms,
        "block_ts": _ms_to_iso(block_ts_ms),
    }


def print_pretty(report):
    print(
        f"[{utc_now_iso()}] block={report['block_number']} "
        f"hash={report['block_hash']} extrinsics={report['extrinsics_count']} "
        f"events={report['events_total']}",
        flush=True,
    )

    if report["events_top"]:
        print("\n[Top events]", flush=True)
        for name, cnt in report["events_top"]:
            print(f"  - {name}: {cnt}", flush=True)

    if report["stake_rows"]:
        print("\n[Stake table]", flush=True)
        print(
            render_custom_table(
                report["stake_rows"],
                columns=["idx", "status", "call", "signer", "real_address", "amount", "netuid", "path"],
                max_width={
                    "idx": 6,
                    "status": 10,
                    "call": 40,
                    "signer": 48,
                    "real_address": 48,
                    "amount": 18,
                    "netuid": 16,
                    "path": 64,
                },
            ),
            flush=True,
        )

    if report["transfer_rows"]:
        print("\n[Balances transfer table]", flush=True)
        print(
            render_custom_table(
                report["transfer_rows"],
                columns=["idx", "status", "call", "signer", "real_address", "to", "amount", "path"],
                max_width={
                    "idx": 6,
                    "status": 10,
                    "call": 34,
                    "signer": 48,
                    "real_address": 48,
                    "to": 48,
                    "amount": 18,
                    "path": 64,
                },
            ),
            flush=True,
        )

    if report["start_call_rows"]:
        print("\n[SubtensorModule.start_call table]", flush=True)
        print(
            render_custom_table(
                report["start_call_rows"],
                columns=["idx", "status", "call", "signer", "real_address", "hotkey", "netuid", "path"],
                max_width={
                    "idx": 6,
                    "status": 10,
                    "call": 40,
                    "signer": 48,
                    "real_address": 48,
                    "hotkey": 48,
                    "netuid": 16,
                    "path": 64,
                },
            ),
            flush=True,
        )

    if report["ethereum_stake_transfer_chains"]:
        print("\n[Ethereum.transact stake transfer chains]", flush=True)
        print(
            render_custom_table(
                report["ethereum_stake_transfer_chains"],
                columns=["idx", "status", "signer", "flow", "details"],
                max_width={
                    "idx": 6,
                    "status": 10,
                    "signer": 48,
                    "flow": 72,
                    "details": 120,
                },
            ),
            flush=True,
        )

    if (
        not report["stake_rows"]
        and not report["transfer_rows"]
        and not report["start_call_rows"]
        and not report["ethereum_stake_transfer_chains"]
    ):
        print("\n(no matching stake/transfer/start_call extrinsics in this block)", flush=True)


def process_one_block(sub, block_number, output, current_only_screen):
    report = parse_block(sub, block_number)
    if current_only_screen and output == "pretty":
        clear_terminal()
    if output == "json":
        print(json.dumps(report, ensure_ascii=False), flush=True)
    else:
        print_pretty(report)


def oneshot(sub, last_n, output, current_only_screen):
    head_hash = sub.get_chain_head()
    head = int(sub.get_block_number(head_hash))
    start = max(0, head - last_n + 1)
    for bn in range(start, head + 1):
        process_one_block(sub, bn, output, current_only_screen=False)
        if output == "pretty":
            print("\n" + ("=" * 120) + "\n", flush=True)


def realtime(sub, poll_interval, output, current_only_screen):
    last_printed_block = None
    while RUNNING:
        try:
            # Print current head once immediately after (re)connect.
            head_hash = sub.get_chain_head()
            head = int(sub.get_block_number(head_hash))
            if last_printed_block is None or head != last_printed_block:
                process_one_block(sub, head, output, current_only_screen)
                last_printed_block = head

            def _on_block(obj, _update_nr, _subscription_id):
                nonlocal last_printed_block
                if not RUNNING:
                    return {"stop": True}
                header = obj.get("header") if isinstance(obj, dict) else None
                if not isinstance(header, dict):
                    return None
                bn = parse_block_number(header.get("number"))
                if bn is None:
                    return None
                if last_printed_block is not None and bn == last_printed_block:
                    return None
                # Head-only mode: process only the newest block seen via subscription.
                process_one_block(sub, bn, output, current_only_screen)
                last_printed_block = bn
                return None

            # Blocks until disconnected / error / stop.
            sub.subscribe_block_headers(_on_block)
        except Exception as exc:
            if output == "json":
                print(json.dumps({"ts": utc_now_iso(), "error": str(exc)}, ensure_ascii=False), flush=True)
            else:
                print(f"[{utc_now_iso()}] error={exc}", flush=True)
        # Reconnect backoff when subscription drops.
        if RUNNING:
            time.sleep(max(0.1, poll_interval))


def main():
    args = parse_args()
    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)

    sub = SubstrateInterface(
        url=args.endpoint,
        type_registry_preset="substrate-node-template",
    )
    sub.init_runtime()

    if args.oneshot:
        oneshot(sub, args.last_n, args.output, args.current_only_screen)
    else:
        realtime(sub, args.poll_interval, args.output, args.current_only_screen)


if __name__ == "__main__":
    main()
