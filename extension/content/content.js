const API_BASE_URL = "http://127.0.0.1:5050";
const HOVER_DELAY_MS = 200;
const VIEWPORT_MARGIN = 10;
const TOOLTIP_GAP = 10;

const POINT_ANCHOR_SIZE = 14;
const ACTIVE_REGION_PADDING = 6;
const BRIDGE_BAND_PADDING = 4;

const state = {
  activeAnchorEl: null,
  activeAnchorPoint: null,
  activeResult: null,
  hoverTimer: null,
  isTooltipHovered: false,
  isPinned: false,
  pinnedKey: null,

  currentHoverAnchor: null,
  pendingAnchor: null,
  hoverRequestId: 0,

  pointerX: null,
  pointerY: null
};

let tooltipEl = null;
const urlMap = Object.create(null);

init();

async function init() {
  createTooltip();
  await syncPinnedState();

  document.addEventListener("mousemove", handleDocumentMouseMove, true);
  document.addEventListener("mouseover", handleDocumentMouseOver, true);
  document.addEventListener("mouseout", handleDocumentMouseOut, true);

  window.addEventListener(
    "scroll",
    () => {
      if (isTooltipVisible() && state.activeAnchorPoint) {
        positionTooltip(state.activeAnchorPoint);
      }
    },
    true
  );

  window.addEventListener("resize", () => {
    if (isTooltipVisible() && state.activeAnchorPoint) {
      positionTooltip(state.activeAnchorPoint);
    }
  });

  window.addEventListener("load", async () => {
    await preloadPageUrls();
  });

  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local") return;

    if (changes.pinnedItem) {
      const newValue = changes.pinnedItem.newValue || null;
      state.pinnedKey = buildItemKey(newValue);
      state.isPinned = state.activeResult
        ? state.pinnedKey === buildItemKey(state.activeResult)
        : false;

      updatePinButtonLabel();
    }
  });
}

async function syncPinnedState() {
  try {
    const { pinnedItem } = await chrome.storage.local.get(["pinnedItem"]);
    state.pinnedKey = buildItemKey(pinnedItem || null);
  } catch (error) {
    console.error("syncPinnedState failed:", error);
  }
}

function createTooltip() {
  tooltipEl = document.createElement("div");
  tooltipEl.id = "url-risk-hover-tooltip";
  tooltipEl.innerHTML = `
    <div class="urih-card urih-card-compact">
      <div class="urih-header">
        <div class="urih-title-group">
          <div class="urih-title">URL 風險檢視</div>
        </div>

        <div class="urih-header-actions">
          <button
            class="urih-icon-btn"
            data-role="pin-btn"
            title="釘選"
            aria-label="釘選"
          >📌</button>

          <button
            class="urih-icon-btn"
            data-role="close-btn"
            title="關閉"
            aria-label="關閉"
          >×</button>
        </div>
      </div>

      <div class="urih-section urih-section-tight">
        <div class="urih-label">URL</div>
        <div class="urih-url" data-role="url-text"></div>
      </div>

      <div class="urih-section urih-section-tight">
        <div class="urih-label">判定結果</div>
        <div class="urih-inline-result">
          <div class="urih-badge" data-role="risk-badge">未知</div>
        </div>
      </div>

      <div class="urih-section urih-section-tight">
        <div class="urih-label">簡短原因</div>
        <div class="urih-reason" data-role="short-reason"></div>
      </div>
    </div>
  `;

  tooltipEl.addEventListener("mouseenter", () => {
    state.isTooltipHovered = true;
  });

  tooltipEl.addEventListener("mouseleave", () => {
    state.isTooltipHovered = false;
    queueCloseCheck();
  });

  tooltipEl.querySelector('[data-role="close-btn"]').addEventListener("click", () => {
    closeTooltip(true);
  });

  tooltipEl.querySelector('[data-role="pin-btn"]').addEventListener("click", async () => {
    if (!state.activeResult) return;

    try {
      const { pinnedItem } = await chrome.storage.local.get(["pinnedItem"]);
      const currentKey = buildItemKey(state.activeResult);
      const pinnedKey = buildItemKey(pinnedItem || null);

      if (currentKey && currentKey === pinnedKey) {
        await chrome.storage.local.remove(["pinnedItem"]);
        state.isPinned = false;
        state.pinnedKey = null;
      } else {
        await chrome.storage.local.set({ pinnedItem: state.activeResult });
        state.isPinned = true;
        state.pinnedKey = currentKey;
      }

      updatePinButtonLabel();
    } catch (error) {
      console.error("toggle pin failed:", error);
    }
  });

  document.documentElement.appendChild(tooltipEl);
}

