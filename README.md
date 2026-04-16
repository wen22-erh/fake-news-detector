# URL Risk Hover Inspector

依照你提供的流程圖，這個專案將「滑鼠 hover 網頁中的 URL」落地成一個可執行系統：

- 前端：Chrome Extension（Manifest V3）
- 後端：Flask REST API
- 資料庫：MongoDB
- 部署：Docker Compose

---

## 1. 合理假設

流程圖沒有明講但實作必須補齊的地方，我做了以下合理假設：

1. **資料查詢順序**
   - Content Script 不直接連資料庫。
   - 會先呼叫 Extension Background Service Worker。
   - Background 先查記憶體快取，再打 Flask API，最後由 Flask 查 MongoDB。

2. **資料庫收錄單位**
   - 以 `normalized_url` 為主，`domain` 為備援。
   - 若 exact URL 找不到，會 fallback 用 domain 查詢。

3. **未知狀態**
   - MongoDB 沒資料時，回傳：
     - `data_status = not_recorded`
     - `risk_level = 3`
     - `risk_label = 未知`

4. **Side Panel 行為**
   - 點「查看詳情」會開啟右側 Side Panel。
   - Side Panel 顯示更完整的分析內容。
   - 點「取消釘選並關閉」會清掉 pinned 狀態並請 Background 關閉 Side Panel。
   - 目前 Chrome 的 `sidePanel` API 已支援 `open()`，且新版本也支援 `close()`；本專案同時做了 fallback，若 `close()` 不可用會退回 `setOptions(enabled: false)`。Chrome Side Panel API 需在 manifest 內宣告 `sidePanel` permission。

5. **Tooltip 定位**
   - Tooltip 以 URL 元件 bounding box 為 anchor。
   - 不跟滑鼠游標。
   - 先試：右上 → 右下 → 左上 → 左下。
   - 若仍超出 viewport，會 clamp 回可視範圍，保留 10px 邊距。

6. **Chrome 版本**
   - 採用 Manifest V3。
   - `chrome.storage` 用於跨 content script / background / side panel 共用狀態；Chrome 文件說明這個 storage 可在 extension service worker、content scripts 等 context 中使用。

7. **Docker 範圍**
   - Docker 主要負責 Flask + MongoDB。
   - Chrome Extension 本身不能直接由 Docker 安裝到使用者瀏覽器，因此採「本機 Load unpacked」方式載入。

---

## 2. 專案目錄結構

```text
url-risk-hover-system/
├── .env.example
├── .gitignore
├── docker-compose.yml
├── README.md
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── run.py
│   └── app/
│       ├── __init__.py
│       ├── config.py
│       ├── db.py
│       ├── api/
│       │   ├── health.py
│       │   └── urls.py
│       ├── repositories/
│       │   └── url_analysis_repository.py
│       ├── services/
│       │   ├── url_normalizer.py
│       │   └── url_service.py
│       └── seed/
│           └── seed_data.py
├── extension/
│   ├── manifest.json
│   ├── background/
│   │   └── service-worker.js
│   ├── content/
│   │   ├── content.css
│   │   └── content.js
│   ├── popup/
│   │   ├── popup.css
│   │   ├── popup.html
│   │   └── popup.js
│   └── sidepanel/
│       ├── sidepanel.css
│       ├── sidepanel.html
│       └── sidepanel.js
└── demo/
    └── index.html
```

---

## 3. 系統架構說明

### 整體元件

1. **Content Script**
   - 監聽頁面上 `<a href>` 的 hover 狀態。
   - 等待 200ms。
   - 取得 URL 與 bounding box。
   - 顯示 anchored tooltip。
   - 負責 tooltip 開關、位置計算、滑鼠移入移出邏輯。

2. **Background Service Worker**
   - 作為前端的背景服務層。
   - 管理記憶體快取。
   - 負責呼叫 Flask API。
   - 負責開啟 / 關閉 Side Panel。
   - 管理 pinned / detailItem / API Base URL 等跨頁狀態。

3. **Side Panel**
   - 顯示詳細分析內容。
   - 支援重新查詢、釘選、取消釘選並關閉。

4. **Popup**
   - 設定 API Base URL。
   - 手動清除快取。

5. **Flask API**
   - 接收 URL 檢查請求。
   - 正規化 URL。
   - 查 MongoDB exact/domain 資料。
   - 回傳 summary 與 details。

