const DEFAULT_API_BASE_URL = "http://120.105.129.171:5050";
const SIDE_PANEL_PATH = "sidepanel/sidepanel.html";
const CACHE_TTL_MS = 5 * 60 * 1000;
const POLL_ALARM_NAME = "pollPendingUrls";
const POLL_INTERVAL_MINUTES = 0.5; // 30秒（開發者模式有效）


// ── 初始化 ───────────────────────────────────────────────────────
chrome.runtime.onInstalled.addListener(async () => {
    const stored = await chrome.storage.local.get(["apiBaseUrl"]);
    if (!stored.apiBaseUrl) {
        await chrome.storage.local.set({ apiBaseUrl: DEFAULT_API_BASE_URL });
    }

    await chrome.storage.local.set({ pendingUrls: [] });
    await configureNativeSidePanel();
});

chrome.runtime.onStartup?.addListener(() => {
    configureNativeSidePanel().catch((error) => {
        console.error("configureNativeSidePanel onStartup failed:", error);
    });
});

// Service worker 被喚醒時也嘗試設定一次，避免更新後狀態不同步
configureNativeSidePanel().catch((error) => {
    console.error("configureNativeSidePanel failed:", error);
});

async function configureNativeSidePanel() {
    if (!chrome.sidePanel) return;

    await chrome.sidePanel.setOptions({
        path: SIDE_PANEL_PATH,
        enabled: true
    });

    // 讓工具列圖示也能開啟原生 Side Panel。
    // 注意：manifest 不能再設定 action.default_popup，否則 action.onClicked 不會觸發。
    await chrome.sidePanel.setPanelBehavior({
        openPanelOnActionClick: true
    });
}

// 點擊 Extension 圖示：開啟原生 Side Panel，並以目前分頁 URL 作為 detail 來源
chrome.action.onClicked.addListener(async (tab) => {
    try {
        const url = isHttpUrl(tab?.url) ? tab.url : "";
        await openSidePanelForUrl(url, tab);
    } catch (e) {
        console.error("Failed to open side panel from action:", e);
    }
});

// 每次 service worker 啟動時確保 alarm 存在
chrome.alarms.create(POLL_ALARM_NAME, { periodInMinutes: POLL_INTERVAL_MINUTES });

// ── 背景輪詢 ─────────────────────────────────────────────────────
chrome.alarms.onAlarm.addListener(async (alarm) => {
    if (alarm.name !== POLL_ALARM_NAME) return;
    await pollAllPendingUrls();
});

async function addToPendingUrls(url) {
    const { pendingUrls = [] } = await chrome.storage.local.get(["pendingUrls"]);
    if (!pendingUrls.includes(url)) {
        pendingUrls.push(url);
        await chrome.storage.local.set({ pendingUrls });
    }
}

async function removeFromPendingUrls(url) {
    const { pendingUrls = [] } = await chrome.storage.local.get(["pendingUrls"]);
    await chrome.storage.local.set({ pendingUrls: pendingUrls.filter(u => u !== url) });
}

async function pollAllPendingUrls() {
    const { pendingUrls = [] } = await chrome.storage.local.get(["pendingUrls"]);
    if (!pendingUrls.length) return;

    console.log(`[POLL] Checking ${pendingUrls.length} pending URLs`);

    for (const url of [...pendingUrls]) {
        try {
            const responseJson = await fetchLabelForUrl(url);
            const data = responseJson.data || {};

            if (data.data_status !== "processing") {
                // 分析完成：更新 cache、移出 pending、廣播給所有分頁
                await writeCache("summary", url, data);
                await removeFromPendingUrls(url);

                const tabs = await chrome.tabs.query({});
                for (const tab of tabs) {
                    try {
                        await chrome.tabs.sendMessage(tab.id, {
                            type: "ANALYSIS_UPDATED",
                            url,
                            result: { ...data, fetch_source: "backend" }
                        });
                    } catch (_) {
                        // 分頁可能沒有 content script，忽略
                    }
                }

                const { detailItem } = await chrome.storage.local.get(["detailItem"]);
                if (buildItemKey(detailItem) === url || buildItemKey(detailItem) === data.normalized_url) {
                    await chrome.storage.local.set({
                        detailItem: { ...data, fetch_source: "backend_detail" }
                    });
                }
            }
        } catch (error) {
            console.warn(`[POLL] Failed to check ${url}:`, error.message);
        }
    }
}

