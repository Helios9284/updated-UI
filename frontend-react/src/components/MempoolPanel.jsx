import { useEffect, useMemo, useState } from "react";
import { ConvertedLabel } from "./ConvertedLabel";
import { TableAmountFilterHeader, tableFilterEmptyHint } from "./TableAmountFilterHeader";
import {
  loadTableFilterPrefs,
  rowMeetsMinAmount,
  saveTableFilterPrefs,
} from "../utils/tableAmountFilter";

const FILTER_STORAGE_KEY = "ultra-mempool-mempool-table-filter";

function formatCell(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function shortenAddress(value) {
  const text = formatCell(value);
  if (text === "-" || text.length <= 14) return text;
  return `${text.slice(0, 6)}..${text.slice(-6)}`;
}

function displayCall(value) {
  const text = formatCell(value);
  if (text === "-") return text;
  return text.replace(/^SubtensorModule\./i, "");
}

function rowClassByCall(callValue) {
  const call = String(callValue || "").toLowerCase();
  if (call.includes("mevshield")) return "row-mev";
  if (call.includes("remove") || call.includes("unstake")) return "row-remove";
  if (call.includes("add") || call.includes("stake")) return "row-add";
  return "";
}

function parseAgeToSeconds(ageValue) {
  const text = formatCell(ageValue).trim();
  if (text === "-") return Number.NEGATIVE_INFINITY;
  const numeric = Number.parseFloat(text.replace(/[^0-9.]+/g, ""));
  if (!Number.isFinite(numeric)) return Number.NEGATIVE_INFINITY;
  const lower = text.toLowerCase();
  if (lower.includes("ms")) return numeric / 1000;
  if (lower.includes("m") && !lower.includes("ms")) return numeric * 60;
  if (lower.includes("h")) return numeric * 3600;
  return numeric;
}

export function MempoolPanel({ rows, pendingCount = 0, error = "", onRowClick, labelFor }) {
  const [minAmount, setMinAmount] = useState(() => loadTableFilterPrefs(FILTER_STORAGE_KEY).minAmount);
  const [filterEnabled, setFilterEnabled] = useState(
    () => loadTableFilterPrefs(FILTER_STORAGE_KEY).enabled
  );

  useEffect(() => {
    saveTableFilterPrefs(FILTER_STORAGE_KEY, { minAmount, enabled: filterEnabled });
  }, [minAmount, filterEnabled]);

  const sortedRows = useMemo(
    () =>
      [...(rows || [])].sort(
        (a, b) => parseAgeToSeconds(b?.age) - parseAgeToSeconds(a?.age)
      ),
    [rows]
  );

  const visibleRows = useMemo(
    () => sortedRows.filter((row) => rowMeetsMinAmount(row, minAmount, filterEnabled)),
    [sortedRows, minAmount, filterEnabled]
  );

  const hiddenCount = sortedRows.length - visibleRows.length;
  const filterHint = tableFilterEmptyHint(filterEnabled, minAmount, hiddenCount);

  const emptyHint = error
    ? `Backend error: ${error}`
    : filterHint
      ? filterHint
      : pendingCount > 0
        ? `${pendingCount} pending extrinsics, none are stake/MevShield txs`
        : "No pending extrinsics in mempool";

  return (
    <section className="panel mempool-panel">
      <TableAmountFilterHeader
        title="Mempool"
        minAmount={minAmount}
        onMinAmountChange={setMinAmount}
        filterEnabled={filterEnabled}
        onFilterEnabledChange={setFilterEnabled}
      />
      {visibleRows.length === 0 ? (
        <div className="panel-empty mono">{emptyHint}</div>
      ) : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>call</th>
              <th>signer</th>
              <th>amount</th>
              <th>netuid</th>
              <th>slippage</th>
              <th>age</th>
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((r, i) => {
              const displaySigner =
                r?.real_address && r.real_address !== "-" ? r.real_address : r?.signer;
              const label = labelFor?.(displaySigner) || "";
              const signerTitle = formatCell(displaySigner);
              const rowProxyFake = r?.proxy_fake ? "row-proxy-fake" : "";
              const fakeTitle = r?.proxy_fake
                ? "Proxy: signer is not a registered delegate for this real account (Proxy::Proxies)."
                : signerTitle !== "-"
                  ? signerTitle
                  : undefined;
              return (
                <tr
                  key={`m-${i}`}
                  className={[rowClassByCall(r?.call), rowProxyFake].filter(Boolean).join(" ") || undefined}
                  title={fakeTitle}
                  onClick={() => onRowClick?.(r)}
                  style={{ cursor: onRowClick ? "pointer" : "default" }}
                >
                  <td className="mono">{displayCall(r.call)}</td>
                  <td className="mono" title={formatCell(displaySigner)}>
                    <ConvertedLabel
                      label={label}
                      fallback={shortenAddress(displaySigner)}
                      title={formatCell(displaySigner)}
                    />
                  </td>
                  <td>{formatCell(r.amount)}</td>
                  <td>{formatCell(r.netuid)}</td>
                  <td>{formatCell(r.slippage)}</td>
                  <td>{formatCell(r.age)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
