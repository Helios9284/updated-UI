#!/usr/bin/env python3
"""Track a new subnet's lifecycle (register_network -> start_call) and, when
armed, fire a pre-signed add_stake the instant the target start_call is seen.

Run modes (--mode):
  full        (default) Phase 1 DISCOVER register_network (mempool + blocks) ->
              bind the new netuid from the NetworkAdded event -> Phase 2 TRACK
              start_call for that netuid -> (optionally fire) -> exit.
  start-call  Phase 2 only. With --netuid N: track that netuid (one-shot,
              fireable). Without --netuid: track ALL start_calls continuously.

In full --fire mode, only start_call extrinsics signed by TRACK_FIRE_SIGNER (.env)
trigger add_stake; proxy real address is also accepted.

Fastest submission (--fire): the moment the target netuid is known we cache the
target alpha price and PRE-SIGN leg1 add_stake (free TAO -> target netuid, immortal
era), caching a pre-serialized author_submitExtrinsic payload on a warm dedicated
websocket. On detection we read the netuid straight from the start_call hex tail
(no decode) and, on a match, send the cached payload -> one socket round-trip.

With --unstake we fire remove_stake_full_limit right after leg1 confirms, unstaking
the entire alpha balance on that netuid (no estimate / margin dust left behind).
"""
import argparse
import contextlib
import json
import multiprocessing
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import websocket
from substrateinterface import SubstrateInterface
from scalecodec import ScaleBytes
from bittensor_wallet import Wallet

from track_poll_schedule import init_poll_schedule, try_acquire_poll_slot


RUNNING = True
_MP_STOP_EVENT = None
SUBTENSOR_MODULE = "subtensormodule"
START_CALL_FUNCTION = "start_call"
REGISTER_NETWORK_FUNCTION = "register_network"
NETWORK_ADDED_EVENT = "networkadded"

# SubtensorModule call indexes: pallet 0x07.
START_CALL_INDEX_HEX = "075c"  # call 0x5c, arg netuid (u16, 2 bytes)
REGISTER_NETWORK_INDEX_HEX = "073b"  # call 0x3b, arg hotkey (AccountId, 32 bytes)

# How often (seconds) to poll the chain head while discovering register_network.
BLOCK_POLL_SEC = 0.5

# add_stake / remove_stake parameters (fired on a matching start_call) from .env:
#   TRACK_STAKE_HOTKEY      hotkey the stake sits under (falls back to HOTKEY_SS58)
#   TRACK_STAKE_AMOUNT_TAO  leg1 size: free TAO (rao) to add_stake on the target netuid
#   TRACK_STAKE_TIP_TAO     extrinsic tip for leg1/leg2 (mempool priority); TRACK_STAKE_TIP_RAO overrides
# Resolved at runtime (resolve_stake_config) AFTER load_env_file() has run.
DEFAULT_STAKE_AMOUNT_TAO = 0.1
# Return leg (leg2): remove_stake_full_limit after leg1 confirms (full alpha balance).
DEFAULT_LEG1_CONFIRM_POLL_MS = 100

ENV_PATH = Path(__file__).resolve().parent / ".env"


def resolve_stake_config():
    """Read hotkey + leg1 amount from env (after .env is loaded)."""
    hotkey = (os.getenv("TRACK_STAKE_HOTKEY") or os.getenv("HOTKEY_SS58") or "").strip()
    try:
        amount_tao = float(os.getenv("TRACK_STAKE_AMOUNT_TAO") or DEFAULT_STAKE_AMOUNT_TAO)
    except (TypeError, ValueError):
        amount_tao = DEFAULT_STAKE_AMOUNT_TAO
    return hotkey, amount_tao, int(amount_tao * 1_000_000_000)


def resolve_stake_tip_config():
    """Return (tip_tao, tip_rao) for presigned add_stake / remove_stake extrinsics."""
    raw_rao = (os.getenv("TRACK_STAKE_TIP_RAO") or "").strip()
    if raw_rao:
        try:
            tip_rao = max(0, int(raw_rao))
            return tip_rao / 1_000_000_000, tip_rao
        except (TypeError, ValueError):
            pass
    raw_tao = (os.getenv("TRACK_STAKE_TIP_TAO") or "").strip()
    if raw_tao:
        try:
            tip_tao = max(0.0, float(raw_tao))
            return tip_tao, int(tip_tao * 1_000_000_000)
        except (TypeError, ValueError):
            pass
    return 0.0, 0


def resolve_leg1_confirm_timeout_sec():
    """Optional wall-clock cap while waiting for leg1. None = wait until confirm/fail."""
    raw = (os.getenv("TRACK_LEG1_CONFIRM_TIMEOUT_SEC") or "").strip()
    if not raw:
        return None
    try:
        val = float(raw)
        if val <= 0:
            return None
        return val
    except (TypeError, ValueError):
        return None


def resolve_leg1_confirm_poll_ms():
    """Poll interval while waiting for leg1 inclusion (account nonce + block scan)."""
    raw = (os.getenv("TRACK_LEG1_CONFIRM_POLL_MS") or "").strip()
    if raw:
        try:
            return max(20, int(raw))
        except (TypeError, ValueError):
            pass
    return DEFAULT_LEG1_CONFIRM_POLL_MS


def resolve_fire_signer():
    """If set, --fire only when the detected start_call signer (or proxy real) matches."""
    return (os.getenv("TRACK_FIRE_SIGNER") or "").strip() or None


def resolve_wallet_password():
    """Return coldkey password when explicitly set; None for passwordless wallets."""
    for key in ("STARTCALLSUBMITWALLETPASSWORD", "STAKE_SIGNER_WALLET_PASSWORD"):
        raw = os.getenv(key)
        if raw is None:
            continue
        val = raw.strip()
        if val:
            return val
    return None


def fire_signer_from_call_row(row):
    """Signer used for start_call matching: proxy real if present, else extrinsic signer."""
    real = (row.get("real_address") or "").strip()
    if real:
        return real
    return (row.get("signer") or "").strip() or None


def read_env_var(key, path=ENV_PATH):
    """Read one key from .env (ignores os.environ)."""
    if not path.exists():
        return None
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return None


def write_env_var(key, value, path=ENV_PATH):
    """Update or append key=value in .env and sync os.environ."""
    value = str(value).strip()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{key}={value}\n", encoding="utf-8")
        os.environ[key] = value
        return
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    prefix = f"{key}="
    found = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if stripped.startswith(prefix):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        new_lines.append(f"{key}={value}\n")
    path.write_text("".join(new_lines), encoding="utf-8")
    os.environ[key] = value


def reload_fire_signer(path=ENV_PATH):
    """Re-read TRACK_FIRE_SIGNER from .env into os.environ."""
    val = read_env_var("TRACK_FIRE_SIGNER", path=path)
    if val is None:
        return resolve_fire_signer()
    os.environ["TRACK_FIRE_SIGNER"] = val
    return val.strip() or None


