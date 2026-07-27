#!/usr/bin/env python3
import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from substrateinterface import SubstrateInterface
from scalecodec import ScaleBytes
from scalecodec.utils.ss58 import ss58_decode, ss58_encode


RUNNING = True
STAKE_FUNCTIONS = {
    "add_stake",
    "add_stake_limit",
    "add_stake_burn",
    "remove_stake",
    "remove_stake_limit",
    "remove_stake_full",
    "remove_stake_full_limit",
    "unstake_all",
    "unstake_all_alpha",
    "move_stake",
    "transfer_stake",
    "swap_stake",
    "swap_stake_limit",
    "lock_stake",
}
MEV_MODULE = "mevshield"
ALPHA_DENOMINATED_FUNCTIONS = {
    "lock_stake",
    "remove_stake",
    "remove_stake_limit",
    "remove_stake_full",
    "remove_stake_full_limit",
    "unstake_all",
    "unstake_all_alpha",
    "move_stake",
    "transfer_stake",
    "swap_stake",
    "swap_stake_limit",
}
SPLIT_STAKE_FUNCTIONS = {"move_stake", "transfer_stake", "swap_stake", "swap_stake_limit"}
# Subtensor query_map page_size; must cover all active netuids (Finney has 128+ subnets).
ALPHA_PRICE_QUERY_PAGE_SIZE = 512
BALANCES_TRANSFER_FUNCTIONS = {
    "transfer_allow_death",
    "transfer_keep_alive",
    "transfer_all",
    "force_transfer",
}
START_CALL_FUNCTION = "start_call"
OTHER_NOTIFICATION_FUNCTIONS = frozenset(
    {
        START_CALL_FUNCTION,
        "register_network",
        "set_subnet_identity",
        "announce_coldkey_swap",
        "swap_coldkey_announced",
    }
)
ANSI_RESET = "\033[0m"
ANSI_RED = "\033[91m"
ANSI_GREEN = "\033[92m"
ANSI_MAGENTA = "\033[95m"


def handle_stop_signal(signum, frame):
    del signum, frame
    global RUNNING
    RUNNING = False


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Realtime Bittensor mempool poller using author_pendingExtrinsics"
    )
    parser.add_argument(
        "--endpoint",
        default="wss://entrypoint-finney.opentensor.ai:443",
        help="WebSocket RPC endpoint",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.0,
        help="Polling interval in seconds. 0 = no sleep (max speed).",
    )
    parser.add_argument(
        "--output",
        choices=("json", "pretty"),
        default="json",
        help="Output format per poll",
    )
    parser.add_argument(
        "--only-when-changed",
        action="store_true",
        help="Print only when pending list changed",
    )
    parser.add_argument(
        "--strict-scale-decode",
        action="store_true",
        help="Pass strict_scale_decode=True to substrate config",
    )
    parser.add_argument(
        "--decode",
        action="store_true",
        help="Decode pending extrinsics into readable fields",
    )
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="Print only newly added extrinsics compared to previous poll",
    )
    parser.add_argument(
        "--current-only-screen",
        action="store_true",
        help="Clear terminal every poll and display only current pending extrinsics",
    )
    parser.add_argument(
        "--stake-only",
        action="store_true",
        help="Track only stake-related extrinsics (supports nested proxy/utility wrappers)",
    )
    parser.add_argument(
        "--include-mev-shield",
        action="store_true",
        help="When used with --stake-only, also track MevShield extrinsics",
    )
    parser.add_argument(
        "--test-proxy-fake",
        action="store_true",
        help="Run proxy-fake detection self-tests and exit",
    )
    return parser.parse_args()


def extract_calls_recursive(obj, path=None, proxy_real=None):
    if path is None:
        path = []

    calls = []
    if isinstance(obj, dict):
        if "call_module" in obj and "call_function" in obj:
            module = obj.get("call_module")
            function = obj.get("call_function")
            current = f"{module}.{function}"
            current_path = path + [current]
            current_proxy_real = proxy_real
            call_args = obj.get("call_args") or []

            if (module or "").lower() == "proxy" and (function or "").lower() == "proxy":
                for arg in call_args:
                    if isinstance(arg, dict) and arg.get("name") == "real":
                        current_proxy_real = _normalize_proxy_ss58(arg.get("value"))
                        break

            calls.append(
                {
                    "module": module,
                    "function": function,
                    "path": " > ".join(current_path),
                    "call_args": call_args,
                    "real_address": current_proxy_real,
                }
            )
            for arg in call_args:
                if isinstance(arg, dict) and "value" in arg:
                    calls.extend(
                        extract_calls_recursive(
                            arg["value"],
                            current_path,
                            current_proxy_real,
                        )
                    )
            return calls

        for value in obj.values():
            calls.extend(extract_calls_recursive(value, path, proxy_real))
        return calls

    if isinstance(obj, list):
        for item in obj:
            calls.extend(extract_calls_recursive(item, path, proxy_real))
        return calls

    return calls


def decode_extrinsic(substrate, extrinsic_hex):
    try:
        extrinsic = substrate.runtime_config.create_scale_object(
            "Extrinsic", metadata=substrate.metadata
        )
        extrinsic.decode(
            ScaleBytes(extrinsic_hex),
            check_remaining=substrate.config.get("strict_scale_decode"),
        )

        value = extrinsic.value or {}
        call = value.get("call") or {}
        call_args = call.get("call_args") or []
        all_calls = extract_calls_recursive(call)
        stake_matches = [
            c for c in all_calls if (c.get("function") or "").lower() in STAKE_FUNCTIONS
        ]
        transfer_matches = [
            c
            for c in all_calls
            if (c.get("module") or "").lower() == "balances"
            and (c.get("function") or "").lower() in BALANCES_TRANSFER_FUNCTIONS
        ]
        start_call_matches = [
            c
            for c in all_calls
            if (c.get("module") or "").lower() == "subtensormodule"
            and (c.get("function") or "").lower() == START_CALL_FUNCTION
        ]
        mev_matches = [
            c for c in all_calls if (c.get("module") or "").lower() == MEV_MODULE
        ]

        return {
            "raw_hex": extrinsic_hex,
            "hash": str(getattr(extrinsic, "extrinsic_hash", "")),
            "version": value.get("version"),
            "signed": value.get("signed"),
            "signer": value.get("address"),
            "nonce": value.get("nonce"),
            "tip": value.get("tip"),
            "era": value.get("era"),
            "call_module": call.get("call_module"),
            "call_function": call.get("call_function"),
            "call_args": call_args,
            "all_calls": all_calls,
            "stake_matches": stake_matches,
            "transfer_matches": transfer_matches,
            "start_call_matches": start_call_matches,
            "mev_matches": mev_matches,
            "is_stake_related": len(stake_matches) > 0,
            "is_transfer_related": len(transfer_matches) > 0,
            "is_start_call_related": len(start_call_matches) > 0,
            "is_mev_related": len(mev_matches) > 0,
        }
    except Exception as exc:
        return {
            "raw_hex": extrinsic_hex,
            "decode_error": str(exc),
        }


