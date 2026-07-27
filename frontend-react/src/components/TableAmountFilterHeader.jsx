function formatCell(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

export function TableAmountFilterHeader({
  title,
  minAmount,
  onMinAmountChange,
  filterEnabled,
  onFilterEnabledChange,
}) {
  return (
    <div className="panel-title table-panel-title">
      <span>{title}</span>
      <div className="table-filter-controls">
        <span className="table-filter-label mono">min TAO</span>
        <input
          type="number"
          min="0"
          step="0.01"
          className="table-filter-threshold-input mono"
          value={Number.isFinite(Number(minAmount)) ? Number(minAmount) : 0}
          onChange={(e) => {
            const n = Number.parseFloat(e.target.value);
            onMinAmountChange?.(Number.isFinite(n) && n >= 0 ? n : 0);
          }}
        />
        <button
          type="button"
          className={`table-filter-toggle ${filterEnabled ? "is-active" : ""}`}
          onClick={() => onFilterEnabledChange?.(!filterEnabled)}
          title={filterEnabled ? "Hide rows below min amount" : "Show all rows"}
        >
          {filterEnabled ? "Hide small" : "Show all"}
        </button>
      </div>
    </div>
  );
}

export function tableFilterEmptyHint(filterEnabled, minAmount, hiddenCount) {
  if (!filterEnabled || hiddenCount <= 0) return "";
  return `${hiddenCount} row(s) hidden below ${formatCell(minAmount)} TAO`;
}
