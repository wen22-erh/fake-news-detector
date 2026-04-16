const DEFAULT_API_BASE_URL = "http://localhost:5000";
const CACHE_TTL_MS = 5 * 60 * 1000;
const cache = new Map();

chrome.runtime.onInstalled.addListener(async () => {
  const stored = await chrome.storage.local.get(["apiBaseUrl"]);
  if (!stored.apiBaseUrl) {
    await chrome.storage.local.set({ apiBaseUrl: DEFAULT_API_BASE_URL });
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    switch (message.type) {
      case "GET_URL_ANALYSIS":
        sendResponse({
          ok: true,
          data: await getSummary(message.url)
        });
        return;

      case "TOGGLE_PIN": {
        const result = await togglePinned(message.item);
        sendResponse({ ok: true, data: result });
        return;
      }

      case "OPEN_SIDE_PANEL":
        // 目前先不接 side panel，避免前端報錯
        sendResponse({
          ok: false,
          error: "目前後端尚未提供詳細資料，暫不開啟側邊欄。"
        });
        return;

      case "CLEAR_SIDE_PANEL":
        sendResponse({ ok: true });
        return;

      case "GET_SETTINGS":
        sendResponse({ ok: true, data: await getSettings() });
        return;

      case "SAVE_SETTINGS":
        await chrome.storage.local.set({
          apiBaseUrl: (message.apiBaseUrl || DEFAULT_API_BASE_URL).trim()
        });
        clearMemoryCache();
        sendResponse({ ok: true });
        return;

      case "CLEAR_CACHE":
        clearMemoryCache();
        sendResponse({ ok: true });
        return;

      case "REFRESH_DETAILS":
        sendResponse({
          ok: false,
          error: "目前後端尚未提供詳細資料。"
        });
        return;

      default:
        sendResponse({ ok: false, error: "Unknown message type" });
    }
  })().catch((error) => {
    console.error(error);
    sendResponse({ ok: false, error: error.message || "Unexpected error" });
  });

  return true;
});

async function getSettings() {
  const { apiBaseUrl } = await chrome.storage.local.get(["apiBaseUrl"]);
  return { apiBaseUrl: apiBaseUrl || DEFAULT_API_BASE_URL };
}

function getCacheKey(kind, url) {
  return `${kind}:${String(url || "").trim()}`;
}

function readCache(kind, url) {
  const key = getCacheKey(kind, url);
  const entry = cache.get(key);

  if (!entry) return null;
  if (Date.now() > entry.expiresAt) {
    cache.delete(key);
    return null;
  }

  return entry.value;
}

function writeCache(kind, url, value) {
  cache.set(getCacheKey(kind, url), {
    value,
    expiresAt: Date.now() + CACHE_TTL_MS
  });
}

function clearMemoryCache() {
  cache.clear();
}

async function getSummary(url) {
  const cached = readCache("summary", url);
  if (cached) {
    return { ...cached, fetch_source: "memory_cache" };
  }

  try {
    const data = await fetchLabelForUrl(url);
    writeCache("summary", url, data);
    return { ...data, fetch_source: "backend" };
  } catch (error) {
    console.warn("Summary fetch failed:", error);
    const fallback = buildOfflineFallback(url, error.message);
    writeCache("summary", url, fallback);
    return { ...fallback, fetch_source: "offline_fallback" };
  }
}

async function fetchLabelForUrl(url) {
  const { apiBaseUrl } = await getSettings();
  const response = await fetch(`${apiBaseUrl}/check_urls_batch`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      urls: [url]
    })
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  const resultMap = await response.json();
  const rawLabel = resultMap ? resultMap[url] : null;

  // 若後端真的還是回 boolean，這裡也先做防呆
  if (typeof rawLabel === "boolean") {
    return mapBooleanResult(url, rawLabel);
  }

  return mapLabelResult(url, rawLabel);
}

function mapBooleanResult(url, exists) {
  if (!exists) {
    return {
      requested_url: url,
      normalized_url: url,
      data_status: "not_recorded",
      data_status_label: "未標記",
      risk_level: 3,
      risk_label: "未知",
      short_reason: "資料庫中沒有這個 URL 的標記結果。",
      raw_label: null,
      detail_available: false
    };
  }

  return {
    requested_url: url,
    normalized_url: url,
    data_status: "recorded",
    data_status_label: "已收錄",
    risk_level: 1,
    risk_label: "已收錄",
    short_reason: "此 URL 已存在於資料庫中，但目前沒有更細的標籤資訊。",
    raw_label: true,
    detail_available: false
  };
}

function mapLabelResult(url, rawLabel) {
  const label = typeof rawLabel === "string" ? rawLabel.trim() : "";

  if (!label) {
    return {
      requested_url: url,
      normalized_url: url,
      data_status: "not_recorded",
      data_status_label: "未標記",
      risk_level: 3,
      risk_label: "未知",
      short_reason: "資料庫中沒有這個 URL 的標記結果。",
      raw_label: null,
      detail_available: false
    };
  }

  const mapped = mapLabelToRisk(label);

  return {
    requested_url: url,
    normalized_url: url,
    data_status: "recorded",
    data_status_label: "已標記",
    risk_level: mapped.risk_level,
    risk_label: mapped.risk_label,
    short_reason: mapped.short_reason,
    raw_label: label,
    detail_available: false
  };
}

function mapLabelToRisk(label) {
  const normalized = String(label || "").trim().toLowerCase();

  switch (normalized) {
    case "real":
    case "safe":
    case "trusted":
      return {
        risk_level: 1,
        risk_label: "可信",
        short_reason: `此 URL 的標記結果為 ${label}。`
      };

    case "mostly_real":
    case "low_risk":
    case "low-risk":
      return {
        risk_level: 2,
        risk_label: "低風險",
        short_reason: `此 URL 的標記結果為 ${label}，目前判定為低風險。`
      };

    case "unknown":
    case "uncertain":
      return {
        risk_level: 3,
        risk_label: "未知",
        short_reason: `此 URL 的標記結果為 ${label}，目前缺少更完整分析。`
      };

    case "suspicious":
      return {
        risk_level: 4,
        risk_label: "可疑",
        short_reason: `此 URL 的標記結果為 ${label}，建議提高警覺。`
      };

    case "fake":
    case "danger":
    case "malicious":
      return {
        risk_level: 5,
        risk_label: "高風險",
        short_reason: `此 URL 的標記結果為 ${label}，建議避免互動。`
      };

    default:
      return {
        risk_level: 3,
        risk_label: label,
        short_reason: `此 URL 的標記結果為 ${label}，但前端尚未定義對應規則。`
      };
  }
}

async function togglePinned(item) {
  const { pinnedItem } = await chrome.storage.local.get(["pinnedItem"]);
  const currentKey = item?.normalized_url || item?.requested_url || item?.url;
  const pinnedKey = pinnedItem?.normalized_url || pinnedItem?.requested_url || pinnedItem?.url;

  if (currentKey && pinnedKey === currentKey) {
    await chrome.storage.local.remove(["pinnedItem"]);
    return { pinned: false, item: null };
  }

  await chrome.storage.local.set({ pinnedItem: item });
  return { pinned: true, item };
}

function buildOfflineFallback(url, errorMessage = "") {
  return {
    requested_url: url,
    normalized_url: url,
    data_status: "service_unavailable",
    data_status_label: "服務無法連線",
    risk_level: 3,
    risk_label: "未知",
    short_reason: errorMessage
      ? `目前無法連線至後端：${errorMessage}`
      : "目前無法連線至後端。",
    raw_label: null,
    detail_available: false
  };
}