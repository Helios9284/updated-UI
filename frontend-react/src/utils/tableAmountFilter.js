export function parseRowAmountTao(row) {
  const text = String(row?.amount_tao ?? row?.amount ?? "").trim();
  if (!text || text === "-") return null;
  const numeric = Number.parseFloat(text.replace(/[^0-9.\-]+/g, ""));
  return Number.isFinite(numeric) ? numeric : null;
}

export function rowMeetsMinAmount(row, minAmount, filterEnabled) {
  if (!filterEnabled) return true;
  const amount = parseRowAmountTao(row);
  if (amount === null) return true;
  const threshold = Number(minAmount);
  if (!Number.isFinite(threshold) || threshold < 0) return true;
  return amount >= threshold;
}

export function loadTableFilterPrefs(storageKeyPrefix, defaults = { minAmount: 0.1, enabled: true }) {
  const fallback = {
    minAmount: defaults.minAmount ?? 0.1,
    enabled: defaults.enabled ?? true,
  };
  try {
    const minRaw = localStorage.getItem(`${storageKeyPrefix}:min`);
    const enabledRaw = localStorage.getItem(`${storageKeyPrefix}:enabled`);
    const minParsed = Number.parseFloat(minRaw);
    return {
      minAmount:
        minRaw != null && Number.isFinite(minParsed) && minParsed >= 0
          ? minParsed
          : fallback.minAmount,
      enabled: enabledRaw != null ? enabledRaw === "1" : fallback.enabled,
    };
  } catch {
    return fallback;
  }
}

export function saveTableFilterPrefs(storageKeyPrefix, { minAmount, enabled }) {
  try {
    localStorage.setItem(`${storageKeyPrefix}:min`, String(minAmount));
    localStorage.setItem(`${storageKeyPrefix}:enabled`, enabled ? "1" : "0");
  } catch {
    // ignore storage failures
  }
}
