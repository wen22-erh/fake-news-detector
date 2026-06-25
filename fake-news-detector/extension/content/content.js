const HOVER_DELAY_MS = 200;
const VIEWPORT_MARGIN = 10;
const TOOLTIP_GAP = 0;

const POINT_ANCHOR_SIZE = 8;
const ACTIVE_REGION_PADDING = 0;
const BRIDGE_BAND_PADDING = 4;

const state = {
  activeAnchorEl: null,
  activeAnchorPoint: null,
  activeResult: null,
  hoverTimer: null,
  pollTimer: null,
  isTooltipHovered: false,
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
    await checkAndHighlightCurrentPage();
  });


  // 接收 Service Worker 背景輪詢完成的通知
  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === "ANALYSIS_UPDATED") {
      const { url, result } = message;

      // 更新本地 urlMap cache
      if (result.data_status !== "processing" && result.data_status !== "not_recorded") {
        urlMap[url] = result;

        // 若是當前頁面，立即嘗試上色
        if (url === window.location.href || url === normalizeUrlForHighlight(window.location.href)) {
          if (result.selected_sentences && result.selected_scores) {
            highlightSuspiciousSentences(result.selected_sentences, result.selected_scores);
          }
        }
      }

      // 若當前 tooltip 正顯示這個 URL，立即重新渲染
      if (state.activeResult && buildItemKey(state.activeResult) === url) {
        state.activeResult = result;
        populateTooltip(result);
      }
    }

    // 側邂欄關閉時清除連結高亮
    if (message.type === "SIDE_PANEL_CLOSED") {
      clearAnchorHighlight();
    }
  });
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
            data-role="detail-btn"
            title="詳細資訊"
            aria-label="詳細資訊"
          >📄</button>

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

  tooltipEl.querySelector('[data-role="detail-btn"]').addEventListener("click", async () => {
    // 著色來源連結，提醒使用者目前查看的是哪個連結
    highlightActiveAnchor(state.activeAnchorEl);
    await openNativeSidePanel(state.activeResult);
    closeTooltip(true);
  });


  document.documentElement.appendChild(tooltipEl);
}

function handleDocumentMouseMove(event) {
  state.pointerX = event.clientX;
  state.pointerY = event.clientY;

  if (!isTooltipVisible()) return;

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

    const response = await sendMessage({
      type: "GET_URL_ANALYSIS_BATCH",
      urls: uniqueUrls
    });

    if (!response.ok) {
      throw new Error(response.error || "Batch preload failed");
    }

    const resultMap = response.data || {};
    for (const url of uniqueUrls) {
      if (Object.prototype.hasOwnProperty.call(resultMap, url)) {
        urlMap[url] = resultMap[url];
      }
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

    populateTooltip(state.activeResult);
    showTooltip();
    positionTooltip(anchorPoint);

    state.pendingAnchor = null;
    // 注意：不再在 tooltip 層級啟動輪詢
    // 輪詢由 service-worker.js 的 chrome.alarms 在背景負責
    // 完成後透過 ANALYSIS_UPDATED 訊息通知此頁面
  }, HOVER_DELAY_MS);
}

// startPolling 已移除：輪詢改由 service-worker.js 的 chrome.alarms 在背景執行
// 分析完成後透過 ANALYSIS_UPDATED 訊息通知各分頁更新 tooltip


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
    const cachedResult = urlMap[url];
    if (cachedResult.data_status !== "processing" && cachedResult.data_status !== "not_recorded") {
      return cachedResult;
    }
  }

  const response = await sendMessage({
    type: "GET_URL_ANALYSIS",
    url
  });

  if (!response.ok) {
    console.error("getUrlAnalysis failed:", response.error);
    const fallback = buildOfflineFallback(url);
    urlMap[url] = fallback;
    return fallback;
  }

  const result = response.data || buildOfflineFallback(url);
  if (result.data_status !== "processing" && result.data_status !== "not_recorded") {
    urlMap[url] = result;
  }
  return result;
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
  clearTimeout(state.pollTimer);
  state.hoverTimer = null;
  state.pollTimer = null;
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
    safeDecodeUrl(result.requested_url || result.normalized_url || "");

  tooltipEl.querySelector('[data-role="short-reason"]').textContent =
    result.short_reason || "無";

  const badge = tooltipEl.querySelector('[data-role="risk-badge"]');
  badge.textContent = displayRiskLabel;
  badge.className = `urih-badge ${riskClassNameForDisplay(result, displayRiskLabel)}`;
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
  if (!force && state.isTooltipHovered) {
    return;
  }

  tooltipEl.classList.remove("visible");
  state.activeAnchorEl = null;
  state.activeAnchorPoint = null;
  state.activeResult = null;
  clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

function queueCloseCheck() {
  requestAnimationFrame(() => {
    if (!isTooltipVisible()) return;
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

function sendMessage(message) {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage(message, (response) => {
        try {
          if (chrome.runtime.lastError) {
            resolve({ ok: false, error: chrome.runtime.lastError.message || "" });
            return;
          }
          resolve(response || { ok: false, error: "Background 沒有回傳 response" });
        } catch (innerErr) {
          // callback 內部例外（Extension context invalidated 常發生在此）
          resolve({ ok: false, error: String(innerErr) });
        }
      });
    } catch (e) {
      // sendMessage 同步丟出例外
      resolve({ ok: false, error: String(e) });
    }
  });
}

