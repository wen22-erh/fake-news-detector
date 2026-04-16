import os
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, urlencode

from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING

# =========================
# Config
# =========================
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "fake_news_detector")
URLS_COLLECTION_NAME = os.getenv("URLS_COLLECTION_NAME", "urls")
ANALYSIS_COLLECTION_NAME = os.getenv("ANALYSIS_COLLECTION_NAME", "url_analyses")

HOST = os.getenv("FLASK_HOST", "0.0.0.0")
PORT = int(os.getenv("FLASK_PORT", "5050"))
DEBUG = os.getenv("FLASK_DEBUG", "true").lower() == "true"

app = Flask(__name__)
CORS(app)

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[MONGO_DB_NAME]
urls_collection = db[URLS_COLLECTION_NAME]
analysis_collection = db[ANALYSIS_COLLECTION_NAME]


# =========================
# Setup
# =========================
def ensure_indexes():
    try:
        urls_collection.create_index([("url", ASCENDING)], unique=False)
        urls_collection.create_index([("label", ASCENDING)], unique=False)

        analysis_collection.create_index([("normalized_url", ASCENDING)], unique=False)
        analysis_collection.create_index([("requested_url", ASCENDING)], unique=False)
        analysis_collection.create_index([("domain", ASCENDING)], unique=False)
    except Exception as error:
        print(f"[WARN] Failed to create indexes: {error}")


ensure_indexes()


# =========================
# Utils / Normalizer
# =========================
def normalize_url(url: str) -> str:
    if not url:
        return ""

    raw = str(url).strip()
    if not raw:
        return ""

    try:
        parsed = urlparse(raw)

        scheme = (parsed.scheme or "http").lower()
        netloc = (parsed.netloc or "").lower()

        path = parsed.path or ""
        if path != "/":
            path = path.rstrip("/")

        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        sorted_query = urlencode(sorted(query_pairs), doseq=True)

        normalized = f"{scheme}://{netloc}{path}"
        if sorted_query:
            normalized += f"?{sorted_query}"

        return normalized
    except Exception:
        return raw


def extract_domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat()


# =========================
# Label / Risk Mapping
# =========================
def map_label_to_risk(label: str):
    normalized = str(label or "").strip().lower()

    if normalized in {"real", "safe", "trusted"}:
        return {
            "risk_level": 1,
            "risk_label": "可信",
            "short_reason": f"此 URL 的標記結果為 {label}。"
        }

    if normalized in {"mostly_real", "low_risk", "low-risk"}:
        return {
            "risk_level": 2,
            "risk_label": "低風險",
            "short_reason": f"此 URL 的標記結果為 {label}，目前判定為低風險。"
        }

    if normalized in {"unknown", "uncertain", ""}:
        return {
            "risk_level": 3,
            "risk_label": "未知",
            "short_reason": "目前只有基本標記，缺少更完整分析。"
        }

    if normalized in {"suspicious"}:
        return {
            "risk_level": 4,
            "risk_label": "可疑",
            "short_reason": f"此 URL 的標記結果為 {label}，建議提高警覺。"
        }

    if normalized in {"fake", "danger", "malicious"}:
        return {
            "risk_level": 5,
            "risk_label": "高風險",
            "short_reason": f"此 URL 的標記結果為 {label}，建議避免互動。"
        }

    return {
        "risk_level": 3,
        "risk_label": str(label or "未知"),
        "short_reason": f"此 URL 的標記結果為 {label}，但尚未定義對應規則。"
    }


# =========================
# Repository-ish helpers
# =========================
def find_legacy_url_doc(url: str):
    normalized_url = normalize_url(url)

    return urls_collection.find_one({
        "$or": [
            {"url": url},
            {"url": normalized_url}
        ]
    })


def find_analysis_doc(url: str):
    normalized_url = normalize_url(url)
    domain = extract_domain(normalized_url)

    doc = analysis_collection.find_one({
        "$or": [
            {"normalized_url": normalized_url},
            {"requested_url": url}
        ]
    })
    if doc:
        doc["_match_scope"] = "url"
        doc["_matched_by"] = "normalized_url"
        return doc

    if domain:
        doc = analysis_collection.find_one({"domain": domain})
        if doc:
            doc["_match_scope"] = "domain"
            doc["_matched_by"] = "domain"
            return doc

    return None


