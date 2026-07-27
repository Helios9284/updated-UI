#!/usr/bin/env python3
"""Fetch all subnet dynamic info via bittensor (run in venv with bittensor installed)."""
import json
import sys
import time

from subnet_utils import normalize_subnet_dynamic_row


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: subnet_fetch_helper.py <endpoint>"}))
        return 1
    endpoint = sys.argv[1].strip()
    if not endpoint:
        print(json.dumps({"error": "endpoint required"}))
        return 1

    try:
        import bittensor as bt
    except ImportError as exc:
        print(json.dumps({"error": f"bittensor not installed: {exc}"}))
        return 1

    network = endpoint
    subtensor = bt.Subtensor(network=network)
    substrate = subtensor.substrate
    try:
        t0 = time.time()
        head = substrate.rpc_request("chain_getHeader", [])["result"]
        block_number = int(head["number"], 16)
        block_hash = substrate.rpc_request("chain_getBlockHash", [block_number])["result"]
        raw = substrate.runtime_call(
            api="SubnetInfoRuntimeApi",
            method="get_all_dynamic_info",
            block_hash=block_hash,
        )
        rows = []
        for item in raw or []:
            normalized = normalize_subnet_dynamic_row(item)
            if normalized is not None:
                rows.append(normalized)
        rows.sort(key=lambda r: r["netuid"])
        fetch_ms = round((time.time() - t0) * 1000.0, 1)
        print(
            json.dumps(
                {
                    "block_number": block_number,
                    "fetch_ms": fetch_ms,
                    "count": len(rows),
                    "subnets": rows,
                }
            )
        )
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    finally:
        try:
            subtensor.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
