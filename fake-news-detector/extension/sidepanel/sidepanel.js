const els = {
  emptyState: document.getElementById("emptyState"),
  detailCard: document.getElementById("detailCard"),
  loadingState: document.getElementById("loadingState"),
  badge: document.getElementById("badge"),
  dataStatus: document.getElementById("dataStatus"),
  analysisTime: document.getElementById("analysisTime"),
  domainText: document.getElementById("domainText"),
  matchInfo: document.getElementById("matchInfo"),
  urlText: document.getElementById("urlText"),
  titleText: document.getElementById("titleText"),
  suspiciousSentences: document.getElementById("suspiciousSentences"),
  sentenceCount: document.getElementById("sentenceCount"),
  articleContent: document.getElementById("articleContent"),
  contentMeta: document.getElementById("contentMeta"),
  refreshBtn: document.getElementById("refreshBtn")
};

let currentItem = null;

init();

// 側邊欄關閉（Chrome 按 X）時，通知 service worker 轉發清除高亮指令
window.addEventListener("pagehide", () => {
  chrome.runtime.sendMessage({ type: "SIDE_PANEL_CLOSED" }).catch(() => { });
});

async function init() {
  bindEvents();

  // 開啟側邊欄時，清除所有分頁的提示 Badge
  try {
    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) {
      if (tab.id != null) {
        await chrome.action.setBadgeText({ text: "", tabId: tab.id });
      }
    }
  } catch (e) {
    console.error("Clear badge failed:", e);
  }

  await loadCurrentItem();

  chrome.storage.onChanged.addListener(async (changes, areaName) => {
    if (areaName !== "local") return;

    if (changes.detailItem) {
      currentItem = changes.detailItem.newValue || null;
      await render();
    }
  });
}

function bindEvents() {
  els.refreshBtn?.addEventListener("click", refreshDetails);
}

async function refreshDetails() {
  if (!currentItem) return;

  const url = currentItem.requested_url || currentItem.normalized_url || currentItem.url;
  if (!url) return;

  // 加上淡出動畫，並保證最少 400ms 緩衝讓使用者感知到有重新載入
  els.detailCard?.classList.add("is-refreshing");

  setLoading(true);
  const [response] = await Promise.all([
    sendMessage({ type: "REFRESH_DETAILS", url }),
    new Promise(r => setTimeout(r, 400))
  ]);
  setLoading(false);

  els.detailCard?.classList.remove("is-refreshing");

  if (response.ok) {
    currentItem = response.data;
    await chrome.storage.local.set({ detailItem: currentItem });
    await render();
  } else {
    console.error("refresh details failed:", response.error);
  }
}

async function loadCurrentItem() {
  const { detailItem } = await chrome.storage.local.get(["detailItem"]);
  currentItem = detailItem || null;
  await render();
}

