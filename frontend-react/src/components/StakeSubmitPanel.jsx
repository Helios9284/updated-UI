import { useEffect, useMemo, useRef, useState } from "react";
import { TransferStakeSidebar } from "./TransferStakeSidebar.jsx";

const SLIPPAGES = [-0.5, 0.2, 0.3, 0.5, 1, 3, 5, 8, 100];
const MIN_SLIPPAGE_PCT = -0.5;
const AMOUNTS = [0.5, 1, 2, 5, 10, 15, 20, 30, 50];
// When MEV protection is on, the order is encrypted/sniped so price impact is
// not a concern: force slippage to 100% and lock the slippage controls.
const MEV_FORCED_SLIPPAGE = 100;
const UNSTAKE_PERCENTS = [25, 50, 100];
const BACKEND_CHECK_MS = 5000;
const BACKEND_FAILS_BEFORE_OFFLINE = 2;

function shortenHotkey(value) {
  const text = String(value || "").trim();
  if (!text) return "-";
  if (text.length <= 16) return text;
  return `${text.slice(0, 8)}..${text.slice(-6)}`;
}

export function StakeSubmitPanel({
  apiBase,
  defaultUseProxy,
  portfolio,
  portfolioLoading,
  portfolioError,
  selectedNetuid,
  selectedNetuidTick,
  onRefreshPortfolio,
  onSubmitAdd,
  onSubmitRemoveFull,
  onSubmitRemoveAmount,
  onSubmitTransfer,
  onSubmitBatch,
  onRefreshNonce,
}) {
  const [netuid, setNetuid] = useState("");
  const [slippage, setSlippage] = useState("0.5");
  const [customSlippage, setCustomSlippage] = useState("");
  const [customAmount, setCustomAmount] = useState("");
  const [removeSlippageMap, setRemoveSlippageMap] = useState({});
  const [removePercentMap, setRemovePercentMap] = useState({});
  const [transferOpen, setTransferOpen] = useState(false);
  const [transferRow, setTransferRow] = useState(null);
  const [transferDestColdkey, setTransferDestColdkey] = useState("");
  const [transferPassword, setTransferPassword] = useState("");
  const [transferAlpha, setTransferAlpha] = useState("");
  const [transferBusy, setTransferBusy] = useState(false);
  const [mevProtection, setMevProtection] = useState(false);
  const [useProxy, setUseProxy] = useState(Boolean(defaultUseProxy));
  const [status, setStatus] = useState("");
  const [batchOps, setBatchOps] = useState([]);
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchAddNetuid, setBatchAddNetuid] = useState("");
  const [batchAddSlippage, setBatchAddSlippage] = useState("0.5");
  const [batchAddAmount, setBatchAddAmount] = useState("");
  const [refreshingNonce, setRefreshingNonce] = useState(false);
  const [backendOk, setBackendOk] = useState(true);
  const [backendWalletInfo, setBackendWalletInfo] = useState(null);
  const [mevDiagnostics, setMevDiagnostics] = useState(null);
  const [mevClientError, setMevClientError] = useState("");
  const [lastSubmitLatencyMs, setLastSubmitLatencyMs] = useState(null);
  // Shown in MEV diagnostics (easier to spot than status-line labels).
  const [lastLatencyBreakdown, setLastLatencyBreakdown] = useState(null);
  const netuidInputRef = useRef(null);

  const asLatencyMs = (v) => {
    if (typeof v === "number" && Number.isFinite(v)) return v;
    if (typeof v === "string" && v.trim() !== "") {
      const n = Number(v);
      if (Number.isFinite(n)) return n;
    }
    return null;
  };

  const applySubmitLatency = (result) => {
    const poolMs = asLatencyMs(result?.pool_submit_latency_ms);
    const composeMs = asLatencyMs(result?.compose_ms);
    const prepMs = asLatencyMs(result?.prep_ms);
    const lockMs = asLatencyMs(result?.lock_sign_ms);
    const signMs = asLatencyMs(result?.sign_ms);
    const innerSignMs = asLatencyMs(result?.inner_sign_ms);
    const outerSignMs = asLatencyMs(result?.outer_sign_ms);
    const encMs = asLatencyMs(result?.encrypt_ms);
    const rpcMs = asLatencyMs(result?.submit_rpc_ms);
    const materialsMs = asLatencyMs(result?.materials_ms);
    const waitMs = asLatencyMs(result?.soft_window_wait_ms);
    const totalMs = asLatencyMs(result?.submit_latency_ms);
    const displayMs = poolMs != null ? poolMs : totalMs;
    if (displayMs != null) setLastSubmitLatencyMs(displayMs);
    const parts = [];
    if (poolMs != null) parts.push(`pool ${poolMs}ms`);
    // Sub-phases that make up pool (exclude soft-window wait).
    // compose_ms and prep_ms are the same one-shot factory timing — show once.
    if (composeMs != null) parts.push(`compose ${composeMs}ms`);
    else if (prepMs != null) parts.push(`prep ${prepMs}ms`);
    if (lockMs != null && lockMs >= 1) parts.push(`lock ${lockMs}ms`);
    if (signMs != null) parts.push(`sign ${signMs}ms`);
    if (innerSignMs != null) parts.push(`in ${innerSignMs}ms`);
    const outerComposeMs = asLatencyMs(result?.outer_compose_ms);
    if (outerComposeMs != null) parts.push(`ocomp ${outerComposeMs}ms`);
    if (outerSignMs != null) parts.push(`out ${outerSignMs}ms`);
    if (encMs != null) parts.push(`enc ${encMs}ms`);
    if (rpcMs != null) parts.push(`rpc ${rpcMs}ms`);
    if (materialsMs != null) parts.push(`mat ${materialsMs}ms`);
    if (waitMs != null && waitMs >= 50) parts.push(`wait ${waitMs}ms`);
    if (totalMs != null) parts.push(`confirm ${totalMs}ms`);
    setLastLatencyBreakdown(parts.length ? parts.join(" · ") : null);
    return parts.length ? parts.join(" · ") : null;
  };

  const disabled = !apiBase || !backendOk;
  const nonceBusy = disabled || refreshingNonce;
  const netuidNum = useMemo(() => Number.parseInt(String(netuid || "").trim(), 10), [netuid]);
  const slippageLocked = Boolean(mevProtection);
  const activeSlippage = customSlippage ? Number(customSlippage) : Number(slippage);
  // Slippage actually sent: forced to 100% whenever MEV protection is enabled.
  const effectiveAddSlippage = slippageLocked ? MEV_FORCED_SLIPPAGE : activeSlippage;
  const removeSlippageFor = (key) =>
    slippageLocked ? MEV_FORCED_SLIPPAGE : Number(removeSlippageMap[key] || 15);
  const signerHotkey = String(backendWalletInfo?.hotkey || "");
  const rows = useMemo(() => {
    const arr = (portfolio?.portfolio || portfolio?.stakes || []).map((row) => ({
      ...row,
      hotkey: String(row?.hotkey || ""),
      stake_tao: row?.stake_tao ?? row?.tao ?? 0,
      alpha: row?.alpha ?? 0,
    }));
    const nonZero = arr.filter(
      (row) => Number(row.alpha) > 0 || Number(row.stake_tao) > 0
    );
    return nonZero.slice().sort((a, b) => {
      const nu = Number(a.netuid) - Number(b.netuid);
      if (nu !== 0) return nu;
      return String(a.hotkey).localeCompare(String(b.hotkey));
    });
  }, [portfolio]);

  useEffect(() => {
    if (!selectedNetuidTick) return;
    const onlyDigits = String(selectedNetuid || "").replace(/\D/g, "");
    setNetuid(onlyDigits);
    netuidInputRef.current?.focus();
    netuidInputRef.current?.select?.();
  }, [selectedNetuid, selectedNetuidTick]);

  useEffect(() => {
    if (!apiBase) {
      setBackendOk(false);
      return undefined;
    }
    let dead = false;
    let failStreak = 0;
    const check = async () => {
      // Prefer /ping (no RPC/locks). Fall back to /health for wallet/mev details.
      try {
        const pingRes = await fetch(`${apiBase}/ping`, {
          signal: AbortSignal.timeout(2500),
        });
        if (!pingRes.ok) throw new Error(`ping ${pingRes.status}`);
        failStreak = 0;
        if (!dead) setBackendOk(true);
      } catch {
        failStreak += 1;
        if (!dead && failStreak >= BACKEND_FAILS_BEFORE_OFFLINE) {
          setBackendOk(false);
        }
        return;
      }
      try {
        const res = await fetch(`${apiBase}/health`, {
          signal: AbortSignal.timeout(4000),
        });
        if (!res.ok) return;
        const body = await res.json();
        if (!dead) {
          setBackendWalletInfo(body?.wallet || null);
          setMevDiagnostics(body?.mev || null);
        }
      } catch {
        // ping already proved liveness — keep backendOk true
      }
    };
    void check();
    const timer = window.setInterval(check, BACKEND_CHECK_MS);
    return () => {
      dead = true;
      clearInterval(timer);
    };
  }, [apiBase]);

  const callAndStore = async (fn, label) => {
    try {
      const result = await fn();
      if (!result?.ok) {
        throw new Error(result?.error || "submission failed");
      }
      if (mevProtection) {
        setMevClientError("");
      }
      const hash = result?.tx_hash ? String(result.tx_hash).slice(0, 18) + "..." : "-";
      // Prefer pool latency (click → mempool). Total confirm also includes
      // waiting for the next block (~0–12s) and looks "slow".
      const latency = applySubmitLatency(result);
      const mode = result?.submit_mode ? String(result.submit_mode) : null;
      setStatus(`${label} submitted · ${hash}${latency ? ` · ${latency}` : ""}${mode ? ` · ${mode}` : ""}`);
      if (onRefreshPortfolio) {
        void onRefreshPortfolio();
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : "submission failed";
      if (mevProtection) {
        // Keep error visible in MEV diagnostics (status alone is easy to miss
        // while mempool still shows a long-lived pending wrapper).
        setMevClientError(`${label}: ${msg}`);
      }
      setStatus(`${label} failed: ${msg}`);
    }
  };

  function toRowNumbers(row) {
    const stakeTao = Number(row.stake_tao || 0);
    const stakeAlpha = Number(row.alpha || 0);
    const taoPerAlpha = stakeAlpha > 0 ? stakeTao / stakeAlpha : 0;
    return { stakeTao, stakeAlpha, taoPerAlpha };
  }

  async function refreshNonce() {
    if (!backendOk) {
      setStatus("nonce refresh failed: stake backend offline");
      return;
    }
    if (!onRefreshNonce) {
      setStatus("nonce refresh unavailable");
      return;
    }
    setRefreshingNonce(true);
    try {
      const result = await onRefreshNonce();
      if (!result?.ok) {
        throw new Error(result?.error || "nonce refresh failed");
      }
      const parts = [];
      if (result.signer_nonce != null) {
        parts.push(`signer ${result.signer_nonce}`);
      }
      if (result.proxy_nonce != null) {
        parts.push(`proxy ${result.proxy_nonce}`);
      }
      const block =
        result.block_number != null ? ` · block ${result.block_number}` : "";
      setStatus(
        `nonce synced · ${parts.length ? parts.join(" · ") : "ok"}${block}`
      );
    } catch (e) {
      setStatus(
        `nonce refresh failed: ${e instanceof Error ? e.message : "nonce refresh failed"}`
      );
    } finally {
      setRefreshingNonce(false);
    }
  }

  async function add(amountTao) {
    if (!backendOk) {
      setStatus("submit failed: stake backend offline");
      return;
    }
    const nu = Number(netuid);
    const amt = Number(amountTao);
    if (!Number.isFinite(nu) || nu < 0) {
      setStatus("invalid netuid");
      return;
    }
    if (!Number.isFinite(amt) || amt <= 0) {
      setStatus("invalid amount");
      return;
    }
    if (!Number.isFinite(effectiveAddSlippage) || effectiveAddSlippage < MIN_SLIPPAGE_PCT) {
      setStatus("invalid slippage");
      return;
    }
    setStatus(`add ${amt} TAO -> SN${nu} submitting...`);
    await callAndStore(
      () =>
        onSubmitAdd?.({
          netuid: nu,
          amount_tao: amt,
          slippage_pct: effectiveAddSlippage,
          use_proxy: useProxy,
          mev_protection: Boolean(mevProtection),
        }),
      "add"
    );
  }

  async function removeFull(row) {
    const key = `${row.netuid}-${row.hotkey}`;
    const slip = removeSlippageFor(key);
    setStatus(`remove full -> SN${row.netuid} submitting...`);
    await callAndStore(
      () =>
        onSubmitRemoveFull?.({
          netuid: Number(row.netuid),
          hotkey: String(row.hotkey || ""),
          mode: "full",
          slippage_pct: Number.isFinite(slip) ? slip : 15,
          use_proxy: useProxy,
          mev_protection: Boolean(mevProtection),
        }),
      "remove full"
    );
  }

  async function removeByAlpha(row, amountAlpha, sourceTao = null) {
    const key = `${row.netuid}-${row.hotkey}`;
    const amount = Number(amountAlpha || 0);
    const slip = removeSlippageFor(key);
    if (!Number.isFinite(amount) || amount <= 0) {
      setStatus("invalid remove alpha amount");
      return;
    }
    const sourceText =
      sourceTao != null && Number.isFinite(Number(sourceTao))
        ? ` (~${Number(sourceTao).toFixed(4)} TAO)`
        : "";
    setStatus(`remove ${amount.toFixed(6)} alpha${sourceText} -> SN${row.netuid} submitting...`);
    await callAndStore(
      () =>
        onSubmitRemoveAmount?.({
          netuid: Number(row.netuid),
          hotkey: String(row.hotkey || ""),
          amount: amount,
          slippage_pct: Number.isFinite(slip) ? slip : 15,
          use_proxy: useProxy,
          mev_protection: Boolean(mevProtection),
        }),
      "remove amount"
    );
  }

  async function removeByPercent(row, pct) {
    const percent = Number(pct);
    const { stakeTao, stakeAlpha } = toRowNumbers(row);
    if (!Number.isFinite(percent) || percent <= 0 || percent > 100) {
      setStatus("invalid remove percent");
      return;
    }
    if (percent >= 100) {
      await removeFull(row);
      return;
    }
    const alphaAmount = (stakeAlpha * percent) / 100;
    const taoAmount = (stakeTao * percent) / 100;
    await removeByAlpha(row, alphaAmount, taoAmount);
  }

  async function removeByCustomPercent(row) {
    const key = `${row.netuid}-${row.hotkey}`;
    const percent = Number(String(removePercentMap[key] ?? "").trim());
    if (!Number.isFinite(percent) || percent <= 0 || percent > 100) {
      setStatus("invalid custom unstake % (use 1–100)");
      return;
    }
    await removeByPercent(row, percent);
  }

  function openTransferPanel(row) {
    const { stakeAlpha } = toRowNumbers(row);
    setTransferRow(row);
    setTransferDestColdkey("");
    setTransferPassword("");
    setTransferAlpha(String(stakeAlpha || row.alpha || ""));
    setTransferOpen(true);
  }

  function closeTransferPanel() {
    setTransferOpen(false);
    setTransferRow(null);
    setTransferDestColdkey("");
    setTransferPassword("");
    setTransferAlpha("");
  }

  async function transferStake() {
    if (!transferRow) return;
    if (!backendOk) {
      setStatus("submit failed: stake backend offline");
      return;
    }
    const destColdkey = String(transferDestColdkey || "").trim();
    const { stakeAlpha } = toRowNumbers(transferRow);
    const rawAlpha = transferAlpha;
    const amountAlpha =
      rawAlpha === undefined || String(rawAlpha).trim() === ""
        ? stakeAlpha
        : Number(rawAlpha);
    const originNu = Number(transferRow.netuid);
    if (!destColdkey) {
      setStatus("destination coldkey is required");
      return;
    }
    const password = String(transferPassword || "").trim();
    if (!password) {
      setStatus("transfer password is required");
      return;
    }
    if (!Number.isFinite(amountAlpha) || amountAlpha <= 0) {
      setStatus("invalid transfer alpha amount");
      return;
    }
    if (amountAlpha > stakeAlpha + 1e-9) {
      setStatus(`transfer amount exceeds position alpha (${stakeAlpha})`);
      return;
    }
    setTransferBusy(true);
    setStatus(
      `transfer ${amountAlpha.toFixed(6)} alpha SN${originNu} → ${destColdkey.slice(0, 10)}... submitting...`
    );
    try {
      await callAndStore(
        () =>
          onSubmitTransfer?.({
            origin_netuid: originNu,
            destination_netuid: originNu,
            destination_coldkey: destColdkey,
            hotkey: String(transferRow.hotkey || ""),
            amount_alpha: amountAlpha,
            password,
            use_proxy: useProxy,
            mev_protection: Boolean(mevProtection),
          }),
        "transfer"
      );
      closeTransferPanel();
    } finally {
      setTransferBusy(false);
    }
  }

  const stageOp = (op) => {
    setBatchOps((prev) => [
      ...prev,
      { id: `${Date.now()}-${prev.length}-${Math.random().toString(36).slice(2, 7)}`, ...op },
    ]);
  };

  function stageAdd(amountTao) {
    const nu = Number(netuid);
    const amt = Number(amountTao);
    if (!Number.isFinite(nu) || nu < 0) {
      setStatus("invalid netuid");
      return;
    }
    if (!Number.isFinite(amt) || amt <= 0) {
      setStatus("invalid amount");
      return;
    }
    if (!Number.isFinite(effectiveAddSlippage) || effectiveAddSlippage < MIN_SLIPPAGE_PCT) {
      setStatus("invalid slippage");
      return;
    }
    stageOp({
      action: "add",
      netuid: nu,
      amount_tao: amt,
      slippage_pct: effectiveAddSlippage,
      label: `add ${amt}τ → SN${nu} @${effectiveAddSlippage}%`,
    });
  }

  function stageBatchAdd() {
    const nu = Number.parseInt(String(batchAddNetuid || "").trim(), 10);
    const amt = Number(batchAddAmount);
    const slip = slippageLocked ? MEV_FORCED_SLIPPAGE : Number(batchAddSlippage);
    if (!Number.isFinite(nu) || nu < 0) {
      setStatus("invalid batch netuid");
      return;
    }
    if (!Number.isFinite(amt) || amt <= 0) {
      setStatus("invalid batch amount");
      return;
    }
    if (!Number.isFinite(slip) || slip < MIN_SLIPPAGE_PCT) {
      setStatus("invalid batch slippage");
      return;
    }
    stageOp({
      action: "add",
      netuid: nu,
      amount_tao: amt,
      slippage_pct: slip,
      label: `add ${amt}τ → SN${nu} @${slip}%`,
    });
    setBatchAddAmount("");
  }

  const removeFromBatch = (id) => setBatchOps((prev) => prev.filter((o) => o.id !== id));
  const clearBatch = () => setBatchOps([]);

  async function submitBatch() {
    if (!backendOk) {
      setStatus("submit failed: stake backend offline");
      return;
    }
    if (!onSubmitBatch) {
      setStatus("batch submit unavailable");
      return;
    }
    if (batchOps.length === 0) {
      setStatus("batch is empty");
      return;
    }
    setBatchBusy(true);
    setStatus(`force_batch submitting ${batchOps.length} ops...`);
    try {
      const ops = batchOps.map((o) => {
        const op = { action: o.action, netuid: o.netuid };
        if (o.hotkey) op.hotkey = o.hotkey;
        if (o.amount_tao != null) op.amount_tao = o.amount_tao;
        if (o.amount_alpha != null) op.amount_alpha = o.amount_alpha;
        // MEV on -> force 100% regardless of what was staged earlier.
        if (slippageLocked) op.slippage_pct = MEV_FORCED_SLIPPAGE;
        else if (o.slippage_pct != null) op.slippage_pct = o.slippage_pct;
        return op;
      });
      const result = await onSubmitBatch({
        use_proxy: useProxy,
        mev_protection: Boolean(mevProtection),
        ops,
      });
      if (!result?.ok) {
        throw new Error(result?.error || "batch submission failed");
      }
      const hash = result?.tx_hash ? `${String(result.tx_hash).slice(0, 18)}...` : "-";
      const latency = applySubmitLatency(result);
      const mode = result?.submit_mode ? String(result.submit_mode) : null;
      setStatus(
        `force_batch submitted · ${hash} · ${result.op_count ?? ops.length} ops${
          latency ? ` · ${latency}` : ""
        }${mode ? ` · ${mode}` : ""}`
      );
      if (mevProtection) {
        setMevClientError("");
      }
      setBatchOps([]);
      if (onRefreshPortfolio) {
        void onRefreshPortfolio();
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : "batch submission failed";
      if (mevProtection) {
        setMevClientError(`force_batch: ${msg}`);
      }
      setStatus(`force_batch failed: ${msg}`);
    } finally {
      setBatchBusy(false);
    }
  }

  return (
    <section className="panel stake-panel">
      <div className="panel-title">Stake</div>
      {!backendOk && (
        <div className="portfolio-msg mono">
          stake backend offline ({apiBase || "set VITE_STAKE_API_BASE"})
        </div>
      )}
      {portfolioError ? <div className="auto-unstake-error mono">{portfolioError}</div> : null}
      <div className="portfolio-summary stake-portfolio-summary mono">
        <div className="stake-portfolio-summary-main">
          <div>free tao: {portfolio?.free_tao ?? portfolio?.free_balance ?? "-"}</div>
          <div>total tao: {portfolio?.total_tao ?? portfolio?.portfolio_total_tao ?? "-"}</div>
          {portfolio?.updated_at_block != null ? (
            <div>portfolio block: {portfolio.updated_at_block}</div>
          ) : null}
        </div>
        <div
          className="stake-submit-latency"
          title="Time from click to mempool accept (pool). Confirm wait is separate."
        >
          <div className="stake-submit-latency-label">pool latency</div>
          <div className="stake-submit-latency-value">
            {lastSubmitLatencyMs != null ? `${lastSubmitLatencyMs}ms` : "—"}
          </div>
        </div>
      </div>

      <div className="stake-controls">
        <div className="stake-inline stake-inline-actions">
          <label className="field-label stake-toggle-inline">
            <input
              type="checkbox"
              checked={mevProtection}
              onChange={(e) => setMevProtection(e.target.checked)}
              disabled={nonceBusy}
            />
            <span>M</span>
          </label>
          <label className="field-label stake-toggle-inline">
            <input
              type="checkbox"
              checked={useProxy}
              onChange={(e) => setUseProxy(e.target.checked)}
              disabled={nonceBusy}
            />
            <span>P</span>
          </label>
          <button
            type="button"
            className="action-btn stake-nonce-refresh-btn"
            disabled={nonceBusy}
            onClick={() => void refreshNonce()}
            title="Re-sync local nonce cache with chain"
          >
            {refreshingNonce ? "Syncing..." : "Nonce sync"}
          </button>
        </div>
      </div>

      <div className="stake-controls">
        <label className="field-label">Netuid</label>
        <input
          ref={netuidInputRef}
          className="wallet-input mono"
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          maxLength={3}
          value={netuid}
          onChange={(e) => setNetuid(String(e.target.value || "").replace(/\D/g, ""))}
        />
      </div>

      <div className="stake-controls">
        <label className="field-label">
          Slippage{slippageLocked ? " · MEV: 100% (locked)" : ""}
        </label>
        <div className="stake-btn-row">
          {SLIPPAGES.map((s) => (
            <button
              key={s}
              className={`action-btn ${
                !slippageLocked && String(s) === String(slippage) && !customSlippage ? "active" : ""
              }`}
              disabled={slippageLocked}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                setSlippage(String(s));
                setCustomSlippage("");
              }}
            >
              {s}%
            </button>
          ))}
          <input
            className="wallet-input mono"
            type="number"
            step="0.01"
            placeholder={slippageLocked ? "100 (MEV)" : "custom %"}
            value={slippageLocked ? "" : customSlippage}
            disabled={slippageLocked}
            onChange={(e) => setCustomSlippage(e.target.value)}
          />
        </div>
      </div>

      <div className="stake-controls">
        <label className="field-label">Add Stake (TAO)</label>
        <div className="stake-btn-row">
          {AMOUNTS.map((a) => (
            <button
              key={a}
              className="action-btn"
              disabled={disabled}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => add(a)}
            >
              {a}
            </button>
          ))}
          <input
            className="wallet-input mono"
            type="number"
            step="any"
            placeholder="custom TAO"
            value={customAmount}
            onChange={(e) => setCustomAmount(e.target.value)}
          />
          <button
            className="action-btn"
            disabled={disabled}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => add(customAmount)}
          >
            Add
          </button>
          <button
            className="action-btn"
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => stageAdd(customAmount)}
            title="Stage this add into the batch (force_batch)"
          >
            +
          </button>
        </div>
      </div>

      <div className="stake-controls">
        <label className="field-label">Remove Stake Panel (Loaded Portfolio)</label>
        {signerHotkey ? (
          <div className="mono field-label" title={signerHotkey}>
            signer: {shortenHotkey(signerHotkey)}
            {backendWalletInfo?.hotkey_name ? ` (${backendWalletInfo.hotkey_name})` : ""}
          </div>
        ) : null}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>netuid</th>
                <th>stake τ</th>
                <th>slip %</th>
                <th>unstake</th>
              </tr>
            </thead>
            <tbody>
              {portfolioLoading && rows.length === 0 ? (
                <tr>
                  <td colSpan={4} className="mono">
                    loading...
                  </td>
                </tr>
              ) : rows.length === 0 ? (
                <tr>
                  <td colSpan={4} className="mono">
                    no positions
                  </td>
                </tr>
              ) : (
                rows.map((row) => {
                  const key = `${row.netuid}-${row.hotkey}`;
                  return (
                    <tr key={key}>
                      <td className="mono">{row.netuid}</td>
                      <td className="mono">{row.stake_tao}</td>
                      <td>
                        <input
                          className="wallet-input mono slippage-input"
                          type="number"
                          step="0.01"
                          min="0"
                          placeholder="15"
                          value={slippageLocked ? "100" : removeSlippageMap[key] || "15"}
                          disabled={slippageLocked}
                          title={slippageLocked ? "MEV protection: slippage locked at 100%" : undefined}
                          onChange={(e) =>
                            setRemoveSlippageMap((prev) => ({ ...prev, [key]: e.target.value }))
                          }
                        />
                      </td>
                      <td>
                        <div className="stake-inline stake-unstake-btns">
                          {UNSTAKE_PERCENTS.map((pct) => (
                            <button
                              key={pct}
                              type="button"
                              className="action-btn danger"
                              disabled={disabled}
                              onClick={() => void removeByPercent(row, pct)}
                            >
                              {pct}%
                            </button>
                          ))}
                          <input
                            className="wallet-input mono remove-value-input"
                            type="number"
                            step="any"
                            min="0"
                            max="100"
                            placeholder="custom %"
                            value={removePercentMap[key] ?? ""}
                            disabled={disabled}
                            onChange={(e) =>
                              setRemovePercentMap((prev) => ({ ...prev, [key]: e.target.value }))
                            }
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void removeByCustomPercent(row);
                            }}
                          />
                          <button
                            type="button"
                            className="action-btn danger stake-unstake-submit-btn"
                            disabled={disabled}
                            onClick={() => void removeByCustomPercent(row)}
                            title="Unstake custom % of this position"
                          >
                            Unstake
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      <div className="stake-controls">
        <div className="stake-inline" style={{ justifyContent: "space-between" }}>
          <label className="field-label">Batch queue ({batchOps.length})</label>
          <div className="stake-inline">
            <button
              type="button"
              className="action-btn"
              disabled={disabled || batchBusy || batchOps.length === 0}
              onClick={() => void submitBatch()}
              title="Submit all staged ops as one Utility.force_batch extrinsic"
            >
              {batchBusy ? "Submitting..." : "Submit batch"}
            </button>
            <button
              type="button"
              className="action-btn"
              disabled={batchBusy || batchOps.length === 0}
              onClick={clearBatch}
            >
              Clear
            </button>
          </div>
        </div>
        <div className="stake-inline batch-add-row">
          <input
            className="wallet-input mono"
            type="text"
            inputMode="numeric"
            pattern="[0-9]*"
            maxLength={3}
            placeholder="netuid"
            value={batchAddNetuid}
            onChange={(e) => setBatchAddNetuid(String(e.target.value || "").replace(/\D/g, ""))}
          />
          <input
            className="wallet-input mono"
            type="number"
            step="any"
            min="0"
            placeholder="TAO"
            value={batchAddAmount}
            onChange={(e) => setBatchAddAmount(e.target.value)}
          />
          <input
            className="wallet-input mono"
            type="number"
            step="0.01"
            min="0"
            placeholder={slippageLocked ? "100 (MEV)" : "slip %"}
            value={slippageLocked ? "100" : batchAddSlippage}
            disabled={slippageLocked}
            title={slippageLocked ? "MEV protection: slippage locked at 100%" : undefined}
            onChange={(e) => setBatchAddSlippage(e.target.value)}
          />
          <button
            type="button"
            className="action-btn"
            onClick={stageBatchAdd}
            title="Stage an add op into the batch"
          >
            + Add
          </button>
        </div>
        {batchOps.length === 0 ? (
          <div className="mono field-label">empty — use “+ Add” above to stage add ops</div>
        ) : (
          <ul className="batch-queue-list mono">
            {batchOps.map((op) => (
              <li key={op.id} className="batch-queue-item">
                <span>{op.label}</span>
                <button
                  type="button"
                  className="action-btn"
                  disabled={batchBusy}
                  onClick={() => removeFromBatch(op.id)}
                  title="Remove from batch"
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <TransferStakeSidebar
        open={transferOpen}
        row={transferRow}
        destColdkey={transferDestColdkey}
        onDestColdkeyChange={setTransferDestColdkey}
        password={transferPassword}
        onPasswordChange={setTransferPassword}
        walletReal={String(backendWalletInfo?.real || "")}
        alphaAmount={transferAlpha}
        onAlphaAmountChange={setTransferAlpha}
        disabled={disabled}
        busy={transferBusy}
        onClose={closeTransferPanel}
        onSubmit={transferStake}
      />

      <div className="stake-status mono">{status}</div>

      {mevProtection ? (
        <div className="mev-diagnostics">
          <div className="mev-diagnostics-title mono">MEV protection — bugs & live status</div>
          {lastLatencyBreakdown ? (
            <div className="mev-diagnostics-meta mono" title="Last MEV submit timing breakdown">
              last submit · {lastLatencyBreakdown}
            </div>
          ) : (
            <div className="mev-diagnostics-meta mono">
              last submit · (after next MEV submit: pool / mat / wait / confirm)
            </div>
          )}
          {mevClientError ? (
            <div className="mev-diagnostics-item mev-level-error mono">{mevClientError}</div>
          ) : null}
          {(mevDiagnostics?.recent_errors || []).map((row, i) => (
            <div key={`mev-err-${i}`} className="mev-diagnostics-item mev-level-error mono">
              {row.message}
            </div>
          ))}
          {(mevDiagnostics?.warnings || []).map((row, i) => (
            <div
              key={`mev-warn-${i}`}
              className={`mev-diagnostics-item mev-level-${row.level || "info"} mono`}
            >
              {row.message}
            </div>
          ))}
          {mevDiagnostics?.block_age_sec != null ? (
            <div className="mev-diagnostics-meta mono">
              block #{mevDiagnostics.block_number ?? "-"} · age {mevDiagnostics.block_age_sec}s ·
              sign window ≤ {mevDiagnostics?.sign_window?.immediate_sec ?? "?"}s
            </div>
          ) : null}
          {(mevDiagnostics?.nonce || []).length > 0 ? (
            <div className="mev-diagnostics-meta mono">
              nonce —{" "}
              {mevDiagnostics.nonce
                .map((n) => `${n.role} chain ${n.chain ?? "?"} hint ${n.hint ?? "?"}`)
                .join(" · ")}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
