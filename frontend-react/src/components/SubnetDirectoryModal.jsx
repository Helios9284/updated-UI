import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

function formatCell(value) {
  if (value === null || value === undefined || value === "") return "-";
  return String(value);
}

function shortenAddress(value) {
  const text = formatCell(value);
  if (text === "-" || text.length <= 18) return text;
  return `${text.slice(0, 8)}..${text.slice(-6)}`;
}

function resolveSubnetsApiUrl(apiBase) {
  if (typeof window !== "undefined") {
    return new URL("/api/subnets", window.location.origin).href;
  }
  if (apiBase) {
    return `${String(apiBase).replace(/\/$/, "")}/api/subnets`;
  }
  return "/api/subnets";
}

export function SubnetDirectoryModal({ open, onClose, apiBase, blockNumber }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const loadedOnceRef = useRef(false);

  const loadSubnets = useCallback(async () => {
    const url = resolveSubnetsApiUrl(apiBase);
    if (!loadedOnceRef.current) setLoading(true);
    try {
      const res = await fetch(url);
      const json = await res.json();
      if (!res.ok) {
        setError(json?.error || `http ${res.status}`);
        return;
      }
      setData(json);
      loadedOnceRef.current = true;
      if (json?.error) {
        setError(String(json.error));
      } else if (!json?.subnets?.length) {
        setError("Subnet cache is empty — waiting for backend refresh…");
      } else {
        setError("");
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, [apiBase]);

  useEffect(() => {
    if (!open) {
      loadedOnceRef.current = false;
      return undefined;
    }
    if (!blockNumber) return undefined;
    loadSubnets();
  }, [open, blockNumber, loadSubnets]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event) => {
      if (event.key === "Escape") onClose?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const rows = useMemo(() => {
    const list = data?.subnets || [];
    const q = filter.trim().toLowerCase();
    if (!q) return list;
    return list.filter((row) => {
      const netuid = String(row?.netuid ?? "");
      const name = String(row?.subnet_name ?? "").toLowerCase();
      const owner = String(row?.owner_coldkey ?? "").toLowerCase();
      return netuid.includes(q) || name.includes(q) || owner.includes(q);
    });
  }, [data, filter]);

  if (!open) return null;

  const modal = (
    <div
      className="subnet-modal-overlay"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose?.();
      }}
    >
      <div className="subnet-modal" role="dialog" aria-modal="true" aria-labelledby="subnet-modal-title">
        <div className="subnet-modal-header">
          <div>
            <div id="subnet-modal-title" className="subnet-modal-title">Subnets</div>
            <div className="subnet-modal-meta mono">
              {formatCell(data?.count)} subnets
              {data?.block_number != null ? ` · block #${data.block_number}` : ""}
              {data?.fetch_ms != null ? ` · ${data.fetch_ms} ms` : ""}
            </div>
          </div>
          <button type="button" className="portfolio-btn" onClick={() => onClose?.()}>
            Close
          </button>
        </div>

        <div className="subnet-modal-toolbar">
          <input
            className="portfolio-input mono"
            placeholder="Filter netuid / name / owner"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
          />
          <button type="button" className="portfolio-btn" onClick={loadSubnets} disabled={loading}>
            Refresh
          </button>
        </div>

        {loading && !data?.subnets?.length ? (
          <div className="subnet-modal-msg mono">loading...</div>
        ) : null}
        {error ? <div className="subnet-modal-msg mono subnet-modal-error">{error}</div> : null}

        <div className="subnet-modal-table-wrap">
          <table>
            <thead>
              <tr>
                <th>uid</th>
                <th>name</th>
                <th>owner</th>
                <th>tao_in</th>
                <th>alpha_in</th>
                <th>price</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`sn-${row.netuid}`}>
                  <td>{formatCell(row.netuid)}</td>
                  <td>
                    <div className="subnet-name-cell">
                      {row.logo_url ? (
                        <img
                          className="subnet-logo"
                          src={row.logo_url}
                          alt=""
                          loading="lazy"
                          referrerPolicy="no-referrer"
                        />
                      ) : null}
                      <span title={formatCell(row.subnet_name)}>{formatCell(row.subnet_name)}</span>
                    </div>
                  </td>
                  <td title={formatCell(row.owner_coldkey)}>{shortenAddress(row.owner_coldkey)}</td>
                  <td>{formatCell(row.tao_in)}</td>
                  <td>{formatCell(row.alpha_in)}</td>
                  <td>{formatCell(row.price_tao)}</td>
                </tr>
              ))}
              {!loading && rows.length === 0 ? (
                <tr>
                  <td colSpan={6} className="mono">No subnets</td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
