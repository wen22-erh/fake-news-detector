# -*- coding: utf-8 -*-
"""
export_to_csv.py
從 new.articles（join new.contents）匯出 CSV。
輸出欄位對齊 fccna_worker.write_csv()：
  job_id, url, normalized_url, domain, site,
  title, text, text_len, fetched_at, status, error_message
"""

import os
import pandas as pd
from datetime import datetime, timezone
from pymongo import MongoClient


CSV_FIELDNAMES = [
    "job_id",
    "url",
    "normalized_url",
    "domain",
    "site",
    "title",
    "text",
    "text_len",
    "fetched_at",
    "status",
    "error_message",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def export_data_to_csv(
    mongo_uri: str,
    db_name: str,
    articles_collection: str,
    contents_collection: str,
    csv_folder: str,
    csv_filename: str = "crawler_export.csv",
    job_id: str = "batch",
    status_filter: str = "",
    limit: int = 0,
    result_db: str = "fake_news_detector",
    result_urls_collection: str = "urls",
    skip_processed: bool = True,        # True = 跳過已在 fake_news_detector.urls 的文章
                                        # 可用環境變數 SKIP_PROCESSED=false 覆蓋
) -> str:
    """
    從 new.articles join new.contents 匯出 CSV。

    Parameters
    ----------
    mongo_uri            : MongoDB 連線字串
    db_name              : 來源 DB（通常是 "new"）
    articles_collection  : 文章索引 collection（通常是 "articles"）
    contents_collection  : 原始內容 collection（通常是 "contents"）
    csv_folder           : 輸出資料夾
    csv_filename         : 輸出檔名
    job_id               : 填入 CSV 的 job_id 欄位
    status_filter        : 只匯出 articles.crawler == status_filter 的文章（空 = 全部）
    limit                : 最多匯出幾筆（0 = 不限）

    Returns
    -------
    str : 輸出的 CSV 完整路徑
    """

    print("========== export_to_csv.py 開始 ==========")

    # 環境變數 SKIP_PROCESSED=false 可強制匯出全部（測試用）
    import os as _os
    if _os.getenv("SKIP_PROCESSED", "").lower() in ("false", "0", "no"):
        skip_processed = False
        print("[INFO] SKIP_PROCESSED=false，將匯出全部文章（含已處理）")

    client = MongoClient(mongo_uri)
    db = client[db_name]
    articles_col = db[articles_collection]
    contents_col = db[contents_collection]

    print(f"來源：{db_name}.{articles_collection}  +  {db_name}.{contents_collection}")

    # ── 取得已處理過的 URL 集合 ──────────────────────────────
    already_processed: set = set()
    if skip_processed:
        try:
            result_col = client[result_db][result_urls_collection]
            processed_docs = result_col.find({}, {"url": 1, "normalized_url": 1, "_id": 0})
            for doc in processed_docs:
                if doc.get("url"):
                    already_processed.add(doc["url"])
                if doc.get("normalized_url"):
                    already_processed.add(doc["normalized_url"])
            print(f"已處理 URL 數（跳過）：{len(already_processed)}")
        except Exception as e:
            print(f"[WARN] 讀取已處理 URL 失敗（將匯出全部）：{e}")

    # ── 查詢 articles ────────────────────────────────────────
    query: dict = {}
    if status_filter:
        query["crawler"] = status_filter

    cursor = articles_col.find(query, {"_id": 0})
    if limit > 0:
        cursor = cursor.limit(limit)

    articles_docs = list(cursor)
    print(f"articles 筆數（DB 中）：{len(articles_docs)}")

    # 過濾掉已處理過的 URL
    if already_processed:
        articles_docs = [
            doc for doc in articles_docs
            if (doc.get("url") not in already_processed
                and doc.get("normalized_url") not in already_processed)
        ]
        print(f"過濾後待處理筆數：{len(articles_docs)}")

    if not articles_docs:
        print("[INFO] 沒有新文章需要處理，pipeline 結束。")
        client.close()
        return ""   # 回傳空字串，run_all.py 收到後可提前結束

    # ── 建立 content_id → text 對照表 ────────────────────────
    from bson import ObjectId

    content_ids = []
    for doc in articles_docs:
        cid = doc.get("content_id")
        if cid:
            try:
                content_ids.append(ObjectId(cid) if not isinstance(cid, ObjectId) else cid)
            except Exception:
                pass

    content_map: dict = {}   # str(ObjectId) → text
    if content_ids:
        contents_docs = list(
            contents_col.find(
                {"_id": {"$in": content_ids}},
                {"_id": 1, "text": 1, "text_len": 1}
            )
        )
        for cdoc in contents_docs:
            content_map[str(cdoc["_id"])] = {
                "text":     cdoc.get("text", ""),
                "text_len": cdoc.get("text_len", 0),
            }

    print(f"contents 對應到 {len(content_map)} 筆")

    # ── 組合輸出列 ─────────────────────────────────────────
    rows = []
    for doc in articles_docs:
        cid     = str(doc.get("content_id", ""))
        content = content_map.get(cid, {})

        text     = content.get("text", "") or ""
        text_len = content.get("text_len") or len(text)

        # fetched_at：優先用 articles 裡的，沒有就用現在
        fetched_at = doc.get("fetched_at", "")
        if hasattr(fetched_at, "isoformat"):
            fetched_at = fetched_at.isoformat()
        fetched_at = str(fetched_at) if fetched_at else utc_now_iso()

        url          = doc.get("url") or doc.get("normalized_url") or ""
        normalized   = doc.get("normalized_url") or url
        domain       = doc.get("domain", "")
        site         = doc.get("site", "")
        title        = doc.get("title", "")

        # 有內文 → content_saved；沒有 → failed
        status = "content_saved" if text.strip() else "failed"

        rows.append({
            "job_id":         job_id,
            "url":            url,
            "normalized_url": normalized,
            "domain":         domain,
            "site":           site,
            "title":          title,
            "text":           text,
            "text_len":       text_len,
            "fetched_at":     fetched_at,
            "status":         status,
            "error_message":  "" if status == "content_saved" else "no text in contents",
        })

    df = pd.DataFrame(rows, columns=CSV_FIELDNAMES)

    # 只保留爬取成功的列送進模型
    df_success = df[df["status"] == "content_saved"].copy()
    print(f"有效（content_saved）筆數：{len(df_success)} / {len(df)}")

    if df_success.empty:
        print("[INFO] 過濾後無有效文章（text 為空），pipeline 結束。")
        client.close()
        return ""

    os.makedirs(csv_folder, exist_ok=True)
    csv_path = os.path.join(csv_folder, csv_filename)
    df_success.to_csv(csv_path, index=False, encoding="utf-8-sig")

    print(f"CSV 已成功匯出：{csv_path}（{len(df_success)} 筆）")
    print("CSV 欄位：", df_success.columns.tolist())
    print("========== export_to_csv.py 完成 ==========")

    client.close()
    return csv_path


# ── 直接執行時的預設值 ────────────────────────────────────────
if __name__ == "__main__":
    MONGO_URI           = "mongodb://localhost:27017/"
    DB_NAME             = "new"
    ARTICLES_COLLECTION = "articles"
    CONTENTS_COLLECTION = "contents"
    CSV_FOLDER          = "../csvfiles"

    export_data_to_csv(
        mongo_uri=MONGO_URI,
        db_name=DB_NAME,
        articles_collection=ARTICLES_COLLECTION,
        contents_collection=CONTENTS_COLLECTION,
        csv_folder=CSV_FOLDER,
    )