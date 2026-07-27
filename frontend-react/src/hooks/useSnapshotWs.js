import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  clearAllStoredNotifications,
  loadOtherNotifications,
  loadTransferNotifications,
  saveOtherNotifications,
  saveTransferNotifications,
} from "../utils/notificationStorage";

function defaultWsUrl() {
  const envUrl = import.meta.env.VITE_WS_URL;
  if (envUrl) return envUrl;
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}/ws`;
}

function apiBaseFromWsUrl(wsUrl) {
  try {
    const u = new URL(wsUrl);
    u.protocol = u.protocol === "wss:" ? "https:" : "http:";
    u.pathname = "";
    return u.toString().replace(/\/$/, "");
  } catch {
    return "";
  }
}

function formatCell(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function preferredSigner(row) {
  return row?.real_address && row.real_address !== "-" ? row.real_address : row?.signer;
}

function buildTransferNotificationId(row) {
  const hash = formatCell(row?.hash);
  if (hash !== "-") return `h:${hash}`;
  return [
    "k",
    formatCell(row?.call),
    formatCell(preferredSigner(row)),
    formatCell(row?.to),
    formatCell(row?.amount),
  ].join("|");
}

function buildOtherNotificationId(row) {
  const hash = formatCell(row?.hash);
  if (hash !== "-") return `oth:h:${hash}:${formatCell(row?.call)}`;
  return [
    "oth",
    formatCell(row?.call),
    formatCell(preferredSigner(row)),
    formatCell(row?.hotkey),
    formatCell(row?.netuid),
    formatCell(row?.old_coldkey),
    formatCell(row?.new_coldkey),
  ].join("|");
}

function pendingFromMempoolRow(row) {
  return {
    id: buildTransferNotificationId(row),
    status: "pending",
    call: row?.call,
    signer: row?.signer,
    real_address: row?.real_address,
    to: row?.to,
    amount: row?.amount,
    age: row?.age,
    hash: row?.hash,
    block_number: null,
    created_at: Date.now(),
    updated_at: Date.now(),
  };
}

function pendingOtherFromMempoolRow(row) {
  return {
    id: buildOtherNotificationId(row),
    status: "pending",
    call: row?.call,
    signer: row?.signer,
    real_address: row?.real_address,
    hotkey: row?.hotkey,
    netuid: row?.netuid,
    subnet_name: row?.subnet_name,
    github_repo: row?.github_repo,
    old_coldkey: row?.old_coldkey,
    new_coldkey: row?.new_coldkey,
    age: row?.age,
    hash: row?.hash,
    block_number: null,
    created_at: Date.now(),
    updated_at: Date.now(),
  };
}

function mergePendingNotifications(prev, rows) {
  const next = [...prev];
  const indexById = new Map(next.map((n, idx) => [n.id, idx]));
  for (const row of rows || []) {
    const incoming = pendingFromMempoolRow(row);
    const idx = indexById.get(incoming.id);
    if (idx === undefined) {
      next.push(incoming);
      indexById.set(incoming.id, next.length - 1);
      continue;
    }
    const current = next[idx];
    const keepFinal = current.status === "confirmed" || current.status === "failed";
    next[idx] = {
      ...current,
      ...incoming,
      status: keepFinal ? current.status : "pending",
      block_number: keepFinal ? current.block_number : null,
      created_at: current.created_at ?? incoming.created_at,
      updated_at: Date.now(),
    };
  }
  return next;
}

function mergeBlockResults(prev, rows, blockNumber) {
  const next = [...prev];
  const indexById = new Map(next.map((n, idx) => [n.id, idx]));
  for (const row of rows || []) {
    const id = buildTransferNotificationId(row);
    const status = String(row?.status || "").toLowerCase() === "failed" ? "failed" : "confirmed";
    const incoming = {
      id,
      status,
      call: row?.call,
      signer: row?.signer,
      real_address: row?.real_address,
      to: row?.to,
      amount: row?.amount,
      age: row?.age ?? "-",
      hash: row?.hash,
      block_number: blockNumber ?? null,
      created_at: Date.now(),
      updated_at: Date.now(),
    };
    const idx = indexById.get(id);
    if (idx === undefined) {
      next.push(incoming);
      indexById.set(id, next.length - 1);
      continue;
    }
    const current = next[idx];
    next[idx] = {
      ...current,
      ...incoming,
      created_at: current.created_at ?? incoming.created_at,
      updated_at: Date.now(),
    };
  }
  return next;
}

function mergePendingOtherNotifications(prev, rows) {
  const next = [...prev];
  const indexById = new Map(next.map((n, idx) => [n.id, idx]));
  for (const row of rows || []) {
    const incoming = pendingOtherFromMempoolRow(row);
    const idx = indexById.get(incoming.id);
    if (idx === undefined) {
      next.push(incoming);
      indexById.set(incoming.id, next.length - 1);
      continue;
    }
    const current = next[idx];
    const keepFinal = current.status === "confirmed" || current.status === "failed";
    next[idx] = {
      ...current,
      ...incoming,
      status: keepFinal ? current.status : "pending",
      block_number: keepFinal ? current.block_number : null,
      created_at: current.created_at ?? incoming.created_at,
      updated_at: Date.now(),
    };
  }
  return next;
}

function mergeBlockOtherResults(prev, rows, blockNumber) {
  const next = [...prev];
  const indexById = new Map(next.map((n, idx) => [n.id, idx]));
  for (const row of rows || []) {
    const id = buildOtherNotificationId(row);
    const status = String(row?.status || "").toLowerCase() === "failed" ? "failed" : "confirmed";
    const incoming = {
      id,
      status,
      call: row?.call,
      signer: row?.signer,
      real_address: row?.real_address,
      hotkey: row?.hotkey,
      netuid: row?.netuid,
      subnet_name: row?.subnet_name,
      github_repo: row?.github_repo,
      old_coldkey: row?.old_coldkey,
      new_coldkey: row?.new_coldkey,
      age: row?.age ?? "-",
      hash: row?.hash,
      block_number: blockNumber ?? null,
      created_at: Date.now(),
      updated_at: Date.now(),
    };
    const idx = indexById.get(id);
    if (idx === undefined) {
      next.push(incoming);
      indexById.set(id, next.length - 1);
      continue;
    }
    const current = next[idx];
    next[idx] = {
      ...current,
      ...incoming,
      created_at: current.created_at ?? incoming.created_at,
      updated_at: Date.now(),
    };
  }
  return next;
}

function normalizeMempoolAmount(value) {
  const n = Number.parseFloat(String(value ?? "").replace(/[^0-9.\-]+/g, ""));
  return Number.isFinite(n) ? n : null;
}

function normalizeMempoolHash(value) {
  const text = String(value ?? "").trim().toLowerCase();
  if (!text || text === "-") return "";
  return text.startsWith("0x") ? text : `0x${text}`;
}

function stakeCallFamily(callName) {
  const call = String(callName || "").toLowerCase();
  if (call.includes("add_stake") || (call.startsWith("add_") && call.includes("stake"))) {
    return "add";
  }
  if (
    call.includes("remove_stake") ||
    call.startsWith("unstake") ||
    (call.startsWith("remove_") && call.includes("stake"))
  ) {
    return "remove";
  }
  return "";
}

function pendingMatchesChainRow(pending, chainRow) {
  const pHash = String(pending?.hash || "").trim();
  if (!pHash.startsWith("pending-")) return false;
  const cHash = normalizeMempoolHash(chainRow?.hash);
  if (cHash) {
    const pNorm = normalizeMempoolHash(pHash);
    if (pNorm && pNorm === cHash) return true;
  }
  const pNu = String(pending?.netuid || "").trim();
  const cNu = String(chainRow?.netuid || "").trim();
  if (!pNu || pNu !== cNu) return false;
  const pFamily = stakeCallFamily(pending?.call);
  const cFamily = stakeCallFamily(chainRow?.call);
  if (!pFamily || pFamily !== cFamily) return false;
  const pAmt = normalizeMempoolAmount(pending?.amount);
  const cAmt = normalizeMempoolAmount(chainRow?.amount);
  if (pAmt != null && cAmt != null && Math.abs(pAmt - cAmt) > 1e-6) {
    const pCall = String(pending?.call || "").toLowerCase();
    if (!pCall.includes("full")) return false;
  }
  return true;
}

function mempoolRowAddress(row) {
  const real = row?.real_address;
  if (real && real !== "-" && real !== "") return String(real);
  const signer = row?.signer;
  if (signer && signer !== "-" && signer !== "") return String(signer);
  return "";
}

function mempoolRowDedupKey(row) {
  const normHash = normalizeMempoolHash(row?.hash);
  const call = formatCell(row?.call);
  const netuid = formatCell(row?.netuid);
  const address = mempoolRowAddress(row) || formatCell(row?.signer);
  // One MevShield wrapper = one extrinsic; ignore netuid/amount stubs vs pins.
  if (normHash && String(call).toLowerCase().includes("mevshield")) {
    return `mev|${normHash}|${address}`;
  }
  if (!normHash) {
    return `${call}|${netuid}|${normalizeMempoolAmount(row?.amount)}|${address}`;
  }
  // force_batch: many inner ops share one extrinsic hash — include call + netuid.
  return `${normHash}|${call}|${netuid}|${address}`;
}

function mempoolRowRichness(row) {
  let score = 0;
  if (formatCell(row?.netuid) !== "-") score += 2;
  if (formatCell(row?.amount) !== "-") score += 2;
  if (formatCell(row?.slippage) !== "-") score += 1;
  return score;
}

function mergeLocalMempoolRows(chainRows, localRows) {
  const chain = Array.isArray(chainRows) ? chainRows : [];
  const local = Array.isArray(localRows) ? localRows : [];
  const bestByKey = new Map();
  const consider = (row) => {
    if (!row) return;
    const hash = String(row?.hash || "").trim();
    if (hash.startsWith("pending-") && chain.some((c) => pendingMatchesChainRow(row, c))) {
      return;
    }
    const key = mempoolRowDedupKey(row);
    const prev = bestByKey.get(key);
    if (!prev || mempoolRowRichness(row) > mempoolRowRichness(prev)) {
      bestByKey.set(key, row);
    }
  };
  for (const row of local) consider(row);
  for (const row of chain) consider(row);

  const merged = [];
  const seen = new Set();
  for (const row of local) {
    const key = mempoolRowDedupKey(row);
    const chosen = bestByKey.get(key);
    if (!chosen || seen.has(key)) continue;
    seen.add(key);
    merged.push(chosen);
  }
  for (const row of chain) {
    const key = mempoolRowDedupKey(row);
    const chosen = bestByKey.get(key);
    if (!chosen || seen.has(key)) continue;
    seen.add(key);
    merged.push(chosen);
  }
  return merged;
}
// When the feed goes stale, try to repair the live socket in place before
// falling back to a full page reload. A dropped / half-open websocket recovers
// via reconnect, and the backend re-pushes the latest snapshot to any
// (re)connecting client, so the UI catches up without losing page state.
// Bittensor block time is ~12s; wait >2 blocks before treating the feed as stale
// so normal chain jitter does not force-close the dashboard websocket.
const STALE_BLOCK_AGE_SEC = 36;
const SOFT_RECONNECT_COOLDOWN_MS = 5000;
const MAX_SOFT_RECONNECTS = 3;

function blockAgeSeconds(receivedAtMs, nowMs = Date.now()) {
  const tsMs = Number(receivedAtMs);
  if (!Number.isFinite(tsMs) || tsMs <= 0) return null;
  return Math.max(0, Math.floor((nowMs - tsMs) / 1000));
}

export function useSnapshotWs() {
  const wsUrl = useMemo(() => defaultWsUrl(), []);
  const apiBase = useMemo(() => apiBaseFromWsUrl(wsUrl), [wsUrl]);
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const [connected, setConnected] = useState(false);
  const [mempoolRows, setMempoolRows] = useState([]);
  const [localMempoolRows, setLocalMempoolRows] = useState([]);
  const [mempoolPendingCount, setMempoolPendingCount] = useState(0);
  const [mempoolError, setMempoolError] = useState("");
  const [mempoolTransferRows, setMempoolTransferRows] = useState(() => loadTransferNotifications());
  const [mempoolOtherNotificationRows, setMempoolOtherNotificationRows] = useState(() =>
    loadOtherNotifications()
  );
  const [blockRows, setBlockRows] = useState([]);
  const [blockNumber, setBlockNumber] = useState(0);
  const [blockReceivedAtMs, setBlockReceivedAtMs] = useState(null);
  const [portfolioAddress, setPortfolioAddress] = useState("");
  const [portfolioData, setPortfolioData] = useState(null);
  const [portfolioLoading, setPortfolioLoading] = useState(false);
  const [portfolioError, setPortfolioError] = useState("");
  const [addressBookSeq, setAddressBookSeq] = useState(0);
  // Wallet (stake) portfolio is now pushed over the same WS channel instead of
  // being polled per block by App.jsx.
  const [stakeWalletPortfolioPush, setStakeWalletPortfolioPush] = useState(null);
  const softReconnectCountRef = useRef(0);
  const lastStaleActionAtRef = useRef(0);

  const mergedMempoolRows = useMemo(
    () => mergeLocalMempoolRows(mempoolRows, localMempoolRows),
    [mempoolRows, localMempoolRows]
  );

  const prependLocalMempoolRow = useCallback((row) => {
    if (!row) return;
    setLocalMempoolRows((prev) => {
      const hash = String(row.hash || "").trim();
      const netuid = String(row.netuid || "").trim();
      const filtered = prev.filter((item) => {
        const itemHash = String(item?.hash || "").trim();
        if (hash && itemHash === hash) return false;
        if (hash.startsWith("pending-") && itemHash.startsWith("pending-")) {
          return String(item?.netuid || "").trim() !== netuid;
        }
        return true;
      });
      return [row, ...filtered].slice(0, 50);
    });
    window.setTimeout(() => {
      setLocalMempoolRows((prev) => {
        const hash = String(row.hash || "").trim();
        if (!hash.startsWith("pending-")) return prev;
        return prev.filter((item) => String(item?.hash || "").trim() !== hash);
      });
    }, 45000);
  }, []);

  useEffect(() => {
    if (!localMempoolRows.length || !mempoolRows.length) return;
    setLocalMempoolRows((prev) =>
      prev.filter((row) => {
        const hash = String(row?.hash || "").trim();
        if (hash.startsWith("pending-")) {
          return !mempoolRows.some((chainRow) => pendingMatchesChainRow(row, chainRow));
        }
        return !mempoolRows.some(
          (chainRow) => mempoolRowDedupKey(chainRow) === mempoolRowDedupKey(row)
        );
      })
    );
  }, [mempoolRows, localMempoolRows.length]);

  useEffect(() => {
    saveTransferNotifications(mempoolTransferRows);
  }, [mempoolTransferRows]);

  useEffect(() => {
    saveOtherNotifications(mempoolOtherNotificationRows);
  }, [mempoolOtherNotificationRows]);

  useEffect(() => {
    if (!blockReceivedAtMs) {
      softReconnectCountRef.current = 0;
      lastStaleActionAtRef.current = 0;
      return undefined;
    }

    const forceSocketReconnect = () => {
      // Closing the socket fires onclose, which schedules the existing 1s
      // auto-reconnect. On reconnect the backend immediately re-sends the
      // current mempool + block snapshots, recovering the feed in place.
      try {
        wsRef.current?.close();
      } catch {
        // ignore
      }
    };

    const probeBackend = async () => {
      let reachable = false;
      let chainHead = 0;
      let dashboardBlock = 0;
      // Cheap liveness first — must not depend on MEV/thread-pool work.
      try {
        const res = await fetch(`${apiBase}/stake-api/ping`, {
          signal: AbortSignal.timeout(2500),
        });
        if (res.ok) reachable = true;
      } catch {
        // ignore
      }
      // Independent chain tip from the stake service (its own worker on the
      // local node) — reflects reality even if the dashboard collector or our
      // socket is stuck.
      try {
        const res = await fetch(`${apiBase}/stake-api/health`, {
          signal: AbortSignal.timeout(4000),
        });
        if (res.ok) {
          const body = await res.json();
          chainHead = Number(body?.block?.latest_block_number || 0);
          reachable = true;
        }
      } catch {
        // ignore
      }
      // The dashboard collector's last published block (what the websocket
      // would deliver).
      try {
        const res = await fetch(`${apiBase}/health`, {
          signal: AbortSignal.timeout(3000),
        });
        if (res.ok) {
          const body = await res.json();
          dashboardBlock = Number(body?.block_number || 0);
          reachable = true;
        }
      } catch {
        // ignore
      }
      return { reachable, chainHead, dashboardBlock };
    };

    const tick = async () => {
      try {
        const ageSec = blockAgeSeconds(blockReceivedAtMs);
        if (ageSec == null || ageSec <= STALE_BLOCK_AGE_SEC) {
          softReconnectCountRef.current = 0;
          return;
        }

        const now = Date.now();
        if (now - lastStaleActionAtRef.current < SOFT_RECONNECT_COOLDOWN_MS) {
          return;
        }
        lastStaleActionAtRef.current = now;

        // Phase 1: repair the live connection in place (no page reload).
        if (softReconnectCountRef.current < MAX_SOFT_RECONNECTS) {
          softReconnectCountRef.current += 1;
          forceSocketReconnect();
          return;
        }

        // Phase 2: soft reconnects didn't restore freshness. Classify the stall so
        // we only hard-reload when it can actually help.
        const ourBlock = Number(blockNumber || 0);
        const { reachable, chainHead, dashboardBlock } = await probeBackend();

        if (!reachable) {
          // Backend unreachable (restart / network) — a full reload re-establishes
          // everything from scratch.
          window.location.reload();
          return;
        }

        if (chainHead > 0 && chainHead <= ourBlock) {
          // The chain isn't producing blocks beyond what we already show (node /
          // chain stalled). Neither reconnect nor reload helps; wait and retry.
          softReconnectCountRef.current = 0;
          return;
        }

        if (dashboardBlock > 0 && chainHead > 0 && dashboardBlock < chainHead - 2) {
          // The backend's own block collector is lagging the chain. A reload would
          // just re-deliver the same stale snapshot, so keep the page and let the
          // server-side collector self-heal; retry softly.
          softReconnectCountRef.current = 0;
          return;
        }

        // Backend is current but our socket still isn't receiving updates after
        // repeated reconnects — full reload as a last resort.
        window.location.reload();
      } catch {
        // Keep the live socket; a bad probe/state must not tear the feed down.
      }
    };

    const timer = window.setInterval(() => {
      void tick();
    }, 1000);

    return () => clearInterval(timer);
  }, [apiBase, blockReceivedAtMs, blockNumber]);

  useEffect(() => {
    let cancelled = false;
    const connect = () => {
      if (cancelled) return;
      setConnected(false);
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
      };

      ws.onclose = () => {
        setConnected(false);
        if (!cancelled) {
          reconnectTimerRef.current = setTimeout(connect, 1000);
        }
      };

      // Do not flip connected=false on error alone — browsers often fire onerror
      // before onclose (or without a lasting drop). Status should follow onclose/onopen.
      ws.onerror = () => {};

      ws.onmessage = (event) => {
        let msg;
        try {
          msg = JSON.parse(event.data);
        } catch {
          return;
        }

        if (msg.type === "update") {
          setMempoolRows(msg.mempool || []);
          setBlockRows(msg.confirmed?.rows || []);
          setBlockNumber(msg.confirmed?.block || msg.banner?.block || 0);
          setBlockReceivedAtMs(null);
          return;
        }

        if (msg.type === "mempool_snapshot") {
          setMempoolRows(msg.payload?.rows || []);
          setMempoolPendingCount(Number(msg.payload?.pending_count || 0));
          setMempoolError(String(msg.payload?.error || ""));
          setAddressBookSeq(Number(msg.payload?.address_book_seq || 0));
          const otherRows =
            msg.payload?.other_notification_rows ||
            msg.payload?.start_call_rows ||
            [];
          setMempoolTransferRows((prev) =>
            mergePendingNotifications(prev, msg.payload?.transfer_rows || [])
          );
          setMempoolOtherNotificationRows((prev) =>
            mergePendingOtherNotifications(prev, otherRows)
          );
          return;
        }
        if (msg.type === "block_snapshot") {
          const otherRows =
            msg.payload?.other_notification_rows ||
            msg.payload?.start_call_rows ||
            [];
          setMempoolTransferRows((prev) =>
            mergeBlockResults(
              prev,
              msg.payload?.transfer_rows || [],
              msg.payload?.block_number || null
            )
          );
          setMempoolOtherNotificationRows((prev) =>
            mergeBlockOtherResults(prev, otherRows, msg.payload?.block_number || null)
          );
          setBlockRows(msg.payload?.rows || []);
          setAddressBookSeq((prev) => {
            const next = Number(msg.payload?.address_book_seq || 0);
            return next > prev ? next : prev;
          });
          setBlockNumber(msg.payload?.block_number || 0);
          const receivedAtMs = Number(msg.payload?.received_at_ms);
          setBlockReceivedAtMs(
            Number.isFinite(receivedAtMs) && receivedAtMs > 0 ? receivedAtMs : null
          );
          return;
        }
        if (msg.type === "portfolio_snapshot") {
          if (msg.payload) setStakeWalletPortfolioPush(msg.payload);
        }
      };
    };

    connect();
    return () => {
      cancelled = true;
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (wsRef.current) wsRef.current.close();
    };
  }, [wsUrl]);

  const removeTransferNotification = (id) => {
    setMempoolTransferRows((prev) => prev.filter((item) => item.id !== id));
  };

  const clearTransferNotifications = () => {
    setMempoolTransferRows([]);
  };

  const removeOtherNotification = (id) => {
    setMempoolOtherNotificationRows((prev) => prev.filter((item) => item.id !== id));
  };

  const clearOtherNotifications = () => {
    setMempoolOtherNotificationRows([]);
  };

  const clearAllNotifications = () => {
    setMempoolTransferRows([]);
    setMempoolOtherNotificationRows([]);
    clearAllStoredNotifications();
  };

  const fetchPortfolio = async (address) => {
    const addr = String(address || "").trim();
    if (!addr || !apiBase) return;
    setPortfolioAddress(addr);
    setPortfolioLoading(true);
    setPortfolioError("");
    try {
      const res = await fetch(`${apiBase}/api/portfolio?address=${encodeURIComponent(addr)}`);
      const json = await res.json();
      if (!res.ok || json?.error) {
        setPortfolioData(null);
        setPortfolioError(json?.error || `http ${res.status}`);
      } else {
        setPortfolioData(json);
      }
    } catch (exc) {
      setPortfolioData(null);
      setPortfolioError(String(exc));
    } finally {
      setPortfolioLoading(false);
    }
  };

  const clearPortfolio = () => {
    setPortfolioAddress("");
    setPortfolioData(null);
    setPortfolioError("");
    setPortfolioLoading(false);
  };

  return {
    apiBase,
    wsUrl,
    connected,
    mempoolRows: mergedMempoolRows,
    mempoolPendingCount,
    mempoolError,
    prependLocalMempoolRow,
    mempoolTransferRows,
    mempoolOtherNotificationRows,
    removeTransferNotification,
    clearTransferNotifications,
    removeOtherNotification,
    clearOtherNotifications,
    clearAllNotifications,
    blockRows,
    blockNumber,
    blockReceivedAtMs,
    portfolioAddress,
    portfolioData,
    portfolioLoading,
    portfolioError,
    addressBookSeq,
    stakeWalletPortfolioPush,
    fetchPortfolio,
    clearPortfolio,
  };
}