def decode_extrinsics_batch(substrate, extrinsic_hex_list):
    """Decode many pending extrinsics in one worker thread.

    SubstrateInterface is not thread-safe, so batching avoids N asyncio.to_thread
    round-trips while still keeping decode on a single thread.
    """
    return {ext_hex: decode_extrinsic(substrate, ext_hex) for ext_hex in extrinsic_hex_list}


def clear_terminal():
    print("\033[2J\033[H", end="", flush=True)


class AlphaPriceCache:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._prices_tao_by_netuid = {0: 1.0}
        self._block_number = None
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def snapshot(self):
        with self._lock:
            return dict(self._prices_tao_by_netuid), self._block_number

    def price_for_netuid(self, netuid):
        nu = int(netuid)
        if nu == 0:
            return 1.0
        with self._lock:
            price = self._prices_tao_by_netuid.get(nu)
        if price is None or price <= 0:
            return None
        return float(price)

    def wait_for_block(self, block_number, stop_event=None, timeout_s=0.25, poll_s=0.01):
        if block_number is None:
            return False
        target = int(block_number)
        deadline = time.time() + float(timeout_s)
        while True:
            _, cached_block = self.snapshot()
            if cached_block is not None and int(cached_block) >= target:
                return True
            if stop_event is not None and stop_event.is_set():
                return False
            if time.time() >= deadline:
                return False
            time.sleep(float(poll_s))

    def _worker(self):
        substrate = SubstrateInterface(
            url=self.endpoint,
            type_registry_preset="substrate-node-template",
        )
        last_seen_block = None

        while self._running:
            try:
                head = substrate.rpc_request("chain_getHeader", [])["result"]
                block_number = int(head["number"], 16)
                if block_number != last_seen_block:
                    block_hash = substrate.rpc_request("chain_getBlockHash", [block_number])[
                        "result"
                    ]
                    # Instantaneous AMM spot price from subnet reserves
                    # (price_tao = SubnetTAO / SubnetAlphaIn). Replaces the removed
                    # Swap.AlphaSqrtPrice storage (Swap AMM redesigned in the v423
                    # runtime upgrade). Both reserves are u64 rao, so the ratio is
                    # already denominated in TAO per alpha.
                    alpha_in_by_netuid = {}
                    for netuid, alpha_raw in substrate.query_map(
                        module="SubtensorModule",
                        storage_function="SubnetAlphaIn",
                        block_hash=block_hash,
                        page_size=ALPHA_PRICE_QUERY_PAGE_SIZE,
                    ):
                        nu = to_int(netuid)
                        if nu is None:
                            continue
                        alpha_in_by_netuid[nu] = to_int(getattr(alpha_raw, "value", alpha_raw))
                    new_prices = {0: 1.0}
                    for netuid, tao_raw in substrate.query_map(
                        module="SubtensorModule",
                        storage_function="SubnetTAO",
                        block_hash=block_hash,
                        page_size=ALPHA_PRICE_QUERY_PAGE_SIZE,
                    ):
                        netuid_int = to_int(netuid)
                        if netuid_int is None:
                            continue
                        if netuid_int == 0:
                            new_prices[0] = 1.0
                            continue
                        tao_in = to_int(getattr(tao_raw, "value", tao_raw))
                        a_in = alpha_in_by_netuid.get(netuid_int)
                        if not a_in or tao_in is None:
                            continue
                        new_prices[netuid_int] = tao_in / a_in

                    with self._lock:
                        self._prices_tao_by_netuid = new_prices
                        self._block_number = block_number
                    last_seen_block = block_number
            except Exception:
                pass

            time.sleep(0.05)


from subnet_utils import normalize_subnet_dynamic_row, persist_subnet_owners


_DIR = Path(__file__).resolve().parent