async function render() {
  if (!currentItem) {
    els.emptyState?.classList.remove("hidden");
    els.detailCard?.classList.add("hidden");
    return;
  }

  els.emptyState?.classList.add("hidden");
  els.detailCard?.classList.remove("hidden");

  // 每次載入新資料時平滑滾回頂端
  window.scrollTo({ top: 0, behavior: "smooth" });

  const isLoading = currentItem.data_status === "loading" || currentItem.fetch_source === "loading";
  setLoading(isLoading);

  const displayRiskLabel = getDisplayRiskLabel(currentItem);
  const riskText = currentItem.risk_level ? `${currentItem.risk_level} - ${displayRiskLabel}` : displayRiskLabel;
  setText(els.badge, riskText || "未知");
  if (els.badge) {
    els.badge.className = `badge ${riskClassNameForDisplay(currentItem, displayRiskLabel)}`;
  }

  setText(els.dataStatus, currentItem.data_status_label || currentItem.data_status || "未知");
  // 分析時間取代 fetchSource 位置，只顯示時間值本身
  const timeStr = formatTime(currentItem.analysis_time);
  if (els.analysisTime) {
    const hasTime = timeStr && timeStr !== "未知";
    els.analysisTime.textContent = hasTime ? timeStr : "";
    els.analysisTime.style.display = hasTime ? "" : "none";
  }
  setText(els.domainText, currentItem.domain || parseDomain(currentItem.normalized_url || currentItem.requested_url) || "未知");
  setText(els.matchInfo, formatMatchInfo(currentItem));
  setText(els.urlText, safeDecodeUrl(currentItem.requested_url || currentItem.normalized_url || currentItem.url || ""));
  setText(els.titleText, currentItem.title || "");

  const sentences = normalizeStringList(currentItem.selected_sentences, "|");
  const scores = normalizeNumberList(currentItem.selected_scores);
  renderSentenceList(els.suspiciousSentences, sentences, scores);
  setText(els.sentenceCount, String(sentences.length));

  // 優先使用模糊比對後的原文句段（matched_sentences），若無則 fallback 到 selected_sentences
  const matched = Array.isArray(currentItem.matched_sentences) ? currentItem.matched_sentences : [];

  const contentInfo = getContentInfo(currentItem);
  // renderArticleContent 接收 matched 物件陣列（含 start_index/end_index）
  // 或 fallback 用字串陣列 + 分數
  renderArticleContent(els.articleContent, contentInfo.text, matched.length ? matched : sentences, scores);
  setText(els.contentMeta, contentInfo.label);
}

function setLoading(isLoading) {
  els.loadingState?.classList.toggle("hidden", !isLoading);
  if (els.refreshBtn) {
    els.refreshBtn.disabled = Boolean(isLoading);
    els.refreshBtn.textContent = isLoading ? "讀取中…" : "重新載入";
  }
}

function getDisplayRiskLabel(item) {
  const isNotRecorded = item.data_status === "not_recorded" || item.data_status_label === "未收錄" || item.data_status_label === "未標記";
  if (isNotRecorded) return "未收錄";
  return item.risk_label || "未知";
}

function riskClassNameForDisplay(item, displayRiskLabel) {
  if (displayRiskLabel === "未收錄") return "risk-not-recorded";
  return riskClassName(item.risk_level);
}

function scoreToAbsoluteRank(score) {
  if (score == null || !Number.isFinite(score)) return 0; // unranked
  if (score > 10) return 1;   // 高度可疑 → 紅
  if (score >= 5) return 2;   // 中度可疑 → 橙
  return 3;                   // 低度可疑 → 黃
}

function renderSentenceList(container, sentences, scores) {
  if (!container) return;
  container.innerHTML = "";

  if (!sentences.length) {
    appendEmptyRow(container, "無");
    return;
  }

  sentences.forEach((sentence, index) => {
    const score = Number.isFinite(scores[index]) ? scores[index] : null;
    const absRank = scoreToAbsoluteRank(score);

    const li = document.createElement("li");
    li.className = `sentence-item${absRank > 0 ? ` rank-${absRank}` : ""}`.trim();
    li.setAttribute("role", "button");
    li.setAttribute("tabindex", "0");
    li.title = "點擊跳轉至原文對應句段";

    const rankIcon = document.createElement("span");
    rankIcon.className = "sentence-rank";
    // 用絕對分數區間顯示圖示
    if (absRank === 1) { rankIcon.textContent = "🔴"; rankIcon.title = "高度可疑"; }
    else if (absRank === 2) { rankIcon.textContent = "🟠"; rankIcon.title = "中度可疑"; }
    else if (absRank === 3) { rankIcon.textContent = "🟡"; rankIcon.title = "低度可疑"; }
    else { rankIcon.textContent = "—"; }

    const text = document.createElement("div");
    text.className = "sentence-text";
    text.textContent = sentence;

    const scoreBadge = document.createElement("span");
    scoreBadge.className = "score-badge";
    scoreBadge.textContent = score == null ? "—" : formatScore(score);

    const hoverHint = document.createElement("span");
    hoverHint.className = "sentence-jump-hint";
    hoverHint.textContent = "▼ 對應原文";

    li.append(rankIcon, text, scoreBadge, hoverHint);

    // 點擊跳轉到文章內容區對應的 mark（用索引對映，不用文字比對）
    li.addEventListener("click", () => scrollToMatchingMark(index));
    li.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") scrollToMatchingMark(index);
    });

    container.appendChild(li);
  });
}