def register_signer_from_block(substrate, hit):
    """Decode register_network signer from an included block hit."""
    xidx = hit.get("extrinsic_idx")
    block = hit.get("block")
    if xidx is None or block is None:
        return None
    try:
        block_hash = substrate.get_block_hash(int(block))
        ext_hexes = block_extrinsic_hexes(substrate, block_hash)
        if xidx >= len(ext_hexes):
            return None
        rows = build_call_rows(
            substrate, ext_hexes[xidx], REGISTER_NETWORK_FUNCTION, "register_included"
        )
        for row in rows:
            addr = fire_signer_from_call_row(row)
            if addr:
                return addr
    except Exception:
        pass
    return None


def latch_register_fire_signer(current, row, *, output):
    """Latch the first register_network signer for full-mode --fire."""
    if current:
        return current
    addr = fire_signer_from_call_row(row)
    if not addr:
        return current
    write_env_var("TRACK_FIRE_SIGNER", addr)
    print(
        f"[fast] latched register_network signer={addr} -> TRACK_FIRE_SIGNER",
        flush=True,
    )
    return addr


def resolve_poll_process_count(args=None):
    if args is not None and getattr(args, "poll_process_count", None) is not None:
        return max(1, int(args.poll_process_count))
    try:
        return max(1, int((os.getenv("TRACK_POLL_PROCESS_COUNT") or "1").strip()))
    except (TypeError, ValueError):
        return 1


def resolve_poll_interval_ms(args=None):
    if args is not None and getattr(args, "poll_interval_ms", None) is not None:
        return max(1, int(args.poll_interval_ms))
    try:
        return max(1, int((os.getenv("TRACK_POLL_INTERVAL_MS") or "40").strip()))
    except (TypeError, ValueError):
        return 40


def resolve_poll_interval_sec():
    """Mempool poll sleep between loops (seconds). 0 = max speed."""
    raw = (os.getenv("TRACK_POLL_INTERVAL_SEC") or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def resolve_poll_retry_sleep_ms():
    try:
        return max(0, int((os.getenv("TRACK_POLL_RETRY_SLEEP_MS") or "1").strip()))
    except (TypeError, ValueError):
        return 1


def resolve_poll_schedule_path():
    raw = (os.getenv("TRACK_POLL_SCHEDULE_PATH") or "").strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent / "data" / "track_poll_schedule.json"


def armed_pattern_for_netuid(target_netuid):
    netuid_le = int(target_netuid).to_bytes(2, "little").hex()
    return f'{START_CALL_INDEX_HEX}{netuid_le}"'


@dataclass(frozen=True)
class TriggerJob:
    ext_hex: str
    poller_id: int


def fetch_alpha_price(substrate, netuid):
    """Spot alpha price (TAO per alpha) = SubnetTAO / SubnetAlphaIn. Both are plain
    u64 rao so the ratio is TAO/alpha. Returns float or None."""
    try:
        tao = int(substrate.query("SubtensorModule", "SubnetTAO", [int(netuid)]).value or 0)
        alpha_in = int(substrate.query("SubtensorModule", "SubnetAlphaIn", [int(netuid)]).value or 0)
    except Exception:
        return None
    if alpha_in <= 0:
        return None
    return tao / alpha_in


def _alpha_storage_to_int(val):
    if val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, dict):
        if "bits" in val:
            return int(val["bits"])
        mantissa = val.get("mantissa")
        exponent = val.get("exponent")
        if mantissa is not None and exponent is not None:
            m, e = int(mantissa), int(exponent)
            if e >= 0:
                return int(m * (10**e))
            return int(m // (10 ** (-e)))
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def fetch_alpha_stake_rao(substrate, hotkey, coldkey, netuid):
    """On-chain alpha stake for (hotkey, coldkey, netuid); checks AlphaV2 then Alpha."""
    for storage in ("AlphaV2", "Alpha"):
        try:
            result = substrate.query("SubtensorModule", storage, [hotkey, coldkey, int(netuid)])
            amount = _alpha_storage_to_int(result.value if hasattr(result, "value") else result)
            if amount and amount > 0:
                return amount
        except Exception:
            continue
    return None


def load_env_file(path=ENV_PATH):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if key and key not in os.environ:
            os.environ[key] = val


def hex_looks_like_start_call(extrinsic_hex):
    """Cheap hex-only pre-filter for a direct (unwrapped) start_call.

    A signed extrinsic ends with its call, and start_call's only arg (netuid)
    is a fixed 2-byte u16, so the hex ends with `075c<netuid LE>` (8 hex chars).
    Counting from the end avoids the variable-length nonce/tip/signature.
    NOTE: only matches the direct form; proxy/batch-wrapped calls won't match.
    """
    h = extrinsic_hex[2:] if extrinsic_hex.startswith("0x") else extrinsic_hex
    h = h.lower()
    return len(h) >= 8 and h[-8:-4] == START_CALL_INDEX_HEX


def start_call_netuid_from_hex(extrinsic_hex):
    """Read a direct start_call's netuid straight from the last 2 bytes (u16 LE)."""
    h = extrinsic_hex[2:] if extrinsic_hex.startswith("0x") else extrinsic_hex
    try:
        return int.from_bytes(bytes.fromhex(h[-4:]), "little")
    except Exception:
        return None


def hex_looks_like_register_network(extrinsic_hex):
    """Cheap hex-only pre-filter for a direct (unwrapped) register_network.

    register_network's only arg is a fixed 32-byte AccountId (hotkey), so the
    hex ends with `073b<32-byte hotkey>` (68 hex chars). Same end-anchored idea.
    """
    h = extrinsic_hex[2:] if extrinsic_hex.startswith("0x") else extrinsic_hex
    h = h.lower()
    return len(h) >= 68 and h[-68:-64] == REGISTER_NETWORK_INDEX_HEX


def handle_stop_signal(signum, frame):
    del signum, frame
    global RUNNING
    RUNNING = False
    ev = _MP_STOP_EVENT
    if ev is not None:
        ev.set()


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_endpoint():
    for key in ("START_CALL_ENDPOINT", "STAKE_CHAIN_ENDPOINT", "RPC_URL"):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return "ws://127.0.0.1:9944"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Track register_network -> start_call and optionally fire add_stake"
    )
    parser.add_argument(
        "--mode",
        choices=("full", "start-call"),
        default="full",
        help="full: register_network discover -> start_call. start-call: phase 2 only.",
    )
    parser.add_argument(
        "--netuid",
        type=int,
        default=None,
        help="start-call mode: track this netuid (one-shot). Omit to track all start_calls.",
    )
    parser.add_argument(
        "--fire",
        action="store_true",
        help="Arm auto-submit: pre-sign and fire leg1 add_stake (free TAO -> target "
        "netuid; hotkey/size/tip from TRACK_STAKE_HOTKEY / TRACK_STAKE_AMOUNT_TAO / "
        "TRACK_STAKE_TIP_TAO in .env) on a matching start_call. Requires a known target netuid.",
    )
    parser.add_argument(
        "--unstake",
        action="store_true",
        help="After leg1 confirms, fire remove_stake_full_limit (entire alpha on that "
        "netuid). Requires --fire.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="After firing, trace which block leg1 / leg2 add_stake / remove_stake "
        "landed in (block #, xidx, nonce, dispatch result, stake events). Post-submit only.",
    )
    parser.add_argument("--endpoint", default=default_endpoint(), help="WebSocket RPC endpoint")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Mempool polling interval in seconds. 0 = no sleep (max speed). "
        "Default: TRACK_POLL_INTERVAL_SEC or 0. Used in discover/track loops and "
        "single-process --fire armed mode.",
    )
    parser.add_argument(
        "--poll-process-count",
        type=int,
        default=None,
        help="Multi-process armed poll workers (default: TRACK_POLL_PROCESS_COUNT or 1). "
        "Values <= 1 use the legacy single-process fast path (recommended for remote RPC).",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=int,
        default=None,
        help="Shared poll cycle in ms for multi-process mode (default: TRACK_POLL_INTERVAL_MS or 40).",
    )
    parser.add_argument(
        "--output",
        choices=("pretty", "json"),
        default="pretty",
        help="Output format per detected event",
    )
    return parser.parse_args()


