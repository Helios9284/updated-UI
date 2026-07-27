import { useEffect, useState } from "react";
import { ConvertedLabel } from "./ConvertedLabel";

const NOTIFICATIONS_OPEN_KEY = "panel:notifications-open";

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
  return text.replace(/^Balances\./i, "").replace(/^SubtensorModule\./i, "");
}

function displayTransferCall(value) {
  const text = displayCall(value).toLowerCase();
  if (text.startsWith("transfer")) return "transfer";
  if (text === "lock_stake") return "lock_stake";
  return displayCall(value);
}

function formatTransferAmount(value) {
  const text = formatCell(value).trim();
  if (text === "-") return "-";
  const numeric = Number.parseFloat(text.replace(/[^0-9.\-]+/g, ""));
  if (!Number.isFinite(numeric)) return text;
  return numeric.toFixed(1);
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

function parseNotificationStatus(value) {
  const status = String(value || "").toLowerCase();
  if (status === "confirmed") return "confirmed";
  if (status === "failed") return "failed";
  return "pending";
}

function callToken(value) {
  const text = displayCall(value).toLowerCase();
  return text.split(" ")[0];
}

function otherCardClass(row) {
  const token = callToken(row?.call);
  if (token === "start_call") return "notification-start-call";
  if (token === "register_network") return "notification-register-network";
  if (token === "set_subnet_identity") return "notification-subnet-identity";
  if (token.includes("coldkey")) return "notification-coldkey-swap";
  return "notification-other";
}

function hasAddressBookLabel(labelFor, ...addresses) {
  return addresses.some((address) => {
    const addr = String(address || "");
    return addr && addr !== "-" && Boolean(labelFor?.(addr));
  });
}

function trackedCardClass(labelFor, ...addresses) {
  return hasAddressBookLabel(labelFor, ...addresses) ? " notification-card-tracked" : "";
}

function NotificationAddressButton({ address, labelFor, onAddressClick, fullAddress = false }) {
  const text = formatCell(address);
  const label = labelFor?.(address) || "";
  const fallback = fullAddress ? text : shortenAddress(address);
  return (
    <button
      type="button"
      className={`notification-address-btn mono${label ? " notification-address-tracked" : ""}`}
      title={text}
      disabled={text === "-"}
      onClick={() => onAddressClick?.(address)}
    >
      <ConvertedLabel label={label} fallback={fallback} title={text} />
    </button>
  );
}

function otherRowAddresses(row) {
  const signer =
    row?.real_address && row.real_address !== "-" ? row.real_address : row?.signer;
  const token = callToken(row?.call);
  if (token === "start_call" || token === "register_network") {
    return [signer, row?.hotkey];
  }
  if (token.includes("coldkey")) {
    return [signer, row?.old_coldkey, row?.new_coldkey];
  }
  return [signer];
}

function OtherNotificationBody({ row, onAddressClick, labelFor }) {
  const signer =
    row?.real_address && row.real_address !== "-" ? row.real_address : row?.signer;
  const token = callToken(row?.call);

  if (token === "start_call" || token === "register_network") {
    return (
      <div className="notification-detail mono">
        <div>
          <span className="notification-detail-label">signer </span>
          <NotificationAddressButton
            address={signer}
            labelFor={labelFor}
            onAddressClick={onAddressClick}
          />
        </div>
        <div>
          <span className="notification-detail-label">hotkey </span>
          <NotificationAddressButton
            address={row?.hotkey}
            labelFor={labelFor}
            onAddressClick={onAddressClick}
          />
        </div>
      </div>
    );
  }

  if (token === "set_subnet_identity") {
    return (
      <div className="notification-detail mono">
        <div>
          <span className="notification-detail-label">signer </span>
          <NotificationAddressButton
            address={signer}
            labelFor={labelFor}
            onAddressClick={onAddressClick}
          />
        </div>
        <div>
          <span className="notification-detail-label">netuid </span>
          {formatCell(row?.netuid)}
        </div>
        <div>
          <span className="notification-detail-label">name </span>
          {formatCell(row?.subnet_name)}
        </div>
        <div>
          <span className="notification-detail-label">repo </span>
          {formatCell(row?.github_repo)}
        </div>
      </div>
    );
  }

  if (token.includes("coldkey")) {
    return (
      <div className="notification-detail mono">
        <div>
          <span className="notification-detail-label">signer </span>
          <NotificationAddressButton
            address={signer}
            labelFor={labelFor}
            onAddressClick={onAddressClick}
          />
        </div>
        <div>
          <span className="notification-detail-label">old </span>
          <NotificationAddressButton
            address={row?.old_coldkey}
            labelFor={labelFor}
            onAddressClick={onAddressClick}
          />
        </div>
        <div>
          <span className="notification-detail-label">new </span>
          <NotificationAddressButton
            address={row?.new_coldkey}
            labelFor={labelFor}
            onAddressClick={onAddressClick}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="notification-detail mono">
      <NotificationAddressButton
        address={signer}
        labelFor={labelFor}
        onAddressClick={onAddressClick}
      />
    </div>
  );
}

function OtherNotificationMeta({ row }) {
  const token = callToken(row?.call);
  const status = parseNotificationStatus(row?.status);

  if (token === "start_call") {
    return (
      <footer className="notification-meta">
        <span>netuid {formatCell(row?.netuid)}</span>
        <span>
          {status === "pending"
            ? `age ${formatCell(row?.age)}`
            : `block ${formatCell(row?.block_number)}`}
        </span>
      </footer>
    );
  }

  if (token === "set_subnet_identity") {
    return (
      <footer className="notification-meta">
        <span>
          {status === "pending"
            ? `age ${formatCell(row?.age)}`
            : `block ${formatCell(row?.block_number)}`}
        </span>
      </footer>
    );
  }

  return (
    <footer className="notification-meta">
      <span>
        {status === "pending"
          ? `age ${formatCell(row?.age)}`
          : `block ${formatCell(row?.block_number)}`}
      </span>
    </footer>
  );
}

export function TransferNotificationsPanel({
  rows,
  otherRows,
  onClearTransfers,
  onClearOther,
  onClearAll,
  onRemoveOne,
  onRemoveOtherOne,
  onAddressClick,
  labelFor,
  minAmount,
  onMinAmountChange,
}) {
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem(NOTIFICATIONS_OPEN_KEY) !== "0";
    } catch {
      return true;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem(NOTIFICATIONS_OPEN_KEY, open ? "1" : "0");
    } catch {
      // ignore storage errors
    }
  }, [open]);

  const sortedRows = [...(rows || [])].sort(
    (a, b) =>
      Number(b?.updated_at ?? 0) - Number(a?.updated_at ?? 0) ||
      parseAgeToSeconds(b?.age) - parseAgeToSeconds(a?.age)
  );
  const sortedOtherRows = [...(otherRows || [])].sort(
    (a, b) =>
      Number(b?.updated_at ?? 0) - Number(a?.updated_at ?? 0) ||
      parseAgeToSeconds(b?.age) - parseAgeToSeconds(a?.age)
  );

  return (
    <section className={`panel notification-split-panel${open ? "" : " panel-collapsed"}`}>
      <div className="panel-title notification-panel-title">
        <button
          type="button"
          className="notification-title-toggle"
          onClick={() => setOpen((prev) => !prev)}
          aria-expanded={open}
        >
          <span className="panel-toggle-icon">{open ? "−" : "+"}</span>
          <span>Notifications</span>
        </button>
        <button
          type="button"
          className="notification-clear-btn"
          onClick={onClearAll}
          disabled={sortedRows.length === 0 && sortedOtherRows.length === 0}
        >
          Clear all
        </button>
      </div>

      {open ? (
      <div className="notification-split-columns">
        <div className="notification-column notification-column-other">
          <div className="notification-column-header">
            <span className="notification-column-title">Other</span>
            <button
              type="button"
              className="notification-clear-btn"
              onClick={onClearOther}
              disabled={sortedOtherRows.length === 0}
            >
              Clear
            </button>
          </div>
          <div className="notification-wrap">
            {sortedOtherRows.length === 0 ? (
              <div className="empty-notification">No other notifications</div>
            ) : (
              sortedOtherRows.map((row, idx) => {
                const status = parseNotificationStatus(row?.status);
                return (
                  <article
                    className={`notification-card ${otherCardClass(row)} status-${status}${trackedCardClass(labelFor, ...otherRowAddresses(row))}`}
                    key={row?.id || `oth-${idx}`}
                  >
                    <header className="notification-header">
                      <span className="notification-call mono">{displayCall(row?.call)}</span>
                      <div className="notification-actions">
                        <span className={`notification-badge status-${status}`}>{status}</span>
                        <button
                          type="button"
                          className="notification-remove-btn"
                          onClick={() => onRemoveOtherOne?.(row?.id)}
                        >
                          Remove
                        </button>
                      </div>
                    </header>
                    <OtherNotificationBody
                      row={row}
                      onAddressClick={onAddressClick}
                      labelFor={labelFor}
                    />
                    <OtherNotificationMeta row={row} />
                  </article>
                );
              })
            )}
          </div>
        </div>
        <div className="notification-column notification-column-transfers">
          <div className="notification-column-header">
            <span className="notification-column-title">Transfers</span>
            <div className="notification-filter-controls">
              <span className="notification-filter-label mono">{`min ${formatCell(minAmount)} TAO`}</span>
              <input
                type="number"
                min="0"
                step="1"
                className="notification-threshold-input"
                value={Number.isFinite(Number(minAmount)) ? Number(minAmount) : 0}
                onChange={(e) => {
                  const n = Number.parseFloat(e.target.value);
                  onMinAmountChange?.(Number.isFinite(n) && n >= 0 ? n : 0);
                }}
              />
            </div>
            <button
              type="button"
              className="notification-clear-btn"
              onClick={onClearTransfers}
              disabled={sortedRows.length === 0}
            >
              Clear
            </button>
          </div>
          <div className="notification-wrap notification-wrap-transfers">
            {sortedRows.length === 0 ? (
              <div className="empty-notification">No transfer notifications</div>
            ) : (
              sortedRows.map((row, idx) => {
                const signer =
                  row?.real_address && row.real_address !== "-" ? row.real_address : row?.signer;
                const status = parseNotificationStatus(row?.status);
                return (
                  <article
                    className={`notification-card notification-card-transfer status-${status}${trackedCardClass(labelFor, signer, row?.to)}`}
                    key={row?.id || `tn-${idx}`}
                  >
                    <header className="notification-header">
                      <span className="notification-call mono">{displayTransferCall(row?.call)}</span>
                      <div className="notification-actions">
                        <span className={`notification-badge status-${status}`}>{status}</span>
                        <button
                          type="button"
                          className="notification-remove-btn"
                          onClick={() => onRemoveOne?.(row?.id)}
                        >
                          Remove
                        </button>
                      </div>
                    </header>
                    <div className="notification-transfer-body mono">
                      <div className="notification-transfer-line">
                        <span className="notification-detail-label">from </span>
                        <NotificationAddressButton
                          address={signer}
                          labelFor={labelFor}
                          onAddressClick={onAddressClick}
                          fullAddress
                        />
                      </div>
                      <div className="notification-transfer-line">
                        <span className="notification-detail-label">to </span>
                        <NotificationAddressButton
                          address={row?.to}
                          labelFor={labelFor}
                          onAddressClick={onAddressClick}
                          fullAddress
                        />
                      </div>
                    </div>
                    <footer className="notification-meta">
                      <span>amount {formatTransferAmount(row?.amount)}</span>
                      <span>
                        {status === "pending"
                          ? `age ${formatCell(row?.age)}`
                          : `block ${formatCell(row?.block_number)}`}
                      </span>
                    </footer>
                  </article>
                );
              })
            )}
          </div>
        </div>

        
      </div>
      ) : null}
    </section>
  );
}