6. **MongoDB**
   - 儲存已收錄 URL 的風險資料。
   - 專案啟動後若資料表為空，會自動 seed demo 資料。

### 資料流

1. 使用者 hover 到網頁 URL。
2. Content Script 等待 200ms。
3. Content Script 傳訊息給 Background。
4. Background 先查記憶體快取。
5. 若沒有，再呼叫 Flask `/api/url/check`。
6. Flask 正規化 URL 並查 MongoDB。
7. 回傳 summary 給 Extension。
8. Content Script 依照流程圖顯示 tooltip。
9. 使用者點「查看詳情」。
10. Background 呼叫 `/api/url/details`，再打開 Side Panel。
11. Side Panel 讀取 storage 內 detailItem 並渲染。

---

## 4. 每個流程節點對應的功能模組

| 流程節點 | 對應模組 |
|---|---|
| 使用者滑鼠移到頁面中的 URL | `extension/content/content.js` |
| 停留超過 200ms? | `scheduleHover()` |
| 不顯示提示框 | `closeTooltip()` |
| 取得目前 URL 與其 bounding box | `anchor.href` + `getBoundingClientRect()` |
| 查詢快取 / 背景服務 / 資料庫 | `background/service-worker.js` + Flask + MongoDB |
| 是否有收錄資料? | `UrlAnalysisRepository.find_best_match()` |
| 取得安全分類結果 | `UrlRiskService.get_summary()` |
| 資料狀態：未收錄資料庫 / 安全等級：未知 | `UrlRiskService._build_unknown()` |
| 建立 Hover 提示框 | `createTooltip()` |
| 提示框內容 | `populateTooltip()` |
| 以 URL 元件為 anchor 定位 | `positionTooltip()` |
| 先嘗試位置：右上 → 右下 → 左上 → 左下 | `getBestPosition()` |
| 是否超出 viewport? | `fitsViewport()` |
| 自動修正位置 | `clampPosition()` |
| 顯示提示框 | `showTooltip()` |
| 滑鼠移出 URL | `handleDocumentMouseOut()` |
| 啟動 200ms 關閉計時 | `scheduleClose()` |
| 滑鼠是否進入提示框? | tooltip `mouseenter` / `mouseleave` |
| 保持顯示 | `state.isTooltipHovered = true` |
| 是否已釘選? | `state.isPinned` + `chrome.storage.local` |
| 保持固定內容 | pinned 狀態不自動關閉 |
| 關閉提示框 | `closeTooltip()` |
| 點擊 查看詳情 | `OPEN_SIDE_PANEL` message |
| 開啟右側詳情面板 / Side Panel | `chrome.sidePanel.open()` |
| 顯示詳細內容 | `extension/sidepanel/sidepanel.js` |
| 點擊 釘選 | `TOGGLE_PIN` message |
| 清除 pinned 狀態並關閉詳情面板 | `unpinAndCloseBtn` + `CLEAR_SIDE_PANEL` |

---

## 5. API 路由設計

| Method | Route | 說明 |
|---|---|---|
| GET | `/api/health` | 健康檢查與 Mongo ping |
| POST | `/api/url/check` | 回傳 tooltip 用的 summary |
| POST | `/api/url/details` | 回傳 Side Panel 用的完整 details |
| GET | `/api/risk-levels` | 回傳風險等級對照表 |

### `POST /api/url/check`
Request:
```json
{
  "url": "https://github.com"
}
```

Response:
```json
{
  "ok": true,
  "data": {
    "requested_url": "https://github.com",
    "normalized_url": "https://github.com/",
    "domain": "github.com",
    "data_status": "recorded",
    "data_status_label": "已收錄資料庫",
    "risk_level": 1,
    "risk_label": "安全",
    "short_reason": "知名程式碼託管平台主網域。",
    "analysis_time": "2026-03-24T00:00:00+00:00"
  }
}
```

### `POST /api/url/details`
除了 summary 欄位外，額外包含：
- `detailed_reason`
- `risk_factors`
- `suggested_actions`
- `analysis_metadata`

---

## 6. 前端頁面設計

### A. Tooltip（注入到頁面）
內容：
- URL
- 安全等級 badge
- 資料狀態
- 簡短原因
- 按鈕：查看詳情 / 釘選 / 關閉

