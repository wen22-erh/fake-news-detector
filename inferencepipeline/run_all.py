# -*- coding: utf-8 -*-
"""
run_all.py  ── 舊版爬蟲統一 Pipeline
========================================
流程：
  [1/4] crawler.py        → 爬取中文新聞，存入 new.articles + new.contents
  [2/4] export_to_csv.py  → 從 new DB 匯出 CSV（欄位對齊 fccna_worker）
  [3/4] total_preprocess  → 文章清理 + 輸出 cleaned CSV
  [4/4] 模型推論 + 存入 fake_news_detector.url_analyses + urls

可獨立呼叫（不含爬蟲）：
  python run_all.py --skip-crawl

NOTE:
  load_model_and_tokenizer 與 analyze_fake_news 皆直接從 testing_mbert/inference.py
  匯入，本檔不再保留任何重複定義，行為永遠與 inference.py 一致。
"""

import os
import sys
import time
import json
import uuid
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Lock 檔路徑（與 run.py 約定同一個檔案）
PIPELINE_LOCK_FILE = Path(os.getenv("PIPELINE_LOCK_FILE",
                                     "/tmp/fake_news_pipeline.lock"))
LOCK_TIMEOUT_SEC   = int(os.getenv("LOCK_TIMEOUT_SEC", "1800"))  # 30 分鐘


def acquire_lock() -> bool:
    """
    嘗試取得 pipeline lock。
    若 lock 存在且未超時 → 回傳 False（目前有其他 pipeline 在跑）。
    若 lock 存在但已超時（超過 LOCK_TIMEOUT_SEC）→ 強制清除並取得。
    """
    if PIPELINE_LOCK_FILE.exists():
        try:
            age = time.time() - PIPELINE_LOCK_FILE.stat().st_mtime
            if age < LOCK_TIMEOUT_SEC:
                print(f"[LOCK] pipeline 鎖定中（{age:.0f}s 前建立），跳過本次執行。")
                return False
            else:
                print(f"[LOCK] 發現過期 lock（{age:.0f}s），強制清除。")
        except Exception:
            pass

    try:
        PIPELINE_LOCK_FILE.write_text(
            json.dumps({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8",
        )
        return True
    except Exception as e:
        print(f"[LOCK] 建立 lock 失敗：{e}，繼續執行（無保護）。")
        return True


def release_lock():
    try:
        if PIPELINE_LOCK_FILE.exists():
            PIPELINE_LOCK_FILE.unlink()
    except Exception as e:
        print(f"[LOCK] 釋放 lock 失敗：{e}")


# =============================================================
# Path 解析（與原本 run_all.py 相同邏輯，但更健壯）
# =============================================================
def get_project_root() -> Path:
    """往上找同時包含 crawler/ 和 testing_mbert/ 的目錄。"""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "crawler").is_dir() and (parent / "testing_mbert").is_dir():
            return parent
    # fallback：__file__ 的上兩層
    return current.parents[1]


PROJECT_ROOT = get_project_root()

# 讓 Python 能 import testing_mbert.inference
TESTING_MBERT_DIR = PROJECT_ROOT / "testing_mbert"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(TESTING_MBERT_DIR))

# 同層目錄下的模組
_SELF_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SELF_DIR))

# =============================================================
# 第三方 imports（在 path 設定完後才 import）
# =============================================================
import total_preprocess  # noqa: E402

# 直接從 inference.py 匯入；analyze_fake_news 由 inference.py 提供，
# 本檔不再保留本地版本，行為永遠與模型端一致。
try:
    from testing_mbert.inference import load_model_and_tokenizer, analyze_fake_news
    _HAS_INFERENCE = True
except ImportError as e:
    print(f"[WARN] inference.py 載入失敗：{e}")
    load_model_and_tokenizer = None
    analyze_fake_news        = None
    _HAS_INFERENCE = False

import pandas as pd  # noqa: E402
from export_to_csv import export_data_to_csv    # noqa: E402
from save_to_mongo import save_results_to_mongo  # noqa: E402


# =============================================================
# Config
# =============================================================
MONGO_URI              = os.getenv("MONGO_URI",              "mongodb://localhost:27017/")
SOURCE_DB              = os.getenv("SOURCE_DB",              "new")
ARTICLES_COLLECTION    = os.getenv("ARTICLES_COLLECTION",    "articles")
CONTENTS_COLLECTION    = os.getenv("CONTENTS_COLLECTION",    "contents")
RESULT_DB              = os.getenv("RESULT_DB",              "fake_news_detector")
URLS_COLLECTION        = os.getenv("URLS_COLLECTION",        "urls")
ANALYSIS_COLLECTION    = os.getenv("ANALYSIS_COLLECTION",    "url_analyses")
MODEL_PATH             = os.getenv("MODEL_PATH",             str(TESTING_MBERT_DIR))