// ── 原生 Chrome Side Panel 開啟 ─────────────────────────────

async function openNativeSidePanel(result) {
  const url = result?.requested_url || result?.normalized_url || result?.url;
  if (!url) return;

  const response = await sendMessage({
    type: "OPEN_SIDE_PANEL",
    url
  });

  if (!response.ok) {
    console.error("open native side panel failed:", response.error);
  }
}

function normalizeUrlForHighlight(url) {
  try {
    return encodeURI(decodeURI(url));
  } catch (_) {
    return url;
  }
}

async function checkAndHighlightCurrentPage() {
  try {
    const currentUrl = window.location.href;
    if (!isSupportedHref(currentUrl)) return;

    // 同時準備原始、編碼、解碼三種版本，依序嘗試直到拿到有效結果
    const urlVariants = [...new Set([
      currentUrl,
      encodeURI(decodeURI(currentUrl)),
      decodeURI(currentUrl)
    ])];

    let result = null;
    for (const url of urlVariants) {
      const response = await sendMessage({ type: "GET_URL_ANALYSIS", url });
      if (!response.ok) continue;
      const data = response.data;
      if (data && data.data_status !== "not_recorded" && data.data_status !== "service_unavailable") {
        result = data;
        break;
      }
    }

    if (result && result.selected_sentences && result.selected_scores) {
      highlightSuspiciousSentences(result.selected_sentences, result.selected_scores);
    }
  } catch (error) {
    console.error("checkAndHighlightCurrentPage failed:", error);
  }
}

function highlightSuspiciousSentences(sentences, scores) {
  if (!sentences || !scores || sentences.length === 0 || sentences.length !== scores.length) return;

  const combined = sentences.map((sentence, index) => ({
    sentence,
    score: scores[index]
  }));

  combined.sort((a, b) => b.score - a.score);

  const colors = ["#ff9999", "#ffcc99", "#ffff99"]; // 比較高=紅色 次高=橘色 第三高=黃色

  combined.forEach((item, index) => {
    if (index < 3) {
      highlightTextInDOM(item.sentence, colors[index]);
    }
  });
}

function highlightTextInDOM(searchText, color) {
  if (!searchText || searchText.trim() === "") return;

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
  const nodesToModify = [];

  let node;
  while ((node = walker.nextNode())) {
    const parent = node.parentElement;
    if (parent && ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEXTAREA'].includes(parent.tagName)) continue;

    if (node.nodeValue.includes(searchText)) {
      nodesToModify.push(node);
    }
  }

  for (const n of nodesToModify) {
    const parent = n.parentNode;
    if (!parent) continue;

    const parts = n.nodeValue.split(searchText);
    if (parts.length === 1) continue;

    const fragment = document.createDocumentFragment();
    for (let i = 0; i < parts.length; i++) {
      fragment.appendChild(document.createTextNode(parts[i]));
      if (i < parts.length - 1) {
        const mark = document.createElement("mark");
        mark.style.backgroundColor = color;
        mark.style.color = "inherit";
        mark.textContent = searchText;
        fragment.appendChild(mark);
      }
    }
    parent.replaceChild(fragment, n);
  }
}

/**
 * 點擊詳細資訊時，在來源 <a> 元素上渲染高亮邊框，
 * 提醒使用者目前側邊欄顯示的是哪一個連結的詳細資訊。
 * 不自動移除，只在點擊新 URL 時才清除舊的高亮。
 *
 * 使用注入 <style> + !important 確保覆蓋網站自身的 CSS，
 * 解決 inline style 被 a { outline: none } 覆蓋的問題。
 */
function ensureHighlightStyle() {
  if (document.getElementById("urih-highlight-style")) return;
  const style = document.createElement("style");
  style.id = "urih-highlight-style";
  style.textContent = `
    a[data-urih-active] {
      /* inset box-shadow 畫在元素內部，不受父層 overflow:hidden 裁切 */
      box-shadow: inset 0 0 0 2px #6366f1 !important;
      border-radius: 4px !important;
    }
  `;
  (document.head || document.documentElement).appendChild(style);
}

function clearAnchorHighlight() {
  document.querySelectorAll("[data-urih-active]").forEach(el => {
    el.removeAttribute("data-urih-active");
  });
}

function highlightActiveAnchor(anchorEl) {
  if (!anchorEl) return;

  ensureHighlightStyle();

  // 清除上一個高亮，再設定新的
  clearAnchorHighlight();

  // 設定新高亮
  anchorEl.setAttribute("data-urih-active", "1");
}

function safeDecodeUrl(url) {
  if (!url) return url;
  try {
    return decodeURIComponent(url);
  } catch (_) {
    try {
      return decodeURI(url);
    } catch (_) {
      return url;
    }
  }
}