# =========================
# Builders
# =========================
def build_result_from_analysis(doc: dict, requested_url: str) -> dict:
    normalized_url = doc.get("normalized_url") or normalize_url(requested_url)
    domain = doc.get("domain") or extract_domain(normalized_url)

    return {
        "requested_url": requested_url,
        "normalized_url": normalized_url,
        "domain": domain,
        "risk_level": doc.get("risk_level", 3),
        "risk_label": doc.get("risk_label", "未知"),
        "data_status": doc.get("data_status", "recorded"),
        "data_status_label": doc.get("data_status_label", "已收錄"),
        "short_reason": doc.get("short_reason", "資料已存在於分析資料庫"),
        "detailed_reason": doc.get("detailed_reason", doc.get("short_reason", "資料已存在於分析資料庫")),
        "risk_factors": doc.get("risk_factors", []),
        "suggested_actions": doc.get("suggested_actions", []),
        "analysis_metadata": doc.get("analysis_metadata", {
            "source": ANALYSIS_COLLECTION_NAME
        }),
        "analysis_time": doc.get("analysis_time", utc_now_iso()),
        "detail_available": True,
        "match_scope": doc.get("_match_scope", "url"),
        "matched_by": doc.get("_matched_by", "normalized_url"),
        "raw_label": doc.get("raw_label")
    }


def build_result_from_legacy(doc: dict, requested_url: str) -> dict:
    normalized_url = normalize_url(requested_url)
    label = doc.get("label", "Unknown")
    mapped = map_label_to_risk(label)

    return {
        "requested_url": requested_url,
        "normalized_url": normalized_url,
        "domain": extract_domain(normalized_url),
        "risk_level": mapped["risk_level"],
        "risk_label": mapped["risk_label"],
        "data_status": "recorded",
        "data_status_label": "已標記",
        "short_reason": mapped["short_reason"],
        "detailed_reason": f"此 URL 在 {URLS_COLLECTION_NAME} collection 中找到，label 為 {label}。",
        "risk_factors": [],
        "suggested_actions": [],
        "analysis_metadata": {
            "source": URLS_COLLECTION_NAME
        },
        "analysis_time": utc_now_iso(),
        "detail_available": False,
        "match_scope": "url",
        "matched_by": "legacy_url",
        "raw_label": label
    }


def build_result_not_found(requested_url: str) -> dict:
    normalized_url = normalize_url(requested_url)

    return {
        "requested_url": requested_url,
        "normalized_url": normalized_url,
        "domain": extract_domain(normalized_url),
        "risk_level": 3,
        "risk_label": "未知",
        "data_status": "not_recorded",
        "data_status_label": "未標記",
        "short_reason": "資料庫中沒有這個 URL 的標記結果。",
        "detailed_reason": f"此 URL 目前不在 {URLS_COLLECTION_NAME}，也不在 {ANALYSIS_COLLECTION_NAME} 中。",
        "risk_factors": [],
        "suggested_actions": [
            "請人工確認網站來源",
            "可將此 URL 加入後續分析流程"
        ],
        "analysis_metadata": {
            "source": "none"
        },
        "analysis_time": utc_now_iso(),
        "detail_available": False,
        "match_scope": "none",
        "matched_by": "none",
        "raw_label": None
    }


# =========================
# Service-ish helpers
# =========================
def resolve_url_result(url: str) -> dict:
    analysis_doc = find_analysis_doc(url)
    if analysis_doc:
        return build_result_from_analysis(analysis_doc, url)

    legacy_doc = find_legacy_url_doc(url)
    if legacy_doc:
        return build_result_from_legacy(legacy_doc, url)

    return build_result_not_found(url)