class SubnetDynamicInfoCache:
    """Background cache for SubnetInfoRuntimeApi.get_all_dynamic_info (one RPC per block)."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self._subnets = []
        self._by_netuid = {}
        self._block_number = None
        self._fetch_ms = None
        self._updated_at = None
        self._error = None
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._fetch_inflight = False

    @staticmethod
    def _helper_python() -> Path | None:
        # bittensor conflicts with scalecodec in the main backend venv; use an
        # isolated env created by setup_subnet_fetch_venv.sh when present.
        for name in ("subnet_fetch_venv", "venv", ".venv"):
            candidate = _DIR / name / "bin" / "python"
            if candidate.is_file():
                return candidate
        return None

    def _run_fetch_helper(self) -> dict:
        helper_py = _DIR / "subnet_fetch_helper.py"
        python_bin = self._helper_python()
        if python_bin is None:
            return {
                "error": (
                    "No python venv found for subnet fetch. "
                    "Run: ./setup_subnet_fetch_venv.sh"
                )
            }
        if not helper_py.is_file():
            return {"error": f"Missing helper: {helper_py}"}
        try:
            proc = subprocess.run(
                [str(python_bin), str(helper_py), self.endpoint],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(_DIR),
            )
        except subprocess.TimeoutExpired:
            return {"error": "subnet fetch helper timed out"}
        except Exception as exc:
            return {"error": str(exc)}
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if not stdout:
            return {"error": stderr or f"subnet helper exited {proc.returncode}"}
        try:
            payload = json.loads(stdout.splitlines()[-1])
        except Exception as exc:
            tail = stdout[-400:] if stdout else stderr
            return {"error": f"invalid helper JSON: {exc}; tail={tail}"}
        if proc.returncode != 0 and payload.get("error"):
            return payload
        if proc.returncode != 0:
            payload = payload if isinstance(payload, dict) else {}
            payload.setdefault("error", stderr or f"helper exit {proc.returncode}")
        return payload

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._worker, daemon=True, name="subnet-dynamic-cache")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def snapshot(self):
        with self._lock:
            return {
                "block_number": self._block_number,
                "fetch_ms": self._fetch_ms,
                "updated_at": self._updated_at,
                "error": self._error,
                "count": len(self._subnets),
                "subnets": list(self._subnets),
                "by_netuid": dict(self._by_netuid),
            }


    def _worker(self):
        substrate = SubstrateInterface(
            url=self.endpoint,
            type_registry_preset="substrate-node-template",
        )
        with contextlib.suppress(Exception):
            substrate.init_runtime()
        last_seen_block = None

        while self._running:
            try:
                head = substrate.rpc_request("chain_getHeader", [])["result"]
                block_number = int(head["number"], 16)
                if block_number != last_seen_block and not self._fetch_inflight:
                    self._fetch_inflight = True

                    def _fetch_job(expected_block: int):
                        try:
                            payload = self._run_fetch_helper()
                            with self._lock:
                                if payload.get("error"):
                                    self._error = str(payload["error"])
                                    return
                                rows = list(payload.get("subnets") or [])
                                by_netuid = {row["netuid"]: row for row in rows if isinstance(row, dict)}
                                self._subnets = rows
                                self._by_netuid = by_netuid
                                self._block_number = payload.get("block_number", expected_block)
                                self._fetch_ms = payload.get("fetch_ms")
                                self._updated_at = datetime.now(timezone.utc).isoformat()
                                self._error = None
                                with contextlib.suppress(Exception):
                                    persist_subnet_owners(
                                        rows,
                                        _DIR / "subnet_owners.json",
                                        block_number=self._block_number,
                                    )
                        finally:
                            self._fetch_inflight = False

                    threading.Thread(
                        target=_fetch_job,
                        args=(block_number,),
                        daemon=True,
                        name="subnet-dynamic-fetch",
                    ).start()
                    last_seen_block = block_number
            except Exception as exc:
                with self._lock:
                    self._error = str(exc)

            time.sleep(0.25)


def call_args_to_dict(call_args):
    mapped = {}
    for arg in call_args or []:
        if isinstance(arg, dict) and "name" in arg:
            mapped[arg["name"]] = arg.get("value")
    return mapped


def get_first_value(values_by_name, candidates):
    for key in candidates:
        if key in values_by_name:
            return values_by_name[key]
    return None


def to_int(value):
    try:
        if hasattr(value, "value"):
            value = value.value
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def rao_to_tao(rao_value):
    as_int = to_int(rao_value)
    if as_int is None:
        return None
    return as_int / 1_000_000_000


def format_tao(value):
    if value is None:
        return "-"
    s = f"{value:.9f}".rstrip("0").rstrip(".")
    return s if s else "0"


def format_slippage(value):
    if value is None:
        return "-"
    return f"{value * 100:.4f}%"


def stake_side_from_function(function_name):
    """Return 'add' / 'remove' / None for a stake call function name."""
    fn = str(function_name or "").lower()
    if "." in fn:
        fn = fn.rsplit(".", 1)[-1]
    if fn.startswith("remove_") or fn.startswith("unstake"):
        return "remove"
    if fn.startswith("add_"):
        return "add"
    return None


def is_alpha_denominated_call(function_name):
    """True when the on-chain amount arg is alpha-RAO (not TAO-RAO).

    add_stake* stakes TAO; remove/unstake/split/lock move or burn alpha.
    See subtensor.com call reference for amount_staked vs amount_unstaked.
    """
    fn = str(function_name or "").lower()
    if "." in fn:
        fn = fn.rsplit(".", 1)[-1]
    if fn.startswith("add_"):
        return False
    if fn.startswith("remove_") or fn.startswith("unstake"):
        return True
    if fn in SPLIT_STAKE_FUNCTIONS or fn == "lock_stake":
        return True
    return fn in ALPHA_DENOMINATED_FUNCTIONS


def stake_amount_arg_candidates(function_name):
    """Pick the correct extrinsic arg for the call's amount denomination."""
    fn = str(function_name or "").lower()
    if "." in fn:
        fn = fn.rsplit(".", 1)[-1]
    if fn.startswith("add_"):
        return (
            "amount_staked",
            "tao_amount",
            "stake_amount",
            "amount",
            "value",
            "stake",
        )
    if is_alpha_denominated_call(fn):
        return (
            "amount_unstaked",
            "alpha_amount",
            "amount_alpha",
            "unstake_amount",
            "amount_to_unstake",
            "amount_to_move",
            "amount_to_swap",
            "amount_to_transfer",
            "amount",
            "value",
        )
    return (
        "amount_staked",
        "amount_unstaked",
        "tao_amount",
        "alpha_amount",
        "amount_alpha",
        "amount",
        "value",
        "stake",
    )


def price_netuid_for_stake_amount(function_name, values_by_name):
    fn = str(function_name or "").lower()
    if "." in fn:
        fn = fn.rsplit(".", 1)[-1]
    if fn in SPLIT_STAKE_FUNCTIONS:
        return to_int(
            get_first_value(values_by_name, ["origin_netuid", "src_netuid", "netuid"])
        )
    return to_int(get_first_value(values_by_name, ["netuid"]))


def alpha_price_tao_from_reserves(substrate, netuid, block_hash):
    """Spot TAO-per-alpha from SubnetTAO / SubnetAlphaIn at a pinned block."""
    nu = to_int(netuid)
    if nu is None:
        return None
    if nu == 0:
        return 1.0
    try:
        tao_raw = substrate.query(
            "SubtensorModule",
            "SubnetTAO",
            [nu],
            block_hash=block_hash,
        )
        alpha_raw = substrate.query(
            "SubtensorModule",
            "SubnetAlphaIn",
            [nu],
            block_hash=block_hash,
        )
        tao_in = to_int(getattr(tao_raw, "value", tao_raw))
        alpha_in = to_int(getattr(alpha_raw, "value", alpha_raw))
        if not alpha_in or tao_in is None:
            return None
        return tao_in / alpha_in
    except Exception:
        return None


def build_alpha_prices_tao_from_reserves_map(substrate, block_hash):
    """Full {netuid: tao_per_alpha} map from on-chain reserve storage."""
    prices = {0: 1.0}
    alpha_in_by_netuid = {}
    try:
        for netuid, alpha_raw in substrate.query_map(
            module="SubtensorModule",
            storage_function="SubnetAlphaIn",
            block_hash=block_hash,
            page_size=ALPHA_PRICE_QUERY_PAGE_SIZE,
        ):
            nu = to_int(netuid)
            if nu is None:
                continue
            alpha_in_by_netuid[nu] = to_int(getattr(alpha_raw, "value", alpha_raw))
        for netuid, tao_raw in substrate.query_map(
            module="SubtensorModule",
            storage_function="SubnetTAO",
            block_hash=block_hash,
            page_size=ALPHA_PRICE_QUERY_PAGE_SIZE,
        ):
            nu = to_int(netuid)
            if nu is None:
                continue
            if nu == 0:
                prices[0] = 1.0
                continue
            tao_in = to_int(getattr(tao_raw, "value", tao_raw))
            alpha_in = alpha_in_by_netuid.get(nu)
            if not alpha_in or tao_in is None:
                continue
            prices[nu] = tao_in / alpha_in
    except Exception:
        pass
    return prices


def netuids_needing_alpha_price_from_decoded(decoded_items):
    """Collect netuids whose stake rows need alpha→TAO conversion."""
    needed = set()
    for ext in decoded_items or []:
        if "decode_error" in ext:
            continue
        for match in ext.get("stake_matches", []) or []:
            fn = (match.get("function") or "").lower()
            if not is_alpha_denominated_call(fn):
                continue
            values = call_args_to_dict(match.get("call_args"))
            nu = price_netuid_for_stake_amount(fn, values)
            if nu is not None:
                needed.add(int(nu))
            if fn in SPLIT_STAKE_FUNCTIONS:
                dst = to_int(
                    get_first_value(values, ["destination_netuid", "dest_netuid"])
                )
                if dst is not None:
                    needed.add(int(dst))
    return needed


