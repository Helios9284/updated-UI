const EMISSION_RANKING_KEY = "ultra-emission-ranking:v1";

function emptySnapshot() {
  return {
    version: 1,
    updated_at: null,
    block_number: null,
    rankings: [],
  };
}

export function loadEmissionRanking() {
  try {
    const raw = localStorage.getItem(EMISSION_RANKING_KEY);
    if (!raw) return emptySnapshot();
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return emptySnapshot();
    const rankings = Array.isArray(parsed.rankings) ? parsed.rankings : [];
    return {
      version: 1,
      updated_at: parsed.updated_at || null,
      block_number: parsed.block_number ?? null,
      rankings: rankings
        .filter((row) => row && Number.isFinite(Number(row.netuid)))
        .map((row, index) => ({
          netuid: Number(row.netuid),
          rank: Number(row.rank) || index + 1,
          subnet_name: String(row.subnet_name || `subnet-${row.netuid}`),
          emission_pct: Number(row.emission_pct) || 0,
          emission_tao: Number(row.emission_tao) || 0,
        })),
    };
  } catch {
    return emptySnapshot();
  }
}

export function saveEmissionRanking(snapshot) {
  try {
    const payload = {
      version: 1,
      updated_at: snapshot?.updated_at || new Date().toISOString(),
      block_number: snapshot?.block_number ?? null,
      rankings: Array.isArray(snapshot?.rankings) ? snapshot.rankings : [],
    };
    localStorage.setItem(EMISSION_RANKING_KEY, JSON.stringify(payload));
    return payload;
  } catch {
    return snapshot;
  }
}

export function buildEmissionRankingSnapshot(emittingRows, blockNumber) {
  const rankings = (emittingRows || []).map((row, index) => ({
    netuid: Number(row.netuid),
    rank: index + 1,
    subnet_name: String(row.subnet_name || `subnet-${row.netuid}`),
    emission_pct: Number(row.emission_pct) || 0,
    emission_tao: Number(row.emission_tao) || 0,
  }));
  return {
    version: 1,
    updated_at: new Date().toISOString(),
    block_number: blockNumber ?? null,
    rankings,
  };
}

function formatPct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  if (n >= 10) return `${n.toFixed(1)}%`;
  if (n >= 1) return `${n.toFixed(2)}%`;
  return `${n.toFixed(3)}%`;
}

/**
 * Compare previous JSON ranking vs current emitting list.
 * Returns Other-notification rows for new emitters and rank changes.
 * First seed (empty previous) returns [] and caller should just save.
 */
export function diffEmissionRanking(previous, currentRows, blockNumber) {
  const prevList = Array.isArray(previous?.rankings) ? previous.rankings : [];
  if (!prevList.length) return [];

  const prevByNetuid = new Map();
  for (const row of prevList) {
    prevByNetuid.set(Number(row.netuid), row);
  }

  const alerts = [];
  const ts = Date.now();
  const block = blockNumber ?? null;

  (currentRows || []).forEach((row, index) => {
    const netuid = Number(row.netuid);
    if (!Number.isFinite(netuid) || netuid <= 0) return;
    const rank = index + 1;
    const name = String(row.subnet_name || `subnet-${netuid}`);
    const pct = Number(row.emission_pct) || 0;
    const prev = prevByNetuid.get(netuid);

    if (!prev) {
      alerts.push({
        id: `emission-new-${netuid}-${block ?? ts}-${ts}`,
        call: "SubtensorModule.new_emission",
        alert_kind: "new_emission",
        signer: "-",
        netuid: String(netuid),
        subnet_name: name,
        rank,
        emission_pct: pct,
        emission_tao: Number(row.emission_tao) || 0,
        message: `new emission · SN${netuid} ${name} · #${rank} (${formatPct(pct)})`,
        block_number: block,
        status: "confirmed",
        age: "0s",
      });
      return;
    }

    const prevRank = Number(prev.rank) || 0;
    if (prevRank > 0 && prevRank !== rank) {
      const direction = rank < prevRank ? "up" : "down";
      alerts.push({
        id: `emission-rank-${netuid}-${prevRank}-${rank}-${block ?? ts}`,
        call: "SubtensorModule.emission_change",
        alert_kind: "emission_change",
        signer: "-",
        netuid: String(netuid),
        subnet_name: name,
        rank,
        prev_rank: prevRank,
        emission_pct: pct,
        emission_tao: Number(row.emission_tao) || 0,
        message: `emission change · SN${netuid} ${name} · #${prevRank} → #${rank} (${direction})`,
        block_number: block,
        status: "confirmed",
        age: "0s",
      });
    }
  });

  // Cap per-block noise.
  return alerts.slice(0, 20);
}