// ── 訊息處理 ─────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    (async () => {
        switch (message.type) {
            case "GET_URL_ANALYSIS":
                sendResponse({
                    ok: true,
                    data: await getSummary(message.url)
                });
                return;

            case "GET_URL_ANALYSIS_BATCH":
                sendResponse({
                    ok: true,
                    data: await getBatchSummary(message.urls || [])
                });
                return;

            case "TOGGLE_PIN": {
                const result = await togglePinned(message.item);
                sendResponse({ ok: true, data: result });
                return;
            }

            case "OPEN_SIDE_PANEL":
                try {
                    const url = String(message.url || "").trim();
                    await openSidePanelForUrl(url, sender?.tab);
                    sendResponse({ ok: true });
                } catch (e) {
                    console.error("OPEN_SIDE_PANEL failed:", e);

                    // 後備：若目前 Chrome 版本或呼叫情境拒絕 open()，
                    // 至少先把 detailItem 寫入，並用 badge 提醒使用者點擊工具列圖示。
                    if (message.url) {
                        await chrome.storage.local.set({
                            detailItem: buildLoadingDetailItem(message.url)
                        });
                    }

                    if (sender?.tab?.id != null) {
                        await chrome.action.setBadgeText({ text: "!", tabId: sender.tab.id });
                        await chrome.action.setBadgeBackgroundColor({ color: "#4285f4", tabId: sender.tab.id });
                    }

                    sendResponse({ ok: false, error: String(e) });
                }
                return;

            case "CLEAR_SIDE_PANEL":
                await chrome.storage.local.remove(["detailItem"]);
                sendResponse({ ok: true });
                return;

            case "SIDE_PANEL_CLOSED":
                // 側邊欄關閉：清除 storage 並通知所有 content scripts 清除高亮
                await chrome.storage.local.remove(["detailItem"]);
                try {
                    const tabs = await chrome.tabs.query({});
                    for (const tab of tabs) {
                        if (tab.id != null) {
                            chrome.tabs.sendMessage(tab.id, { type: "SIDE_PANEL_CLOSED" }).catch(() => { });
                        }
                    }
                } catch (_) { }
                sendResponse({ ok: true });
                return;

            case "GET_SETTINGS":
                sendResponse({ ok: true, data: await getSettings() });
                return;

                await chrome.storage.local.set({
                    apiBaseUrl: normalizeBaseUrl(message.apiBaseUrl || DEFAULT_API_BASE_URL)
                });
                await clearMemoryCache();
                sendResponse({ ok: true });
                return;

            case "CLEAR_CACHE":
                await clearMemoryCache();
                sendResponse({ ok: true });
                return;

            case "REFRESH_DETAILS":
                try {
                    const result = await getFreshDetails(message.url);
                    sendResponse({ ok: true, data: result });
                } catch (e) {
                    sendResponse({ ok: false, error: String(e) });
                }
                return;

            default:
                sendResponse({ ok: false, error: "Unknown message type" });
        }
    })().catch((error) => {
        console.error(error);
        sendResponse({ ok: false, error: error.message || "Unexpected error" });
    });

    return true; // 保持 message channel 開啟
});


// ── Native Side Panel helpers ─────────────────────────────────────
async function openSidePanelForUrl(url, tab) {
    if (!chrome.sidePanel) {
        throw new Error("chrome.sidePanel API is unavailable");
    }

    const openOptions = getSidePanelOpenOptions(tab);

    // sidePanel.open() 必須盡量貼近使用者動作呼叫。
    // 先開 panel，再寫入 loading/detail，避免 user gesture 被非必要 await 消耗。
    await chrome.sidePanel.open(openOptions);

    if (tab?.id != null) {
        await chrome.action.setBadgeText({ text: "", tabId: tab.id });
    }

    const cleanUrl = String(url || "").trim();
    if (!cleanUrl) {
        await chrome.storage.local.remove(["detailItem"]);
        return;
    }

    await chrome.storage.local.set({
        detailItem: buildLoadingDetailItem(cleanUrl)
    });

    const detail = await getFreshDetails(cleanUrl);
    await chrome.storage.local.set({
        detailItem: detail
    });
}