def enrich_alpha_prices_tao_by_netuid(
    alpha_prices_tao_by_netuid,
    netuids,
    substrate,
    block_hash,
):
    """Fill missing subnet prices with per-netuid reserve queries."""
    out = dict(alpha_prices_tao_by_netuid or {0: 1.0})
    out.setdefault(0, 1.0)
    for nu in sorted({int(n) for n in (netuids or []) if to_int(n) is not None}):
        if nu == 0:
            out[0] = 1.0
            continue
        existing = out.get(nu)
        if existing is not None and existing > 0:
            continue
        fetched = alpha_price_tao_from_reserves(substrate, nu, block_hash)
        if fetched is not None and fetched > 0:
            out[nu] = float(fetched)
    return out


def resolve_alpha_price_tao(
    netuid,
    alpha_prices_tao_by_netuid,
    *,
    limit_price_rao=None,
):
    """Resolve TAO-per-alpha for display; limit_price is the last-resort bound."""
    nu = to_int(netuid)
    if nu is None:
        return None
    if nu == 0:
        return 1.0
    price = (alpha_prices_tao_by_netuid or {}).get(nu)
    if price is not None and price > 0:
        return float(price)
    limit_tao = rao_to_tao(limit_price_rao)
    if limit_tao is not None and limit_tao > 0:
        return float(limit_tao)
    return None


def stake_amount_to_tao(
    amount_rao,
    function_name,
    values_by_name,
    alpha_prices_tao_by_netuid,
    *,
    limit_price_rao=None,
):
    """Convert a stake call's raw amount to TAO for display."""
    amount_units = rao_to_tao(amount_rao)
    if amount_units is None:
        return None
    if not is_alpha_denominated_call(function_name):
        return amount_units
    price_netuid = price_netuid_for_stake_amount(function_name, values_by_name)
    alpha_price_tao = resolve_alpha_price_tao(
        price_netuid,
        alpha_prices_tao_by_netuid,
        limit_price_rao=limit_price_rao,
    )
    if alpha_price_tao is None:
        return None
    return amount_units * alpha_price_tao


def slippage_netuid_for_call(function_name, values_by_name):
    fn = str(function_name or "").lower()
    if "." in fn:
        fn = fn.rsplit(".", 1)[-1]
    if fn in SPLIT_STAKE_FUNCTIONS:
        return to_int(
            get_first_value(values_by_name, ["origin_netuid", "src_netuid", "netuid"])
        )
    return to_int(get_first_value(values_by_name, ["netuid"]))


def compute_slippage_fraction(limit_price_tao, alpha_price_tao, side):
    """Slippage tolerance as a fraction, expressed from the order's own side.

    The on-chain limit price sits on the *worse* side of the current market
    price: above market for buys (add_stake_limit -> price*(1+tol)) and below
    market for sells (remove_stake_limit -> price*(1-tol)). Reporting
    (limit - market)/market for every call therefore prints a NEGATIVE number
    for unstakes even though the order is valid and confirms. Compute it from
    the side's perspective so a tolerance always reads as a positive percent.
    """
    if limit_price_tao is None or alpha_price_tao is None or alpha_price_tao <= 0:
        return None
    if side == "remove":
        return (alpha_price_tao - limit_price_tao) / alpha_price_tao
    # add side (and unknown side falls back to the buy convention)
    return (limit_price_tao - alpha_price_tao) / alpha_price_tao


def format_age(seconds):
    if seconds is None:
        return "-"
    return f"{seconds:.1f}s"


def _mempool_row_address(row):
    real_address = format_cell(row.get("real_address"))
    if real_address and real_address != "-":
        return real_address
    return format_cell(row.get("signer"))


def _parse_display_amount_tao(text):
    value = format_cell(text).strip()
    if value == "-":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _mempool_merge_group_key(row):
    flow = str(row.get("_row_type") or "other").strip().lower()
    return (
        format_cell(row.get("hash")),
        _mempool_row_address(row),
        format_cell(row.get("netuid")),
        flow,
    )


def consolidate_mempool_display_rows(rows):
    passthrough = []
    merged = {}
    merge_order = []

    for row in rows:
        if row.get("_same_origin_destination"):
            continue

        cleaned = dict(row)
        cleaned.pop("_same_origin_destination", None)

        ext_hash = format_cell(cleaned.get("hash"))
        if ext_hash == "-":
            passthrough.append(cleaned)
            continue

        key = _mempool_merge_group_key(cleaned)
        if key not in merged:
            merged[key] = cleaned
            merge_order.append(key)
            continue

        existing = merged[key]
        left = _parse_display_amount_tao(existing.get("amount"))
        right = _parse_display_amount_tao(cleaned.get("amount"))
        if left is not None and right is not None:
            existing["amount"] = format_tao(left + right)

        if format_cell(existing.get("slippage")) != format_cell(cleaned.get("slippage")):
            existing["slippage"] = "-"

    return passthrough + [merged[key] for key in merge_order]


def format_cell(value):
    if value is None:
        return "-"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def truncate(text, width):
    text = str(text)
    if len(text) <= width:
        return text
    return text[: max(1, width - 3)] + "..."


def render_table(rows):
    columns = ["call", "signer", "real_address", "amount", "netuid", "slippage", "age"]
    max_width = {
        "call": 40,
        "signer": 50,
        "real_address": 50,
        "amount": 20,
        "netuid": 20,
        "slippage": 14,
        "age": 10,
    }
    widths = {}
    for col in columns:
        widths[col] = min(
            max_width[col],
            max(len(col), *(len(str(row.get(col, "-"))) for row in rows)),
        )

    def hline():
        return "+" + "+".join("-" * (widths[c] + 2) for c in columns) + "+"

    def rline(row):
        row_type = row.get("_row_type", "")
        row_color = ""
        if row_type == "remove":
            row_color = ANSI_RED
        elif row_type == "add":
            row_color = ANSI_GREEN
        elif row_type == "mev":
            row_color = ANSI_MAGENTA

        rendered_cells = []
        for c in columns:
            cell = truncate(row.get(c, "-"), widths[c]).ljust(widths[c])
            if row_color:
                cell = f"{row_color}{cell}{ANSI_RESET}"
            rendered_cells.append(cell)

        return "| " + " | ".join(rendered_cells) + " |"

    out = [hline(), rline({c: c for c in columns}), hline()]
    for row in rows:
        out.append(rline(row))
    out.append(hline())
    return "\n".join(out)


def render_custom_table(rows, columns, max_width):
    widths = {}
    for col in columns:
        widths[col] = min(
            max_width.get(col, 48),
            max(len(col), *(len(str(row.get(col, "-"))) for row in rows)),
        )

    def hline():
        return "+" + "+".join("-" * (widths[c] + 2) for c in columns) + "+"

    def rline(row):
        rendered_cells = []
        for c in columns:
            cell = truncate(row.get(c, "-"), widths[c]).ljust(widths[c])
            rendered_cells.append(cell)
        return "| " + " | ".join(rendered_cells) + " |"

    out = [hline(), rline({c: c for c in columns}), hline()]
    for row in rows:
        out.append(rline(row))
    out.append(hline())
    return "\n".join(out)