function renderArticleContent(container, content, sentences, scores) {
  if (!container) return;
  container.innerHTML = "";

  const text = String(content || "").trim();
  if (!text) {
    const empty = document.createElement("div");
    empty.className = "empty-row";
    empty.textContent = "後端 detail 目前沒有回傳 content / cleaned_content。";
    container.appendChild(empty);
    return;
  }

  const ranges = buildHighlightRanges(text, sentences, scores);
  let cursor = 0;

  for (const range of ranges) {
    if (range.start > cursor) {
      container.appendChild(document.createTextNode(text.slice(cursor, range.start)));
    }

    const mark = document.createElement("mark");
    if (range.rank <= 3) mark.className = `rank-${range.rank}`;
    if (range.score != null) mark.title = `score: ${formatScore(range.score)}`;
    // 加 data-orig-index 供點擊句段列表時索引對映
    if (range.origIndex != null) mark.dataset.origIndex = String(range.origIndex);
    mark.textContent = text.slice(range.start, range.end);
    container.appendChild(mark);

    cursor = range.end;
  }

  if (cursor < text.length) {
    container.appendChild(document.createTextNode(text.slice(cursor)));
  }
}

function buildHighlightRanges(content, sentencesOrMatched, scores) {
  const ranges = [];
  const seen = new Set();

  sentencesOrMatched.forEach((item, index) => {
    // item 可以是 matched_sentences 物件（含 start_index/end_index）或純字串
    const isObj = item !== null && typeof item === "object";
    const text = isObj ? (item.matched_text || item.matched || item.preprocessed || "") : String(item || "");
    const value = text.trim();
    if (!value || seen.has(value)) return;
    seen.add(value);

    // 與 renderSentenceList 相同的評分來源：selected_scores[index]
    // 絕對不可改用 span_similarity（不同量度，會導致顏色錯亂）
    const score = Number.isFinite(scores?.[index]) ? scores[index] : null;

    // 候選序列以 origIndex 記錄供排序用
    if (isObj && typeof item.start_index === "number" && typeof item.end_index === "number") {
      ranges.push({ start: item.start_index, end: item.end_index, score, origIndex: index });
      return;
    }

    // Fallback：在 content 中做 indexOf
    const start = content.indexOf(value);
    if (start < 0) return;
    ranges.push({ start, end: start + value.length, score, origIndex: index });
  });

  // 移除重疊（保留距離最近的一筆）
  const deduped = [];
  ranges.forEach(r => {
    const overlaps = deduped.some(d => r.start < d.end && r.end > d.start);
    if (!overlaps) deduped.push(r);
  });

  // 依絕對分數區間分配 rank，與 scoreToAbsoluteRank 完全一致
  deduped.forEach(r => { r.rank = scoreToAbsoluteRank(r.score); });

  // 再依位置排序供段落渲染
  deduped.sort((a, b) => a.start - b.start);
  return deduped;
}

function appendEmptyRow(container, text) {
  const li = document.createElement("li");
  li.className = "empty-row";
  li.textContent = text;
  container.appendChild(li);
}

function getContentInfo(item) {
  if (typeof item.content === "string" && item.content.trim()) {
    return {
      text: item.content,
      label: `${item.content.length.toLocaleString()} 字｜content`
    };
  }

  if (typeof item.cleaned_content === "string" && item.cleaned_content.trim()) {
    return {
      text: item.cleaned_content,
      label: `${item.cleaned_content.length.toLocaleString()} 字｜cleaned_content`
    };
  }

  return {
    text: "",
    label: "無內容"
  };
}