def new_substrate(endpoint):
    sub = SubstrateInterface(url=endpoint, type_registry_preset="substrate-node-template")
    # Required so substrate.metadata is populated; without it every decode()
    # raises (metadata is None) and the silent except swallows all matches.
    sub.init_runtime()
    return sub


def extract_calls_recursive(obj, path=None, proxy_real=None):
    if path is None:
        path = []
    calls = []
    if isinstance(obj, dict):
        if "call_module" in obj and "call_function" in obj:
            module = obj.get("call_module")
            function = obj.get("call_function")
            current_path = path + [f"{module}.{function}"]
            current_proxy_real = proxy_real
            call_args = obj.get("call_args") or []
            if (module or "").lower() == "proxy" and (function or "").lower() == "proxy":
                for arg in call_args:
                    if isinstance(arg, dict) and arg.get("name") == "real":
                        current_proxy_real = arg.get("value")
                        break
            calls.append(
                {
                    "module": module,
                    "function": function,
                    "call_args": call_args,
                    "real_address": current_proxy_real,
                }
            )
            for arg in call_args:
                if isinstance(arg, dict) and "value" in arg:
                    calls.extend(
                        extract_calls_recursive(arg["value"], current_path, current_proxy_real)
                    )
            return calls
        for value in obj.values():
            calls.extend(extract_calls_recursive(value, path, proxy_real))
        return calls
    if isinstance(obj, list):
        for item in obj:
            calls.extend(extract_calls_recursive(item, path, proxy_real))
    return calls


def call_args_to_dict(call_args):
    out = {}
    for arg in call_args or []:
        if isinstance(arg, dict) and "name" in arg:
            out[str(arg["name"])] = arg.get("value")
    return out


def get_first_value(values_by_name, candidates):
    for key in candidates:
        if key in values_by_name and values_by_name[key] is not None:
            return values_by_name[key]
    return None


def decode_extrinsic(substrate, extrinsic_hex):
    """Decode one SCALE extrinsic hex -> (value_dict, ext_hash) or (None, None)."""
    try:
        extrinsic = substrate.runtime_config.create_scale_object(
            "Extrinsic", metadata=substrate.metadata
        )
        extrinsic.decode(
            ScaleBytes(extrinsic_hex),
            check_remaining=substrate.config.get("strict_scale_decode"),
        )
        value = extrinsic.value or {}
        raw_hash = getattr(extrinsic, "extrinsic_hash", None)
        if isinstance(raw_hash, (bytes, bytearray)):
            ext_hash = "0x" + bytes(raw_hash).hex()
        else:
            ext_hash = str(raw_hash or "")
        return value, ext_hash
    except Exception:
        return None, None


def find_subtensor_calls(value, function):
    """Return matching SubtensorModule.<function> calls within a decoded extrinsic."""
    call = (value or {}).get("call") or {}
    return [
        c
        for c in extract_calls_recursive(call)
        if (c.get("module") or "").lower() == SUBTENSOR_MODULE
        and (c.get("function") or "").lower() == function
    ]


def build_call_rows(substrate, extrinsic_hex, function, kind):
    """Decode an extrinsic and build report rows for SubtensorModule.<function>."""
    value, ext_hash = decode_extrinsic(substrate, extrinsic_hex)
    if value is None:
        return []
    rows = []
    for match in find_subtensor_calls(value, function):
        args = call_args_to_dict(match.get("call_args"))
        rows.append(
            {
                "kind": kind,
                "ts": utc_now_iso(),
                "hash": ext_hash,
                "signer": value.get("address"),
                "real_address": match.get("real_address"),
                "netuid": get_first_value(args, ["netuid"]),
                "hotkey": get_first_value(args, ["hotkey"]),
                "nonce": value.get("nonce"),
                "tip": value.get("tip"),
            }
        )
    return rows


def _attr_netuid(attributes):
    if isinstance(attributes, (list, tuple)) and attributes:
        return attributes[0]
    if isinstance(attributes, dict):
        if "netuid" in attributes:
            return attributes["netuid"]
        return next(iter(attributes.values()), None)
    return attributes


def block_extrinsic_hexes(substrate, block_hash):
    try:
        res = substrate.rpc_request("chain_getBlock", [block_hash]).get("result") or {}
        return ((res.get("block") or {}).get("extrinsics")) or []
    except Exception:
        return []


def extrinsic_has_subtensor_call(substrate, extrinsic_hex, function):
    value, _ = decode_extrinsic(substrate, extrinsic_hex)
    if value is None:
        return False
    return bool(find_subtensor_calls(value, function))


def find_registered_netuid_in_block(substrate, block_number):
    """If this block registered a subnet via a *plain* register_network, return
    {"netuid", "block", "extrinsic_idx"}; else None.

    Uses the NetworkAdded event for the netuid (the authoritative source), then
    confirms the triggering extrinsic is register_network (not the _with_identity
    variant or some other path)."""
    try:
        block_hash = substrate.get_block_hash(block_number)
        events = substrate.get_events(block_hash)
    except Exception:
        return None

    candidates = []
    for ev in events:
        v = ev.value if hasattr(ev, "value") else ev
        if (v.get("module_id") or "").lower() == SUBTENSOR_MODULE and (
            v.get("event_id") or ""
        ).lower() == NETWORK_ADDED_EVENT:
            candidates.append((v.get("extrinsic_idx"), _attr_netuid(v.get("attributes"))))
    if not candidates:
        return None

    ext_hexes = block_extrinsic_hexes(substrate, block_hash)
    for xidx, netuid in candidates:
        if xidx is None or xidx >= len(ext_hexes):
            continue
        if extrinsic_has_subtensor_call(substrate, ext_hexes[xidx], REGISTER_NETWORK_FUNCTION):
            return {"netuid": netuid, "block": block_number, "extrinsic_idx": xidx}
    return None