def extract_display_rows(
    decoded_items,
    alpha_prices_tao_by_netuid,
    stake_only=False,
    include_mev_shield=False,
):
    rows = []
    for ext in decoded_items:
        if "decode_error" in ext:
            rows.append(
                {
                    "call": "[decode_error]",
                    "signer": "-",
                    "amount": "-",
                    "netuid": "-",
                    "slippage": "-",
                    "age": "-",
                    "_row_type": "other",
                }
            )
            continue

        signer = format_cell(_canonical_proxy_ss58(ext.get("signer")))
        age_display = format_age(ext.get("_age_seconds"))
        ext_hash = format_cell(ext.get("hash"))
        ext_age_seconds = ext.get("_age_seconds")

        if stake_only:
            for match in ext.get("stake_matches", []):
                call_name = f"{match.get('module')}.{match.get('function')}"
                function_name = (match.get("function") or "").lower()
                values_by_name = call_args_to_dict(match.get("call_args"))

                amount_rao = get_first_value(
                    values_by_name, stake_amount_arg_candidates(function_name)
                )
                netuid_value = get_first_value(values_by_name, ["netuid"])
                if netuid_value is None:
                    src = get_first_value(values_by_name, ["origin_netuid", "src_netuid"])
                    dst = get_first_value(values_by_name, ["destination_netuid", "dest_netuid"])
                    if src is not None or dst is not None:
                        netuid_value = f"{format_cell(src)}->{format_cell(dst)}"

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
                limit_price_tao = rao_to_tao(limit_price_rao)

                slippage_netuid = slippage_netuid_for_call(function_name, values_by_name)
                alpha_price_for_slippage = resolve_alpha_price_tao(
                    slippage_netuid,
                    alpha_prices_tao_by_netuid,
                    limit_price_rao=limit_price_rao,
                )
                # Split stake's first leg sells the origin alpha, so it is a
                # 'remove' for slippage-sign purposes; otherwise derive the side
                # from the call (remove_* / unstake* vs add_*).
                if function_name in SPLIT_STAKE_FUNCTIONS:
                    slippage_side = "remove"
                else:
                    slippage_side = stake_side_from_function(function_name)
                slippage = compute_slippage_fraction(
                    limit_price_tao, alpha_price_for_slippage, slippage_side
                )

                real_address = _format_real_address_cell(match.get("real_address"))
                call_path = format_cell(match.get("path"))
                origin_netuid = to_int(
                    get_first_value(values_by_name, ["origin_netuid", "src_netuid", "netuid"])
                )
                destination_netuid = to_int(
                    get_first_value(values_by_name, ["destination_netuid", "dest_netuid"])
                )

                if function_name in SPLIT_STAKE_FUNCTIONS and destination_netuid is not None:
                    same_origin_destination = (
                        origin_netuid is not None
                        and destination_netuid is not None
                        and int(origin_netuid) == int(destination_netuid)
                    )
                    dest_alpha_price = resolve_alpha_price_tao(
                        destination_netuid,
                        alpha_prices_tao_by_netuid,
                        limit_price_rao=limit_price_rao,
                    )
                    # Second leg buys the destination alpha -> 'add' side.
                    dest_slippage = compute_slippage_fraction(
                        limit_price_tao, dest_alpha_price, "add"
                    )

                    if same_origin_destination:
                        continue

                    rows.append(
                        {
                            "hash": ext_hash,
                            "call": "SubtensorModule.remove_stake (origin)",
                            "signer": signer,
                            "real_address": real_address,
                            "path": call_path,
                            "amount": format_tao(amount_tao),
                            "netuid": format_cell(origin_netuid),
                            "slippage": format_slippage(slippage),
                            "age": age_display,
                            "_age_seconds": ext_age_seconds,
                            "_row_type": "remove",
                        }
                    )
                    rows.append(
                        {
                            "hash": ext_hash,
                            "call": "SubtensorModule.add_stake (destination)",
                            "signer": signer,
                            "real_address": real_address,
                            "path": call_path,
                            "amount": format_tao(amount_tao),
                            "netuid": format_cell(destination_netuid),
                            "slippage": format_slippage(dest_slippage),
                            "age": age_display,
                            "_age_seconds": ext_age_seconds,
                            "_row_type": "add",
                        }
                    )
                else:
                    row_type = "other"
                    if function_name.startswith("remove_") or function_name.startswith("unstake"):
                        row_type = "remove"
                    elif function_name.startswith("add_"):
                        row_type = "add"

                    rows.append(
                        {
                            "hash": ext_hash,
                            "call": call_name,
                            "signer": signer,
                            "real_address": real_address,
                            "path": call_path,
                            "amount": format_tao(amount_tao),
                            "netuid": format_cell(netuid_value),
                            "slippage": format_slippage(slippage),
                            "age": age_display,
                            "_age_seconds": ext_age_seconds,
                            "_row_type": row_type,
                        }
                    )

            if include_mev_shield:
                for match in ext.get("mev_matches", []):
                    rows.append(
                        {
                            "hash": ext_hash,
                            "call": f"{match.get('module')}.{match.get('function')}",
                            "signer": signer,
                            "real_address": format_cell(match.get("real_address")),
                            "path": format_cell(match.get("path")),
                            "amount": "-",
                            "netuid": "-",
                            "slippage": "-",
                            "age": age_display,
                            "_age_seconds": ext_age_seconds,
                            "_row_type": "mev",
                        }
                    )
        else:
            values_by_name = call_args_to_dict(ext.get("call_args"))
            amount_rao = get_first_value(values_by_name, ["amount", "value", "tao_amount", "alpha_amount"])
            limit_price_rao = get_first_value(values_by_name, ["limit_price", "price_limit"])
            limit_price_tao = rao_to_tao(limit_price_rao)
            netuid_val = to_int(get_first_value(values_by_name, ["netuid"]))
            alpha_price_for_slippage = (
                alpha_prices_tao_by_netuid.get(netuid_val) if netuid_val is not None else None
            )
            slippage = compute_slippage_fraction(
                limit_price_tao,
                alpha_price_for_slippage,
                stake_side_from_function(ext.get("call_function")),
            )
            rows.append(
                {
                    "call": f"{ext.get('call_module')}.{ext.get('call_function')}",
                    "signer": signer,
                    "real_address": "-",
                    "amount": format_tao(rao_to_tao(amount_rao)),
                    "netuid": format_cell(get_first_value(values_by_name, ["netuid"])),
                    "slippage": format_slippage(slippage),
                    "age": age_display,
                    "_row_type": "other",
                }
            )

    if stake_only:
        rows = consolidate_mempool_display_rows(rows)

    rows.sort(key=lambda r: r.get("_age_seconds") or -1, reverse=True)
    return rows