CSV_FOLDER    = PROJECT_ROOT / "csvfiles"
CLEANED_FOLDER = PROJECT_ROOT / "csvfiles" / "cleaned"


# =============================================================
# 工具
# =============================================================
def format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m}m {s:.2f}s"


def make_job_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{str(uuid.uuid4())[:8]}"


# =============================================================
# 模型推論（inline，不依賴 run.py）
# =============================================================
def load_model(model_path: str):
    if not _HAS_INFERENCE:
        raise RuntimeError(
            "無法載入 testing_mbert.inference，"
            "請確認 testing_mbert/ 資料夾與 inference.py 存在。"
        )
    model, tokenizer, device = load_model_and_tokenizer(model_path)
    return model, tokenizer, device


def run_inference_on_csv(
    cleaned_csv: str,
    model,
    tokenizer,
    device,
    results_json: str = None,
) -> tuple:
    """
    讀取 cleaned CSV → 對每筆呼叫 analyze_fake_news → 回傳 (predictions, prediction_details)。

    predictions       : { url → confidence_level }
    prediction_details: { url → 完整 result dict }
    """
    df = pd.read_csv(cleaned_csv)

    url_col  = "url"  if "url"  in df.columns else "normalized_url"
    text_col = "cleaned_content" if "cleaned_content" in df.columns else "text"

    predictions: dict       = {}
    prediction_details: dict = {}

    for _, row in df.iterrows():
        url  = row.get(url_col)
        text = row.get(text_col)

        if not url:
            continue
        if not text or (isinstance(text, float)) or not str(text).strip():
            continue

        try:
            result = analyze_fake_news(str(text), model, tokenizer, device)
            predictions[url]        = result["confidence_level"]
            prediction_details[url] = result
        except Exception as e:
            print(f"[INFERENCE ERROR] {url}: {e}")
            predictions[url]        = "不確定 (Uncertain)"
            prediction_details[url] = {"confidence_level": "不確定 (Uncertain)", "error": str(e)}

    if results_json:
        with open(results_json, "w", encoding="utf-8") as f:
            json.dump(prediction_details, f, ensure_ascii=False, indent=2)

    return predictions, prediction_details