function normalizeStringList(value, delimiter = "|") {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  }

  if (typeof value === "string" && value.trim()) {
    return value
      .split(delimiter)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  return [];
}

function normalizeNumberList(value) {
  if (Array.isArray(value)) {
    return value.map(Number).map((n) => (Number.isFinite(n) ? n : null));
  }

  if (typeof value === "string" && value.trim()) {
    return value
      .split(",")
      .map((item) => Number.parseFloat(item.trim()))
      .map((n) => (Number.isFinite(n) ? n : null));
  }

  return [];
}


function riskClassName(level) {
  switch (Number(level)) {
    case 1: return "risk-safe";
    case 2: return "risk-low";
    case 3: return "risk-unknown";
    case 4: return "risk-suspicious";
    case 5: return "risk-danger";
    default: return "risk-unknown";
  }
}

function buildItemKey(item) {
  if (!item) return null;
  return item.normalized_url || item.requested_url || item.url || null;
}

function formatTime(value) {
  if (!value) return "未知";
  try {
    return new Date(value).toLocaleString();
  } catch (_) {
    return value;
  }
}

function formatScore(score) {
  if (!Number.isFinite(score)) return "—";
  if (score >= 0 && score <= 1) return score.toFixed(3);
  return String(Math.round(score * 1000) / 1000);
}

function formatFetchSource(value) {
  switch (value) {
    case "backend_detail": return "後端最新 detail";
    case "backend": return "後端資料";
    case "memory_cache": return "記憶體快取";
    case "offline_fallback": return "離線 fallback";
    case "loading": return "讀取中";
    default: return value ? String(value) : "來源未知";
  }
}

function formatMatchInfo(item) {
  const parts = [];
  if (item.match_scope) parts.push(`scope: ${item.match_scope}`);
  if (item.matched_by) parts.push(`by: ${item.matched_by}`);
  return parts.length ? parts.join("｜") : "未知";
}

function parseDomain(url) {
  try {
    return new URL(url).hostname;
  } catch (_) {
    return "";
  }
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

function setText(element, value) {
  if (!element) return;
  element.textContent = value == null || value === "" ? "無" : String(value);
}

function sendMessage(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        resolve({
          ok: false,
          error: chrome.runtime.lastError.message
        });
        return;
      }

      resolve(
        response || {
          ok: false,
          error: "Background 沒有回傳 response"
        }
      );
    });
  });
}

/**
 * 點擊句段列表時，透過 data-orig-index 索引找到對應 mark，
 * 再用 getBoundingClientRect + window.scrollTo 將其捲動至中央。
 */
function scrollToMatchingMark(origIndex) {
  const container = els.articleContent;
  if (!container) return;

  // 清除之前的強調狀態
  container.querySelectorAll("mark.active-mark").forEach(m => m.classList.remove("active-mark"));

  // 用索引直接找 mark，不依賴文字比對
  const targetMark = container.querySelector(`mark[data-orig-index="${origIndex}"]`);

  if (!targetMark) {
    // 找不到對應 mark（文章內容未完全比對）：閃爍提示
    container.classList.add("content-flash");
    setTimeout(() => container.classList.remove("content-flash"), 800);
    return;
  }

  targetMark.classList.add("active-mark");

  // getBoundingClientRect 取得元素相對視窗位置，
  // 加上 window.scrollY 得到絕對位置，再用 window.scrollTo 捲動置中
  const rect = targetMark.getBoundingClientRect();
  const absoluteTop = rect.top + window.scrollY;
  const centerOffset = (window.innerHeight / 2) - (rect.height / 2);
  window.scrollTo({ top: Math.max(0, absoluteTop - centerOffset), behavior: "smooth" });

  // 1.5 秒後移除標記
  setTimeout(() => targetMark.classList.remove("active-mark"), 1500);
}