def extract_balance_transfer_rows(decoded_items, alpha_prices_tao_by_netuid=None):
    rows = []
    alpha_prices = alpha_prices_tao_by_netuid or {0: 1.0}
    for ext in decoded_items:
        if "decode_error" in ext:
            continue
        signer = format_cell(_canonical_proxy_ss58(ext.get("signer")))
        age_display = format_age(ext.get("_age_seconds"))
        for match in ext.get("transfer_matches", []):
            values_by_name = call_args_to_dict(match.get("call_args"))
            amount_rao = get_first_value(values_by_name, ["value", "amount"])
            dest = get_first_value(values_by_name, ["dest"])
            rows.append(
                {
                    "hash": format_cell(ext.get("hash")),
                    "call": f"{match.get('module')}.{match.get('function')}",
                    "signer": signer,
                    "real_address": format_cell(match.get("real_address")),
                    "to": format_cell(dest),
                    "amount": format_tao(rao_to_tao(amount_rao)),
                    "age": age_display,
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
                alpha_prices,
            )
            rows.append(
                {
                    "hash": format_cell(ext.get("hash")),
                    "call": f"{match.get('module')}.{match.get('function')}",
                    "signer": signer,
                    "real_address": format_cell(match.get("real_address")),
                    "to": format_cell(get_first_value(values_by_name, ["hotkey", "dest"])),
                    "amount": format_tao(amount_tao),
                    "age": age_display,
                }
            )
    return rows


PROXY_DELEGATES_CACHE_TTL_SEC = float(
    (os.getenv("MEMPOOL_PROXY_CACHE_TTL_SEC") or "600").strip() or "600"
)
PROXY_FAKE_MAX_LOOKUPS = max(
    0,
    int((os.getenv("MEMPOOL_PROXY_FAKE_MAX_LOOKUPS") or "24").strip() or "24"),
)
PROXY_FAKE_BUDGET_SEC = max(
    0.25,
    float((os.getenv("MEMPOOL_PROXY_FAKE_BUDGET_SEC") or "2.5").strip() or "2.5"),
)
PROXY_FAKE_CHECK_ENABLED = (
    (os.getenv("MEMPOOL_PROXY_FAKE_CHECK") or "true").strip().lower()
    not in {"0", "false", "no", "off"}
)


class ProxyDelegatesCache:
    """TTL cache for Proxy::Proxies(real) delegate lists (mempool display only)."""

    def __init__(self, ttl_sec=PROXY_DELEGATES_CACHE_TTL_SEC):
        self._ttl = max(1.0, float(ttl_sec))
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, frozenset[str]]] = {}

    def get_delegates(self, substrate, real_address: str) -> frozenset[str] | None:
        real = _normalize_proxy_ss58(real_address)
        if not real:
            return frozenset()
        cached = self._cached_delegates(real)
        if cached is not None:
            return cached
        delegates = _fetch_proxy_delegates(substrate, real)
        if delegates is None:
            return None
        with self._lock:
            self._entries[real] = (time.time(), delegates)
        return delegates

    def _cached_delegates(self, real: str) -> frozenset[str] | None:
        now = time.time()
        with self._lock:
            hit = self._entries.get(real)
            if hit and (now - hit[0]) < self._ttl:
                return hit[1]
        return None


_PROXY_DELEGATES_CACHE = ProxyDelegatesCache()


