#!/usr/bin/env python3
"""Background updater: top-emission validator hotkey per netuid, every N blocks."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from validator_hotkeys import (  # noqa: E402
    default_hotkeys_path,
    load_validator_hotkeys,
    parse_netuid_filter,
    parse_refresh_blocks,
    pick_top_emission_validator,
    save_validator_hotkeys,
)


def _load_env() -> None:
    env_file = APP_DIR / ".env"
    if load_dotenv is not None:
        load_dotenv(env_file)
    elif env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def _endpoint_from_env(fallback: str) -> str:
    for key in (
        "VALIDATOR_HOTKEYS_ENDPOINT",
        "STAKE_CHAIN_ENDPOINT",
        "BACKEND_ENDPOINT",
        "RPC_URL",
    ):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return fallback


async def _list_netuids(sub, netuid_filter: set[int] | None) -> list[int]:
    if netuid_filter is not None:
        return sorted(netuid_filter)
    subnets = await sub.all_subnets()
    out: list[int] = []
    for sn in subnets:
        try:
            nu = int(sn.netuid)
        except Exception:
            continue
        if nu <= 0:
            continue
        out.append(nu)
    return sorted(set(out))


async def _refresh_netuid(sub, netuid: int, previous: dict[int, str]) -> tuple[int, str | None, float, str]:
    try:
        mg = await sub.metagraph(netuid)
        hk, em = pick_top_emission_validator(
            mg.hotkeys,
            mg.validator_permit,
            mg.stake,
            mg.emission,
        )
        if hk:
            return netuid, hk, em, ""
        if netuid in previous:
            return netuid, previous[netuid], 0.0, "no_validators_kept_previous"
        return netuid, None, 0.0, "no_validators"
    except Exception as exc:
        if netuid in previous:
            return netuid, previous[netuid], 0.0, f"error_kept_previous:{exc}"
        return netuid, None, 0.0, f"error:{exc}"


async def refresh_all(
    sub,
    *,
    netuid_filter: set[int] | None,
    previous: dict[int, str],
    block_number: int,
    out_path: Path,
    parallel: int,
) -> dict[int, str]:
    netuids = await _list_netuids(sub, netuid_filter)
    if not netuids:
        print("no netuids to refresh", flush=True)
        return dict(previous)

    sem = asyncio.Semaphore(max(1, parallel))

    async def one(nu: int):
        async with sem:
            return await _refresh_netuid(sub, nu, previous)

    started = time.perf_counter()
    results = await asyncio.gather(*[one(nu) for nu in netuids])
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)

    hotkeys = dict(previous)
    ok = 0
    for netuid, hk, em, note in results:
        if hk:
            hotkeys[netuid] = hk
            ok += 1
            suffix = f" ({note})" if note else ""
            print(f"  SN{netuid}: {hk[:16]}... emission={em:.6f}{suffix}", flush=True)
        else:
            print(f"  SN{netuid}: skipped ({note})", flush=True)

    save_validator_hotkeys(
        hotkeys,
        path=out_path,
        updated_at_block=block_number,
        extra={"ranking": "emission", "netuids_refreshed": len(netuids), "netuids_ok": ok},
    )
    print(
        f"saved {ok}/{len(netuids)} hotkeys -> {out_path} block={block_number} ({elapsed_ms} ms)",
        flush=True,
    )
    return hotkeys


async def run_loop(args) -> None:
    import bittensor as bt

    endpoint = args.endpoint
    out_path = Path(args.out).expanduser()
    refresh_blocks = parse_refresh_blocks(args.refresh_blocks)
    netuid_filter = parse_netuid_filter(args.netuids)
    poll_sec = max(1.0, float(args.poll_sec))

    print(f"bittensor {bt.__version__}", flush=True)
    print(f"endpoint={endpoint}", flush=True)
    print(f"out={out_path} refresh_every={refresh_blocks} blocks poll={poll_sec}s", flush=True)
    if netuid_filter is not None:
        print(f"netuid filter={sorted(netuid_filter)}", flush=True)

    previous: dict[int, str] = {}
    loaded, _ = load_validator_hotkeys(out_path)
    previous.update(loaded)
    last_refresh_block: int | None = None

    async with bt.AsyncSubtensor(network=endpoint) as sub:
        while True:
            try:
                block = int(await sub.get_current_block())
            except Exception as exc:
                print(f"block fetch failed: {exc}", flush=True)
                await asyncio.sleep(poll_sec)
                continue

            need = last_refresh_block is None or (block - last_refresh_block) >= refresh_blocks
            if need:
                print(f"refresh at block {block} (last={last_refresh_block})", flush=True)
                previous = await refresh_all(
                    sub,
                    netuid_filter=netuid_filter,
                    previous=previous,
                    block_number=block,
                    out_path=out_path,
                    parallel=args.parallel,
                )
                last_refresh_block = block

            await asyncio.sleep(poll_sec)


def parse_args():
    p = argparse.ArgumentParser(description="Update top-emission validator hotkeys per netuid")
    p.add_argument(
        "--endpoint",
        default=_endpoint_from_env("ws://127.0.0.1:9944"),
        help="Subtensor WebSocket RPC endpoint",
    )
    p.add_argument(
        "--out",
        default=str(default_hotkeys_path()),
        help="Output JSON path (default: data/validator_hotkeys.json)",
    )
    p.add_argument(
        "--refresh-blocks",
        default=os.getenv("VALIDATOR_HOTKEYS_REFRESH_BLOCKS", "100"),
        help="Refresh interval in blocks (default: 100)",
    )
    p.add_argument(
        "--netuids",
        default=os.getenv("VALIDATOR_HOTKEYS_NETUIDS", ""),
        help="Optional comma-separated netuid filter; default all active subnets",
    )
    p.add_argument("--poll-sec", type=float, default=12.0, help="Block poll interval seconds")
    p.add_argument("--parallel", type=int, default=4, help="Max concurrent metagraph fetches")
    p.add_argument(
        "--once",
        action="store_true",
        help="Run one refresh then exit (uses current block as updated_at_block)",
    )
    return p.parse_args()


async def run_once(args) -> None:
    import bittensor as bt

    endpoint = args.endpoint
    out_path = Path(args.out).expanduser()
    netuid_filter = parse_netuid_filter(args.netuids)
    previous, _ = load_validator_hotkeys(out_path)

    async with bt.AsyncSubtensor(network=endpoint) as sub:
        block = int(await sub.get_current_block())
        await refresh_all(
            sub,
            netuid_filter=netuid_filter,
            previous=previous,
            block_number=block,
            out_path=out_path,
            parallel=args.parallel,
        )


def main() -> None:
    _load_env()
    args = parse_args()
    if args.once:
        asyncio.run(run_once(args))
    else:
        asyncio.run(run_loop(args))


if __name__ == "__main__":
    main()
