import { useEffect, useRef, useState } from "react";
import { CollapsiblePanel } from "./CollapsiblePanel";

function parseTargetsInput(text) {
  return String(text || "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

function targetsKey(mempoolTargets, blockTargets) {
  return JSON.stringify({
    m: [...(mempoolTargets || [])].sort(),
    b: [...(blockTargets || [])].sort(),
  });
}

function TargetList({ targets, type, disabled, onRemove }) {
  if (targets.length === 0) {
    return <div className="target-list-empty mono">-</div>;
  }
  return (
    <ul className="target-list mono">
      {targets.map((addr) => (
        <li key={`${type}-${addr}`} className="target-list-item">
          <span className="target-list-text" title={addr}>
            {addr}
          </span>
          <button
            type="button"
            className="portfolio-btn target-remove-btn"
            disabled={disabled}
            onClick={() => onRemove(addr)}
          >
            X
          </button>
        </li>
      ))}
    </ul>
  );
}

export function AutoUnstakePanel({ config, error, onSave, onClearLogs }) {
  const [mempoolTargets, setMempoolTargets] = useState([]);
  const [blockTargets, setBlockTargets] = useState([]);
  const [mempoolDraft, setMempoolDraft] = useState("");
  const [blockDraft, setBlockDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [clearingLogs, setClearingLogs] = useState(false);
  const hydratedRef = useRef(false);
  const lastSyncedKeyRef = useRef("");
  const recentLogs = Array.isArray(config?.recent) ? config.recent : [];

  useEffect(() => {
    if (!config) return;
    const key = targetsKey(config.mempool_targets, config.block_targets);
    if (!hydratedRef.current) {
      setMempoolTargets(config.mempool_targets || []);
      setBlockTargets(config.block_targets || []);
      hydratedRef.current = true;
      lastSyncedKeyRef.current = key;
      return;
    }
    if (key !== lastSyncedKeyRef.current) {
      setMempoolTargets(config.mempool_targets || []);
      setBlockTargets(config.block_targets || []);
      lastSyncedKeyRef.current = key;
    }
  }, [config]);

  const persistTargets = async (nextMempool, nextBlock) => {
    if (!onSave) return null;
    setSaving(true);
    try {
      const body = await onSave({
        mempool_targets: nextMempool,
        block_targets: nextBlock,
      });
      if (body) {
        const mem = body.mempool_targets || [];
        const blk = body.block_targets || [];
        setMempoolTargets(mem);
        setBlockTargets(blk);
        lastSyncedKeyRef.current = targetsKey(mem, blk);
      }
      return body;
    } finally {
      setSaving(false);
    }
  };

  const addTarget = async (type) => {
    if (type === "mempool") {
      const adds = parseTargetsInput(mempoolDraft);
      if (adds.length === 0) return;
      const next = Array.from(new Set([...mempoolTargets, ...adds]));
      setMempoolDraft("");
      setMempoolTargets(next);
      await persistTargets(next, blockTargets);
      return;
    }
    const adds = parseTargetsInput(blockDraft);
    if (adds.length === 0) return;
    const next = Array.from(new Set([...blockTargets, ...adds]));
    setBlockDraft("");
    setBlockTargets(next);
    await persistTargets(mempoolTargets, next);
  };

  const removeTarget = async (type, target) => {
    if (type === "mempool") {
      const next = mempoolTargets.filter((addr) => addr !== target);
      setMempoolTargets(next);
      await persistTargets(next, blockTargets);
      return;
    }
    const next = blockTargets.filter((addr) => addr !== target);
    setBlockTargets(next);
    await persistTargets(mempoolTargets, next);
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
      title="Auto Unstake Bot"
      storageKey="ui.auto-unstake-bot.open"
      className="auto-unstake-panel"
    >
      <p className="auto-unstake-hint mono">
        Block targets are used for confirmed StakeAdded monitoring. Mempool targets are stored for
        reference.
      </p>
      <div className="auto-unstake-controls">
        <label className="auto-unstake-label">mempool</label>
        <div className="auto-unstake-input-row">
          <input
            className="portfolio-input mono"
            type="text"
            value={mempoolDraft}
            onChange={(e) => setMempoolDraft(e.target.value)}
            placeholder="address"
            disabled={saving}
          />
          <button
            type="button"
            className="portfolio-btn"
            disabled={saving}
            onClick={() => addTarget("mempool")}
          >
            Add
          </button>
        </div>
        <div className="target-list-wrap">
          <TargetList
            targets={mempoolTargets}
            type="m"
            disabled={saving}
            onRemove={(addr) => removeTarget("mempool", addr)}
          />
        </div>

        <label className="auto-unstake-label">block</label>
        <div className="auto-unstake-input-row">
          <input
            className="portfolio-input mono"
            type="text"
            value={blockDraft}
            onChange={(e) => setBlockDraft(e.target.value)}
            placeholder="address"
            disabled={saving}
          />
          <button
            type="button"
            className="portfolio-btn"
            disabled={saving}
            onClick={() => addTarget("block")}
          >
            Add
          </button>
        </div>
        <div className="target-list-wrap">
          <TargetList
            targets={blockTargets}
            type="b"
            disabled={saving}
            onRemove={(addr) => removeTarget("block", addr)}
          />
        </div>

        <div className="auto-unstake-actions">
          <button
            type="button"
            className="portfolio-btn"
            disabled={saving}
            onClick={() => persistTargets(mempoolTargets, blockTargets)}
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
                <div key={`aul-${idx}`} className="auto-unstake-log-item">
                  [{String(log?.result || "-")}] netuid {String(log?.netuid ?? "-")} source{" "}
                  {String(log?.source || "-")} {String(log?.tx_hash ? `tx ${log.tx_hash}` : "")}{" "}
                  {String(log?.error || log?.reason || "")}
                </div>
              ))
          )}
        </div>
        {error ? <div className="auto-unstake-error mono">{error}</div> : null}
      </div>
    </CollapsiblePanel>
  );
}