function getSidePanelOpenOptions(tab) {
    if (tab?.windowId != null) {
        return { windowId: tab.windowId };
    }
    if (tab?.id != null) {
        return { tabId: tab.id };
    }
    throw new Error("Missing tab/window context for side panel");
}

function buildLoadingDetailItem(url) {
    return {
        requested_url: url,
        normalized_url: url,
        data_status: "loading",
        data_status_label: "讀取中",
        risk_level: 3,
        risk_label: "未知",
        short_reason: "正在向後端取得最新詳細資料…",
        detail_available: false,
        fetch_source: "loading"
    };
}

async function getFreshDetails(url) {
    const cached = await readCache("detail", url);
    if (cached) {
        return { ...cached, fetch_source: "memory_cache" };
    }

    try {
        const responseJson = await fetchLabelForUrl(url);
        const data = responseJson.data || {};

        if (data.data_status === "processing") {
            await addToPendingUrls(url);
        } else if (data.data_status !== "not_recorded") {
            await writeCache("detail", url, data);
            await writeCache("summary", url, data);
            await removeFromPendingUrls(url);
        }

        return { ...data, fetch_source: "backend_detail" };
    } catch (error) {
        console.warn("Detail fetch failed:", error);
        return { ...buildOfflineFallback(url, error.message), fetch_source: "offline_fallback" };
    }
}

function isHttpUrl(url) {
    try {
        const parsed = new URL(url);
        return parsed.protocol === "http:" || parsed.protocol === "https:";
    } catch (_) {
        return false;
    }
}

// ── Settings ─────────────────────────────────────────────────────
async function getSettings() {
    const { apiBaseUrl } = await chrome.storage.local.get(["apiBaseUrl"]);
    return { apiBaseUrl: normalizeBaseUrl(apiBaseUrl || DEFAULT_API_BASE_URL) };
}

function normalizeBaseUrl(value) {
    let url = String(value || "").trim();
    if (!url) return DEFAULT_API_BASE_URL;
    url = url.replace(/\/+$/, "");
    if (url.endsWith("/api")) {
        url = url.slice(0, -4);
    }
    return url || DEFAULT_API_BASE_URL;
}

// ── Persistent Cache (Chrome Storage Local) ─────────────────────
function getCacheKey(kind, url) {
    return `cache:${kind}:${String(url || "").trim()}`;
}

async function readCache(kind, url) {
    const key = getCacheKey(kind, url);
    const stored = await chrome.storage.local.get([key]);
    const entry = stored[key];
    if (!entry) return null;
    if (Date.now() > entry.expiresAt) {
        await chrome.storage.local.remove([key]);
        return null;
    }
    return entry.value;
}

async function writeCache(kind, url, value) {
    const key = getCacheKey(kind, url);
    await chrome.storage.local.set({
        [key]: {
            value,
            expiresAt: Date.now() + CACHE_TTL_MS
        }
    });
}

async function clearMemoryCache() {
    const all = await chrome.storage.local.get(null);
    const keysToRemove = Object.keys(all).filter(key => key.startsWith("cache:"));
    if (keysToRemove.length > 0) {
        await chrome.storage.local.remove(keysToRemove);
    }
}

// ── Summary（單一 URL）────────────────────────────────────────────
async function getSummary(url) {
    const cached = await readCache("summary", url);
    if (cached) {
        return { ...cached, fetch_source: "memory_cache" };
    }

    try {
        const responseJson = await fetchLabelForUrl(url);
        const data = responseJson.data || {};

        if (data.data_status === "processing") {
            await addToPendingUrls(url); // 加入背景輪詢
        } else if (data.data_status !== "not_recorded") {
            await writeCache("summary", url, data);
            await removeFromPendingUrls(url);
        }

        return { ...data, fetch_source: "backend" };
    } catch (error) {
        console.warn("Summary fetch failed:", error);
        return { ...buildOfflineFallback(url, error.message), fetch_source: "offline_fallback" };
    }
}