def print_event(row, output):
    if output == "json":
        print(json.dumps(row, ensure_ascii=False, default=str), flush=True)
        return
    kind = row.get("kind")
    ts = row.get("ts")
    if kind == "register_pending":
        print(
            f"[{ts}] register_network PENDING signer={row.get('signer') or '-'} "
            f"hotkey={row.get('hotkey')} hash={row.get('hash')}",
            flush=True,
        )
    elif kind == "network_added":
        nu = row.get("netuid")
        print(
            f"[{ts}] register_network INCLUDED netuid={nu} block=#{row.get('block')} "
            f"xidx={row.get('extrinsic_idx')} -> now tracking start_call for netuid={nu}",
            flush=True,
        )
    elif kind == "start_call":
        signer = row.get("signer") or "-"
        real = row.get("real_address")
        via = f" via proxy {real}" if real and real != signer else ""
        tag = " (MATCH target netuid)" if row.get("matched") else ""
        print(
            f"[{ts}] start_call netuid={row.get('netuid')} signer={signer}{via} "
            f"hotkey={row.get('hotkey')} hash={row.get('hash')}{tag}",
            flush=True,
        )
    else:
        print(f"[{ts}] {json.dumps(row, ensure_ascii=False, default=str)}", flush=True)


class Firer:
    """Pre-signs an add_stake / remove_stake round-trip (leg1 free TAO -> target,
    leg2 target -> free TAO) and fires each leg via a warm dedicated websocket."""

    def __init__(self, substrate, endpoint, output, do_unstake=False, debug=False):
        self.substrate = substrate
        self.endpoint = endpoint
        self.output = output
        self.keypair = None
        self.netuid = None
        self.alpha_price = None
        self.payload = None  # pre-serialized leg1 add_stake
        self.do_unstake = bool(do_unstake)  # whether to also fire the return leg2
        self.debug = bool(debug)
        self.leg1_confirm_timeout_sec = resolve_leg1_confirm_timeout_sec()
        self.leg1_confirm_poll_ms = resolve_leg1_confirm_poll_ms()
        self.leg1_nonce = None
        self.leg2_nonce = None
        self.leg1_tx_hash = None
        self.leg2_tx_hash = None
        self.ws = None
        self.stake_hotkey, self.stake_amount_tao, self.stake_amount_rao = resolve_stake_config()
        self.stake_tip_tao, self.stake_tip_rao = resolve_stake_tip_config()

    def load_signer(self):
        # Dedicated start_call submit wallet, falling back to the main signer.
        wallet_name = (
            os.getenv("STARTCALLSUBMITWALLET") or os.getenv("STAKE_SIGNER_WALLET_NAME") or ""
        ).strip()
        password = resolve_wallet_password()
        wallets_dir = (os.getenv("BT_WALLETS_DIR") or "/root/.bittensor/wallets").strip()
        if not wallet_name:
            raise RuntimeError(
                "STARTCALLSUBMITWALLET (or STAKE_SIGNER_WALLET_NAME) is required in env/.env to --fire"
            )
        if not self.stake_hotkey:
            raise RuntimeError(
                "TRACK_STAKE_HOTKEY (or HOTKEY_SS58) is required in env or .env to --fire"
            )
        wallet = Wallet(name=wallet_name, path=wallets_dir)
        is_encrypted = getattr(wallet.coldkey_file, "is_encrypted", lambda: False)()
        if is_encrypted:
            if not password:
                raise RuntimeError(
                    "Wallet coldkey is encrypted. Set STARTCALLSUBMITWALLETPASSWORD in .env "
                    "(or STAKE_SIGNER_WALLET_PASSWORD)."
                )
            self.keypair = wallet.get_coldkey(password=password)
        else:
            self.keypair = wallet.get_coldkey()
        print(
            f"[fire] wallet='{wallet_name}' signer={self.keypair.ss58_address} "
            f"hotkey={self.stake_hotkey} leg1={self.stake_amount_tao} TAO (free balance)"
            + (f" tip={self.stake_tip_tao} TAO" if self.stake_tip_rao > 0 else ""),
            flush=True,
        )

    def warm_socket(self):
        try:
            self.ws = websocket.create_connection(self.endpoint)
        except Exception as exc:
            self.ws = None
            print(f"[fire] warm socket failed: {exc}", flush=True)

    def _submit_payload(self, call, nonce):
        """Sign one call (immortal era) and return the JSON-RPC submit payload."""
        extrinsic = self.substrate.create_signed_extrinsic(
            call=call,
            keypair=self.keypair,
            nonce=nonce,
            tip=self.stake_tip_rao,
        )
        raw = str(extrinsic.data)
        return json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "author_submitExtrinsic", "params": [raw]}
        )

    def presign(self, netuid):
        """Compose+sign leg1 add_stake first (hot path), then optional leg2 presign."""
        self.netuid = int(netuid)
        nonce = self.substrate.get_account_nonce(self.keypair.ss58_address)
        self.leg1_nonce = nonce

        leg1 = self.substrate.compose_call(
            call_module="SubtensorModule",
            call_function="add_stake",
            call_params={
                "hotkey": self.stake_hotkey,
                "netuid": int(netuid),
                "amount_staked": self.stake_amount_rao,
            },
        )
        self.payload = self._submit_payload(leg1, nonce)
        self.warm_socket()
        print(
            f"[fire] ARMED add_stake SN{netuid} hotkey={self.stake_hotkey} "
            f"amount={self.stake_amount_rao} rao (~{self.stake_amount_tao} TAO free) "
            f"nonce={nonce}"
            + (f" tip={self.stake_tip_rao} rao" if self.stake_tip_rao > 0 else ""),
            flush=True,
        )

        if self.do_unstake:
            print(
                f"[fire] leg2 remove_stake_full_limit on leg1 confirm SN{netuid} "
                f"(full alpha balance)",
                flush=True,
            )

    def _send(self, payload):
        """Send one cached payload on the warm socket. Returns (resp, latency_ms)."""
        started = time.perf_counter()
        for attempt in range(2):
            try:
                if self.ws is None:
                    self.warm_socket()
                self.ws.send(payload)
                resp = self.ws.recv()
                latency = round((time.perf_counter() - started) * 1000.0, 3)
                return resp, latency
            except Exception as exc:
                self.ws = None
                if attempt == 0:
                    continue
                print(f"[fire] submit failed: {exc}", flush=True)
                return None, None
        return None, None

    def fire_leg1(self):
        """Send the pre-signed leg1 add_stake. Returns latency ms."""
        if not self.payload:
            return None
        resp, latency = self._send(self.payload)
        if resp is None:
            return None
        self.leg1_tx_hash = self._report("add_stake", resp, latency)
        return latency

    def fire_leg2(self):
        """After leg1 confirm: sign+send remove_stake_full_limit for entire alpha balance."""
        if not self.do_unstake or self.netuid is None:
            return None
        coldkey = self.keypair.ss58_address
        alpha_before = fetch_alpha_stake_rao(
            self.substrate, self.stake_hotkey, coldkey, self.netuid
        )
        nonce = self.substrate.get_account_nonce(coldkey)
        self.leg2_nonce = nonce
        call_params = {
            "hotkey": self.stake_hotkey,
            "netuid": int(self.netuid),
            "limit_price": None,
        }
        try:
            leg2 = self.substrate.compose_call(
                call_module="SubtensorModule",
                call_function="remove_stake_full_limit",
                call_params=call_params,
            )
            action = "remove_stake_full_limit"
        except Exception as exc:
            if not alpha_before:
                print(f"[fire] leg2 compose failed and no alpha balance: {exc}", flush=True)
                return None
            print(
                f"[fire] remove_stake_full_limit unavailable ({exc}); "
                f"using remove_stake alpha={alpha_before}",
                flush=True,
            )
            leg2 = self.substrate.compose_call(
                call_module="SubtensorModule",
                call_function="remove_stake",
                call_params={
                    "hotkey": self.stake_hotkey,
                    "netuid": int(self.netuid),
                    "amount_unstaked": int(alpha_before),
                },
            )
            action = "remove_stake"
        payload = self._submit_payload(leg2, nonce)
        print(
            f"[fire] firing {action} SN{self.netuid} hotkey={self.stake_hotkey} "
            f"alpha_before={alpha_before} nonce={nonce}",
            flush=True,
        )
        resp, latency = self._send(payload)
        if resp is None:
            return None
        self.leg2_tx_hash = self._report(action, resp, latency)
        return latency

    def _report(self, action, resp, latency):
        """Print the submit result and return the tx hash (or None on error)."""
        result = None
        error = None
        try:
            data = json.loads(resp)
            result = data.get("result")
            error = data.get("error")
        except Exception:
            result = resp
        if error:
            print(f"[fire] SENT {action} netuid={self.netuid} latency={latency}ms error={error}", flush=True)
            return None
        print(
            f"[fire] SENT {action} netuid={self.netuid} latency={latency}ms tx_hash={result}",
            flush=True,
        )
        return result

    def trace_inclusion(self, base_block, timeout_sec=60):
        """DEBUG: scan forward from base_block to find which block each fired
        add_stake / remove_stake landed in (disambiguated by nonce), plus its
        dispatch result + stake events. Runs AFTER submission, so it never
        delays the hot path."""
        signer = self.keypair.ss58_address
        want = {self.leg1_nonce: "add_stake"}
        tx_by_nonce = {self.leg1_nonce: self.leg1_tx_hash}
        if self.do_unstake and self.leg2_nonce is not None:
            want[self.leg2_nonce] = "remove_stake"
            tx_by_nonce[self.leg2_nonce] = self.leg2_tx_hash
        found = {}
        if base_block is None:
            try:
                base_block = self.substrate.get_block_number(self.substrate.get_chain_head()) - 3
            except Exception:
                return
        next_block = max(0, int(base_block))
        print(
            f"[debug] tracing inclusion for {sorted(want.values())} from block "
            f"#{next_block} (signer={signer})",
            flush=True,
        )
        deadline = time.time() + timeout_sec
        while RUNNING and (set(want) - set(found)) and time.time() < deadline:
            try:
                head = self.substrate.get_block_number(self.substrate.get_chain_head())
            except Exception:
                time.sleep(0.3)
                continue
            while next_block <= head and (set(want) - set(found)):
                self._scan_block(next_block, signer, want, found, tx_by_nonce)
                next_block += 1
            if set(want) - set(found):
                time.sleep(0.3)
        for nonce in sorted(set(want) - set(found)):
            print(
                f"[debug] {want[nonce]} (nonce {nonce}): NOT included within {timeout_sec}s",
                flush=True,
            )

    def _scan_block(self, n, signer, want, found, tx_by_nonce):
        try:
            block_hash = self.substrate.get_block_hash(n)
            block = self.substrate.get_block(block_hash=block_hash)
            events = self.substrate.get_events(block_hash)
        except Exception:
            return
        sys_by_idx = {}
        sub_by_idx = {}
        for ev in events:
            v = ev.value if hasattr(ev, "value") else ev
            xi = v.get("extrinsic_idx")
            mod = (v.get("module_id") or "").lower()
            eid = v.get("event_id") or ""
            if mod == "system" and eid in ("ExtrinsicSuccess", "ExtrinsicFailed"):
                sys_by_idx[xi] = eid
            elif mod == SUBTENSOR_MODULE and eid in (
                "StakeAdded",
                "StakeRemoved",
                "StakeTransferred",
            ):
                sub_by_idx.setdefault(xi, []).append((eid, v.get("attributes")))
        for idx, ext in enumerate(block["extrinsics"]):
            xv = ext.value
            if xv.get("address") != signer:
                continue
            nonce = xv.get("nonce")
            if nonce in want and nonce not in found:
                label = want[nonce]
                found[nonce] = n
                expected = tx_by_nonce.get(nonce)
                actual = None
                raw_hash = getattr(ext, "extrinsic_hash", None)
                if isinstance(raw_hash, (bytes, bytearray)):
                    actual = "0x" + bytes(raw_hash).hex()
                match = " hash=OK" if (expected and actual == expected) else ""
                fn = (xv.get("call") or {}).get("call_function")
                print(
                    f"[debug] {label} INCLUDED block=#{n} xidx={idx} nonce={nonce} "
                    f"fn={fn} result={sys_by_idx.get(idx)} "
                    f"stake_events={sub_by_idx.get(idx)}{match}",
                    flush=True,
                )

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


