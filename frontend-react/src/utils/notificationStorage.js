const TRANSFER_KEY = "ultra-notifications:transfers";
const OTHER_KEY = "ultra-notifications:other";
const MIN_AMOUNT_KEY = "ultra-notifications:transfer-min-tao";
const MAX_ITEMS = 300;

function sanitizeList(value) {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === "object" && item.id);
}

export function loadTransferNotifications() {
  try {
    const raw = localStorage.getItem(TRANSFER_KEY);
    if (!raw) return [];
    return sanitizeList(JSON.parse(raw));
  } catch {
    return [];
  }
}

export function saveTransferNotifications(items) {
  try {
    const trimmed = sanitizeList(items).slice(0, MAX_ITEMS);
    localStorage.setItem(TRANSFER_KEY, JSON.stringify(trimmed));
    return trimmed;
  } catch {
    return items;
  }
}

export function loadOtherNotifications() {
  try {
    const raw = localStorage.getItem(OTHER_KEY);
    if (!raw) return [];
    return sanitizeList(JSON.parse(raw));
  } catch {
    return [];
  }
}

export function saveOtherNotifications(items) {
  try {
    const trimmed = sanitizeList(items).slice(0, MAX_ITEMS);
    localStorage.setItem(OTHER_KEY, JSON.stringify(trimmed));
    return trimmed;
  } catch {
    return items;
  }
}

export function loadTransferMinAmount(defaultValue = 50) {
  try {
    const raw = localStorage.getItem(MIN_AMOUNT_KEY);
    const parsed = Number.parseFloat(raw);
    if (raw != null && Number.isFinite(parsed) && parsed >= 0) {
      return parsed;
    }
  } catch {
    // ignore
  }
  return defaultValue;
}

export function saveTransferMinAmount(value) {
  try {
    const n = Number.parseFloat(value);
    if (Number.isFinite(n) && n >= 0) {
      localStorage.setItem(MIN_AMOUNT_KEY, String(n));
    }
  } catch {
    // ignore
  }
}

export function clearAllStoredNotifications() {
  try {
    localStorage.removeItem(TRANSFER_KEY);
    localStorage.removeItem(OTHER_KEY);
  } catch {
    // ignore
  }
}