function handleDocumentMouseMove(event) {
  state.pointerX = event.clientX;
  state.pointerY = event.clientY;

  if (!isTooltipVisible()) return;
  if (state.isPinned) return;

  if (state.currentHoverAnchor) return;
  if (state.pendingAnchor) return;
  if (isPointerInActiveRegion()) return;

  closeTooltip();
}

async function preloadPageUrls() {
  try {
    const urls = Array.from(document.querySelectorAll("a[href]"))
      .map((a) => a.href)
      .filter((href) => isSupportedHref(href));

    const uniqueUrls = [...new Set(urls)];
    if (uniqueUrls.length === 0) return;

    const resultMap = await fetchBatchStatus(uniqueUrls);

    for (const url of uniqueUrls) {
      const rawValue = Object.prototype.hasOwnProperty.call(resultMap, url)
        ? resultMap[url]
        : undefined;
      urlMap[url] = mapBackendValueToResult(url, rawValue);
    }
  } catch (error) {
    console.error("preloadPageUrls failed:", error);
  }
}

function handleDocumentMouseOver(event) {
  const anchor = findAnchor(event.target);
  const fromAnchor = findAnchor(event.relatedTarget);

  if (!anchor || anchor === fromAnchor) return;
  if (!isSupportedLink(anchor)) return;

  state.currentHoverAnchor = anchor;

  if (state.isPinned) return;

  scheduleHover(anchor);
}

function handleDocumentMouseOut(event) {
  const anchor = findAnchor(event.target);
  const toAnchor = findAnchor(event.relatedTarget);

  if (!anchor || anchor === toAnchor) return;

  if (state.currentHoverAnchor === anchor) {
    state.currentHoverAnchor = null;
  }

  if (state.pendingAnchor === anchor) {
    clearHoverTimer();
  }

  if (anchor !== state.activeAnchorEl) return;

  if (toAnchor && isSupportedLink(toAnchor)) {
    return;
  }

  if (tooltipEl && event.relatedTarget && tooltipEl.contains(event.relatedTarget)) {
    return;
  }

  queueCloseCheck();
}

function scheduleHover(anchor) {
  clearHoverTimer();

  if (!anchor) return;
  if (!isSupportedLink(anchor)) return;
  if (state.isPinned) return;

  state.pendingAnchor = anchor;
  const requestId = ++state.hoverRequestId;

  state.hoverTimer = setTimeout(async () => {
    state.hoverTimer = null;

    if (state.currentHoverAnchor !== anchor) {
      state.pendingAnchor = null;
      return;
    }

    if (isPointerInTooltip()) {
      state.pendingAnchor = null;
      return;
    }

    if (!anchor.isConnected) {
      state.pendingAnchor = null;
      return;
    }

    const anchorPoint = getCurrentPointerPoint(anchor);

    const result = await getUrlAnalysis(anchor.href);

    if (requestId !== state.hoverRequestId) {
      state.pendingAnchor = null;
      return;
    }

    if (state.currentHoverAnchor !== anchor) {
      state.pendingAnchor = null;
      return;
    }

    if (isPointerInTooltip()) {
      state.pendingAnchor = null;
      return;
    }

    if (!anchor.isConnected) {
      state.pendingAnchor = null;
      return;
    }

    state.activeAnchorEl = anchor;
    state.activeAnchorPoint = anchorPoint;
    state.activeResult = result;
    state.isPinned = state.pinnedKey === buildItemKey(state.activeResult);

    populateTooltip(state.activeResult);
    showTooltip();
    positionTooltip(anchorPoint);

    state.pendingAnchor = null;
  }, HOVER_DELAY_MS);
}

function getCurrentPointerPoint(anchor) {
  if (state.pointerX != null && state.pointerY != null) {
    return {
      x: state.pointerX,
      y: state.pointerY
    };
  }

  const rect = anchor.getBoundingClientRect();
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2
  };
}

