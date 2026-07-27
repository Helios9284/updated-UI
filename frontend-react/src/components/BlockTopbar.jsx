import { useEffect, useMemo, useState } from "react";

export function BlockTopbar({ connected, blockNumber, blockReceivedAtMs, wsUrl, onToggleBots }) {
  const [nowMs, setNowMs] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const blockAgeSec = useMemo(() => {
    const tsMs = Number(blockReceivedAtMs);
    if (!Number.isFinite(tsMs) || tsMs <= 0) return null;
    return Math.max(0, Math.floor((nowMs - tsMs) / 1000));
  }, [blockReceivedAtMs, nowMs]);

  return (
    <header className="topbar">
      <div className="topbar-left">
        {onToggleBots ? (
          <button
            type="button"
            className="hamburger-btn"
            onClick={onToggleBots}
            title="Open bots (follow-stake / auto-unstake)"
            aria-label="Open bots panel"
          >
            ☰
          </button>
        ) : null}
        <h1>block #{blockNumber || "-"} · age {blockAgeSec == null ? "-" : `${blockAgeSec}s`}</h1>
      </div>
      <div className="right">
        <span className="mono">ws</span>
        <span className={`status ${connected ? "connected" : "stale"}`}>
          {connected ? "connected" : "stale"}
        </span>
        <span className="mono ws-url" title={wsUrl}>
          {wsUrl}
        </span>
      </div>
    </header>
  );
}
