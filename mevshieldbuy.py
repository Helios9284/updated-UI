"""
Bittensor MEV-Shield buy helper — portable module for any project.

Submits `add_stake_limit` wrapped in `Utility.force_batch`, encrypted via
`MevShield.submit_encrypted`.  Inner extrinsic must use force_batch; shield
and inner share the same era (finalised head, period=8).

Quick start
-----------
    import asyncio
    from bittensor_wallet import Wallet
    from bittensor.core.async_subtensor import AsyncSubtensor
    from mevshieldbuy import mev_shield_buy

    async def main():
        wallet = Wallet(name="my-wallet", hotkey="my-hotkey")
        async with AsyncSubtensor(network="finney") as subtensor:
            ok, inner_hash = await mev_shield_buy(
                subtensor=subtensor,
                wallet=wallet,
                netuid=1,
                tao_amount=1.0,
                hotkey_ss58="5F...",   # validator hotkey to stake TO
                tip=5_000_000,
                tolerance_pct=10.0,
            )
            print(ok, inner_hash)

    asyncio.run(main())

Lower-level API: submit_mev_shield_extrinsic() + build_add_stake_limit_batch_call().
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from bittensor_wallet import Wallet
from bittensor.core.async_subtensor import AsyncSubtensor

# ── Constants ──────────────────────────────────────────────────────────────────

MEV_SHIELD_PUBLIC_KEY_SIZE = 1184
MEV_SHIELD_ERA_PERIOD = 8
MEV_SHIELD_MIN_TOLERANCE_PCT = float(os.getenv("MEV_SHIELD_MIN_TOLERANCE_PCT", "10"))
TX_FEE_BUFFER_TAO = 0.005

__all__ = [
    "MEV_SHIELD_ERA_PERIOD",
    "MEV_SHIELD_MIN_TOLERANCE_PCT",
    "MEV_SHIELD_PUBLIC_KEY_SIZE",
    "TX_FEE_BUFFER_TAO",
    "build_add_stake_limit_batch_call",
    "effective_mev_tolerance",
    "get_mev_shield_encrypt_key",
    "limit_price_rao",
    "make_add_stake_limit_factory",
    "mev_shield_buy",
    "submit_mev_shield_extrinsic",
    "wait_for_extrinsic_inclusion",
    "wait_for_mev_shield_inner",
]


# ── Utilities ──────────────────────────────────────────────────────────────────

def limit_price_rao(current_price_tao: float, tolerance_pct: float) -> int:
    """Max acceptable price (rao) for add_stake_limit given tolerance above spot."""
    return int(current_price_tao * (1.0 + tolerance_pct / 100.0) * 1e9)


def effective_mev_tolerance(tolerance_pct: float) -> float:
    """MEV-shield execution is delayed — enforce a minimum tolerance floor."""
    if tolerance_pct < MEV_SHIELD_MIN_TOLERANCE_PCT:
        return MEV_SHIELD_MIN_TOLERANCE_PCT
    return tolerance_pct


def _log(msg: str, prefix: str = "[mevshield]") -> None:
    print(f"{prefix} {msg}")


def _coerce_mev_shield_public_key(raw: Any) -> bytes:
    """Parse MevShield NextKey / CurrentKey storage into 1184 raw bytes."""
    if raw is None:
        raise ValueError("MEV Shield public key not available on chain")
    if hasattr(raw, "value"):
        raw = raw.value
    if isinstance(raw, (bytes, bytearray)):
        public_key_bytes = bytes(raw)
    elif isinstance(raw, str):
        public_key_bytes = bytes.fromhex(raw.removeprefix("0x"))
    elif isinstance(raw, (list, tuple)):
        if len(raw) == 1 and isinstance(raw[0], (list, tuple)):
            public_key_bytes = bytes(raw[0])
        elif raw and isinstance(raw[0], int):
            public_key_bytes = bytes(raw)
        else:
            raise ValueError(f"Unexpected MEV Shield key list format: {raw!r}")
    else:
        raise ValueError(f"Unexpected MEV Shield key type: {type(raw)}")

    if len(public_key_bytes) != MEV_SHIELD_PUBLIC_KEY_SIZE:
        raise ValueError(
            f"Invalid MEV Shield key size: {len(public_key_bytes)} "
            f"(expected {MEV_SHIELD_PUBLIC_KEY_SIZE})"
        )
    return public_key_bytes


def _norm_extrinsic_hash(value: Any) -> str:
    if isinstance(value, bytes):
        return f"0x{value.hex()}"
    text = str(value)
    return text if text.startswith("0x") else f"0x{text}"


# ── Chain queries ──────────────────────────────────────────────────────────────

async def get_mev_shield_encrypt_key(
    subtensor: AsyncSubtensor,
    inclusion_block: int,
) -> bytes:
    """
    Public key for MevShield.submit_encrypted ciphertext.

    Block authors decrypt with CurrentKey(inclusion_block) == NextKey(inclusion_block - 2).
    """
    key_block = max(inclusion_block - 2, 0)
    result = await subtensor.query_module("MevShield", "NextKey", block=key_block)
    return _coerce_mev_shield_public_key(result)


async def _mev_shield_era(subtensor: AsyncSubtensor) -> dict:
    """Era shared by inner + shield; finalised head gives a wider forward window."""
    finalised_hash = await subtensor.substrate.get_chain_finalised_head()
    finalised_block = await subtensor.substrate.get_block_number(finalised_hash)
    return {"period": MEV_SHIELD_ERA_PERIOD, "current": finalised_block}


async def wait_for_extrinsic_inclusion(
    subtensor: AsyncSubtensor,
    extrinsic_hash: str,
    submit_block: int,
    timeout_blocks: int = 3,
) -> tuple[bool, object | None, int | None]:
    """Poll blocks after submit_block until the extrinsic lands or times out."""
    from async_substrate_interface import AsyncExtrinsicReceipt

    target = _norm_extrinsic_hash(extrinsic_hash)
    current = submit_block + 1
    last_checked = submit_block

    while current - submit_block <= timeout_blocks:
        if current > last_checked:
            try:
                await subtensor.wait_for_block(current)
            except Exception:
                await asyncio.sleep(12)

        block_hash = await subtensor.substrate.get_block_hash(current)
        extrinsics = await subtensor.substrate.get_extrinsics(block_hash)

        for idx, extrinsic in enumerate(extrinsics):
            ext_h = extrinsic.extrinsic_hash
            if hasattr(ext_h, "hex"):
                ext_h = ext_h.hex()
            if _norm_extrinsic_hash(ext_h) != target:
                continue
            receipt = AsyncExtrinsicReceipt(
                substrate=subtensor.substrate,
                block_hash=block_hash,
                block_number=current,
                extrinsic_idx=idx,
            )
            if await receipt.is_success:
                return True, receipt, current
            return False, receipt, current

        last_checked = current
        current += 1

    return False, None, None


async def wait_for_mev_shield_inner(
    subtensor: AsyncSubtensor,
    inner_hash: str,
    submit_block_hash: str,
    coldkey_ss58: str | None = None,
    netuid: int | None = None,
    timeout_blocks: int = 4,
) -> tuple[bool, str | None]:
    """Poll blocks until the decrypted inner extrinsic lands or times out."""
    from async_substrate_interface import AsyncExtrinsicReceipt
    from bittensor.utils import format_error_message

    starting_block = await subtensor.substrate.get_block_number(submit_block_hash)
    current_block = starting_block

    while current_block - starting_block <= timeout_blocks:
        block_hash = await subtensor.substrate.get_block_hash(current_block)
        extrinsics = await subtensor.substrate.get_extrinsics(block_hash)

        for idx, extrinsic in enumerate(extrinsics):
            if f"0x{extrinsic.extrinsic_hash.hex()}" != inner_hash:
                continue
            receipt = AsyncExtrinsicReceipt(
                substrate=subtensor.substrate,
                block_hash=block_hash,
                block_number=current_block,
                extrinsic_idx=idx,
            )
            if await receipt.is_success:
                return True, None
            return False, format_error_message(await receipt.error_message)

        if coldkey_ss58 and netuid is not None:
            events = await subtensor.substrate.get_events(block_hash)
            for event in events:
                ev = event.value if hasattr(event, "value") else event
                if ev.get("module_id") != "SubtensorModule":
                    continue
                if ev.get("event_id") != "StakeAdded":
                    continue
                attrs = ev.get("attributes", [])
                if isinstance(attrs, (list, tuple)) and len(attrs) >= 5:
                    ck = str(attrs[0])
                    nu = int(attrs[4])
                    if ck == coldkey_ss58 and nu == netuid:
                        return True, None

        current_block += 1

    return False, (
        f"MEV-shield inner {inner_hash[:18]}… not decrypted within "
        f"{timeout_blocks} blocks after #{starting_block}"
    )


# ── Inner call builder ─────────────────────────────────────────────────────────

async def build_add_stake_limit_batch_call(
    subtensor: AsyncSubtensor,
    *,
    netuid: int,
    hotkey_ss58: str,
    amount_rao: int,
    tolerance_pct: float,
    allow_partial: bool = False,
) -> object:
    """
    Build Utility.force_batch([SubtensorModule.add_stake_limit]).

    Refreshes subnet price on every call — use as inner_call_factory for retries.
    """
    tol = effective_mev_tolerance(tolerance_pct)
    chain_head = await subtensor.substrate.get_chain_head()
    subnet_info = await subtensor.subnet(netuid=netuid)
    cur_price = float(subnet_info.price.tao)
    limit_price = limit_price_rao(cur_price, tol)

    stake_call = await subtensor.substrate.compose_call(
        call_module="SubtensorModule",
        call_function="add_stake_limit",
        call_params={
            "hotkey": hotkey_ss58,
            "netuid": netuid,
            "amount_staked": amount_rao,
            "limit_price": limit_price,
            "allow_partial": allow_partial,
        },
        block_hash=chain_head,
    )
    return await subtensor.substrate.compose_call(
        call_module="Utility",
        call_function="force_batch",
        call_params={"calls": [stake_call]},
        block_hash=chain_head,
    )


def make_add_stake_limit_factory(
    subtensor: AsyncSubtensor,
    *,
    netuid: int,
    hotkey_ss58: str,
    amount_rao: int,
    tolerance_pct: float,
    allow_partial: bool = False,
) -> Callable[[], Awaitable[object]]:
    """Return an async factory suitable for submit_mev_shield_extrinsic."""

    async def factory() -> object:
        return await build_add_stake_limit_batch_call(
            subtensor,
            netuid=netuid,
            hotkey_ss58=hotkey_ss58,
            amount_rao=amount_rao,
            tolerance_pct=tolerance_pct,
            allow_partial=allow_partial,
        )

    return factory


# ── Core submit ────────────────────────────────────────────────────────────────

async def submit_mev_shield_extrinsic(
    subtensor: AsyncSubtensor,
    wallet: Wallet,
    inner_call: object | None,
    tip: int,
    *,
    netuid: int | None = None,
    inner_call_factory: Callable[[], Awaitable[object]] | None = None,
    confirm_inner: bool = True,
    log_prefix: str = "[mevshield]",
) -> tuple[bool, str]:
    """
    Encrypt inner call via MevShield.submit_encrypted and submit.

    Nonce layout per round: shield_nonce=N, inner_nonce=N+1 (reserved, not submitted).
    Returns (success, inner_extrinsic_hash).
    """
    from bittensor_drand import encrypt_mlkem768
    from async_substrate_interface.errors import SubstrateRequestException

    coldkey_ss58 = wallet.coldkeypub.ss58_address
    inner_hash = ""
    receipt = None
    max_submit_rounds = 3

    for submit_round in range(1, max_submit_rounds + 1):
        shield_nonce = await subtensor.substrate.get_account_next_index(
            coldkey_ss58, use_cache=False,
        )
        inner_nonce = shield_nonce + 1

        wrapper_ok = False
        incl_block = None
        public_key = None
        inclusion_block = 0

        for attempt in range(1, 4):
            head_block = await subtensor.get_current_block()
            inclusion_block = head_block + 1
            era = await _mev_shield_era(subtensor)

            call = await inner_call_factory() if inner_call_factory else inner_call

            inner_extrinsic = await subtensor.substrate.create_signed_extrinsic(
                call=call,
                keypair=wallet.coldkey,
                era=era,
                tip=0,
                nonce=inner_nonce,
            )
            inner_hash = f"0x{inner_extrinsic.extrinsic_hash.hex()}"

            _log(
                f"MEV-shield sign  round={submit_round}  block=#{head_block}  "
                f"era=#{era['current']}  shield_nonce={shield_nonce}  "
                f"inner_nonce={inner_nonce}",
                log_prefix,
            )

            public_key = await get_mev_shield_encrypt_key(subtensor, inclusion_block)
            ciphertext = encrypt_mlkem768(
                public_key,
                bytes(inner_extrinsic.data.data),
                include_key_hash=True,
            )
            shield_call = await subtensor.substrate.compose_call(
                call_module="MevShield",
                call_function="submit_encrypted",
                call_params={"ciphertext": ciphertext},
            )
            shield_extrinsic = await subtensor.substrate.create_signed_extrinsic(
                call=shield_call,
                keypair=wallet.coldkey,
                era=era,
                tip=tip,
                nonce=shield_nonce,
            )
            submit_block = head_block
            try:
                pending = await subtensor.substrate.submit_extrinsic(
                    shield_extrinsic,
                    wait_for_inclusion=False,
                    wait_for_finalization=False,
                )
            except (SubstrateRequestException, Exception) as exc:
                err = str(exc).lower()
                retryable = (
                    "outdated" in err or "invalid" in err or "priority is too low" in err
                )
                if retryable and attempt < 3:
                    reason = "era outdated" if "outdated" in err else "tx rejected"
                    _log(
                        f"MEV-shield {reason} at block #{head_block} "
                        f"(attempt {attempt}/3) — re-signing …",
                        log_prefix,
                    )
                    subtensor.substrate._nonces.pop(coldkey_ss58, None)
                    await asyncio.sleep(0.05)
                    continue
                subtensor.substrate._nonces.pop(coldkey_ss58, None)
                raise

            ext_hash = _norm_extrinsic_hash(pending.extrinsic_hash)
            _log(
                f"MEV-shield wrapper submitted {ext_hash[:20]}…  block=#{submit_block}",
                log_prefix,
            )

            ok, receipt, incl_block = await wait_for_extrinsic_inclusion(
                subtensor, ext_hash, submit_block, timeout_blocks=3,
            )
            if not ok:
                if receipt is not None:
                    from bittensor.utils import format_error_message
                    err = format_error_message(await receipt.error_message)
                    _log(f"MEV-shield wrapper failed block #{incl_block}: {err}", log_prefix)
                else:
                    _log(
                        f"MEV-shield wrapper not included within 3 blocks "
                        f"(attempt {attempt}/3) — re-signing …",
                        log_prefix,
                    )
                receipt = None
                subtensor.substrate._nonces.pop(coldkey_ss58, None)
                if attempt < 3:
                    continue
                break
            wrapper_ok = True
            break

        if not wrapper_ok:
            subtensor.substrate._nonces.pop(coldkey_ss58, None)
            continue

        submit_block = incl_block
        wrapper_block_hash = receipt.block_hash
        expected_key = await get_mev_shield_encrypt_key(subtensor, submit_block)
        if expected_key != public_key:
            _log(
                f"WARNING: encrypt key targeted block #{inclusion_block} "
                f"but wrapper landed in #{submit_block}",
                log_prefix,
            )
        _log(f"MEV-shield wrapper included block #{submit_block}", log_prefix)

        if not confirm_inner:
            _log(f"MEV-shield inner {inner_hash} (wrapper-only confirm)", log_prefix)
            return True, inner_hash

        _log(f"MEV-shield waiting for inner {inner_hash}", log_prefix)

        ok, err = await wait_for_mev_shield_inner(
            subtensor,
            inner_hash,
            wrapper_block_hash,
            coldkey_ss58=coldkey_ss58,
            netuid=netuid,
        )
        if ok:
            _log(f"MEV-shield inner confirmed: {inner_hash}", log_prefix)
            return True, inner_hash

        _log(f"MEV-shield inner not confirmed: {err}", log_prefix)
        if submit_round < max_submit_rounds:
            _log(
                f"MEV-shield retrying full submit (round {submit_round + 1}) …",
                log_prefix,
            )
            await asyncio.sleep(0.1)

    return False, inner_hash


# ── High-level buy ─────────────────────────────────────────────────────────────

async def mev_shield_buy(
    subtensor: AsyncSubtensor,
    wallet: Wallet,
    *,
    netuid: int,
    tao_amount: float,
    hotkey_ss58: str,
    tip: int,
    tolerance_pct: float = MEV_SHIELD_MIN_TOLERANCE_PCT,
    confirm_inner: bool = True,
    allow_partial: bool = False,
    log_prefix: str = "[mevshield]",
) -> tuple[bool, str]:
    """
    MEV-shield protected add_stake_limit buy.

    Parameters
    ----------
    netuid : subnet to stake on
    tao_amount : TAO to stake
    hotkey_ss58 : validator hotkey address to stake TO
    tip : priority tip in RAO for the shield wrapper extrinsic
    tolerance_pct : price slippage tolerance (%); auto-bumped to MEV_SHIELD_MIN_TOLERANCE_PCT
    confirm_inner : wait for inner StakeAdded before returning success
    allow_partial : pass through to add_stake_limit

    Returns
    -------
    (success, inner_extrinsic_hash)
    """
    coldkey_ss58 = wallet.coldkeypub.ss58_address
    tol = effective_mev_tolerance(tolerance_pct)
    if tol != tolerance_pct:
        _log(
            f"tolerance bumped {tolerance_pct}% → {tol}% (delayed execution)",
            log_prefix,
        )

    balance = await subtensor.get_balance(coldkey_ss58)
    required = tao_amount + TX_FEE_BUFFER_TAO
    if balance.tao < required:
        _log(
            f"ERROR: insufficient balance — need {required:.6f}, have {balance.tao:.6f} TAO",
            log_prefix,
        )
        return False, ""

    subnet_info = await subtensor.subnet(netuid=netuid)
    cur_price = float(subnet_info.price.tao)
    approx_alpha = tao_amount / cur_price if cur_price else 0
    _log(
        f"Subnet {netuid}  price={cur_price:.9f} TAO/α  "
        f"~{approx_alpha:.4f} α  tol={tol}%  amount={tao_amount} TAO",
        log_prefix,
    )

    amount_rao = int(tao_amount * 1e9)
    factory = make_add_stake_limit_factory(
        subtensor,
        netuid=netuid,
        hotkey_ss58=hotkey_ss58,
        amount_rao=amount_rao,
        tolerance_pct=tol,
        allow_partial=allow_partial,
    )

    t0 = time.time()
    _log(
        f"MEV-shield buy — encrypting force_batch(add_stake_limit) (era={MEV_SHIELD_ERA_PERIOD})",
        log_prefix,
    )
    try:
        ok, inner_hash = await submit_mev_shield_extrinsic(
            subtensor,
            wallet,
            None,
            tip,
            netuid=netuid,
            inner_call_factory=factory,
            confirm_inner=confirm_inner,
            log_prefix=log_prefix,
        )
        if ok:
            _log(f"✅  MEV-shield buy done in {time.time() - t0:.3f}s", log_prefix)
        return ok, inner_hash
    except Exception as exc:
        _log(f"❌  MEV-shield submit error: {exc}", log_prefix)
        return False, ""