def build_batch_label_map(urls):
    """
    回傳格式維持你目前目標後端想要的 url -> label map。
    若找不到，該 URL 就不放進 map。
    """
    response_map = {}

    if not urls:
        return response_map

    # 先查 legacy urls collection
    normalized_lookup = {normalize_url(url): url for url in urls}

    legacy_docs = list(urls_collection.find({
        "$or": [
            {"url": {"$in": urls}},
            {"url": {"$in": list(normalized_lookup.keys())}}
        ]
    }))

    for doc in legacy_docs:
        stored_url = doc.get("url")
        label = doc.get("label", "Unknown")
        if not stored_url:
            continue

        # 優先用原始輸入 URL 當 key，避免前端取不到
        original_input_url = stored_url if stored_url in urls else normalized_lookup.get(stored_url, stored_url)
        response_map[original_input_url] = label

    # 若 legacy 沒找到，再補查 analysis_collection
    unresolved_urls = [url for url in urls if url not in response_map]
    if unresolved_urls:
        for url in unresolved_urls:
            analysis_doc = find_analysis_doc(url)
            if analysis_doc:
                analysis_label = analysis_doc.get("raw_label") or analysis_doc.get("risk_label") or "Unknown"
                response_map[url] = analysis_label

    return response_map


# =========================
# Routes
# =========================
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "ok": True,
        "message": "backend is running",
        "mongo_db": MONGO_DB_NAME,
        "urls_collection": URLS_COLLECTION_NAME,
        "analysis_collection": ANALYSIS_COLLECTION_NAME
    })


@app.route("/check_urls_batch", methods=["POST"])
def check_urls_batch():
    """
    與你目前目標後端相容的批次查詢：
    input:  {"urls": ["https://a.com", "https://b.com"]}
    output: {"https://a.com": "Real", "https://b.com": "Fake"}
    """
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])

    if not isinstance(urls, list):
        return jsonify({
            "ok": False,
            "error": "urls must be a list"
        }), 400

    response_map = build_batch_label_map(urls)
    return jsonify(response_map)


@app.route("/api/url/check", methods=["POST"])
def check_one_url():
    """
    給 extension tooltip 用的單筆摘要 API
    """
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not url:
        return jsonify({
            "ok": False,
            "error": "Missing url"
        }), 400

    result = resolve_url_result(url)

    return jsonify({
        "ok": True,
        "data": {
            "requested_url": result["requested_url"],
            "normalized_url": result["normalized_url"],
            "domain": result["domain"],
            "risk_level": result["risk_level"],
            "risk_label": result["risk_label"],
            "data_status": result["data_status"],
            "data_status_label": result["data_status_label"],
            "short_reason": result["short_reason"],
            "detail_available": result["detail_available"],
            "match_scope": result["match_scope"],
            "matched_by": result["matched_by"],
            "analysis_time": result["analysis_time"],
            "raw_label": result["raw_label"]
        }
    })


@app.route("/api/url/details", methods=["POST"])
def check_url_details():
    """
    給 side panel 用的詳細資料 API
    若目前只有 legacy label，仍會回最小可用結構。
    """
    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()

    if not url:
        return jsonify({
            "ok": False,
            "error": "Missing url"
        }), 400

    result = resolve_url_result(url)

    return jsonify({
        "ok": True,
        "data": result
    })


@app.route("/save_url", methods=["POST"])
def save_url():
    """
    可選：保留你舊專案可能用到的 URL 紀錄接口
    input: {"urls": ["https://a.com", "https://b.com"]}
    """
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])

    if not isinstance(urls, list):
        return jsonify({
            "ok": False,
            "error": "urls must be a list"
        }), 400

    inserted_count = 0
    now = utc_now_iso()

    for url in urls:
        url = str(url).strip()
        if not url:
            continue

        urls_collection.update_one(
            {"url": url},
            {
                "$setOnInsert": {
                    "url": url,
                    "label": "Unknown",
                    "created_at": now
                }
            },
            upsert=True
        )
        inserted_count += 1

    return jsonify({
        "ok": True,
        "inserted_count": inserted_count
    })


# =========================
# Main
# =========================
if __name__ == "__main__":
    app.run(host=HOST, port=PORT, debug=DEBUG)