async function getUrlAnalysis(url) {
  if (!url || !isSupportedHref(url)) return buildOfflineFallback(url);

  if (Object.prototype.hasOwnProperty.call(urlMap, url)) {
    return urlMap[url];
  }

  try {
    const resultMap = await fetchBatchStatus([url]);
    const rawValue = Object.prototype.hasOwnProperty.call(resultMap, url)
      ? resultMap[url]
      : undefined;

    const mapped = mapBackendValueToResult(url, rawValue);
    urlMap[url] = mapped;
    return mapped;
  } catch (error) {
    console.error("getUrlAnalysis failed:", error);
    const fallback = buildOfflineFallback(url);
    urlMap[url] = fallback;
    return fallback;
  }
}

async function fetchBatchStatus(urls) {
  const response = await fetch(`${API_BASE_URL}/check_urls_batch`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ urls })
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }

  return await response.json();
}

function mapBackendValueToResult(url, rawValue) {
  if (typeof rawValue === "boolean") {
    if (rawValue === true) {
      return {
        requested_url: url,
        normalized_url: url,
        data_status: "recorded",
        data_status_label: "已標記",
        risk_level: 1,
        risk_label: "可信",
        short_reason: "此 URL 的標記結果為 Real。"
      };
    }

    return {
      requested_url: url,
      normalized_url: url,
      data_status: "recorded",
      data_status_label: "已標記",
      risk_level: 5,
      risk_label: "高風險",
      short_reason: "此 URL 的標記結果為 Fake。"
    };
  }

  if (typeof rawValue === "string") {
    return mapLabelToResult(url, rawValue);
  }

  return {
    requested_url: url,
    normalized_url: url,
    data_status: "not_recorded",
    data_status_label: "未收錄",
    risk_level: null,
    risk_label: "未收錄",
    short_reason: "此 URL 目前未收錄於資料庫。"
  };
}

function mapLabelToResult(url, rawLabel) {
  const label = String(rawLabel || "").trim();
  const normalized = label.toLowerCase();

  if (["real", "safe", "trusted"].includes(normalized)) {
    return {
      requested_url: url,
      normalized_url: url,
      data_status: "recorded",
      data_status_label: "已標記",
      risk_level: 1,
      risk_label: "可信",
      short_reason: `此 URL 的標記結果為 ${label}。`
    };
  }

  if (["mostly_real", "low_risk", "low-risk"].includes(normalized)) {
    return {
      requested_url: url,
      normalized_url: url,
      data_status: "recorded",
      data_status_label: "已標記",
      risk_level: 2,
      risk_label: "低風險",
      short_reason: `此 URL 的標記結果為 ${label}，目前判定為低風險。`
    };
  }

  if (["unknown", "uncertain"].includes(normalized)) {
    return {
      requested_url: url,
      normalized_url: url,
      data_status: "recorded",
      data_status_label: "已標記",
      risk_level: 3,
      risk_label: "未知",
      short_reason: `此 URL 的標記結果為 ${label}。`
    };
  }

  if (["suspicious"].includes(normalized)) {
    return {
      requested_url: url,
      normalized_url: url,
      data_status: "recorded",
      data_status_label: "已標記",
      risk_level: 4,
      risk_label: "可疑",
      short_reason: `此 URL 的標記結果為 ${label}，建議提高警覺。`
    };
  }

  if (["fake", "danger", "malicious"].includes(normalized)) {
    return {
      requested_url: url,
      normalized_url: url,
      data_status: "recorded",
      data_status_label: "已標記",
      risk_level: 5,
      risk_label: "高風險",
      short_reason: `此 URL 的標記結果為 ${label}，建議避免互動。`
    };
  }

  return {
    requested_url: url,
    normalized_url: url,
    data_status: "recorded",
    data_status_label: "已標記",
    risk_level: 3,
    risk_label: label || "未知",
    short_reason: `此 URL 的標記結果為 ${label || "未知"}。`
  };
}

function buildOfflineFallback(url) {
  return {
    requested_url: url,
    normalized_url: url,
    data_status: "service_unavailable",
    data_status_label: "服務無法連線",
    risk_level: 3,
    risk_label: "未知",
    short_reason: "目前無法連線至後端。"
  };
}

function clearHoverTimer() {
  clearTimeout(state.hoverTimer);
  state.hoverTimer = null;
  state.pendingAnchor = null;
}

function populateTooltip(result) {
  const isNotRecorded =
    result.data_status === "not_recorded" ||
    result.data_status_label === "未收錄";

  const displayRiskLabel = isNotRecorded
    ? "未收錄"
    : (result.risk_label || "未知");

  tooltipEl.querySelector('[data-role="url-text"]').textContent =
    result.requested_url || result.normalized_url || "";

  tooltipEl.querySelector('[data-role="short-reason"]').textContent =
    result.short_reason || "無";

  const badge = tooltipEl.querySelector('[data-role="risk-badge"]');
  badge.textContent = displayRiskLabel;
  badge.className = `urih-badge ${riskClassNameForDisplay(result, displayRiskLabel)}`;

  updatePinButtonLabel();
}

