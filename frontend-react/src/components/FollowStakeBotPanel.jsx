import { useEffect, useRef, useState } from "react";
import { CollapsiblePanel } from "./CollapsiblePanel";

const EMPTY_DRAFT = {
  address: "",
  source: "both",
  watchAdd: true,
  watchRemove: true,
  amount_tao: "1",
  slippage_pct: "0.5",
};

function normalizeTarget(raw) {
  const address = String(raw?.address || "").trim();
  if (!address) return null;
  const source = ["mempool", "block", "both"].includes(String(raw?.source || "").toLowerCase())
    ? String(raw.source).toLowerCase()
    : "both";
  const callTypes = Array.isArray(raw?.call_types) ? raw.call_types.map(String) : [];
  const watchAdd = callTypes.length === 0 ? true : callTypes.includes("add");
  const watchRemove = callTypes.length === 0 ? true : callTypes.includes("remove");
  const amount = Number(raw?.amount_tao);
  const slippage = Number(raw?.slippage_pct);
  if (!Number.isFinite(amount) || amount <= 0) return null;
  return {
    address,
    source,
    call_types: [
      ...(watchAdd ? ["add"] : []),
      ...(watchRemove ? ["remove"] : []),
    ],
    amount_tao: amount,
    slippage_pct: Number.isFinite(slippage) && slippage >= 0 ? slippage : 0.5,
  };
}

function targetsKey(targets) {
  return JSON.stringify(
    (targets || []).map((t) => ({
      address: t.address,
      source: t.source,
      call_types: [...(t.call_types || [])].sort(),
      amount_tao: t.amount_tao,
      slippage_pct: t.slippage_pct,
    }))
  );
}

function draftToTarget(draft) {
  const call_types = [];
  if (draft.watchAdd) call_types.push("add");
  if (draft.watchRemove) call_types.push("remove");
  return normalizeTarget({
    address: draft.address,
    source: draft.source,
    call_types,
    amount_tao: Number(draft.amount_tao),
    slippage_pct: Number(draft.slippage_pct),
  });
}

function targetToDraft(target) {
  const callTypes = target?.call_types || [];
  return {
    address: String(target?.address || ""),
    source: String(target?.source || "both"),
    watchAdd: callTypes.includes("add"),
    watchRemove: callTypes.includes("remove"),
    amount_tao: String(target?.amount_tao ?? ""),
    slippage_pct: String(target?.slippage_pct ?? "0.5"),
  };
}

