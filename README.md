# 基於階層式 mBERT 與可解釋性 AI 之假新聞即時辨識 Chrome 擴充套件

本專題實作一套可嵌入日常瀏覽情境的假新聞輔助辨識系統。使用者在網頁中 hover 到新聞 URL 後，Chrome 擴充套件會在低干擾的提示框中顯示該連結的風險摘要；若需要更多資訊，也可以開啟側邊欄查看模型判斷結果、候選可疑句段與文章內容標示。

系統核心結合 Chrome Extension、Flask API、MongoDB、Firecrawl 爬蟲流程，以及階層式 mBERT 長文本分類模型，目標是在使用者閱讀新聞或接觸可疑連結的當下，提供即時、可理解的輔助判斷。

## 專題資訊

| 項目 | 內容 |
|---|---|
| 學校 | 國立聯合大學 |
| 系所 | 資訊工程學系 |
| 班級 | 112 級大學部資工三甲 |
| 專題題目 | 基於階層式 mBERT 與可解釋性 AI 之假新聞即時辨識 Chrome 擴充套件 |
| 英文題目 | A Real-Time Fake News Detection Chrome Extension Based on Hierarchical mBERT and Explainable AI |
| 指導教師 | 温育瑋 博士 |
| 組長 | 范文爾 U1224030 |
| 組員 | 毛彥霖 U1224029、蘇景賢 U1224041 |

## 研究動機

假新聞與錯假資訊會透過社群平台、通訊軟體與新聞網站快速擴散。傳統事實查核平台通常需要使用者主動複製文章並搜尋查核結果，流程較繁瑣，也難以在閱讀當下即時提醒。

因此，本專題希望將查核流程融入使用者原本的瀏覽行為：當使用者查看網頁連結時，系統能自動擷取 URL、查詢資料庫、必要時觸發爬蟲與模型推論，最後以提示框和側邊欄呈現輔助判斷結果，降低誤信與轉傳不實資訊的風險。

## 系統功能

- URL hover 即時提示：使用者 hover 連結超過 200ms 後，顯示該 URL 的風險摘要。
- 非侵入式 UI：提示框以 URL 元件為 anchor 定位，避免遮擋主要閱讀內容。
- 詳細資訊側邊欄：顯示 URL 分析結果、模型信心程度、候選可疑句段與文章內容。
- 可疑句段標示：將模型內部訊號評分較高的候選句段對應回原文位置，輔助使用者理解模型輸出。
- MongoDB 快取與去重複：已分析過的 URL 會儲存於資料庫，減少重複爬取與推論成本。
- Firecrawl 動態網頁擷取：處理 JavaScript 動態載入內容，提升新聞正文擷取完整度。
- 階層式 mBERT 長文本分類：將長篇新聞切成多個 chunk，透過階層式架構進行真偽判斷。
- 機率校準：透過 Temperature Scaling、NLL 與 ECE 評估模型信心分數，使前端提示更可靠。

## 系統架構

```text
使用者 hover URL
        |
        v
Chrome Extension Content Script
        |
        v
Background Service Worker
        |
        v
Flask REST API
        |
        +--> MongoDB 查詢已分析 URL
        |
        +--> Firecrawl 擷取未知 URL 文章內容
        |
        +--> 文本前處理
        |
        +--> 階層式 mBERT 推論
        |
        +--> 分析結果寫回 MongoDB
        |
        v
Tooltip / Side Panel 顯示結果
```

## 成果畫面

### Hover 提示框

使用者 hover 到 URL 後，前端會顯示提示框並向後端請求摘要資訊。若只是快速滑過連結，系統不會送出請求，避免產生不必要的後端負擔。

![Hover 提示框](docs/assets/demo-tooltip.jpg)

### 側邊欄詳細資訊

點擊「查看詳情」後，Chrome 右側會開啟 Side Panel，顯示更完整的模型分析資訊與候選句段。

![側邊欄詳細資訊](docs/assets/demo-sidepanel.jpg)

### 原文模糊比對與醒目提示

系統會將模型輸出的候選句段對應回爬取到的原文內容，讓使用者快速定位模型可能關注的文章區域。

![原文醒目提示](docs/assets/demo-highlight.jpg)

### 可疑句段互動效果

使用者 hover 可疑句段時，前端會產生懸浮效果，並可點擊跳轉到下方文章內容的對應位置。

![可疑句段互動效果](docs/assets/demo-hover-sentence.jpg)

## 影片與相關連結

| 類型 | 連結 |
|---|---|
| Demo 影片 | https://www.youtube.com/watch?v=wxfsOjTkmXA |
| 專題簡報 | https://canva.link/jlb67pyrjgrwlih |
| 專題成果報告 | https://365nuu-my.sharepoint.com/:w:/g/personal/m1424005_o365_nuu_edu_tw/IQD-LrN2faecRIh8QUGBHX88Adg066KOFxRW6rALUsiKQyY |
| 專題精簡論文 | https://365nuu-my.sharepoint.com/:w:/g/personal/m1424005_o365_nuu_edu_tw/IQAonCrX6qBLSbFmpdZFZ7fgAdlQ5MHWOkDegocfdYf6TNI |
| GitHub Repository | 待補：推上 GitHub 後填入 repo URL |

## 專案目錄

```text
.
├── testing_mbert/          # mBERT 推論與模型相關程式
├── inferencepipeline/      # 爬取、匯出、前處理、推論、寫入 MongoDB 的 pipeline
├── crawler/                # Firecrawl worker 與爬蟲流程
├── fake-news-detector/     # Flask backend 與 Chrome Extension
├── docs/assets/            # README 使用的成果截圖
├── requirements.txt        # Python 依賴
├── .env.example            # 環境變數範例
└── README.md
```

## 技術使用

- Frontend：Chrome Extension Manifest V3、Content Script、Background Service Worker、Side Panel
- Backend：Flask REST API
- Database：MongoDB
- Crawler：Firecrawl、URL normalization、domain rules、retry/backoff
- NLP / ML：PyTorch、Transformers、mBERT、階層式長文本分類
- Explainability：模型內部訊號候選句段、LIME 對照實驗
- Calibration：Temperature Scaling、Negative Log-Likelihood、Expected Calibration Error

## Setup

Create a local Python environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create local environment variables:

```bash
cp .env.example .env
```

Edit `.env` for your MongoDB, model path, crawler, and Firecrawl settings.

## Run Inference Pipeline

Run the full pipeline:

```bash
python inferencepipeline/run_all.py
```

Run without crawling:

```bash
python inferencepipeline/run_all.py --skip-crawl
```

## Run Extension Backend

The Chrome extension backend lives in `fake-news-detector/backend`. See `fake-news-detector/README.md` for the detailed extension, backend, MongoDB, and Docker instructions.

## GitHub Notes

This repository intentionally ignores local secrets, virtual environments, generated CSV/JSONL outputs, crawler results, model checkpoints, packaged extensions, and third-party cloned source such as `firecrawl/`.

Do not commit real `.env` files, `*.pem` keys, `.crx` packages, scraped news exports, or trained model weights unless you have verified they are safe and licensed for public release.