function updatePinButtonLabel() {
  const pinBtn = tooltipEl.querySelector('[data-role="pin-btn"]');
  pinBtn.textContent = state.isPinned ? "📍" : "📌";
  pinBtn.title = state.isPinned ? "取消釘選" : "釘選";
  pinBtn.setAttribute("aria-label", state.isPinned ? "取消釘選" : "釘選");
  pinBtn.classList.toggle("is-pinned", state.isPinned);
}

function riskClassName(level) {
  switch (Number(level)) {
    case 1:
      return "risk-safe";
    case 2:
      return "risk-low";
    case 3:
      return "risk-unknown";
    case 4:
      return "risk-suspicious";
    case 5:
      return "risk-danger";
    default:
      return "risk-unknown";
  }
}

function riskClassNameForDisplay(result, displayRiskLabel) {
  if (displayRiskLabel === "未收錄") {
    return "risk-not-recorded";
  }

  return riskClassName(result.risk_level);
}

function showTooltip() {
  tooltipEl.classList.add("visible");
}

function closeTooltip(force = false) {
  if (!force && (state.isTooltipHovered || state.isPinned)) {
    return;
  }

  tooltipEl.classList.remove("visible");
  state.activeAnchorEl = null;
  state.activeAnchorPoint = null;
  state.activeResult = null;
}

function queueCloseCheck() {
  requestAnimationFrame(() => {
    if (!isTooltipVisible()) return;
    if (state.isPinned) return;
    if (state.currentHoverAnchor) return;
    if (state.pendingAnchor) return;
    if (isPointerInActiveRegion()) return;

    closeTooltip();
  });
}

function isPointerInTooltip() {
  if (!tooltipEl || !isTooltipVisible()) return false;
  if (state.pointerX == null || state.pointerY == null) return false;

  const rect = tooltipEl.getBoundingClientRect();
  return isPointInRect(state.pointerX, state.pointerY, rect, ACTIVE_REGION_PADDING);
}

function isPointerInActiveRegion() {
  if (!tooltipEl || !isTooltipVisible()) return false;
  if (!state.activeAnchorPoint) return false;
  if (state.pointerX == null || state.pointerY == null) return false;

  const anchorRect = getPointAnchorRect(state.activeAnchorPoint);
  const tooltipRect = tooltipEl.getBoundingClientRect();
  const bridgeRect = getBridgeRect(anchorRect, tooltipRect);

  return (
    isPointInRect(state.pointerX, state.pointerY, anchorRect, ACTIVE_REGION_PADDING) ||
    isPointInRect(state.pointerX, state.pointerY, tooltipRect, ACTIVE_REGION_PADDING) ||
    isPointInRect(state.pointerX, state.pointerY, bridgeRect, 0)
  );
}

function getPointAnchorRect(point) {
  const half = POINT_ANCHOR_SIZE / 2;
  return {
    left: point.x - half,
    right: point.x + half,
    top: point.y - half,
    bottom: point.y + half,
    width: POINT_ANCHOR_SIZE,
    height: POINT_ANCHOR_SIZE
  };
}

function isPointInRect(x, y, rect, padding = 0) {
  if (!rect) return false;
  return (
    x >= rect.left - padding &&
    x <= rect.right + padding &&
    y >= rect.top - padding &&
    y <= rect.bottom + padding
  );
}