def _extrinsic_hex_at_pattern(text, pattern, start=0):
    """Return (extrinsic_hex, next_search_pos) anchored at pattern, or (None, -1)."""
    idx = text.find(pattern, start)
    if idx == -1:
        return None, -1
    end = idx + len(pattern) - 1
    open_q = text.rfind('"0x', 0, idx)
    if open_q == -1:
        return None, idx + 1
    return text[open_q + 1 : end], idx + 1


def _start_call_signer_matches(substrate, ext_hex, required_signer):
    """True if required_signer matches signer / proxy real on start_call."""
    if not required_signer or not ext_hex:
        return False
    rows = build_call_rows(substrate, ext_hex, START_CALL_FUNCTION, "start_call")
    if not rows:
        return False
    row = rows[0]
    return required_signer in (row.get("signer"), row.get("real_address"))


def find_fireable_start_call_hex(substrate, text, pattern, required_signer):
    """Among mempool hits for pattern, return the first start_call whose signer matches."""
    pos = 0
    while True:
        ext_hex, next_pos = _extrinsic_hex_at_pattern(text, pattern, pos)
        if ext_hex is None:
            return None
        if _start_call_signer_matches(substrate, ext_hex, required_signer):
            return ext_hex
        pos = next_pos


def _log_matched_from_text(substrate, text, pattern, output, ext_hex=None):
    """Best-effort: log signer/hash for the matched start_call. Runs AFTER firing."""
    try:
        if not ext_hex:
            ext_hex, _ = _extrinsic_hex_at_pattern(text, pattern)
        if ext_hex:
            rows = build_call_rows(substrate, ext_hex, START_CALL_FUNCTION, "start_call")
            if rows:
                for row in rows:
                    row["matched"] = True
                    print_event(row, output)
                return
    except Exception:
        pass
    print(f"[{utc_now_iso()}] start_call netuid match fired (raw)", flush=True)