# =============================================================
# 主 Pipeline
# =============================================================
def main(skip_crawl: bool = False):
    # ── Lock 檢查：防止與 run.py 或另一個 run_all.py 同時執行 ──
    if not acquire_lock():
        print("[SKIP] 本次排程跳過（另一個 pipeline 正在執行）。")
        return {"job_id": None, "skipped": True}

    total_start = time.time()
    job_id = make_job_id()

    try:
        print("=" * 55)
        print(f"  後端資料處理 Pipeline 開始  (job_id={job_id})")
        print("=" * 55)

        os.makedirs(CSV_FOLDER,     exist_ok=True)
        os.makedirs(CLEANED_FOLDER, exist_ok=True)

        # ──────────────────────────────────────────────────────
        # [1/4] 爬蟲（可選）
        # ──────────────────────────────────────────────────────
        if not skip_crawl:
            print("\n[1/4] 啟動 crawler.py（Frontier 爬蟲）...")
            t1 = time.time()
            try:
                import crawler as _crawler
                result = _crawler.crawl_frontier()
                print(f"[1/4] 爬蟲完成 {result}　耗時：{format_elapsed(time.time()-t1)}")
            except Exception as e:
                print(f"[1/4] 爬蟲失敗（跳過）：{e}")
        else:
            print("\n[1/4] --skip-crawl，跳過爬蟲步驟")

        # ──────────────────────────────────────────────────────
        # [2/4] 從 new DB 匯出 CSV
        # ──────────────────────────────────────────────────────
        print("\n[2/4] 從 MongoDB 匯出 CSV...")
        t2 = time.time()

        raw_csv = export_data_to_csv(
            mongo_uri=MONGO_URI,
            db_name=SOURCE_DB,
            articles_collection=ARTICLES_COLLECTION,
            contents_collection=CONTENTS_COLLECTION,
            csv_folder=str(CSV_FOLDER),
            csv_filename=f"{job_id}_raw.csv",
            job_id=job_id,
            result_db=RESULT_DB,
            result_urls_collection=URLS_COLLECTION,
            skip_processed=True,
        )

        if not raw_csv:
            print("[2/4] 無新文章，本次 pipeline 提前結束。")
            return {"job_id": job_id, "predictions": 0, "db_summary": {}}
            # NOTE: finally 區塊會自動執行 release_lock()

        print(f"[2/4] CSV 匯出完成　耗時：{format_elapsed(time.time()-t2)}")

        # ──────────────────────────────────────────────────────
        # [3/4] 前處理（total_preprocess）
        # ──────────────────────────────────────────────────────
        print("\n[3/4] 前處理（total_preprocess）...")
        t3 = time.time()

        cleaned_csv = str(CLEANED_FOLDER / f"{job_id}_cleaned.csv")
        total_preprocess.process_csv_file(raw_csv, cleaned_csv)

        print(f"[3/4] 前處理完成　耗時：{format_elapsed(time.time()-t3)}")
        print(f"      cleaned CSV：{cleaned_csv}")

        # ──────────────────────────────────────────────────────
        # [4/4] 模型推論 + 存入 MongoDB
        # ──────────────────────────────────────────────────────
        print("\n[4/4] 模型推論 + 寫入 MongoDB...")
        t4 = time.time()

        print("  載入模型...")
        model, tokenizer, device = load_model(MODEL_PATH)

        raw_content_by_url:     dict = {}
        cleaned_content_by_url: dict = {}
        title_by_url:           dict = {}

        try:
            raw_df  = pd.read_csv(raw_csv)
            url_col  = "url"  if "url"  in raw_df.columns else "normalized_url"
            text_col = "text" if "text" in raw_df.columns else "content"
            for _, row in raw_df.iterrows():
                u = str(row.get(url_col) or "").strip()
                if u:
                    raw_content_by_url[u] = str(row.get(text_col) or "")
                    title_by_url[u]       = str(row.get("title") or "")
        except Exception as e:
            print(f"  [WARN] raw CSV 讀取失敗：{e}")

        try:
            cleaned_df = pd.read_csv(cleaned_csv)
            c_url_col  = "url" if "url" in cleaned_df.columns else "normalized_url"
            c_text_col = "cleaned_content" if "cleaned_content" in cleaned_df.columns else "text"
            for _, row in cleaned_df.iterrows():
                u = str(row.get(c_url_col) or "").strip()
                if u:
                    cleaned_content_by_url[u] = str(row.get(c_text_col) or "")
                    if u not in title_by_url:
                        title_by_url[u] = str(row.get("title") or "")
        except Exception as e:
            print(f"  [WARN] cleaned CSV 讀取失敗：{e}")

        results_json = str(CLEANED_FOLDER / f"{job_id}_predictions.json")
        predictions, prediction_details = run_inference_on_csv(
            cleaned_csv, model, tokenizer, device, results_json
        )
        print(f"  推論完成：{len(predictions)} 筆")

        summary = save_results_to_mongo(
            mongo_uri=MONGO_URI,
            db_name=RESULT_DB,
            predictions=predictions,
            prediction_details=prediction_details,
            raw_content_by_url=raw_content_by_url,
            cleaned_content_by_url=cleaned_content_by_url,
            title_by_url=title_by_url,
            job_id=job_id,
            urls_collection_name=URLS_COLLECTION,
            analysis_collection_name=ANALYSIS_COLLECTION,
            source_csv=raw_csv,
            cleaned_csv=cleaned_csv,
            tokenizer=tokenizer,
        )

        print(f"[4/4] 完成　耗時：{format_elapsed(time.time()-t4)}")

        total_elapsed = time.time() - total_start
        print("\n" + "=" * 55)
        print(f"  Pipeline 完成  (job_id={job_id})")
        print(f"  推論筆數                 : {len(predictions)}")
        print(f"  寫入 url_analyses + urls : {summary['inserted_or_updated']} 筆操作")
        print(f"  寫入失敗                 : {summary['failed']} 筆")
        print(f"  總耗時                   : {format_elapsed(total_elapsed)}")
        print("=" * 55)

        return {
            "job_id":       job_id,
            "raw_csv":      raw_csv,
            "cleaned_csv":  cleaned_csv,
            "results_json": results_json,
            "predictions":  len(predictions),
            "db_summary":   summary,
        }

    except Exception as e:
        print(f"[ERROR] Pipeline 異常終止：{e}")
        raise
    finally:
        release_lock()


# =============================================================
# CLI 入口
# =============================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="舊版爬蟲統一 Pipeline")
    parser.add_argument(
        "--skip-crawl", action="store_true",
        help="跳過爬蟲步驟，直接從 new DB 現有資料開始",
    )
    args = parser.parse_args()
    main(skip_crawl=args.skip_crawl)