function getBridgeRect(anchorRect, tooltipRect) {
  if (tooltipRect.left >= anchorRect.right) {
    const midY = (Math.max(anchorRect.top, tooltipRect.top) + Math.min(anchorRect.bottom, tooltipRect.bottom)) / 2;
    return {
      left: anchorRect.right - 1,
      right: tooltipRect.left + 1,
      top: midY - BRIDGE_BAND_PADDING,
      bottom: midY + BRIDGE_BAND_PADDING
    };
  }

  if (anchorRect.left >= tooltipRect.right) {
    const midY = (Math.max(anchorRect.top, tooltipRect.top) + Math.min(anchorRect.bottom, tooltipRect.bottom)) / 2;
    return {
      left: tooltipRect.right - 1,
      right: anchorRect.left + 1,
      top: midY - BRIDGE_BAND_PADDING,
      bottom: midY + BRIDGE_BAND_PADDING
    };
  }

  if (tooltipRect.top >= anchorRect.bottom) {
    const midX = (Math.max(anchorRect.left, tooltipRect.left) + Math.min(anchorRect.right, tooltipRect.right)) / 2;
    return {
      left: midX - BRIDGE_BAND_PADDING,
      right: midX + BRIDGE_BAND_PADDING,
      top: anchorRect.bottom - 1,
      bottom: tooltipRect.top + 1
    };
  }

  if (anchorRect.top >= tooltipRect.bottom) {
    const midX = (Math.max(anchorRect.left, tooltipRect.left) + Math.min(anchorRect.right, tooltipRect.right)) / 2;
    return {
      left: midX - BRIDGE_BAND_PADDING,
      right: midX + BRIDGE_BAND_PADDING,
      top: tooltipRect.bottom - 1,
      bottom: anchorRect.top + 1
    };
  }

  return {
    left: Math.min(anchorRect.left, tooltipRect.left),
    right: Math.max(anchorRect.right, tooltipRect.right),
    top: Math.min(anchorRect.top, tooltipRect.top),
    bottom: Math.max(anchorRect.bottom, tooltipRect.bottom)
  };
}

function positionTooltip(anchorPoint) {
  if (!anchorPoint || !tooltipEl) return;

  const anchorRect = getPointAnchorRect(anchorPoint);

  tooltipEl.style.left = "-9999px";
  tooltipEl.style.top = "-9999px";
  tooltipEl.classList.add("visible");

  const tooltipRect = tooltipEl.getBoundingClientRect();
  const position = getBestPosition(anchorRect, tooltipRect);

  tooltipEl.style.left = `${position.left + window.scrollX}px`;
  tooltipEl.style.top = `${position.top + window.scrollY}px`;
}

function getBestPosition(anchorRect, tooltipRect) {
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;

  const candidates = [
    {
      left: anchorRect.right + TOOLTIP_GAP,
      top: anchorRect.bottom + TOOLTIP_GAP
    },
    {
      left: anchorRect.right + TOOLTIP_GAP,
      top: anchorRect.top - tooltipRect.height - TOOLTIP_GAP
    },
    {
      left: anchorRect.left - tooltipRect.width - TOOLTIP_GAP,
      top: anchorRect.bottom + TOOLTIP_GAP
    },
    {
      left: anchorRect.left - tooltipRect.width - TOOLTIP_GAP,
      top: anchorRect.top - tooltipRect.height - TOOLTIP_GAP
    }
  ];

  for (const candidate of candidates) {
    if (fitsViewport(candidate, tooltipRect, viewportWidth, viewportHeight)) {
      return candidate;
    }
  }

  return clampPosition(candidates[0], tooltipRect, viewportWidth, viewportHeight);
}

function fitsViewport(position, tooltipRect, viewportWidth, viewportHeight) {
  return (
    position.left >= VIEWPORT_MARGIN &&
    position.top >= VIEWPORT_MARGIN &&
    position.left + tooltipRect.width <= viewportWidth - VIEWPORT_MARGIN &&
    position.top + tooltipRect.height <= viewportHeight - VIEWPORT_MARGIN
  );
}

function clampPosition(position, tooltipRect, viewportWidth, viewportHeight) {
  return {
    left: Math.min(
      Math.max(position.left, VIEWPORT_MARGIN),
      viewportWidth - tooltipRect.width - VIEWPORT_MARGIN
    ),
    top: Math.min(
      Math.max(position.top, VIEWPORT_MARGIN),
      viewportHeight - tooltipRect.height - VIEWPORT_MARGIN
    )
  };
}

function isTooltipVisible() {
  return tooltipEl.classList.contains("visible");
}

function findAnchor(node) {
  return node?.closest?.("a[href]") || null;
}

function isSupportedLink(anchor) {
  try {
    const url = new URL(anchor.href);
    return ["http:", "https:"].includes(url.protocol);
  } catch (_) {
    return false;
  }
}

function isSupportedHref(href) {
  try {
    const url = new URL(href);
    return ["http:", "https:"].includes(url.protocol);
  } catch (_) {
    return false;
  }
}

function buildItemKey(item) {
  if (!item) return null;
  return item.normalized_url || item.requested_url || item.url || null;
}