特性：
- 以 anchor 定位
- 不跟滑鼠
- 自動避開 viewport 邊界
- 支援 hover 延遲與 close 延遲

### B. Side Panel
內容：
- 風險等級 badge
- 資料狀態
- 分析時間
- URL
- 判定依據
- 風險因子
- 建議操作
- 分析 metadata
- 操作按鈕：重新查詢 / 關閉 / 釘選 / 取消釘選並關閉

### C. Popup
內容：
- API Base URL 設定欄位
- 儲存設定
- 清除快取

---

## 7. 核心程式碼

核心檔案如下：

- **前端 hover 與 tooltip**
  - `extension/content/content.js`
- **背景服務、快取、Side Panel 開關**
  - `extension/background/service-worker.js`
- **Side Panel 詳情頁**
  - `extension/sidepanel/sidepanel.js`
- **Flask API**
  - `backend/app/api/urls.py`
- **URL 正規化與風險查詢**
  - `backend/app/services/url_normalizer.py`
  - `backend/app/services/url_service.py`
- **MongoDB Repository**
  - `backend/app/repositories/url_analysis_repository.py`

完整可執行程式碼已經放在專案檔案中。

---

## 8. README

本檔案即為 README，已包含：
- 架構
- 模組分工
- API
- 前端設計
- 啟動方式
- 後續擴充方向

---

## 9. 啟動方式

### 9.1 啟動後端與 MongoDB
在專案根目錄執行：

```bash
docker compose up --build
```

成功後：
- Flask API：`http://localhost:5000`
- Health Check：`http://localhost:5000/api/health`

### 9.2 載入 Chrome Extension
1. 打開 Chrome。
2. 進入 `chrome://extensions/`
3. 開啟右上角「開發人員模式」
4. 點「載入未封裝項目」
5. 選擇本專案中的 `extension/` 資料夾

### 9.3 設定 API URL
1. 點工具列中的 extension icon
2. 確認 `API Base URL` 為：
   - `http://localhost:5000/api`

### 9.4 開啟 Demo 頁
直接用瀏覽器打開：
- `demo/index.html`

或自行在任何網站上測試 hover 連結。

---

## 10. 後續可擴充功能

1. **真實風險分析引擎**
   - 接入 URL reputation provider
   - 接入 phishing / malware feed
   - 接入 LLM 或規則引擎做說明文字生成

2. **背景非同步分析**
   - 若 Mongo 沒有資料，先回 unknown，再排入分析佇列
   - 之後更新結果並快取

3. **快取升級**
   - 由記憶體快取改成 `chrome.storage.session` 或 IndexedDB
   - 加上 TTL 與 eviction 策略

4. **更多 UI 狀態**
   - loading skeleton
   - API error state
   - 「最近檢查過的 URL」清單

5. **管理後台**
   - 管理 URL 分類資料
   - 批次匯入黑名單 / 白名單
   - 調整規則權重

6. **更細的匹配規則**
   - 子網域繼承
   - path-based rule
   - regex/wildcard rule
   - 時效性分析

7. **安全事件記錄**
   - 紀錄使用者曾查看過的高風險 URL
   - 匯出 CSV / audit log

8. **測試**
   - Flask 單元測試
   - E2E 自動化測試
   - Extension message flow 測試

---

## 11. 本專案已完成的流程圖覆蓋範圍

- hover 200ms 判定
- 取得 URL 與 bounding box
- 查詢快取 / 背景服務 / MongoDB
- 有資料 / 無資料分支
- tooltip 建立與內容顯示
- anchored 定位與 viewport 修正
- 滑鼠移出 URL / 移入提示框 / 關閉計時
- 查看詳情
- 釘選 / 取消釘選
- Side Panel 顯示詳細內容

---

## 12. 注意事項

1. Chrome Extension 不會注入到：
   - `chrome://*`
   - Chrome Web Store
   - 部分瀏覽器保護頁面

2. `demo` 內部分可疑網址是示意字串，不代表真實網站。

3. 這份專案是「可執行 MVP」，目的是把流程圖完整落成系統骨架與核心互動流程。


## 2026-03 修正

- 修正 Side Panel 無法正常開啟的問題：`chrome.sidePanel.open()` 現在會在使用者點擊「查看詳情」後立即觸發，避免先等待 API 導致 user gesture 遺失。
- 新增 `minimum_chrome_version: 116`，因為 `sidePanel.open()` 需 Chrome 116 以上。