// ── Batch Summary ─────────────────────────────────────────────────
async function getBatchSummary(urls) {
    const cleanUrls = [...new Set((urls || []).map(u => String(u || "").trim()).filter(Boolean))];
    if (!cleanUrls.length) return {};

    const result = {};
    const missingUrls = [];

    for (const url of cleanUrls) {
        const cached = await readCache("summary", url);
        if (cached) {
            result[url] = { ...cached, fetch_source: "memory_cache" };
        } else {
            missingUrls.push(url);
        }
    }

    if (!missingUrls.length) return result;

    try {
        const fetchedMap = await fetchLabelsForUrls(missingUrls);
        for (const url of missingUrls) {
            const rawLabel = (fetchedMap && fetchedMap[url]) || "";
            const mapped = mapLabelToRisk(rawLabel);
            const dataStatus = mapped.data_status || (rawLabel === "processing" ? "processing" : "recorded");
            const entry = {
                requested_url: url, normalized_url: url,
                data_status: dataStatus,
                data_status_label: dataStatus === "processing" ? "處理中" : "已標記",
                risk_level: mapped.risk_level,
                risk_label: mapped.risk_label,
                short_reason: mapped.short_reason,
                raw_label: rawLabel || null,
                detail_available: false
            };

            if (dataStatus === "processing") {
                await addToPendingUrls(url);
            } else if (dataStatus !== "not_recorded") {
                await writeCache("summary", url, entry);
                await removeFromPendingUrls(url);
            }

            result[url] = { ...entry, fetch_source: "backend" };
        }
    } catch (error) {
        console.warn("Batch summary fetch failed:", error);
        for (const url of missingUrls) {
            result[url] = { ...buildOfflineFallback(url, error.message), fetch_source: "offline_fallback" };
        }
    }

    return result;
}

// ── Fetch Helpers ─────────────────────────────────────────────────
async function fetchLabelsForUrls(urls) {
    const { apiBaseUrl } = await getSettings();
    const response = await fetch(`${apiBaseUrl}/check_urls_batch`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ urls })
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
}

async function fetchLabelForUrl(url) {
    const { apiBaseUrl } = await getSettings();
    const response = await fetch(`${apiBaseUrl}/api/url/details`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url })
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
}

// ── Label Mapping（僅支援五種新版 confidence_level）────────────────
function mapLabelToRisk(label) {
    const n = String(label || "").trim().toLowerCase();

    if (n.includes("highly real")) return { risk_level: 1, risk_label: "高度可信", short_reason: "模型判定此文章為高度真實。" };
    if (n.includes("likely real")) return { risk_level: 2, risk_label: "可能可信", short_reason: "模型判定此文章可能為真實。" };
    if (n.includes("likely fake")) return { risk_level: 4, risk_label: "可能假新聞", short_reason: "模型判定此文章可能為假新聞，建議提高警覺。" };
    if (n.includes("highly fake")) return { risk_level: 5, risk_label: "高度假新聞", short_reason: "模型判定此文章為高度假新聞，建議避免互動。" };
    if (n.includes("uncertain")) return { risk_level: 3, risk_label: "不確定", short_reason: "模型對此文章真假判斷不確定。" };

    if (["processing", "queued", "crawling"].includes(n))
        return { risk_level: 3, risk_label: "分析中", data_status: "processing", short_reason: "系統正在背景爬取並分析此網頁，請稍候…" };

    return { risk_level: 3, risk_label: "未知", short_reason: "無法識別分類結果，系統尚未完成分析。" };
}

// ── Pin ───────────────────────────────────────────────────────────
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

// ── Offline Fallback ──────────────────────────────────────────────
function buildOfflineFallback(url, errorMessage = "") {
    return {
        requested_url: url, normalized_url: url,
        data_status: "service_unavailable", data_status_label: "服務無法連線",
        risk_level: 3, risk_label: "未知",
        short_reason: errorMessage ? `目前無法連線至後端：${errorMessage}` : "目前無法連線至後端。",
        raw_label: null, detail_available: false
    };
}