export function FollowStakeBotPanel({ config, error, onSave, onClearLogs }) {
  const [enabled, setEnabled] = useState(false);
  const [targets, setTargets] = useState([]);
  const [draft, setDraft] = useState(EMPTY_DRAFT);
  const [editingAddress, setEditingAddress] = useState(null);
  const [saving, setSaving] = useState(false);
  const [clearingLogs, setClearingLogs] = useState(false);
  const hydratedRef = useRef(false);
  const lastSyncedKeyRef = useRef("");
  const recentLogs = Array.isArray(config?.recent) ? config.recent : [];

  useEffect(() => {
    if (!config) return;
    const normalized = (config.targets || []).map(normalizeTarget).filter(Boolean);
    const key = targetsKey(normalized);
    if (!hydratedRef.current) {
      setEnabled(Boolean(config.enabled));
      setTargets(normalized);
      hydratedRef.current = true;
      lastSyncedKeyRef.current = key;
      return;
    }
    if (key !== lastSyncedKeyRef.current) {
      setEnabled(Boolean(config.enabled));
      setTargets(normalized);
      lastSyncedKeyRef.current = key;
    }
  }, [config]);

  const persist = async (nextEnabled, nextTargets) => {
    if (!onSave) return null;
    setSaving(true);
    try {
      const body = await onSave({
        enabled: nextEnabled,
        targets: nextTargets,
      });
      if (body) {
        const normalized = (body.targets || []).map(normalizeTarget).filter(Boolean);
        setEnabled(Boolean(body.enabled));
        setTargets(normalized);
        lastSyncedKeyRef.current = targetsKey(normalized);
      }
      return body;
    } finally {
      setSaving(false);
    }
  };

  const cancelEdit = () => {
    setEditingAddress(null);
    setDraft(EMPTY_DRAFT);
  };

  const startEdit = (target) => {
    setEditingAddress(target.address);
    setDraft(targetToDraft(target));
  };

  const submitDraft = async () => {
    const nextTarget = draftToTarget(draft);
    if (!nextTarget) return;
    if (!draft.watchAdd && !draft.watchRemove) return;
    let next;
    if (editingAddress) {
      next = targets.filter((t) => t.address !== editingAddress && t.address !== nextTarget.address);
      next = [...next, nextTarget];
    } else {
      next = [...targets.filter((t) => t.address !== nextTarget.address), nextTarget];
    }
    setTargets(next);
    cancelEdit();
    await persist(enabled, next);
  };

  const removeTarget = async (address) => {
    if (editingAddress === address) cancelEdit();
    const next = targets.filter((t) => t.address !== address);
    setTargets(next);
    await persist(enabled, next);
  };

  const toggleEnabled = async () => {
    const next = !enabled;
    setEnabled(next);
    await persist(next, targets);
  };

  const clearLogs = async () => {
    if (!onClearLogs) return;
    setClearingLogs(true);
    try {
      await onClearLogs();
    } finally {
      setClearingLogs(false);
    }
  };

  return (
    <CollapsiblePanel
      title="Follow Stake Bot"
      storageKey="ui.follow-stake-bot.open"
      className="auto-unstake-panel"
    >
      <p className="auto-unstake-hint mono">
        When a target adds or removes stake (per call type filter), submit add_stake on the same
        netuid using the fixed amount and slippage for that target.
      </p>
      <div className="auto-unstake-controls">
        <label className="auto-unstake-label">
          <input type="checkbox" checked={enabled} disabled={saving} onChange={toggleEnabled} /> enabled
        </label>

        <label className="auto-unstake-label">{editingAddress ? "edit target" : "add target"}</label>
        <div className="follow-stake-draft-grid mono">
          <input
            className="portfolio-input"
            type="text"
            placeholder="address"
            value={draft.address}
            disabled={saving}
            onChange={(e) => setDraft((prev) => ({ ...prev, address: e.target.value }))}
          />
          <select
            className="portfolio-input"
            value={draft.source}
            disabled={saving}
            onChange={(e) => setDraft((prev) => ({ ...prev, source: e.target.value }))}
          >
            <option value="both">both</option>
            <option value="mempool">mempool</option>
            <option value="block">block</option>
          </select>
          <input
            className="portfolio-input"
            type="text"
            placeholder="amount tao"
            value={draft.amount_tao}
            disabled={saving}
            onChange={(e) => setDraft((prev) => ({ ...prev, amount_tao: e.target.value }))}
          />
          <input
            className="portfolio-input"
            type="text"
            placeholder="slippage %"
            value={draft.slippage_pct}
            disabled={saving}
            onChange={(e) => setDraft((prev) => ({ ...prev, slippage_pct: e.target.value }))}
          />
        </div>
        <div className="follow-stake-call-types mono">
          <label>
            <input
              type="checkbox"
              checked={draft.watchAdd}
              disabled={saving}
              onChange={(e) => setDraft((prev) => ({ ...prev, watchAdd: e.target.checked }))}
            />{" "}
            watch add
          </label>
          <label>
            <input
              type="checkbox"
              checked={draft.watchRemove}
              disabled={saving}
              onChange={(e) => setDraft((prev) => ({ ...prev, watchRemove: e.target.checked }))}
            />{" "}
            watch remove
          </label>
          <button type="button" className="portfolio-btn" disabled={saving} onClick={submitDraft}>
            {editingAddress ? "Update" : "Add"}
          </button>
          {editingAddress ? (
            <button type="button" className="portfolio-btn" disabled={saving} onClick={cancelEdit}>
              Cancel
            </button>
          ) : null}
        </div>

        <label className="auto-unstake-label">targets</label>
        <div className="follow-stake-target-list mono">
          {targets.length === 0 ? (
            <div className="target-list-empty">-</div>
          ) : (
            targets.map((t) => (
              <div
                key={t.address}
                className={`follow-stake-target-item${editingAddress === t.address ? " is-editing" : ""}`}
              >
                <div className="follow-stake-target-main" title={t.address}>
                  {t.address}
                </div>
                <div className="follow-stake-target-meta">
                  {t.source} · {(t.call_types || []).join("/")} · {t.amount_tao} tao · slip{" "}
                  {t.slippage_pct}%
                </div>
                <div className="follow-stake-target-actions">
                  <button
                    type="button"
                    className="portfolio-btn target-edit-btn"
                    disabled={saving}
                    onClick={() => startEdit(t)}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="portfolio-btn target-remove-btn"
                    disabled={saving}
                    onClick={() => removeTarget(t.address)}
                  >
                    X
                  </button>
                </div>
              </div>
            ))
          )}
        </div>

        <div className="auto-unstake-actions">
          <button
            type="button"
            className="portfolio-btn"
            disabled={saving}
            onClick={() => persist(enabled, targets)}
          >
            {saving ? "Saving..." : "Save"}
          </button>
          <button
            type="button"
            className="portfolio-btn"
            disabled={clearingLogs || recentLogs.length === 0}
            onClick={clearLogs}
          >
            {clearingLogs ? "Clearing..." : "Clear Logs"}
          </button>
        </div>

        <label className="auto-unstake-label">trigger logs</label>
        <div className="auto-unstake-log-wrap mono">
          {recentLogs.length === 0 ? (
            <div className="target-list-empty">-</div>
          ) : (
            recentLogs
              .slice()
              .reverse()
              .map((log, idx) => (
                <div key={`fsl-${idx}`} className="auto-unstake-log-item">
                  [{String(log?.result || "-")}] netuid {String(log?.netuid ?? "-")} source{" "}
                  {String(log?.source || "-")} side {String(log?.call_side || "-")} amt{" "}
                  {String(log?.amount_tao ?? "-")} slip {String(log?.slippage_pct ?? "-")}%{" "}
                  {String(log?.tx_hash ? `tx ${log.tx_hash}` : "")}{" "}
                  {String(log?.error || "")}
                </div>
              ))
          )}
        </div>
        {error ? <div className="auto-unstake-error mono">{error}</div> : null}
      </div>
    </CollapsiblePanel>
  );
}
