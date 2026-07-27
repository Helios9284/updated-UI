import { useMemo, useState } from "react";

function formatAddressShort(value) {
  const text = String(value || "-");
  if (text === "-" || text.length <= 12) return text;
  return `${text.slice(0, 5)}...${text.slice(-5)}`;
}

export function AddressBookPanel({
  entries,
  loading,
  error,
  onSave,
  onDelete,
  onSelectAddress,
}) {
  const [newAddress, setNewAddress] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [query, setQuery] = useState("");
  const [drafts, setDrafts] = useState({});

  const rows = useMemo(() => {
    const arr = Object.entries(entries || {}).map(([address, label]) => ({ address, label }));
    arr.sort(
      (a, b) => (a.label || "").localeCompare(b.label || "") || a.address.localeCompare(b.address)
    );
    const q = query.trim().toLowerCase();
    if (!q) return arr;
    return arr.filter((row) => {
      const addr = String(row.address || "").toLowerCase();
      const lbl = String(row.label || "").toLowerCase();
      return addr.includes(q) || lbl.includes(q);
    });
  }, [entries, query]);

  return (
    <section className="panel address-book-panel">
      <div className="panel-title">Address Book</div>
      <div className="address-book-controls">
        <input
          className="portfolio-input mono"
          type="text"
          placeholder="address"
          value={newAddress}
          onChange={(e) => setNewAddress(e.target.value)}
        />
        <input
          className="portfolio-input"
          type="text"
          placeholder="label"
          value={newLabel}
          onChange={(e) => setNewLabel(e.target.value)}
        />
        <button
          type="button"
          className="portfolio-btn"
          onClick={() => {
            const address = newAddress.trim();
            if (!address) return;
            onSave?.(address, newLabel.trim());
            setNewAddress("");
            setNewLabel("");
          }}
        >
          Add
        </button>
      </div>
      <div className="address-book-filter">
        <input
          className="portfolio-input"
          type="text"
          placeholder="search label/address"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {loading ? <div className="portfolio-msg mono">loading...</div> : null}
      {error ? <div className="auto-unstake-error mono">{error}</div> : null}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>address</th>
              <th>label</th>
              <th>action</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const draft = Object.prototype.hasOwnProperty.call(drafts, row.address)
                ? drafts[row.address]
                : row.label;
              return (
                <tr
                  key={row.address}
                  onClick={() => onSelectAddress?.(row.address)}
                  style={{ cursor: "pointer" }}
                >
                  <td className="mono" title={row.address}>
                    {formatAddressShort(row.address)}
                  </td>
                  <td>
                    <input
                      className="portfolio-input"
                      type="text"
                      value={draft}
                      onClick={(e) => e.stopPropagation()}
                      onChange={(e) =>
                        setDrafts((prev) => ({
                          ...prev,
                          [row.address]: e.target.value,
                        }))
                      }
                    />
                  </td>
                  <td>
                    <div className="address-book-actions">
                      <button
                        type="button"
                        className="portfolio-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          onSave?.(row.address, String(draft || "").trim());
                        }}
                      >
                        Save
                      </button>
                      <button
                        type="button"
                        className="portfolio-btn address-book-delete-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          onDelete?.(row.address);
                        }}
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
