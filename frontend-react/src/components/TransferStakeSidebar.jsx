import { useEffect, useRef } from "react";

function shortenHotkey(value) {
  const text = String(value || "").trim();
  if (!text) return "-";
  if (text.length <= 16) return text;
  return `${text.slice(0, 8)}..${text.slice(-6)}`;
}

export function TransferStakeSidebar({
  open,
  row,
  destColdkey,
  onDestColdkeyChange,
  password,
  onPasswordChange,
  walletReal,
  alphaAmount,
  onAlphaAmountChange,
  disabled,
  busy,
  onClose,
  onSubmit,
}) {
  const destInputRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (e) => {
      if (e.key === "Escape") onClose?.();
    };
    window.addEventListener("keydown", onKeyDown);
    const timer = window.setTimeout(() => destInputRef.current?.focus(), 220);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.clearTimeout(timer);
    };
  }, [open, onClose]);

  if (!row) return null;

  const stakeAlpha = Number(row.alpha || 0);
  const stakeTao = Number(row.stake_tao || 0);
  const originNetuid = Number(row.netuid);

  return (
    <>
      <div
        className={`transfer-sidebar-backdrop${open ? " open" : ""}`}
        onClick={() => onClose?.()}
        aria-hidden={!open}
      />
      <aside
        className={`transfer-sidebar${open ? " open" : ""}`}
        aria-hidden={!open}
        aria-label="Transfer stake"
      >
        <div className="transfer-sidebar-header">
          <div>
            <div className="transfer-sidebar-title">Transfer Stake</div>
            <div className="transfer-sidebar-sub mono">
              SN{originNetuid} · same hotkey · recipient coldkey
            </div>
          </div>
          <button type="button" className="action-btn" onClick={() => onClose?.()} aria-label="Close">
            ✕
          </button>
        </div>

        <div className="transfer-sidebar-body">
          <div className="transfer-sidebar-section">
            <div className="field-label">Origin</div>
            <div className="transfer-sidebar-kv mono">
              <span>netuid</span>
              <span>{originNetuid}</span>
            </div>
            <div className="transfer-sidebar-kv mono">
              <span>dest netuid</span>
              <span>{originNetuid}</span>
            </div>
            <div className="transfer-sidebar-kv mono">
              <span>hotkey</span>
              <span title={String(row.hotkey || "")}>{shortenHotkey(row.hotkey)}</span>
            </div>
            <div className="transfer-sidebar-kv mono">
              <span>position</span>
              <span>
                {stakeAlpha} α · {stakeTao} τ
              </span>
            </div>
          </div>

          <div className="transfer-sidebar-section">
            <label className="field-label" htmlFor="transfer-dest-coldkey">
              Destination coldkey
            </label>
            <div className="stake-inline">
              <input
                id="transfer-dest-coldkey"
                ref={destInputRef}
                className="wallet-input mono"
                type="text"
                spellCheck={false}
                autoComplete="off"
                placeholder="5..."
                value={destColdkey}
                onChange={(e) => onDestColdkeyChange?.(e.target.value)}
              />
              {walletReal ? (
                <button
                  type="button"
                  className="action-btn"
                  title={`Fill ${walletReal}`}
                  onClick={() => onDestColdkeyChange?.(walletReal)}
                >
                  Mine
                </button>
              ) : null}
            </div>
          </div>

          <div className="transfer-sidebar-section">
            <label className="field-label" htmlFor="transfer-password">
              Password
            </label>
            <input
              id="transfer-password"
              className="wallet-input mono"
              type="password"
              autoComplete="off"
              placeholder="transfer confirmation password"
              value={password}
              onChange={(e) => onPasswordChange?.(e.target.value)}
            />
          </div>

          <div className="transfer-sidebar-section">
            <label className="field-label" htmlFor="transfer-alpha-amount">
              Alpha amount
            </label>
            <div className="stake-inline">
              <input
                id="transfer-alpha-amount"
                className="wallet-input mono"
                type="number"
                step="any"
                min="0"
                placeholder={String(stakeAlpha || "")}
                value={alphaAmount}
                onChange={(e) => onAlphaAmountChange?.(e.target.value)}
              />
              <button
                type="button"
                className="action-btn"
                onClick={() => onAlphaAmountChange?.(String(stakeAlpha || ""))}
              >
                MAX
              </button>
            </div>
            <div className="transfer-sidebar-hint mono">
              destination_netuid is the same as origin. Stake moves to the recipient coldkey on
              SN{originNetuid} under the same hotkey. No slippage limit on transfer_stake.
            </div>
          </div>
        </div>

        <div className="transfer-sidebar-footer">
          <button type="button" className="action-btn" onClick={() => onClose?.()} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="action-btn"
            disabled={disabled || busy}
            onClick={() => void onSubmit?.()}
          >
            {busy ? "Submitting..." : "Submit transfer"}
          </button>
        </div>
      </aside>
    </>
  );
}
