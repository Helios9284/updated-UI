import { useEffect, useMemo, useState } from "react";
import { ConvertedLabel } from "./ConvertedLabel";
import { TableAmountFilterHeader, tableFilterEmptyHint } from "./TableAmountFilterHeader";
import {
  loadTableFilterPrefs,
  rowMeetsMinAmount,
  saveTableFilterPrefs,
} from "../utils/tableAmountFilter";

const FILTER_STORAGE_KEY = "ultra-mempool-block-table-filter";

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

function displayStatus(value) {
  const text = formatCell(value).toLowerCase();
  if (text === "-") return "-";
  if (text === "success") return "ok";
  if (text === "failed") return "fail";
  return text;
}

function rowClassByCall(callValue) {
  const call = String(callValue || "").toLowerCase();
  if (call.includes("mevshield")) return "row-mev";
  if (call.includes("remove") || call.includes("unstake")) return "row-remove";
  if (call.includes("add") || call.includes("stake")) return "row-add";
  return "";
}

function keyDisplaySigner(row) {
  return formatCell(row?.real_address && row.real_address !== "-" ? row.real_address : row?.signer);
}

function keyAmount(row) {
  return formatCell(row?.amount_tao ?? row?.amount);
}

function parseRowAmountTao(row) {
  const text = keyAmount(row).trim();
  if (text === "-") return null;
  const numeric = Number.parseFloat(text);
  return Number.isFinite(numeric) ? numeric : null;
}

function formatTaoAmount(value) {
  if (value == null || !Number.isFinite(value)) return "-";
  const s = value.toFixed(9).replace(/\.?0+$/, "");
  return s || "0";
}

function blockRowAddress(row) {
  return keyDisplaySigner(row);
}

function blockMergeGroupKey(row) {
  return [
    formatCell(row?.idx ?? row?.extrinsic_id),
    blockRowAddress(row),
    formatCell(row?.netuid),
  ].join("|");
}

function getStakeCallSide(row) {
  const call = String(row?.call || "").toLowerCase();
  if (call.includes("stakeremoved") || call.includes("remove_stake") || call.includes("unstake")) {
    return "remove";
  }
  if (call.includes("stakeadded") || call.includes("add_stake")) {
    return "add";
  }
  if (call.includes("remove")) return "remove";
  if (call.includes("add")) return "add";
  return null;
}

function consolidateBlockDisplayRows(rows) {
  const passthrough = [];
  const groups = new Map();
  const groupOrder = [];

  for (const row of rows || []) {
    if (row?._same_origin_destination) continue;

    const netuidText = formatCell(row?.netuid);
    if (netuidText === "-" || netuidText.includes("->")) {
      passthrough.push(row);
      continue;
    }

    const side = getStakeCallSide(row);
    const amount = parseRowAmountTao(row);
    if (!side || amount == null) {
      passthrough.push(row);
      continue;
    }

    const key = blockMergeGroupKey(row);
    if (!groups.has(key)) {
      groups.set(key, {
        template: row,
        removeTotal: side === "remove" ? amount : 0,
        addTotal: side === "add" ? amount : 0,
      });
      groupOrder.push(key);
      continue;
    }

    const bucket = groups.get(key);
    if (side === "remove") bucket.removeTotal += amount;
    else bucket.addTotal += amount;
  }

  const consolidated = [];
  for (const key of groupOrder) {
    const bucket = groups.get(key);
    const netRemove = bucket.removeTotal - bucket.addTotal;
    if (Math.abs(netRemove) < 1e-9) continue;

    const template = bucket.template;
    if (netRemove > 0) {
      consolidated.push({
        ...template,
        call: "remove_stake",
        amount: formatTaoAmount(netRemove),
        amount_tao: formatTaoAmount(netRemove),
      });
    } else {
      consolidated.push({
        ...template,
        call: "add_stake",
        amount: formatTaoAmount(-netRemove),
        amount_tao: formatTaoAmount(-netRemove),
      });
    }
  }

  return [...passthrough, ...consolidated];
}

function splitDedupKey(row, normalizedCall, netuidValue) {
  return [
    formatCell(row?.idx ?? row?.extrinsic_id),
    normalizedCall,
    formatCell(netuidValue),
    keyAmount(row),
    keyDisplaySigner(row),
  ].join("|");
}