def _leg1_inclusion_result(substrate, block_number, signer, nonce):
    """Return block_number if leg1 nonce succeeded, -1 if failed, else None."""
    try:
        block_hash = substrate.get_block_hash(block_number)
        block = substrate.get_block(block_hash=block_hash)
        events = substrate.get_events(block_hash)
    except Exception:
        return None

    success_by_idx = {}
    failed_by_idx = {}
    for ev in events:
        v = ev.value if hasattr(ev, "value") else ev
        xi = v.get("extrinsic_idx")
        if (v.get("module_id") or "").lower() != "system":
            continue
        eid = v.get("event_id") or ""
        if eid == "ExtrinsicSuccess":
            success_by_idx[xi] = True
        elif eid == "ExtrinsicFailed":
            failed_by_idx[xi] = True

    for idx, ext in enumerate(block["extrinsics"]):
        xv = ext.value
        if xv.get("address") != signer or xv.get("nonce") != nonce:
            continue
        if success_by_idx.get(idx):
            return block_number
        if failed_by_idx.get(idx):
            return -1
    return None


def wait_for_leg1_confirmed(firer, substrate, start_block=None, timeout_sec=None):
    """Wait until leg1 ExtrinsicSuccess (or ExtrinsicFailed). No block-time assumption.

    Polls account nonce + new blocks; fires remove_stake caller on success.
    Waits until confirm/fail unless timeout_sec (or TRACK_LEG1_CONFIRM_TIMEOUT_SEC) is set.
    """
    if firer.leg1_nonce is None:
        return None
    signer = firer.keypair.ss58_address
    nonce = firer.leg1_nonce
    if timeout_sec is None:
        timeout_sec = firer.leg1_confirm_timeout_sec
    poll_sec = max(0.02, firer.leg1_confirm_poll_ms / 1000.0)
    if start_block is None:
        try:
            start_block = substrate.get_block_number(substrate.get_chain_head())
        except Exception:
            start_block = 0
    scan_from = max(0, int(start_block))
    if timeout_sec:
        print(
            f"[fire] waiting for leg1 confirm from block #{scan_from} "
            f"(optional cap={timeout_sec}s, poll={firer.leg1_confirm_poll_ms}ms) ...",
            flush=True,
        )
    else:
        print(
            f"[fire] waiting for leg1 confirm from block #{scan_from} "
            f"(until ExtrinsicSuccess/Fail, poll={firer.leg1_confirm_poll_ms}ms) ...",
            flush=True,
        )
    deadline = (time.time() + timeout_sec) if timeout_sec else None
    last_log = 0.0
    next_block = scan_from
    head = scan_from
    account_nonce = nonce
    while RUNNING and (deadline is None or time.time() < deadline):
        try:
            account_nonce = substrate.get_account_nonce(signer)
            head = substrate.get_block_number(substrate.get_chain_head())
        except Exception:
            time.sleep(poll_sec)
            continue

        if account_nonce > nonce and next_block > head:
            next_block = scan_from

        while next_block <= head and RUNNING:
            result = _leg1_inclusion_result(substrate, next_block, signer, nonce)
            if result == -1:
                print(
                    f"[fire] leg1 FAILED block=#{next_block} nonce={nonce}; "
                    "skipping remove_stake",
                    flush=True,
                )
                return -1
            if result is not None:
                print(
                    f"[fire] leg1 CONFIRMED block=#{result} nonce={nonce}",
                    flush=True,
                )
                return result
            next_block += 1

        now = time.time()
        if now - last_log >= 30.0:
            print(
                f"[fire] leg1 still waiting nonce={nonce} head=#{head} "
                f"account_nonce={account_nonce} scan_next=#{next_block} "
                f"({'mempool/unincluded' if account_nonce == nonce else 'included, scanning'})",
                flush=True,
            )
            last_log = now

        time.sleep(poll_sec)

    if deadline is not None:
        print(
            f"[fire] leg1 NOT confirmed within {timeout_sec}s cap "
            f"(head=#{head}, account_nonce={account_nonce}, scan_next=#{next_block})",
            flush=True,
        )
    return None


def fire_leg2_after_leg1_confirmed(firer, substrate, start_block=None):
    """Wait for leg1 ExtrinsicSuccess, then remove full alpha via remove_stake_full_limit."""
    if not firer.do_unstake:
        return None
    confirmed = wait_for_leg1_confirmed(firer, substrate, start_block=start_block)
    if confirmed is None or confirmed < 0:
        return confirmed
    firer.fire_leg2()
    return confirmed


def _build_armed_config(args, target_netuid, fire_signer):
    pattern = armed_pattern_for_netuid(target_netuid)
    return {
        "endpoint": args.endpoint,
        "target_netuid": int(target_netuid),
        "fire_signer": fire_signer,
        "output": args.output,
        "do_unstake": bool(args.unstake),
        "debug": bool(args.debug),
        "schedule_path": str(resolve_poll_schedule_path()),
        "poll_retry_sleep_ms": resolve_poll_retry_sleep_ms(),
        "pattern": pattern,
    }


def _finish_after_leg1_fired(
    substrate, args, firer, fire_signer, ext_hex, poll_text, pattern, scan_from=None
):
    """Slow path after leg1 is on the wire: unstake wait, log."""
    global RUNNING
    if scan_from is None:
        try:
            scan_from = substrate.get_block_number(substrate.get_chain_head())
        except Exception:
            scan_from = None
    trace_from = scan_from
    if firer.do_unstake:
        confirmed = fire_leg2_after_leg1_confirmed(firer, substrate, start_block=scan_from)
        if confirmed is not None and confirmed > 0:
            trace_from = confirmed
    _log_matched_from_text(
        substrate,
        poll_text or "",
        pattern,
        args.output,
        ext_hex=ext_hex,
    )
    if firer.debug:
        firer.trace_inclusion(trace_from)
    RUNNING = False


def _fire_matched_start_call(substrate, args, firer, fire_signer, ext_hex, poll_text, pattern):
    """Shared one-shot fire path after a matching start_call (signer verified first)."""
    if not fire_signer:
        return False
    if not ext_hex or not _start_call_signer_matches(substrate, ext_hex, fire_signer):
        return False
    firer.fire_leg1()
    _finish_after_leg1_fired(
        substrate, args, firer, fire_signer, ext_hex, poll_text, pattern
    )
    return True


