import "./App.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSnapshotWs } from "./hooks/useSnapshotWs";
import { BlockTopbar } from "./components/BlockTopbar";
import { MempoolPanel } from "./components/MempoolPanel";
import { BlockPanel } from "./components/BlockPanel";
import { TransferNotificationsPanel } from "./components/TransferNotificationsPanel";
import { PortfolioPanel } from "./components/PortfolioPanel";
import { AutoUnstakePanel } from "./components/AutoUnstakePanel";
import { FollowStakeBotPanel } from "./components/FollowStakeBotPanel";
import { StakeSubmitPanel } from "./components/StakeSubmitPanel";
import {
  loadTransferMinAmount,
  saveTransferMinAmount,
} from "./utils/notificationStorage";

const ALPHA_PRICE_WINDOW = 5;
// A genuinely "profitable" subnet drifts up by a small, near-constant % each
// block. A big single-block jump is a one-off buyer moving the AMM price, not
// repeatable yield. So we rank by the MEDIAN per-block % (robust to spikes) and
// demote subnets whose movement is spike-driven / inconsistent.
const STEADY_MIN_DELTAS = 3; // need a few blocks before classifying steadiness
const SPIKE_MULT = 3; // a delta > 3x the typical (median) magnitude is a spike

function median(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function computeAlphaMovers(history) {
  const rows = [];
  for (const [netuid, hist] of history) {
    const samples = hist.length;
    const last = hist[samples - 1];
    const price = last?.price ?? 0;
    const meta = last?.meta || {};
    let deltaAbs = 0; // median per-block price delta (derived)
    let deltaPct = 0; // median per-block % delta = steady rate
    let consistency = 0;
    let direction = 0;
    let steady = false;
    let hasSpike = false;
    let spikePct = 0; // largest single-block % move flagged as a spike
    if (samples >= 2) {
      const pctDeltas = [];
      for (let i = 1; i < samples; i += 1) {
        const prev = hist[i - 1].price;
        pctDeltas.push(prev > 0 ? ((hist[i].price - prev) / prev) * 100 : 0);
      }
      deltaPct = median(pctDeltas);
      deltaAbs = (deltaPct / 100) * price;
      direction = deltaPct > 0 ? 1 : deltaPct < 0 ? -1 : 0;
      const dominant = Math.max(
        pctDeltas.filter((d) => d > 0).length,
        pctDeltas.filter((d) => d < 0).length
      );
      consistency = dominant / pctDeltas.length;
      if (pctDeltas.length >= STEADY_MIN_DELTAS) {
        const typical = median(pctDeltas.map((d) => Math.abs(d)));
        const threshold = SPIKE_MULT * typical;
        for (const d of pctDeltas) {
          if (Math.abs(d) > threshold && Math.abs(d) > Math.abs(deltaPct)) {
            hasSpike = true;
            if (Math.abs(d) > Math.abs(spikePct)) spikePct = d;
          }
        }
        steady = direction !== 0 && consistency >= 0.8 && !hasSpike;
      }
    }
    rows.push({
      netuid,
      name: meta.name || `subnet-${netuid}`,
      symbol: meta.symbol || "",
      depth: Number.isFinite(meta.depth) ? meta.depth : null,
      price,
      deltaAbs,
      deltaPct,
      consistency,
      direction,
      steady,
      hasSpike,
      spikePct,
      samples,
    });
  }
  // Tier 0: steady earners (small, consistent, no spike) ranked by median %/block.
  // Tier 1: spiky / inconsistent movers (demoted) ranked by median %/block.
  // Tier 2: not enough samples yet (parked at bottom).
  const tierOf = (r) => {
    if (r.samples < STEADY_MIN_DELTAS + 1) return 2;
    return r.steady ? 0 : 1;
  };
  rows.sort((a, b) => {
    const ta = tierOf(a);
    const tb = tierOf(b);
    if (ta !== tb) return ta - tb;
    if (ta === 2) return a.netuid - b.netuid;
    return b.deltaPct - a.deltaPct || a.netuid - b.netuid;
  });
  return rows;
}

function resolveRowAddress(row) {
  const real = row?.real_address;
  if (real && real !== "-" && real !== "") return real;
  const signer = row?.signer;
  if (signer && signer !== "-" && signer !== "") return signer;
  return "";
}

function preferredNotificationAddress(row) {
  const real = row?.real_address;
  if (real && real !== "-" && real !== "") return String(real);
  const signer = row?.signer;
  if (signer && signer !== "-" && signer !== "") return String(signer);
  return "";
}

function parseAmountTao(value) {
  const text = String(value ?? "").trim();
  if (!text || text === "-") return null;
  const numeric = Number.parseFloat(text.replace(/[^0-9.\-]+/g, ""));
  return Number.isFinite(numeric) ? numeric : null;
}

function fallbackCopyText(text) {
  try {
    const el = document.createElement("textarea");
    el.value = text;
    el.setAttribute("readonly", "");
    el.style.position = "fixed";
    el.style.left = "-9999px";
    el.style.top = "-9999px";
    document.body.appendChild(el);
    el.focus();
    el.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(el);
    return ok;
  } catch {
    return false;
  }
}

function stakeApiBaseFromWsUrl(wsUrl) {
  const envBase = import.meta.env.VITE_STAKE_API_BASE;
  if (envBase) {
    const cleaned = String(envBase).replace(/\/$/, "");
    try {
      const envUrl = new URL(cleaned);
      const isLocalhost =
        envUrl.hostname === "127.0.0.1" ||
        envUrl.hostname === "localhost" ||
        envUrl.hostname === "::1";
      const pageHost = String(window.location.hostname || "").toLowerCase();
      const pageIsRemote = !["127.0.0.1", "localhost", "::1"].includes(pageHost);
      // If the page is opened remotely, ignore localhost-only stake API settings.
      if (!(isLocalhost && pageIsRemote)) {
        return cleaned;
      }
    } catch {
      return cleaned;
    }
  }
  // Default: route through the Vite dev-server proxy (same origin as the page).
  // This avoids hardcoding the server IP and means only the frontend port needs
  // to be reachable/forwarded; Vite forwards "/stake-api/*" to 127.0.0.1:8780.
  return "/stake-api";
}

async function readApiResponse(res) {
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = null;
  }
  if (!res.ok) {
    const message = body?.error || text || `HTTP ${res.status}`;
    throw new Error(message);
  }
  if (body === null) {
    throw new Error(text || "invalid JSON response");
  }
  return body;
}