def _normalize_proxy_ss58(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("Id", "id", "AccountId", "account_id"):
            if key in value:
                return _normalize_proxy_ss58(value[key])
        return ""
    text = format_cell(value).strip()
    if not text or text == "-":
        return ""
    if text.startswith("{") and text.endswith("}"):
        with contextlib.suppress(Exception):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return _normalize_proxy_ss58(parsed)
    return text


def _canonical_proxy_ss58(value):
    text = _normalize_proxy_ss58(value)
    if not text:
        return ""
    for ss58_format in (42, None):
        try:
            if ss58_format is None:
                public_key = ss58_decode(text)
                return ss58_encode(public_key, ss58_format=42)
            public_key = ss58_decode(text, valid_ss58_format=ss58_format)
            return ss58_encode(public_key, ss58_format=ss58_format)
        except Exception:
            continue
    return text


def _proxy_delegate_from_entry(entry):
    if entry is None:
        return ""
    if isinstance(entry, dict):
        return _normalize_proxy_ss58(entry.get("delegate"))
    delegate = getattr(entry, "delegate", None)
    if delegate is not None:
        return _normalize_proxy_ss58(getattr(delegate, "value", delegate))
    inner = getattr(entry, "value", None)
    if isinstance(inner, dict):
        return _normalize_proxy_ss58(inner.get("delegate"))
    if inner is not None:
        return _normalize_proxy_ss58(getattr(inner, "delegate", None))
    return ""


def _fetch_proxy_delegates(substrate, real_address: str) -> frozenset[str] | None:
    real = _canonical_proxy_ss58(real_address)
    if not real:
        return frozenset()
    try:
        result = substrate.query(
            module="Proxy",
            storage_function="Proxies",
            params=[real],
        )
        raw = getattr(result, "value", result)
        if raw is None:
            return frozenset()
        entries = raw[0] if isinstance(raw, (list, tuple)) and raw else raw
        if not entries:
            return frozenset()
        out = set()
        for entry in entries:
            delegate = _canonical_proxy_ss58(_proxy_delegate_from_entry(entry))
            if delegate:
                out.add(delegate)
        return frozenset(out)
    except Exception:
        return None


def _format_real_address_cell(value):
    canon = _canonical_proxy_ss58(value)
    return canon if canon else "-"


def _path_has_proxy_wrapper_for_stake(path_text):
    """True when call path includes Proxy.proxy wrapping an inner dispatch."""
    path = str(path_text or "").lower()
    if "proxy.proxy" not in path:
        return False
    parts = [p.strip() for p in path.split(">") if p.strip()]
    if not parts:
        return False
    try:
        proxy_idx = parts.index("proxy.proxy")
    except ValueError:
        return False
    return proxy_idx < len(parts) - 1


def _call_path_has_proxy_wrapper(row):
    return _path_has_proxy_wrapper_for_stake((row or {}).get("path"))


def _row_uses_proxy_wrapper(row):
    if not _call_path_has_proxy_wrapper(row):
        return False
    signer = _canonical_proxy_ss58((row or {}).get("signer"))
    real = _canonical_proxy_ss58((row or {}).get("real_address"))
    return bool(signer and real and signer != real)


def annotate_mempool_proxy_fake(rows, substrate, cache=None):
    """Mark mempool rows whose proxy signer is not registered for the real account."""
    if not rows or substrate is None or not PROXY_FAKE_CHECK_ENABLED:
        return rows
    cache = cache or _PROXY_DELEGATES_CACHE
    pending = []
    for row in rows:
        if not _row_uses_proxy_wrapper(row):
            row.pop("proxy_fake", None)
            continue
        pending.append(
            (
                row,
                _canonical_proxy_ss58(row.get("real_address")),
                _canonical_proxy_ss58(row.get("signer")),
            )
        )
    if not pending:
        return rows

    unique_reals: list[str] = []
    seen_reals = set()
    for _, real, _ in pending:
        if real in seen_reals:
            continue
        seen_reals.add(real)
        unique_reals.append(real)

    delegates_by_real: dict[str, frozenset[str] | None] = {}
    lookups = 0
    # Hard wall-clock budget so Proxies RPC stalls cannot freeze the mempool
    # worker (and starve stake-api thread-pool jobs) for a whole poll cycle.
    deadline = time.monotonic() + PROXY_FAKE_BUDGET_SEC
    for real in unique_reals:
        cached = cache._cached_delegates(real)
        if cached is not None:
            delegates_by_real[real] = cached
            continue
        if lookups >= PROXY_FAKE_MAX_LOOKUPS:
            continue
        if time.monotonic() >= deadline:
            break
        delegates_by_real[real] = cache.get_delegates(substrate, real)
        lookups += 1

    for row, real, signer in pending:
        delegates = delegates_by_real.get(real)
        if delegates is None:
            row.pop("proxy_fake", None)
            continue
        if signer not in delegates:
            row["proxy_fake"] = True
        else:
            row.pop("proxy_fake", None)
    return rows


def run_proxy_fake_self_tests():
    """Pure-logic checks for proxy fake eligibility (no chain RPC)."""
    direct = {
        "path": "SubtensorModule.add_stake",
        "signer": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        "real_address": "-",
    }
    assert not _row_uses_proxy_wrapper(direct)

    proxy_swap = {
        "path": "Proxy.proxy > SubtensorModule.swap_stake",
        "call": "SubtensorModule.remove_stake (origin)",
        "signer": "5FHneW46xGXgs5mUiveU4sbTyGBzmstSpyr8AbZCb7cr",
        "real_address": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    }
    assert _row_uses_proxy_wrapper(proxy_swap)

    split_no_proxy = {
        "path": "SubtensorModule.swap_stake",
        "call": "SubtensorModule.add_stake (destination)",
        "signer": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        "real_address": "-",
    }
    assert not _row_uses_proxy_wrapper(split_no_proxy)

    proxy_only = {
        "path": "Proxy.proxy",
        "signer": "5FHneW46xGXgs5mUiveU4sbTyGBzmstSpyr8AbZCb7cr",
        "real_address": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    }
    assert not _row_uses_proxy_wrapper(proxy_only)

    same_signer = {
        "path": "Proxy.proxy > SubtensorModule.add_stake",
        "signer": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
        "real_address": "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
    }
    assert not _row_uses_proxy_wrapper(same_signer)

    real = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
    proxy = "5FHneW46xGXgs5mUiveU4sbTyGBzmstSpyr8AbZCb7cr"
    row = {
        "path": "Proxy.proxy > SubtensorModule.add_stake",
        "signer": proxy,
        "real_address": real,
    }

    class _MockCache:
        def __init__(self, delegates_by_real):
            self._delegates_by_real = delegates_by_real

        def _cached_delegates(self, real_address):
            return None

        def get_delegates(self, _substrate, real_address):
            return self._delegates_by_real.get(real_address)

    registered = annotate_mempool_proxy_fake(
        [dict(row)],
        object(),
        cache=_MockCache({real: frozenset({proxy})}),
    )[0]
    assert not registered.get("proxy_fake")

    unregistered = annotate_mempool_proxy_fake(
        [dict(row)],
        object(),
        cache=_MockCache({real: frozenset()}),
    )[0]
    assert unregistered.get("proxy_fake") is True


def build_mempool_stake_display(
    decoded_items,
    alpha_prices_tao_by_netuid,
    substrate,
    price_block,
    *,
    stake_only=True,
    include_mev_shield=True,
):
    """Decode rows with reserve prices enriched for every alpha-denominated netuid."""
    if price_block is not None:
        block_hash = substrate.get_block_hash(int(price_block))
    else:
        block_hash = substrate.get_chain_head()
    needed = netuids_needing_alpha_price_from_decoded(decoded_items)
    enriched = enrich_alpha_prices_tao_by_netuid(
        alpha_prices_tao_by_netuid, needed, substrate, block_hash
    )
    rows = extract_display_rows(
        decoded_items=decoded_items,
        alpha_prices_tao_by_netuid=enriched,
        stake_only=stake_only,
        include_mev_shield=include_mev_shield,
    )
    annotate_mempool_proxy_fake(rows, substrate)
    transfer_rows = extract_balance_transfer_rows(decoded_items, enriched)
    other_notification_rows = extract_other_notification_rows(decoded_items)
    return rows, transfer_rows, other_notification_rows


def _notification_row_from_match(ext, match, *, age_display="-", status=None, block_number=None):
    values_by_name = call_args_to_dict(match.get("call_args"))
    function_name = str(match.get("function") or "").lower()
    row = {
        "hash": format_cell(ext.get("hash")),
        "call": f"{match.get('module')}.{match.get('function')}",
        "signer": format_cell(ext.get("signer")),
        "real_address": format_cell(match.get("real_address")),
        "netuid": format_cell(get_first_value(values_by_name, ["netuid"])),
        "hotkey": format_cell(get_first_value(values_by_name, ["hotkey"])),
        "age": age_display,
    }
    if status is not None:
        row["status"] = format_cell(status)
    if block_number is not None:
        row["block_number"] = int(block_number)

    if function_name == "set_subnet_identity":
        row["subnet_name"] = format_cell(
            get_first_value(values_by_name, ["subnet_name", "name", "identity"])
        )
        row["github_repo"] = format_cell(
            get_first_value(values_by_name, ["github_repo", "repo", "github"])
        )
    elif function_name in {"announce_coldkey_swap", "swap_coldkey_announced"}:
        row["old_coldkey"] = format_cell(
            get_first_value(
                values_by_name,
                ["old_coldkey", "old_coldkey_ss58", "old", "from_coldkey"],
            )
        )
        row["new_coldkey"] = format_cell(
            get_first_value(
                values_by_name,
                ["new_coldkey", "new_coldkey_ss58", "new", "to_coldkey"],
            )
        )
    return row


def notification_rows_from_decoded_ext(
    ext,
    *,
    age_display=None,
    status=None,
    block_number=None,
):
    if "decode_error" in ext:
        return []
    age = age_display if age_display is not None else format_age(ext.get("_age_seconds"))
    rows = []
    for match in ext.get("all_calls", []):
        module_name = str(match.get("module") or "").lower()
        function_name = str(match.get("function") or "").lower()
        if module_name != "subtensormodule":
            continue
        if function_name not in OTHER_NOTIFICATION_FUNCTIONS:
            continue
        rows.append(
            _notification_row_from_match(
                ext,
                match,
                age_display=age,
                status=status,
                block_number=block_number,
            )
        )
    return rows


def extract_other_notification_rows(decoded_items):
    rows = []
    for ext in decoded_items:
        rows.extend(notification_rows_from_decoded_ext(ext))
    return rows


def extract_start_call_rows(decoded_items):
    return [
        row
        for row in extract_other_notification_rows(decoded_items)
        if str(row.get("call") or "").lower().endswith(".start_call")
    ]


def main():
    args = parse_args()
    if args.test_proxy_fake:
        run_proxy_fake_self_tests()
        print("proxy_fake self-tests passed", flush=True)
        return 0
    if args.current_only_screen:
        args.output = "pretty"
        args.new_only = False
    if args.stake_only:
        args.decode = True

    signal.signal(signal.SIGINT, handle_stop_signal)
    signal.signal(signal.SIGTERM, handle_stop_signal)

    substrate = SubstrateInterface(
        url=args.endpoint,
        type_registry_preset="substrate-node-template",
        config={"strict_scale_decode": args.strict_scale_decode},
    )
    if args.decode:
        substrate.init_runtime()

    alpha_price_cache = None
    if args.stake_only:
        alpha_price_cache = AlphaPriceCache(endpoint=args.endpoint)
        alpha_price_cache.start()

    poll_index = 0
    last_snapshot = None
    last_pending_set = set()
    first_seen_by_extrinsic = {}

    while RUNNING:
        poll_started = time.perf_counter()

        try:
            response = substrate.rpc_request("author_pendingExtrinsics", [])
            pending = response.get("result", [])
        except Exception as exc:
            if args.current_only_screen:
                clear_terminal()
            err_payload = {
                "ts": utc_now_iso(),
                "poll": poll_index,
                "error": str(exc),
            }
            if args.output == "json":
                print(json.dumps(err_payload, ensure_ascii=False), flush=True)
            else:
                print(
                    f"[{err_payload['ts']}] poll={poll_index} error={err_payload['error']}",
                    flush=True,
                )
            if args.poll_interval > 0:
                time.sleep(args.poll_interval)
            continue

        changed = pending != last_snapshot
        current_pending_set = set(pending)
        new_pending = [ext for ext in pending if ext not in last_pending_set]
        now_ts = time.time()
        for ext_hex in pending:
            first_seen_by_extrinsic.setdefault(ext_hex, now_ts)
        first_seen_by_extrinsic = {
            k: v for k, v in first_seen_by_extrinsic.items() if k in current_pending_set
        }
        latency_ms = (time.perf_counter() - poll_started) * 1000.0

        if (not args.only_when_changed) or changed:
            decoded = None
            if args.decode:
                decode_source = pending if args.current_only_screen else (new_pending if args.new_only else pending)
                decoded = [decode_extrinsic(substrate, ext_hex) for ext_hex in decode_source]
                for ext in decoded:
                    raw_hex = ext.get("raw_hex")
                    if raw_hex is not None and raw_hex in first_seen_by_extrinsic:
                        ext["_age_seconds"] = now_ts - first_seen_by_extrinsic[raw_hex]
                    else:
                        ext["_age_seconds"] = None
                if args.stake_only:
                    decoded = [
                        ext
                        for ext in decoded
                        if ext.get("is_stake_related")
                        or ext.get("is_transfer_related")
                        or ext.get("is_start_call_related")
                        or (args.include_mev_shield and ext.get("is_mev_related"))
                    ]

            if args.output == "json":
                payload = {
                    "ts": utc_now_iso(),
                    "poll": poll_index,
                    "pending_count": len(pending),
                    "new_count": len(new_pending),
                    "latency_ms": round(latency_ms, 3),
                    "extrinsics": (
                        decoded
                        if args.decode
                        else (new_pending if args.new_only else pending)
                    ),
                }
                has_output = bool(payload["extrinsics"])
                if ((not args.new_only) or new_pending) and ((not args.stake_only) or has_output):
                    print(json.dumps(payload, ensure_ascii=False), flush=True)
            else:
                if args.current_only_screen:
                    clear_terminal()

                if args.new_only and not new_pending:
                    last_snapshot = pending
                    last_pending_set = current_pending_set
                    poll_index += 1
                    if args.poll_interval > 0:
                        elapsed = time.perf_counter() - poll_started
                        sleep_s = max(0.0, args.poll_interval - elapsed)
                        if sleep_s > 0:
                            time.sleep(sleep_s)
                    continue

                print(
                    f"[{utc_now_iso()}] poll={poll_index} "
                    f"pending={len(pending)} new={len(new_pending)} "
                    f"tracked={(len(decoded) if args.decode else (len(new_pending) if args.new_only else len(pending)))} "
                    f"latency_ms={latency_ms:.3f}"
                    + (
                        (
                            f" price_block={alpha_price_cache.snapshot()[1]}"
                            if alpha_price_cache is not None
                            else ""
                        )
                    ),
                    flush=True,
                )
                if args.decode:
                    alpha_prices, _ = (
                        alpha_price_cache.snapshot()
                        if alpha_price_cache is not None
                        else ({0: 1.0}, None)
                    )
                    rows = extract_display_rows(
                        decoded_items=decoded,
                        alpha_prices_tao_by_netuid=alpha_prices,
                        stake_only=args.stake_only,
                        include_mev_shield=args.include_mev_shield,
                    )
                    if rows:
                        print("\n[Stake table]", flush=True)
                        print(render_table(rows), flush=True)
                    transfer_rows = extract_balance_transfer_rows(decoded)
                    if transfer_rows:
                        print("\n[Balances transfer table]", flush=True)
                        print(
                            render_custom_table(
                                transfer_rows,
                                columns=["call", "signer", "real_address", "to", "amount", "age"],
                                max_width={
                                    "call": 34,
                                    "signer": 48,
                                    "real_address": 48,
                                    "to": 48,
                                    "amount": 18,
                                    "age": 10,
                                },
                            ),
                            flush=True,
                        )
                    start_call_rows = extract_start_call_rows(decoded)
                    if start_call_rows:
                        print("\n[SubtensorModule.start_call table]", flush=True)
                        print(
                            render_custom_table(
                                start_call_rows,
                                columns=["call", "signer", "real_address", "hotkey", "netuid", "age"],
                                max_width={
                                    "call": 40,
                                    "signer": 48,
                                    "real_address": 48,
                                    "hotkey": 48,
                                    "netuid": 16,
                                    "age": 10,
                                },
                            ),
                            flush=True,
                        )
                else:
                    exts_to_print = pending if args.current_only_screen else (
                        new_pending if args.new_only else pending
                    )
                    for ext in exts_to_print:
                        print(f"  {ext}", flush=True)

        last_snapshot = pending
        last_pending_set = current_pending_set
        poll_index += 1

        if args.poll_interval > 0:
            elapsed = time.perf_counter() - poll_started
            sleep_s = max(0.0, args.poll_interval - elapsed)
            if sleep_s > 0:
                time.sleep(sleep_s)
        else:
            time.sleep(0.001)

    if alpha_price_cache is not None:
        alpha_price_cache.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
