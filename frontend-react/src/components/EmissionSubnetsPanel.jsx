function formatCell(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function formatEmission(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n >= 1) return n.toFixed(4);
  if (n >= 0.0001) return n.toFixed(6);
  return n.toExponential(2);
}

function formatPercent(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return "-";
  if (n >= 10) return `${n.toFixed(1)}%`;
  if (n >= 1) return `${n.toFixed(2)}%`;
  return `${n.toFixed(3)}%`;
}

/**
 * Subnets currently receiving block emission (emission_tao > 0),
 * sorted largest → smallest.
 */
export function EmissionSubnetsPanel({ rows, blockNumber, onNetuidClick }) {
  const list = Array.isArray(rows) ? rows : [];

  return (
    <section className="panel emission-subnets-panel">
      <div className="panel-header-row">
        <div className="panel-title">Emitting</div>
        <div className="emission-subnets-meta mono">
          {list.length}
          {blockNumber != null ? ` · #${blockNumber}` : ""}
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>sn</th>
              <th>name</th>
              <th>emis</th>
              <th>%</th>
            </tr>
          </thead>
          <tbody>
            {list.map((row) => {
              const netuid = Number(row?.netuid);
              const clickable = typeof onNetuidClick === "function" && Number.isFinite(netuid);
              return (
                <tr
                  key={`em-sn-${netuid}`}
                  className={clickable ? "emission-row-clickable" : undefined}
                  onClick={clickable ? () => onNetuidClick(netuid) : undefined}
                >
                  <td className="mono">{formatCell(netuid)}</td>
                  <td title={formatCell(row?.subnet_name)}>
                    {formatCell(row?.subnet_name || `subnet-${netuid}`)}
                  </td>
                  <td className="mono" title={`${formatCell(row?.emission_tao)} TAO/block`}>
                    {formatEmission(row?.emission_tao)}
                  </td>
                  <td className="mono">{formatPercent(row?.emission_pct)}</td>
                </tr>
              );
            })}
            {list.length === 0 ? (
              <tr>
                <td colSpan={4} className="mono">
                  No emitting subnets
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}