def _poller_worker_main(process_index, config, trigger_queue, stop_event):
    """Staggered mempool poller; enqueue only start_calls from TRACK_FIRE_SIGNER."""
    endpoint = str(config["endpoint"])
    pattern = str(config["pattern"])
    fire_signer = config.get("fire_signer")
    schedule_path = Path(str(config["schedule_path"]))
    retry_sleep = max(0.0, int(config.get("poll_retry_sleep_ms") or 1)) / 1000.0
    substrate = new_substrate(endpoint)
    poll_req = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "author_pendingExtrinsics", "params": []}
    )
    poll_ws = None
    print(
        f"[fast] poller p{process_index} started endpoint={endpoint} pattern={pattern}",
        flush=True,
    )
    try:
        while not stop_event.is_set():
            granted, send_ms, _ = try_acquire_poll_slot(int(process_index), schedule_path)
            if not granted:
                if retry_sleep:
                    time.sleep(retry_sleep)
                continue
            try:
                if poll_ws is None:
                    poll_ws = websocket.create_connection(endpoint)
                poll_ws.send(poll_req)
                text = poll_ws.recv()
            except Exception as exc:
                print(f"[fast] poller p{process_index} socket error: {exc}", flush=True)
                try:
                    if poll_ws is not None:
                        poll_ws.close()
                except Exception:
                    pass
                poll_ws = None
                time.sleep(0.2)
                continue

            if isinstance(text, (bytes, bytearray)):
                text = text.decode("utf-8", "ignore")

            if pattern not in text:
                continue

            ext_hex = find_fireable_start_call_hex(substrate, text, pattern, fire_signer)
            if not ext_hex:
                continue

            job = TriggerJob(ext_hex=str(ext_hex), poller_id=int(process_index))
            try:
                trigger_queue.put_nowait(job)
            except Exception:
                with contextlib.suppress(Exception):
                    trigger_queue.put(job, timeout=0.05)
            print(
                f"[fast] poller p{process_index} matched start_call send_ms={send_ms}",
                flush=True,
            )
            stop_event.set()
            break
    finally:
        if poll_ws is not None:
            with contextlib.suppress(Exception):
                poll_ws.close()
        with contextlib.suppress(Exception):
            substrate.close()


def _submit_worker_main(config, trigger_queue, stop_event, done_event):
    """Single submit worker: presign once, fire at most once on queued match."""
    load_env_file()
    endpoint = str(config["endpoint"])
    target_netuid = int(config["target_netuid"])
    fire_signer = config.get("fire_signer")
    pattern = str(config["pattern"])

    class _Args:
        output = config.get("output", "pretty")
        unstake = bool(config.get("do_unstake"))
        debug = bool(config.get("debug"))

    args = _Args()
    substrate = new_substrate(endpoint)
    firer = Firer(
        substrate,
        endpoint,
        args.output,
        do_unstake=args.unstake,
        debug=args.debug,
    )
    fired = False
    try:
        firer.load_signer()
        firer.presign(target_netuid)
        print(
            f"[fast] submit worker armed netuid={target_netuid} endpoint={endpoint}",
            flush=True,
        )
        while not stop_event.is_set() and RUNNING:
            try:
                job = trigger_queue.get(timeout=0.001)
            except Exception:
                continue
            if fired or not isinstance(job, TriggerJob):
                continue
            if _fire_matched_start_call(
                substrate,
                args,
                firer,
                fire_signer,
                job.ext_hex or None,
                "",
                pattern,
            ):
                fired = True
                stop_event.set()
                break
    finally:
        firer.close()
        with contextlib.suppress(Exception):
            substrate.close()
        done_event.set()


def run_armed_track_multiprocess(args, target_netuid, fire_signer=None):
    """Multi-process armed path: N staggered pollers + one submit worker."""
    global _MP_STOP_EVENT
    if not fire_signer:
        raise SystemExit("--fire requires TRACK_FIRE_SIGNER in .env")
    process_count = resolve_poll_process_count(args)
    poll_interval_ms = resolve_poll_interval_ms(args)
    schedule_path = resolve_poll_schedule_path()
    schedule = init_poll_schedule(
        schedule_path,
        process_count=process_count,
        poll_interval_ms=poll_interval_ms,
    )
    config = _build_armed_config(args, target_netuid, fire_signer)
    signer_note = f" fire_signer={fire_signer}" if fire_signer else ""
    print(
        f"[fast] multi-process armed for start_call netuid={target_netuid} "
        f"pollers={process_count} interval_ms={poll_interval_ms} "
        f"slot_gap_ms={schedule.get('slot_gap_ms')} schedule={schedule_path}"
        f"{signer_note} on {args.endpoint}",
        flush=True,
    )

    ctx = multiprocessing.get_context("spawn")
    trigger_queue = ctx.Queue(maxsize=32)
    stop_event = ctx.Event()
    done_event = ctx.Event()
    _MP_STOP_EVENT = stop_event

    submit_proc = ctx.Process(
        target=_submit_worker_main,
        args=(config, trigger_queue, stop_event, done_event),
        name="track-submit",
        daemon=False,
    )
    poller_procs = []
    submit_proc.start()
    for process_index in range(process_count):
        proc = ctx.Process(
            target=_poller_worker_main,
            args=(process_index, config, trigger_queue, stop_event),
            name=f"track-poller-{process_index}",
            daemon=True,
        )
        proc.start()
        poller_procs.append(proc)

    submit_proc.join()
    stop_event.set()
    for proc in poller_procs:
        proc.join(timeout=3.0)
        if proc.is_alive():
            proc.terminate()
    submit_proc.join(timeout=1.0)
    _MP_STOP_EVENT = None


def run_armed_track_or_multiprocess(substrate, args, firer, target_netuid, fire_signer=None):
    if resolve_poll_process_count(args) <= 1:
        if firer is None:
            raise RuntimeError("single-process armed mode requires Firer")
        run_armed_track(substrate, args, firer, target_netuid, fire_signer=fire_signer)
        return
    run_armed_track_multiprocess(args, target_netuid, fire_signer=fire_signer)


