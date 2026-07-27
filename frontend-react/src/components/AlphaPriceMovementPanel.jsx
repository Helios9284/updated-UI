import { useMemo, useState } from "react";
import { CollapsiblePanel } from "./CollapsiblePanel";

const BLOCKS_PER_HOUR = 300; // ~12s blocks

function formatPrice(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return n.toFixed(6);
}

function formatDelta(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return n === 0 ? "0" : "-";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(8)}`;
}

function formatPct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toFixed(4)}%`;
}

function formatDepth(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toFixed(0);
}

function trendGlyph(row) {
  if (!row || row.samples < 2 || row.direction === 0) return "~";
  if (row.steady) return row.direction > 0 ? "▲" : "▼";
  return row.direction > 0 ? "△" : "▽";
}

function rowClass(row) {
  if (!row || row.samples < 2 || row.direction === 0) return "apm-flat";
  if (!row.steady) return "apm-noisy";
  return row.direction > 0 ? "apm-up" : "apm-down";
}

function trendTitle(row) {
  if (!row || row.samples < 2) return "collecting...";
  const parts = [`${Math.round(row.consistency * 100)}% consistent over ${row.samples} blocks`];
  if (row.hasSpike) {
    const sign = row.spikePct > 0 ? "+" : "";
    parts.push(
      `spike ignored: a single-block move of ${sign}${Number(row.spikePct).toFixed(4)}% (likely a buyer/seller) is excluded from the steady rate`
    );
  } else if (row.steady) {
    parts.push("steady drift (no spikes)");
  }
  return parts.join(" · ");
}

function ProfitEstimator({ list }) {
  const [netuid, setNetuid] = useState("");
  const [taoAmount, setTaoAmount] = useState("");
  const [hours, setHours] = useState("");

  const result = useMemo(() => {
    const nu = Number.parseInt(String(netuid).trim(), 10);
    const tao = Number.parseFloat(String(taoAmount).trim());
    const h = Number.parseFloat(String(hours).trim());
    if (!Number.isFinite(nu)) return null;
    if (!Number.isFinite(tao) || tao <= 0) return null;
    if (!Number.isFinite(h) || h <= 0) return null;
    const row = list.find((r) => Number(r.netuid) === nu);
    if (!row) return { error: `subnet ${nu} not tracked yet` };
    if (row.samples < 4) return { error: `subnet ${nu}: not enough price history yet` };
    const r = Number(row.deltaPct) / 100; // steady per-block rate (fraction)
    const n = h * BLOCKS_PER_HOUR;
    const growth = Math.pow(1 + r, n);
    const profitPct = (growth - 1) * 100;
    const profitTao = tao * (growth - 1);
    return { profitTao, profitPct, ratePct: Number(row.deltaPct) };
  }, [netuid, taoAmount, hours, list]);

  const sign = (n) => (n > 0 ? "+" : "");

  return (
    <div className="apm-estimator">
      <div className="apm-estimator-inputs">
        <label className="field-label">
          netuid
          <input
            className="wallet-input mono"
            type="text"
            inputMode="numeric"
            placeholder="69"
            value={netuid}
            onChange={(e) => setNetuid(e.target.value.replace(/\D/g, ""))}
          />
        </label>
        <label className="field-label">
          tao
          <input
            className="wallet-input mono"
            type="number"
            min="0"
            step="0.1"
            placeholder="10"
            value={taoAmount}
            onChange={(e) => setTaoAmount(e.target.value)}
          />
        </label>
        <label className="field-label">
          hours
          <input
            className="wallet-input mono"
            type="number"
            min="0"
            step="1"
            placeholder="24"
            value={hours}
            onChange={(e) => setHours(e.target.value)}
          />
        </label>
      </div>
      <div className="apm-estimator-result mono">
        {!result ? (
          <span className="apm-estimator-hint">enter netuid, tao, hours</span>
        ) : result.error ? (
          <span className="apm-estimator-hint">{result.error}</span>
        ) : (
          <span className={result.profitTao >= 0 ? "apm-up" : "apm-down"}>
            est. profit: {sign(result.profitTao)}
            {result.profitTao.toFixed(4)} τ ({sign(result.profitPct)}
            {result.profitPct.toFixed(3)}%)
          </span>
        )}
      </div>
    </div>
  );
}

export function AlphaPriceMovementPanel({ rows, windowMax, samplesTracked, lastBlock }) {
  const list = rows || [];
  return (
    <CollapsiblePanel
      title={`Most Profitable Subnets · alpha price${
        samplesTracked ? ` (${Math.min(samplesTracked, windowMax)}/${windowMax} blocks)` : ""
      }`}
      storageKey="panel:alpha-price-movement"
      className="alpha-movement-panel"
    >
      {lastBlock ? (
        <div className="portfolio-summary mono">
          <div>block: {lastBlock}</div>
          <div>subnets: {list.length}</div>
        </div>
      ) : null}
      <ProfitEstimator list={list} />
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>subnet</th>
              <th>price τ</th>
              <th>Δ/block</th>
              <th title="median per-block % change (robust to one-off spikes)">
                steady Δ%/block
              </th>
              <th>depth τ</th>
              <th>trend</th>
            </tr>
          </thead>
          <tbody>
            {list.length === 0 ? (
              <tr>
                <td colSpan={7} className="mono">
                  collecting price history...
                </td>
              </tr>
            ) : (
              list.map((row, idx) => (
                <tr key={row.netuid} className={rowClass(row)}>
                  <td className="mono">{row.samples < 2 ? "-" : idx + 1}</td>
                  <td className="mono" title={row.name}>
                    {row.symbol ? `${row.symbol} ` : ""}
                    {row.netuid}
                  </td>
                  <td className="mono">{formatPrice(row.price)}</td>
                  <td className="mono">{row.samples < 2 ? "-" : formatDelta(row.deltaAbs)}</td>
                  <td className="mono">
                    {row.samples < 2 ? "-" : formatPct(row.deltaPct)}
                    {row.hasSpike ? (
                      <span
                        className="apm-spike"
                        title={`single-block spike of ${row.spikePct > 0 ? "+" : ""}${Number(
                          row.spikePct
                        ).toFixed(4)}% excluded (trade-driven, not steady yield)`}
                      >
                        {" ⚡"}
                      </span>
                    ) : null}
                  </td>
                  <td className="mono" title="TAO reserve in the subnet pool (liquidity)">
                    {formatDepth(row.depth)}
                  </td>
                  <td className="mono apm-trend" title={trendTitle(row)}>
                    {trendGlyph(row)}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </CollapsiblePanel>
  );
}
