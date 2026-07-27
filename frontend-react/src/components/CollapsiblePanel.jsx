import { useEffect, useState } from "react";

export function CollapsiblePanel({ title, storageKey, defaultOpen = true, className = "", children }) {
  const [open, setOpen] = useState(() => {
    if (!storageKey) return defaultOpen;
    try {
      const saved = localStorage.getItem(storageKey);
      if (saved === "0") return false;
      if (saved === "1") return true;
    } catch {
      // ignore storage errors
    }
    return defaultOpen;
  });

  useEffect(() => {
    if (!storageKey) return;
    try {
      localStorage.setItem(storageKey, open ? "1" : "0");
    } catch {
      // ignore storage errors
    }
  }, [open, storageKey]);

  return (
    <section className={`panel ${className} ${open ? "" : "panel-collapsed"}`.trim()}>
      <button
        type="button"
        className="panel-title panel-title-toggle"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
      >
        <span>{title}</span>
        <span className="panel-toggle-icon">{open ? "−" : "+"}</span>
      </button>
      {open ? children : null}
    </section>
  );
}