def run_armed_track(substrate, args, firer, target_netuid, fire_signer=None):
    """Poll mempool for start_call; fire only when signer matches TRACK_FIRE_SIGNER."""
    global RUNNING
    if not fire_signer:
        raise RuntimeError("armed track requires TRACK_FIRE_SIGNER in .env")
    netuid_le = int(target_netuid).to_bytes(2, "little").hex()
    # `075c<netuid LE>"` : the closing quote anchors the match to the END of an
    # extrinsic hex in the JSON array -> a direct (unwrapped) start_call.
    pattern = f'{START_CALL_INDEX_HEX}{netuid_le}"'
    poll_req = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "author_pendingExtrinsics", "params": []}
    )
    signer_note = f" fire_signer={fire_signer}" if fire_signer else ""
    print(
        f"[fast] raw-scan armed for start_call netuid={target_netuid} (pattern={pattern})"
        f"{signer_note} on {args.endpoint}",
        flush=True,
    )
    poll_ws = None
    while RUNNING:
        try:
            if poll_ws is None:
                poll_ws = websocket.create_connection(args.endpoint)
            poll_ws.send(poll_req)
            text = poll_ws.recv()
        except Exception as exc:
            print(f"[fast] poll socket error: {exc}; reconnecting...", flush=True)
            try:
                if poll_ws is not None:
                    poll_ws.close()
            except Exception:
                pass
            poll_ws = None
            time.sleep(0.2)
            continue

        if isinstance(text, (bytes, bytearray)):
            text = text.decode("utf-8", "ignore")

        if pattern in text:
            ext_hex = find_fireable_start_call_hex(substrate, text, pattern, fire_signer)
            if not ext_hex:
                if args.poll_interval > 0:
                    time.sleep(args.poll_interval)
                continue
            firer.fire_leg1()
            _finish_after_leg1_fired(
                substrate,
                args,
                firer,
                fire_signer,
                ext_hex,
                text,
                pattern,
            )
            break

        if args.poll_interval > 0:
            time.sleep(args.poll_interval)

    try:
        if poll_ws is not None:
            poll_ws.close()
    except Exception:
        pass


def main():
    global RUNNING
    load_env_file()
    args = parse_args()
    if args.poll_interval is None:
        args.poll_interval = resolve_poll_interval_sec()
    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)

    # --- mode / fire validation ---
    track_all = args.mode == "start-call" and args.netuid is None
    if args.fire and track_all:
        raise SystemExit("--fire requires a known target netuid (use --mode full, or --netuid N)")
    if args.unstake and not args.fire:
        raise SystemExit("--unstake requires --fire")

    substrate = new_substrate(args.endpoint)
    fire_signer = None
    if args.fire:
        fire_signer = reload_fire_signer()
        if not fire_signer:
            raise SystemExit("--fire requires TRACK_FIRE_SIGNER in .env")
        print(f"[fire] only start_call from signer={fire_signer}", flush=True)
    register_fire_signer = fire_signer
    use_multiprocess = args.fire and resolve_poll_process_count(args) > 1

    firer = None
    if args.fire and not use_multiprocess:
        firer = Firer(
            substrate, args.endpoint, args.output, do_unstake=args.unstake, debug=args.debug
        )
        firer.load_signer()

    # --- pick starting phase + target ---
    target_netuid = None
    if args.mode == "full":
        phase = "discover"
        print(
            f"Phase 1 DISCOVER: watching mempool + blocks for register_network "
            f"on {args.endpoint} (poll={args.poll_interval}s, fire={args.fire})",
            flush=True,
        )
    else:  # start-call
        phase = "track"
        target_netuid = args.netuid
        if track_all:
            print(
                f"TRACK: watching mempool for ALL start_calls on {args.endpoint} "
                f"(poll={args.poll_interval}s)",
                flush=True,
            )
        else:
            print(
                f"TRACK: watching mempool for start_call netuid={target_netuid} on "
                f"{args.endpoint} (poll={args.poll_interval}s, fire={args.fire})",
                flush=True,
            )
            if firer:
                firer.presign(target_netuid)
            run_armed_track_or_multiprocess(
                substrate, args, firer, target_netuid, fire_signer=fire_signer
            )
            if firer:
                firer.close()
            return

    last_pending_set = set()
    last_block = None
    last_head_check = 0.0

    while RUNNING:
        # --- mempool ---
        try:
            pending = substrate.rpc_request("author_pendingExtrinsics", []).get("result", [])
        except Exception as exc:
            print(f"[{utc_now_iso()}] rpc error: {exc}; reconnecting...", flush=True)
            time.sleep(1.0)
            try:
                substrate = new_substrate(args.endpoint)
            except Exception:
                pass
            continue

        new_pending = [ext for ext in pending if ext not in last_pending_set]
        last_pending_set = set(pending)

        if phase == "discover":
            for ext_hex in new_pending:
                if not hex_looks_like_register_network(ext_hex):
                    continue
                for row in build_call_rows(
                    substrate, ext_hex, REGISTER_NETWORK_FUNCTION, "register_pending"
                ):
                    print_event(row, args.output)
                    if args.fire and args.mode == "full":
                        register_fire_signer = latch_register_fire_signer(
                            register_fire_signer, row, output=args.output
                        )
                        fire_signer = register_fire_signer

            # --- blocks: scan new blocks for NetworkAdded (binds the netuid) ---
            now = time.monotonic()
            if now - last_head_check >= BLOCK_POLL_SEC:
                last_head_check = now
                try:
                    head_num = substrate.get_block_number(substrate.get_chain_head())
                except Exception:
                    head_num = None
                if head_num is not None:
                    if last_block is None:
                        last_block = head_num  # only watch blocks from now on
                    while last_block < head_num and phase == "discover":
                        last_block += 1
                        if args.fire and args.mode == "full":
                            reloaded = reload_fire_signer()
                            if reloaded:
                                register_fire_signer = reloaded
                                fire_signer = reloaded
                        hit = find_registered_netuid_in_block(substrate, last_block)
                        if hit:
                            target_netuid = hit["netuid"]
                            print_event({"kind": "network_added", "ts": utc_now_iso(), **hit}, args.output)
                            if args.fire and args.mode == "full":
                                if not register_fire_signer:
                                    block_signer = register_signer_from_block(substrate, hit)
                                    if block_signer:
                                        register_fire_signer = block_signer
                                        write_env_var("TRACK_FIRE_SIGNER", block_signer)
                                        print(
                                            f"[fast] register_network signer from block "
                                            f"#{hit.get('block')}={block_signer}",
                                            flush=True,
                                        )
                                fire_signer = reload_fire_signer()
                                if not fire_signer:
                                    raise SystemExit(
                                        "full mode --fire: TRACK_FIRE_SIGNER not set in .env"
                                    )
                            if firer:
                                firer.presign(target_netuid)
                            run_armed_track_or_multiprocess(
                                substrate,
                                args,
                                firer,
                                target_netuid,
                                fire_signer=fire_signer,
                            )
                            if firer:
                                firer.close()
                            return
        else:  # phase == "track"
            for ext_hex in new_pending:
                if not hex_looks_like_start_call(ext_hex):
                    continue
                if track_all:
                    for row in build_call_rows(substrate, ext_hex, START_CALL_FUNCTION, "start_call"):
                        print_event(row, args.output)
                    continue
                # specific target (no --fire): log the match and exit. The armed
                # (--fire) cases use the faster run_armed_track() raw-scan path.
                if start_call_netuid_from_hex(ext_hex) != target_netuid:
                    continue
                for row in build_call_rows(substrate, ext_hex, START_CALL_FUNCTION, "start_call"):
                    row["matched"] = True
                    print_event(row, args.output)
                RUNNING = False
                break

        if args.poll_interval > 0:
            time.sleep(args.poll_interval)

    if firer:
        firer.close()


if __name__ == "__main__":
    main()