function expandSplitStakeRows(rows) {
  const input = rows || [];
  const passthrough = [];
  const synthetic = [];
  const splitDerivedKeys = new Set();

  for (const row of input) {
    const call = String(row?.call || "").toLowerCase();
    const callToken = call.split(".").pop().split(" ")[0];
    const isSplitStakeKind = [
      "transfer_stake",
      "transfer_stake_limit",
      "move_stake",
      "move_stake_limit",
      "swap_stake",
      "swap_stake_limit",
      "staketransferred",
      "stakemoved",
      "stakeswapped",
    ].includes(callToken);
    const netuidText = formatCell(row?.netuid);
    const hasOriginDest = netuidText.includes("->");
    if (!isSplitStakeKind) {
      passthrough.push(row);
      continue;
    }
    if (!hasOriginDest) continue;

    const [originRaw, destinationRaw] = netuidText.split("->", 2);
    const originNetuid = formatCell(originRaw).trim();
    const destinationNetuid = formatCell(destinationRaw).trim();

    synthetic.push({
      ...row,
      call: "remove_stake",
      netuid: originNetuid || "-",
      _splitOrder: 0,
    });
    splitDerivedKeys.add(splitDedupKey(row, "remove_stake", originNetuid || "-"));

    synthetic.push({
      ...row,
      call: "add_stake",
      netuid: destinationNetuid || "-",
      _splitOrder: 1,
    });
    splitDerivedKeys.add(splitDedupKey(row, "add_stake", destinationNetuid || "-"));
  }

  const filteredPassThrough = passthrough.filter((row) => {
    const call = String(row?.call || "").toLowerCase();
    const isStakeRemovedEvent = call.includes("stakeremoved [event]");
    const isStakeAddedEvent = call.includes("stakeadded [event]");
    if (!isStakeRemovedEvent && !isStakeAddedEvent) return true;
    const normalizedCall = isStakeRemovedEvent ? "remove_stake" : "add_stake";
    const key = splitDedupKey(row, normalizedCall, formatCell(row?.netuid));
    return !splitDerivedKeys.has(key);
  });

  return [...filteredPassThrough, ...synthetic];
}

export function BlockPanel({ rows, onRowClick, labelFor }) {
  const [minAmount, setMinAmount] = useState(() => loadTableFilterPrefs(FILTER_STORAGE_KEY).minAmount);
  const [filterEnabled, setFilterEnabled] = useState(
    () => loadTableFilterPrefs(FILTER_STORAGE_KEY).enabled
  );

  useEffect(() => {
    saveTableFilterPrefs(FILTER_STORAGE_KEY, { minAmount, enabled: filterEnabled });
  }, [minAmount, filterEnabled]);

  const sortedRows = useMemo(() => {
    const displayRows = consolidateBlockDisplayRows(expandSplitStakeRows(rows));
    return [...displayRows].sort((a, b) => {
      const aIdx = Number(a?.idx ?? a?.extrinsic_id ?? Number.POSITIVE_INFINITY);
      const bIdx = Number(b?.idx ?? b?.extrinsic_id ?? Number.POSITIVE_INFINITY);
      if (aIdx !== bIdx) return aIdx - bIdx;
      const aNetuid = Number.parseInt(formatCell(a?.netuid), 10);
      const bNetuid = Number.parseInt(formatCell(b?.netuid), 10);
      if (Number.isFinite(aNetuid) && Number.isFinite(bNetuid) && aNetuid !== bNetuid) {
        return aNetuid - bNetuid;
      }
      return String(a?.call || "").localeCompare(String(b?.call || ""));
    });
  }, [rows]);

  const visibleRows = useMemo(
    () => sortedRows.filter((row) => rowMeetsMinAmount(row, minAmount, filterEnabled)),
    [sortedRows, minAmount, filterEnabled]
  );

  const hiddenCount = sortedRows.length - visibleRows.length;
  const filterHint = tableFilterEmptyHint(filterEnabled, minAmount, hiddenCount);

  return (
    <section className="panel block-panel">
      <TableAmountFilterHeader
        title="Block"
        minAmount={minAmount}
        onMinAmountChange={setMinAmount}
        filterEnabled={filterEnabled}
        onFilterEnabledChange={setFilterEnabled}
      />
      {visibleRows.length === 0 ? (
        <div className="panel-empty mono">{filterHint || "No stake rows in block"}</div>
      ) : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>idx</th>
              <th>status</th>
              <th>call</th>
              <th>signer</th>
              <th>amount</th>
              <th>netuid</th>
            </tr>
          </thead>
          <tbody>
            {visibleRows.map((r, i) => {
              const displaySigner =
                r?.real_address && r.real_address !== "-" ? r.real_address : r?.signer;
              const label = labelFor?.(displaySigner) || "";
              const signerTitle = formatCell(displaySigner);
              const statusClass =
                String(r?.status || "").toLowerCase() === "failed" ? "row-failed" : "";
              const rowProxyFake =
                r?.proxy_fake && String(r?.status || "").toLowerCase() === "failed"
                  ? "row-proxy-fake"
                  : "";
              const rowClass = [rowClassByCall(r?.call), statusClass, rowProxyFake]
                .filter(Boolean)
                .join(" ");
              const fakeTitle = r?.proxy_fake
                ? "Proxy: signer is not a registered delegate for this real account (Proxy::Proxies)."
                : signerTitle !== "-"
                  ? signerTitle
                  : undefined;
              return (
                <tr
                  key={`b-${i}`}
                  className={rowClass || undefined}
                  title={fakeTitle}
                  onClick={() => onRowClick?.(r)}
                  style={{ cursor: onRowClick ? "pointer" : "default" }}
                >
                  <td>{formatCell(r.idx ?? r.extrinsic_id)}</td>
                  <td className={`mono status-cell status-${String(r?.status || "unknown").toLowerCase()}`}>
                    {displayStatus(r?.status)}
                  </td>
                  <td className="mono">{displayCall(r.call)}</td>
                  <td className="mono" title={formatCell(displaySigner)}>
                    <ConvertedLabel
                      label={label}
                      fallback={shortenAddress(displaySigner)}
                      title={formatCell(displaySigner)}
                    />
                  </td>
                  <td>{formatCell(r.amount_tao ?? r.amount)}</td>
                  <td>{formatCell(r.netuid)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
