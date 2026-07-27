import { useState } from "react";
import { SubnetDirectoryModal } from "./SubnetDirectoryModal";

function formatCell(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function shortenAddress(value) {
  const text = formatCell(value);
  if (text === "-" || text.length <= 18) return text;
  return `${text.slice(0, 10)}..${text.slice(-6)}`;
}

export function PortfolioPanel({
  apiBase,
  blockNumber,
  address,
  data,
  loading,
  error,
  onSearch,
  onClear,
}) {
  const [input, setInput] = useState("");
  const [subnetsOpen, setSubnetsOpen] = useState(false);
  const rows = data?.portfolio || [];
  const expanded = loading || rows.length > 0;

  return (
    <section
      className={`panel portfolio-panel${expanded ? " portfolio-panel-expanded" : " portfolio-panel-compact"}`}
    >
      <div className="panel-header-row">
        <div className="panel-title">Portfolio</div>
        <button
          type="button"
          className="portfolio-btn panel-header-btn"
          onClick={() => setSubnetsOpen((open) => !open)}
          aria-expanded={subnetsOpen}
        >
          {subnetsOpen ? "Hide Subnets" : "Subnets"}
        </button>
      </div>
      <div className="portfolio-controls">
        <input
          className="portfolio-input mono"
          placeholder="Search wallet (SS58)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
        <button
          type="button"
          className="portfolio-btn"
          onClick={() => onSearch?.(input.trim())}
          disabled={!input.trim()}
        >
          Search
        </button>
        <button type="button" className="portfolio-btn" onClick={() => {
          setInput("");
          onClear?.();
        }}>
          Clear
        </button>
      </div>
      <div className="portfolio-summary mono">
        <div title={formatCell(address)}>address: {formatCell(address)}</div>
        <div>free: {formatCell(data?.free_balance)} TAO</div>
        <div>staked: {formatCell(data?.portfolio_total_tao)} TAO</div>
        <div>fetch: {formatCell(data?.total_fetch_ms ?? data?.fetch_ms)} ms</div>
      </div>
      {loading ? <div className="portfolio-msg mono">loading...</div> : null}
      {error ? <div className="portfolio-msg mono">{error}</div> : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>validator</th>
              <th>netuid</th>
              <th>tao</th>
              <th>emission</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => (
              <tr key={`pf-${idx}-${row.hotkey || ""}-${row.netuid}`}>
                <td title={formatCell(row.hotkey)}>{shortenAddress(row.hotkey)}</td>
                <td>{formatCell(row.netuid)}</td>
                <td>{formatCell(row.tao)}</td>
                <td>{formatCell(row.emission)}</td>
              </tr>
            ))}
            {!loading && rows.length === 0 ? (
              <tr>
                <td colSpan={4} className="mono">No positions</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <SubnetDirectoryModal
        open={subnetsOpen}
        onClose={() => setSubnetsOpen(false)}
        apiBase={apiBase}
        blockNumber={blockNumber}
      />
    </section>
  );
}