function alphaAmountToTao(netuid, alphaAmt, { priceByNetuid, portfolio } = {}) {
  const nu = Number(netuid);
  const alpha = Number(alphaAmt);
  if (!Number.isFinite(nu) || !Number.isFinite(alpha) || alpha <= 0) return undefined;
  const cached = Number(priceByNetuid?.[nu]);
  if (Number.isFinite(cached) && cached > 0) return alpha * cached;
  const stakes = portfolio?.stakes || portfolio?.portfolio || [];
  const row = stakes.find((s) => Number(s?.netuid) === nu);
  if (row) {
    const stakeTao = Number(row?.stake_tao ?? row?.tao);
    const stakeAlpha = Number(row?.stake_alpha ?? row?.alpha);
    if (Number.isFinite(stakeTao) && Number.isFinite(stakeAlpha) && stakeAlpha > 0) {
      return alpha * (stakeTao / stakeAlpha);
    }
    const implied = Number(row?.alpha_price_tao ?? row?.price);
    if (Number.isFinite(implied) && implied > 0) return alpha * implied;
  }
  return undefined;
}

function App() {
  const {
    apiBase,
    connected,
    mempoolRows,
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
    wsUrl,
    portfolioAddress,
    portfolioData,
    portfolioLoading,
    portfolioError,
    addressBookSeq,
    stakeWalletPortfolioPush,
    fetchPortfolio,
    clearPortfolio,
  } = useSnapshotWs();
  const [addressBook, setAddressBook] = useState({});
  const [addressBookLoading, setAddressBookLoading] = useState(false);
  const [addressBookError, setAddressBookError] = useState("");
  const [autoUnstakeConfig, setAutoUnstakeConfig] = useState(null);
  const [autoUnstakeError, setAutoUnstakeError] = useState("");
  const [followStakeConfig, setFollowStakeConfig] = useState(null);
  const [followStakeError, setFollowStakeError] = useState("");
  const [botsDrawerOpen, setBotsDrawerOpen] = useState(false);
  const [notificationMinAmount, setNotificationMinAmount] = useState(() =>
    loadTransferMinAmount(50)
  );
  const [selectedNetuid, setSelectedNetuid] = useState("");
  const [selectedNetuidTick, setSelectedNetuidTick] = useState(0);
  const [stakeWalletPortfolio, setStakeWalletPortfolio] = useState(null);
  const [stakeWalletPortfolioError, setStakeWalletPortfolioError] = useState("");
  const stakeWalletPortfolioLoading = stakeWalletPortfolio == null;
  const alphaPriceHistoryRef = useRef(new Map());
  const alphaPriceByNetuidRef = useRef({});
  const [alphaMovers, setAlphaMovers] = useState([]);
  const [alphaMoversMeta, setAlphaMoversMeta] = useState({ samples: 0, block: null });
  const stakeApiBase = useMemo(() => stakeApiBaseFromWsUrl(wsUrl), [wsUrl]);
  const stakeApiTokenRef = useRef(String(import.meta.env.VITE_STAKE_API_TOKEN || ""));
  const [stakeApiToken, setStakeApiToken] = useState(stakeApiTokenRef.current);
  const stakeDefaultUseProxy = String(import.meta.env.VITE_STAKE_DEFAULT_USE_PROXY || "true")
    .toLowerCase()
    .trim();

  useEffect(() => {
    if (!apiBase) return;
    let dead = false;
    const loadStakeToken = async () => {
      try {
        const res = await fetch(`${apiBase}/api/stake-client-config`);
        const body = await readApiResponse(res);
        const token = String(body?.stake_api_token || "").trim();
        if (!dead && token) {
          stakeApiTokenRef.current = token;
          setStakeApiToken(token);
        }
      } catch {
        // Keep VITE fallback if backend config is unavailable.
      }
    };
    void loadStakeToken();
    return () => {
      dead = true;
    };
  }, [apiBase]);

  const refreshStakeWalletPortfolio = useCallback(async () => {
    if (!stakeApiBase) return null;
    try {
      const res = await fetch(`${stakeApiBase}/api/wallet/portfolio`);
      const body = await readApiResponse(res);
      setStakeWalletPortfolio(body);
      setStakeWalletPortfolioError(body?.error || "");
      return body;
    } catch (e) {
      setStakeWalletPortfolioError(
        e instanceof Error ? e.message : "stake wallet portfolio fetch failed"
      );
      return null;
    }
  }, [stakeApiBase]);

  // Portfolio is pushed over the WebSocket (see useSnapshotWs); just mirror the
  // latest pushed snapshot into local state. Manual refresh after a submit still
  // updates the same state directly for instant feedback.
  useEffect(() => {
    if (!stakeWalletPortfolioPush) return;
    setStakeWalletPortfolio(stakeWalletPortfolioPush);
    setStakeWalletPortfolioError(stakeWalletPortfolioPush?.error || "");
  }, [stakeWalletPortfolioPush]);

  // On each new block, pull all-subnet alpha prices and track per-block movement.
  useEffect(() => {
    if (!apiBase || !blockNumber) return undefined;
    let cancelled = false;

    const loadAlphaPrices = async () => {
      let body = null;
      try {
        const res = await fetch(`${apiBase}/api/subnets`);
        body = await readApiResponse(res);
      } catch {
        return;
      }
      if (cancelled || !body) return;
      const subnets = Array.isArray(body.subnets) ? body.subnets : [];
      const block = Number(body.block_number ?? blockNumber);
      const history = alphaPriceHistoryRef.current;
      const present = new Set();
      const priceByNetuid = { 0: 1 };
      for (const s of subnets) {
        const netuid = Number(s?.netuid);
        if (!Number.isFinite(netuid) || netuid === 0) continue;
        const price = Number(s?.price_tao);
        if (!Number.isFinite(price)) continue;
        priceByNetuid[netuid] = price;
        present.add(netuid);
        let hist = history.get(netuid);
        if (!hist) {
          hist = [];
          history.set(netuid, hist);
        }
        const lastSample = hist[hist.length - 1];
        if (lastSample && lastSample.block === block) {
          lastSample.price = price;
          lastSample.meta = { name: s?.subnet_name, symbol: s?.symbol, depth: Number(s?.tao_in) };
        } else {
          hist.push({
            block,
            price,
            meta: { name: s?.subnet_name, symbol: s?.symbol, depth: Number(s?.tao_in) },
          });
        }
        if (hist.length > ALPHA_PRICE_WINDOW) hist.splice(0, hist.length - ALPHA_PRICE_WINDOW);
      }
      for (const netuid of [...history.keys()]) {
        if (!present.has(netuid)) history.delete(netuid);
      }
      alphaPriceByNetuidRef.current = priceByNetuid;
      let maxSamples = 0;
      for (const hist of history.values()) maxSamples = Math.max(maxSamples, hist.length);
      setAlphaMovers(computeAlphaMovers(history));
      setAlphaMoversMeta({ samples: maxSamples, block });
    };

    void loadAlphaPrices();
    return () => {
      cancelled = true;
    };
  }, [blockNumber, apiBase]);

  useEffect(() => {
    if (!apiBase) return;
    let dead = false;
    let timer = null;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/api/auto-unstake`);
        const body = await readApiResponse(res);
        if (dead) return;
        setAutoUnstakeConfig(body);
        if (body?.ok !== false) setAutoUnstakeError("");
      } catch (e) {
        if (dead) return;
        setAutoUnstakeError(e instanceof Error ? e.message : "auto unstake bot fetch failed");
      } finally {
        if (!dead) {
          timer = window.setTimeout(() => {
            void load();
          }, 1500);
        }
      }
    };
    void load();
    return () => {
      dead = true;
      if (timer) clearTimeout(timer);
    };
  }, [apiBase]);

  useEffect(() => {
    if (!apiBase) return;
    let dead = false;
    let timer = null;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/api/follow-stake-bot`);
        const body = await readApiResponse(res);
        if (dead) return;
        setFollowStakeConfig(body);
        if (body?.ok !== false) setFollowStakeError("");
      } catch (e) {
        if (dead) return;
        setFollowStakeError(e instanceof Error ? e.message : "follow stake bot fetch failed");
      } finally {
        if (!dead) {
          timer = window.setTimeout(() => {
            void load();
          }, 1500);
        }
      }
    };
    void load();
    return () => {
      dead = true;
      if (timer) clearTimeout(timer);
    };
  }, [apiBase]);

  useEffect(() => {
    if (!apiBase) return;
    let dead = false;
    const load = async () => {
      setAddressBookLoading(true);
      try {
        const res = await fetch(`${apiBase}/api/address-book`);
        const body = await readApiResponse(res);
        if (dead) return;
        setAddressBook(body?.entries || {});
        setAddressBookError("");
      } catch (e) {
        if (dead) return;
        setAddressBookError(e instanceof Error ? e.message : "address book fetch failed");
      } finally {
        if (!dead) setAddressBookLoading(false);
      }
    };
    void load();
    return () => {
      dead = true;
    };
  }, [apiBase, addressBookSeq]);

  const labelFor = (address) => {
    const addr = String(address || "");
    return addressBook[addr] || "";
  };

  const handleNotificationMinAmountChange = (value) => {
    setNotificationMinAmount(value);
    saveTransferMinAmount(value);
  };

  const filteredTransferRows = useMemo(() => {
    return (mempoolTransferRows || []).filter((row) => {
      const signerAddr = preferredNotificationAddress(row);
      const toAddr = String(row?.to || "");
      const isTracked = Boolean(labelFor(signerAddr) || labelFor(toAddr));
      if (isTracked) return true;
      const amount = parseAmountTao(row?.amount);
      return amount !== null && amount > notificationMinAmount;
    });
  }, [mempoolTransferRows, addressBook, notificationMinAmount]);

  const clearAllNotificationCards = () => {
    clearAllNotifications();
  };

  const saveAutoUnstakeConfig = async (next) => {
    if (!apiBase) throw new Error("api base unavailable");
    setAutoUnstakeError("");
    const res = await fetch(`${apiBase}/api/auto-unstake`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(next || {}),
    });
    const body = await readApiResponse(res);
    setAutoUnstakeConfig(body);
    return body;
  };

  const clearAutoUnstakeLogs = async () => {
    if (!apiBase) throw new Error("api base unavailable");
    setAutoUnstakeError("");
    const res = await fetch(`${apiBase}/api/auto-unstake/logs`, {
      method: "DELETE",
    });
    const body = await readApiResponse(res);
    setAutoUnstakeConfig(body);
    return body;
  };

  const saveFollowStakeConfig = async (next) => {
    if (!apiBase) throw new Error("api base unavailable");
    setFollowStakeError("");
    const res = await fetch(`${apiBase}/api/follow-stake-bot`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(next || {}),
    });
    const body = await readApiResponse(res);
    setFollowStakeConfig(body);
    return body;
  };

  const clearFollowStakeLogs = async () => {
    if (!apiBase) throw new Error("api base unavailable");
    setFollowStakeError("");
    const res = await fetch(`${apiBase}/api/follow-stake-bot/logs`, {
      method: "DELETE",
    });
    const body = await readApiResponse(res);
    setFollowStakeConfig(body);
    return body;
  };

  const selectLookupAddress = async (address) => {
    const addr = String(address || "").trim();
    if (!addr) return;
    fetchPortfolio(addr);
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(addr);
        return;
      }
    } catch {
      // fall through to legacy copy path
    }
    fallbackCopyText(addr);
  };

  const handleRowClick = async (row) => {
    const addr = resolveRowAddress(row);
    const n = Number(row?.netuid);
    if (Number.isFinite(n) && n >= 0) {
      setSelectedNetuid(String(Math.floor(n)));
      setSelectedNetuidTick((v) => v + 1);
    }
    await selectLookupAddress(addr);
  };

  const saveAddressBookEntry = async (address, label) => {
    const addr = String(address || "").trim();
    if (!addr || !apiBase) return;
    const lbl = String(label || "").trim();
    setAddressBook((prev) => ({ ...prev, [addr]: lbl }));
    setAddressBookError("");
    try {
      const res = await fetch(`${apiBase}/api/address-book`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address: addr, label: lbl }),
      });
      const body = await readApiResponse(res);
      if (body?.ok === false) {
        throw new Error(body?.error || "address book save failed");
      }
    } catch (e) {
      setAddressBookError(e instanceof Error ? e.message : "address book save failed");
    }
  };

  const deleteAddressBookEntry = async (address) => {
    const addr = String(address || "").trim();
    if (!addr || !apiBase) return;
    setAddressBook((prev) => {
      const next = { ...prev };
      delete next[addr];
      return next;
    });
    setAddressBookError("");
    try {
      const res = await fetch(`${apiBase}/api/address-book/${encodeURIComponent(addr)}`, {
        method: "DELETE",
      });
      const body = await readApiResponse(res);
      if (body?.ok === false) {
        throw new Error(body?.error || "address book delete failed");
      }
    } catch (e) {
      setAddressBookError(e instanceof Error ? e.message : "address book delete failed");
    }
  };

  const resolveStakeApiToken = async () => {
    const cached = String(stakeApiTokenRef.current || "").trim();
    if (cached) return cached;
    if (!apiBase) throw new Error("stake API base unavailable");
    const res = await fetch(`${apiBase}/api/stake-client-config`);
    const body = await readApiResponse(res);
    const token = String(body?.stake_api_token || "").trim();
    if (!token) throw new Error("missing stake API token");
    stakeApiTokenRef.current = token;
    setStakeApiToken(token);
    return token;
  };

  const submitStakeRequest = async (path, payload) => {
    if (!stakeApiBase) throw new Error("stake API base unavailable");
    const token = await resolveStakeApiToken();
    const res = await fetch(`${stakeApiBase}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(payload || {}),
    });
    const body = await readApiResponse(res);
    if (body?.ok !== false && body?.success !== false) {
      void refreshStakeWalletPortfolio();
    }
    return body;
  };

  const submitAddStake = async (payload) => {
    const nu = Number(payload?.netuid);
    const amt = Number(payload?.amount_tao);
    const slip = Number(payload?.slippage_pct);
    const walletAddr = String(stakeWalletPortfolio?.address || "").trim();
    // MEV: do not paint a fake pending row on click (eye-wash). Server pushes
    // one real MevShield hash only after the node accepts the wrapper.
    if (!payload?.mev_protection) {
      prependLocalMempoolRow({
        hash: `pending-${Date.now()}`,
        call: "SubtensorModule.add_stake_limit",
        signer: walletAddr || "-",
        real_address: walletAddr || "-",
        amount: Number.isFinite(amt) ? String(amt) : "-",
        netuid: Number.isFinite(nu) ? String(nu) : "-",
        slippage: Number.isFinite(slip) ? `${slip}%` : "-",
        age: "0.0s",
        _age_seconds: 0,
        _row_type: "add",
      });
    }
    const alphaPrice = alphaPriceByNetuidRef.current[nu];
    return submitStakeRequest("/api/stake/add", {
      ...payload,
      ...(Number.isFinite(alphaPrice) && alphaPrice > 0
        ? { alpha_price_tao: alphaPrice }
        : {}),
    });
  };

  const buildOptimisticRemoveRow = ({
    netuid,
    amount,
    slippage,
    full = false,
    mev = false,
  }) => {
    const walletAddr = String(stakeWalletPortfolio?.address || "").trim();
    const slip = Number(slippage);
    return {
      hash: `pending-${Date.now()}`,
      call: mev
        ? "MevShield.submit_encrypted"
        : full
          ? "SubtensorModule.remove_stake_full_limit"
          : "SubtensorModule.remove_stake_limit",
      signer: walletAddr || "-",
      real_address: walletAddr || "-",
      amount:
        amount != null && Number.isFinite(Number(amount)) ? String(Number(amount)) : "-",
      netuid: String(netuid),
      slippage: Number.isFinite(slip) ? `${slip}%` : "-",
      age: "0.0s",
      _age_seconds: 0,
      _row_type: mev ? "mev" : "remove",
    };
  };

  const submitRemoveFull = async (payload) => {
    const nu = Number(payload?.netuid);
    const hotkey = String(payload?.hotkey || "").trim();
    const stakes = stakeWalletPortfolio?.stakes || stakeWalletPortfolio?.portfolio || [];
    const stakeRow = stakes.find(
      (row) => Number(row?.netuid) === nu && String(row?.hotkey || "").trim() === hotkey
    );
    const stakeTao = Number(stakeRow?.stake_tao ?? stakeRow?.tao ?? 0);
    if (!payload?.mev_protection) {
      prependLocalMempoolRow(
        buildOptimisticRemoveRow({
          netuid: nu,
          amount: Number.isFinite(stakeTao) && stakeTao > 0 ? stakeTao : undefined,
          slippage: payload?.slippage_pct,
          full: true,
        })
      );
    }
    return submitStakeRequest("/api/stake/remove", payload);
  };

  const submitRemoveAmount = async (payload) => {
    const nu = Number(payload?.netuid);
    const alphaAmt = Number(payload?.amount);
    const displayTao = alphaAmountToTao(nu, alphaAmt, {
      priceByNetuid: alphaPriceByNetuidRef.current,
      portfolio: stakeWalletPortfolio,
    });
    const alphaPrice = alphaPriceByNetuidRef.current[nu];
    if (!payload?.mev_protection) {
      prependLocalMempoolRow(
        buildOptimisticRemoveRow({
          netuid: nu,
          amount: displayTao,
          slippage: payload?.slippage_pct,
          full: false,
        })
      );
    }
    return submitStakeRequest("/api/stake/remove-amount", {
      ...payload,
      ...(Number.isFinite(alphaPrice) && alphaPrice > 0
        ? { alpha_price_tao: alphaPrice }
        : {}),
    });
  };

  const buildOptimisticBatchRow = (op, index, { mev = false } = {}) => {
    const walletAddr = String(stakeWalletPortfolio?.address || "").trim();
    const action = String(op?.action || "add").toLowerCase();
    const nu = Number(op?.netuid);
    const slip = Number(op?.slippage_pct);
    const isRemoveFull =
      action === "remove_full" || action === "remove" || action === "remove_stake_full";
    const isRemove = isRemoveFull || action.includes("remove");
    let call = "SubtensorModule.add_stake_limit";
    if (mev) call = "MevShield.submit_encrypted";
    else if (isRemoveFull) call = "SubtensorModule.remove_stake_full_limit";
    else if (isRemove) call = "SubtensorModule.remove_stake_limit";
    let amount = "-";
    if (op?.amount_tao != null && Number.isFinite(Number(op.amount_tao))) {
      amount = String(Number(op.amount_tao));
    } else if (op?.amount_alpha != null && Number.isFinite(Number(op.amount_alpha))) {
      const displayTao = alphaAmountToTao(nu, Number(op.amount_alpha), {
        priceByNetuid: alphaPriceByNetuidRef.current,
        portfolio: stakeWalletPortfolio,
      });
      if (Number.isFinite(displayTao)) {
        amount = String(displayTao);
      }
    }
    return {
      hash: `pending-${Date.now()}-${index}`,
      call,
      signer: walletAddr || "-",
      real_address: walletAddr || "-",
      amount,
      netuid: Number.isFinite(nu) ? String(nu) : "-",
      slippage: Number.isFinite(slip) ? `${slip}%` : "-",
      age: "0.0s",
      _age_seconds: 0,
      _row_type: mev ? "mev" : isRemove ? "remove" : "add",
    };
  };

  const submitBatchStake = async (payload) => {
    const ops = Array.isArray(payload?.ops) ? payload.ops : [];
    // MEV batch: wait for real wrapper hash from server (no fake pending rows).
    if (!payload?.mev_protection) {
      ops.forEach((op, index) => {
        prependLocalMempoolRow(buildOptimisticBatchRow(op, index));
      });
    }
    return submitStakeRequest("/api/stake/batch", payload);
  };

  return (
    <main className="layout-grid">
      {/* Column 1: portfolio (3/5) + notifications (2/5) side by side. */}
      <section className="lane lane-portfolio">
        <PortfolioPanel
          apiBase={apiBase}
          blockNumber={blockNumber}
          address={portfolioAddress}
          data={portfolioData}
          loading={portfolioLoading}
          error={portfolioError}
          onSearch={fetchPortfolio}
          onClear={clearPortfolio}
        />
        <TransferNotificationsPanel
          rows={filteredTransferRows}
          otherRows={mempoolOtherNotificationRows}
          onRemoveOne={removeTransferNotification}
          onRemoveOtherOne={removeOtherNotification}
          onClearTransfers={clearTransferNotifications}
          onClearOther={clearOtherNotifications}
          onClearAll={clearAllNotificationCards}
          onAddressClick={selectLookupAddress}
          labelFor={labelFor}
          minAmount={notificationMinAmount}
          onMinAmountChange={handleNotificationMinAmountChange}
        />
      </section>

      {/* Column 2 (50%): mempool / block monitoring. */}
      <section className="lane dashboard-lane">
        <BlockTopbar
          connected={connected}
          blockNumber={blockNumber}
          blockReceivedAtMs={blockReceivedAtMs}
          wsUrl={wsUrl}
          onToggleBots={() => setBotsDrawerOpen((v) => !v)}
        />
        <MempoolPanel
          rows={mempoolRows}
          pendingCount={mempoolPendingCount}
          error={mempoolError}
          onRowClick={handleRowClick}
          labelFor={labelFor}
        />
        <BlockPanel rows={blockRows} onRowClick={handleRowClick} labelFor={labelFor} />
      </section>

      <section className="lane lane-stake">
        <StakeSubmitPanel
          apiBase={stakeApiBase}
          defaultUseProxy={!["0", "false", "no", "off"].includes(stakeDefaultUseProxy)}
          portfolio={stakeWalletPortfolio}
          portfolioLoading={stakeWalletPortfolioLoading}
          portfolioError={stakeWalletPortfolioError}
          selectedNetuid={selectedNetuid}
          selectedNetuidTick={selectedNetuidTick}
          onRefreshPortfolio={refreshStakeWalletPortfolio}
          onSubmitAdd={submitAddStake}
          onSubmitRemoveFull={submitRemoveFull}
          onSubmitRemoveAmount={submitRemoveAmount}
          onSubmitTransfer={(payload) => submitStakeRequest("/api/stake/transfer", payload)}
          onSubmitBatch={submitBatchStake}
          onRefreshNonce={() => submitStakeRequest("/api/nonce/refresh", {})}
        />
      </section>

      {/* Hamburger drawer: follow-stake + auto-unstake bots. */}
      <div
        className={`bots-drawer-backdrop${botsDrawerOpen ? " open" : ""}`}
        onClick={() => setBotsDrawerOpen(false)}
      />
      <aside
        className={`bots-drawer${botsDrawerOpen ? " open" : ""}`}
        aria-hidden={!botsDrawerOpen}
      >
        <div className="bots-drawer-header">
          <div className="bots-drawer-title">Bots</div>
          <button
            type="button"
            className="action-btn"
            onClick={() => setBotsDrawerOpen(false)}
            aria-label="Close bots panel"
          >
            ✕
          </button>
        </div>
        <div className="bots-drawer-body">
          <FollowStakeBotPanel
            config={followStakeConfig}
            error={followStakeError}
            onSave={async (next) => {
              try {
                return await saveFollowStakeConfig(next);
              } catch (e) {
                setFollowStakeError(e instanceof Error ? e.message : "follow stake bot save failed");
                throw e;
              }
            }}
            onClearLogs={async () => {
              try {
                return await clearFollowStakeLogs();
              } catch (e) {
                setFollowStakeError(
                  e instanceof Error ? e.message : "follow stake bot clear logs failed"
                );
                throw e;
              }
            }}
          />
          <AutoUnstakePanel
            config={autoUnstakeConfig}
            error={autoUnstakeError}
            onSave={async (next) => {
              try {
                return await saveAutoUnstakeConfig(next);
              } catch (e) {
                setAutoUnstakeError(e instanceof Error ? e.message : "auto unstake save failed");
                throw e;
              }
            }}
            onClearLogs={async () => {
              try {
                return await clearAutoUnstakeLogs();
              } catch (e) {
                setAutoUnstakeError(
                  e instanceof Error ? e.message : "auto unstake bot clear logs failed"
                );
                throw e;
              }
            }}
          />
        </div>
      </aside>
    </main>
  );
}

export default App;
