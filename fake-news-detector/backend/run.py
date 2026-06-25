import os
import sys
import json
import uuid
import subprocess
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qsl, urlencode, unquote, quote

# =====================================================================
# 計時工具
# =====================================================================
import time
import functools

def timer(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        result = func(*args, **kwargs)
        elapsed = time.time() - t0
        print(f"[TIMER] {func.__name__} 耗時：{elapsed:.2f} 秒")
        return result
    return wrapper


# =========================
# Path Resolution
# =========================
def get_project_root():
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "crawler").is_dir() and (parent / "testing_mbert").is_dir():
            return parent
    return current.parents[4]

PROJECT_ROOT = get_project_root()
CRAWLER_DIR = PROJECT_ROOT / "crawler"
JOBS_DIR = CRAWLER_DIR / "jobs"
EXPORTS_DIR = CRAWLER_DIR / "exports"
PROCESSED_DIR = CRAWLER_DIR / "processed"
RESULTS_DIR = CRAWLER_DIR / "results"
MODEL_SCRIPT_DIR = PROJECT_ROOT / "testing_mbert"
MODEL_PATH = os.getenv("MODEL_PATH", str(MODEL_SCRIPT_DIR))
UTC_PLUS_8 = timezone(timedelta(hours=8))

PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# =====================================================================
# Pipeline Lock（與 run_all.py 共用，防止兩套 pipeline 同時執行）
# =====================================================================
_PIPELINE_LOCK = Path("/tmp/fake_news_pipeline.lock")
_PIPELINE_LOCK_TIMEOUT = 1800  # 30 分鐘後視為過期

sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(MODEL_SCRIPT_DIR))

#try:
import total_preprocess
#except ImportError as e:
    #print(f"[WARN] Failed to import total_preprocess: {e}")

try:
    from testing_mbert.inference import load_model_and_tokenizer, analyze_fake_news
    _inf_model = None
    _inf_tokenizer = None
    _inf_device = None
except ImportError as e:
    #print(f"[WARN] Failed to import inference.py: {e}")
    load_model_and_tokenizer = None
    analyze_fake_news = None
    _inf_model = None
    _inf_tokenizer = None
    _inf_device = None

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
UNKNOWN_URLS_COLLECTION_NAME = os.getenv("UNKNOWN_URLS_COLLECTION_NAME", "unknown_urls")

HOST = os.getenv("FLASK_HOST", "0.0.0.0")
PORT = int(os.getenv("FLASK_PORT", "5050"))
DEBUG = os.getenv("FLASK_DEBUG", "true").lower() == "true"

CRAWLER_WORKER_PY = os.getenv("CRAWLER_WORKER_PY", str(CRAWLER_DIR / "fccna_worker.py"))
CRAWLER_JOBS_DIR = os.getenv("CRAWLER_JOBS_DIR", str(JOBS_DIR))
CRAWLER_EXPORT_DIR = os.getenv("CRAWLER_EXPORT_DIR", str(EXPORTS_DIR))
ENABLE_CRAWLER_TRIGGER = os.getenv("ENABLE_CRAWLER_TRIGGER", "true").lower() == "true"

app = Flask(__name__)
CORS(app)

# 關閉 Flask 預設的每次請求 access log（太吵，正常 200 不需要印）
# 若需要除錯，臨時改成 logging.INFO 即可
import logging
logging.getLogger('werkzeug').setLevel(logging.INFO)

# =========================
# Heartbeat 計數器
# =========================
_request_counter = 0

@app.before_request
def _count_request():
    global _request_counter
    _request_counter += 1

mongo_client = MongoClient(MONGO_URI)
db = mongo_client[MONGO_DB_NAME]
urls_collection = db[URLS_COLLECTION_NAME]
analysis_collection = db[ANALYSIS_COLLECTION_NAME]
unknown_urls_collection = db[UNKNOWN_URLS_COLLECTION_NAME]


# =========================
# Setup
# =========================
def ensure_indexes():
    #try:
        urls_collection.create_index([("url", ASCENDING)], unique=False)
        urls_collection.create_index([("label", ASCENDING)], unique=False)

        analysis_collection.create_index([("normalized_url", ASCENDING)], unique=False)
        analysis_collection.create_index([("requested_url", ASCENDING)], unique=False)
        analysis_collection.create_index([("domain", ASCENDING)], unique=False)

        unknown_urls_collection.create_index([("normalized_url", ASCENDING)], unique=True)
        unknown_urls_collection.create_index([("crawl_status", ASCENDING)], unique=False)
        unknown_urls_collection.create_index([("crawler_pushed", ASCENDING)], unique=False)
        unknown_urls_collection.create_index([("last_seen_at", ASCENDING)], unique=False)
        unknown_urls_collection.create_index([("last_job_id", ASCENDING)], unique=False)
        unknown_urls_collection.create_index([("domain", ASCENDING)], unique=False)
    #except Exception as error:
        #print(f"[WARN] Failed to create indexes: {error}")


ensure_indexes()

def preload_model():
    global _inf_model, _inf_tokenizer, _inf_device
    #print("[INFO] Pre-loading model into memory...")
    #try:
    if load_model_and_tokenizer is None:
        raise RuntimeError("inference.py not imported")
    _inf_model, _inf_tokenizer, _inf_device = load_model_and_tokenizer(MODEL_PATH)
    #print("[INFO] Model pre-loaded successfully.")
    #except Exception as e:
        #print(f"[WARN] Model pre-load failed: {e}")

preload_model()


# =========================
# Utils / Normalizer
# =========================

# 正規化時要去除的 query 參數（行銷追蹤 + 不影響內容的分類參數）
_STRIP_QUERY_PARAMS = {
    # 行銷追蹤
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "igshid", "mc_cid", "mc_eid",
    # CNA 分類/列表參數（不影響文章內容，帶此參數會導致爬蟲抓到列表頁而非文章）
    "topic",
}

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

        # decode 再重新 encode，確保不同編碼格式的同一 URL 對應到同一個 normalized URL
        path = parsed.path or ""
        path = quote(unquote(path), safe="/-._~!$&'()*+,;=:@")
        if path != "/":
            path = path.rstrip("/")

        query_pairs = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in _STRIP_QUERY_PARAMS
        ]
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
    return datetime.now(UTC_PLUS_8).isoformat()


# =========================
# Label / Risk Mapping
# =========================
def map_label_to_risk(label: str):
    """將模型輸出的 confidence_level 對應至前端顯示的風險等級。
    僅支援 inference.py 五種新版 confidence_level。
    """
    normalized = str(label or "").strip().lower()

    if "highly real" in normalized:
        return {"risk_level": 1, "risk_label": "高度可信", "short_reason": "模型判定此文章為高度真實。"}

    if "likely real" in normalized:
        return {"risk_level": 2, "risk_label": "可能可信", "short_reason": "模型判定此文章可能為真實。"}

    if "uncertain" in normalized:
        return {"risk_level": 3, "risk_label": "不確定", "short_reason": "模型對此文章真假判斷不確定。"}

    if "likely fake" in normalized:
        return {"risk_level": 4, "risk_label": "可能假新聞", "short_reason": "模型判定此文章可能為假新聞，建議提高警覺。"}

    if "highly fake" in normalized:
        return {"risk_level": 5, "risk_label": "高度假新聞", "short_reason": "模型判定此文章為高度假新聞，建議避免互動。"}

    return {"risk_level": 3, "risk_label": "未知", "short_reason": "無法識別分類結果，系統尚未完成分析。"}




def find_analysis_doc(url: str):
    normalized_url = normalize_url(url)

    # URL 精確比對，不做 domain fallback。
    # domain fallback 會在同 domain 多篇文章時拿到錯誤文章的分析結果。
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

    return None


# =========================
# Fuzzy Sentence Matching
# =========================
import re as _re
import difflib as _difflib

def fuzzy_match_sentences_to_original(
    selected_sentences_str: str,
    raw_text: str,
    min_similarity: float = 0.30,
    top_k: int = 5,
) -> list[dict]:
    """
    將推論模型輸出的預處理關鍵句（selected_sentences，以 ' | ' 分隔）
    精準定位至原始文章中的對應片段，並回傳字元層級的 start/end offset。

    Pipeline:
    1. 用 BertTokenizerFast 對預處理句分詞（normalize）
    2. 將 raw_text 以標點斷句，保留每句在 raw_text 中的字元 offset
    3. 用 bag-of-tokens 餘弦相似度快速篩選 top-k 候選句
    4. 僅在 top-k 候選句內做 sliding window（窗長 = len(pre_tokens)），
       以 SequenceMatcher 找出最佳匹配片段
    5. 利用 BertTokenizerFast 的 offset_mapping 將 token 視窗起訖
       轉換為候選句內的字元 offset，再加上候選句的全文 offset

    Returns
    -------
    list[dict]
        每個元素對應一個關鍵句：
        {
            "preprocessed":    <模型輸出的預處理句>,
            "matched_text":    <在原文中定位到的片段，門檻不足則為 None>,
            "matched":         <同 matched_text，向後相容>,
            "start_index":     <matched_text 在 raw_text 中的起始字元位置>,
            "end_index":       <matched_text 在 raw_text 中的結束字元位置（不含）>,
            "cos_similarity":  <餘弦相似度（top-k 篩選用）>,
            "span_similarity": <最佳視窗的 SequenceMatcher ratio>,
            "similarity":      <cos 與 span 的平均，作為最終分數>,
        }
    """
    if not selected_sentences_str or not raw_text:
        return []

    preprocessed_sentences = [s.strip() for s in selected_sentences_str.split(" | ") if s.strip()]
    if not preprocessed_sentences:
        return []

    tokenizer = _inf_tokenizer  # noqa: F821

    # ── 工具函數 ────────────────────────────────────────────────────

    def _tokenize(text: str) -> list[str]:
        """小寫後用 BertTokenizerFast 做 WordPiece 分詞（與推論一致）。"""
        lowered = text.lower()
        if tokenizer is not None:
            try:
                return tokenizer.tokenize(lowered)
            except Exception:
                pass
        return lowered.split()

    def _tokenize_with_offsets(text: str) -> tuple[list[str], list[tuple[int, int]]]:
        """
        分詞並取得 token → 字元 offset 映射。
        回傳 (tokens, offset_mapping)，offset_mapping[i] = (char_start, char_end)
        均為在 text.lower() 中的位置（小寫不改變字元數）。
        """
        lowered = text.lower()
        if tokenizer is not None:
            try:
                enc = tokenizer(
                    lowered,
                    return_offsets_mapping=True,
                    add_special_tokens=False,
                    truncation=False,
                )
                return enc.tokens(), list(enc["offset_mapping"])
            except Exception:
                pass
        # Fallback：空白分詞並手動計算 offset
        tokens, offsets = [], []
        pos = 0
        for tok in lowered.split():
            start = lowered.find(tok, pos)
            offsets.append((start, start + len(tok)))
            tokens.append(tok)
            pos = start + len(tok)
        return tokens, offsets

    def _cosine_similarity(tokens_a: list[str], tokens_b: list[str]) -> float:
        """Bag-of-tokens 餘弦相似度（用於 top-k 粗篩）。"""
        if not tokens_a or not tokens_b:
            return 0.0
        from collections import Counter
        fa, fb = Counter(tokens_a), Counter(tokens_b)
        common = set(fa) & set(fb)
        if not common:
            return 0.0
        dot    = sum(fa[k] * fb[k] for k in common)
        norm_a = sum(v * v for v in fa.values()) ** 0.5
        norm_b = sum(v * v for v in fb.values()) ** 0.5
        return dot / (norm_a * norm_b) if (norm_a and norm_b) else 0.0

    # ── Step 2: raw_text 斷句，保留字元 offset ──────────────────────

    def _split_with_offsets(text: str, min_len: int = 3) -> list[dict]:
        """
        以標點斷句，回傳 [{"text", "start", "end"}, ...]。
        start / end 為 stripped 句段在 text 中的字元 offset。
        min_len 設小一點（不自行過濾，交由 _merge_short_segments 处理）。
        """
        boundaries = [0] + [m.end() for m in _re.finditer(r'(?<=[.!?,。？！，\n])\s*', text)]
        if boundaries[-1] < len(text):
            boundaries.append(len(text))

        segments = []
        for i in range(len(boundaries) - 1):
            seg_raw  = text[boundaries[i]: boundaries[i + 1]]
            stripped = seg_raw.strip()
            if len(stripped) < min_len:
                continue
            lstrip = len(seg_raw) - len(seg_raw.lstrip())
            rstrip = len(seg_raw) - len(seg_raw.rstrip())
            segments.append({
                "text":  stripped,
                "start": boundaries[i] + lstrip,
                "end":   boundaries[i + 1] - rstrip,
            })
        return segments

    def _merge_short_segments(
        base_segs:   list[dict],
        base_tokens: list[list[str]],
        pre_len:     int,
    ) -> list[tuple[list[str], dict]]:
        """
        將過短的相鄰 segments 合併，直到合併後的 token 數 >= pre_len。

        合併規則：
          - 對每個 segment，若其 token 數 < pre_len（即短於 selected_sentence），
            則持續往後合併下一個相鄰 segment（s1 + s2, s1 + s2 + s3, ...），
            直到合併後 token 數 >= pre_len 或超過 pre_len * 2 為止
          - 合併後直接累積 token 列表，不需重新分詞
          - 保證進入 sliding window 的候選句長度 >= pre_len

        Returns: [(merged_tokens, merged_seg_dict), ...]
        """
        if not base_segs:
            return []

        min_toks = pre_len              # 合併直到 token 數 >= selected_sentence 長度
        max_toks = int(pre_len * 2.0)  # 上限：避免合併過多導致 sliding window 太慢

        result: list[tuple[list[str], dict]] = []
        i = 0
        n = len(base_segs)

        while i < n:
            grp_toks  = list(base_tokens[i])
            grp_start = base_segs[i]["start"]
            grp_end   = base_segs[i]["end"]
            i        += 1

            # 若當前組太短，且合併後不會超長，則往後合併
            while (
                len(grp_toks) < min_toks
                and i < n
                and len(grp_toks) + len(base_tokens[i]) <= max_toks
            ):
                grp_toks += base_tokens[i]
                grp_end   = base_segs[i]["end"]
                i        += 1

            grp_text = raw_text[grp_start:grp_end].strip()
            if grp_text:
                result.append((
                    grp_toks,
                    {"text": grp_text, "start": grp_start, "end": grp_end},
                ))

        return result

    # ── Step 4: 候選句內 sliding window 精確定位 ────────────────────

    def _best_span_in_candidate(pre_tokens: list[str], cand_seg: dict) -> dict:
        """
        在單一候選句內做動態 sliding window，
        找出與 pre_tokens 最接近的 token 視窗，並透過 offset_mapping
        轉換為 raw_text 全文字元 offset。

        相似度計算：純 Normalized Levenshtein
          span_sim = 1 - edit_dist / max(len_pre, len_window)
          直接測量最小 token 編輯操作數，對替換、插入、刪除均敏感。
        """
        cand_text         = cand_seg["text"]
        cand_global_start = cand_seg["start"]
        cand_tokens, offsets = _tokenize_with_offsets(cand_text)
        pre_len  = len(pre_tokens)
        cand_len = len(cand_tokens)

        # ── Levenshtein 相似度（Wagner-Fischer DP，token 序列） ──────
        def _lev_sim(a: list[str], b: list[str]) -> float:
            """Normalized Levenshtein：1 - edit_dist / max(|a|, |b|)。"""
            la, lb = len(a), len(b)
            if la == 0 and lb == 0: return 1.0
            if la == 0 or lb == 0:  return 0.0
            prev = list(range(lb + 1))
            for tok_a in a:
                curr = [prev[0] + 1] + [0] * lb
                for j, tok_b in enumerate(b, 1):
                    curr[j] = prev[j - 1] if tok_a == tok_b else 1 + min(prev[j], curr[j - 1], prev[j - 1])
                prev = curr
            return 1.0 - prev[lb] / max(la, lb)

        # 候選句 token 數 <= pre_len：理論上經 _merge_short_segments 後不應發生，
        # 此分支為安全網（例如 max_toks 限制導致合併提前停止）。
        # 直接以整句做比對，不做滑動視窗。
        if cand_len == 0 or cand_len <= pre_len:
            sim = _lev_sim(pre_tokens, cand_tokens)
            return {
                "matched_text":    cand_text,
                "start":           cand_global_start,
                "end":             cand_global_start + len(cand_text),
                "span_similarity": round(sim, 4),
            }

        # 動態 Sliding window：視窗大小從 pre_len×0.8 到 pre_len×1.5
        # 原因：預處理句已移除標點，原文段落保留標點，
        # 因此原文對應片段的 token 數通常大於 pre_len。
        # 允許視窗彈性伸縮，可完整覆蓋原文中多出的標點 tokens。
        min_win = max(1,        int(pre_len * 1))
        max_win = min(cand_len, int(pre_len * 1.5))

        best_sim        = -1.0
        best_char_start = offsets[0][0]
        best_char_end   = offsets[min_win - 1][1]

        for w_size in range(min_win, max_win + 1):
            for w in range(cand_len - w_size + 1):
                window = cand_tokens[w: w + w_size]
                sim = _lev_sim(pre_tokens, window)
                if sim > best_sim:
                    best_sim        = sim
                    best_char_start = offsets[w][0]
                    best_char_end   = offsets[w + w_size - 1][1]

        global_start = cand_global_start + best_char_start
        global_end   = cand_global_start + best_char_end
        return {
            "matched_text":    raw_text[global_start:global_end],
            "start":           global_start,
            "end":             global_end,
            "span_similarity": round(best_sim, 4),
        }

    # ── 主流程 ─────────────────────────────────────────────────────

    # Step 2: 全文斷句並預先分詞（base 層，供合併使用）
    base_segments = _split_with_offsets(raw_text)
    if not base_segments:
        return []
    base_seg_tokens = [_tokenize(seg["text"]) for seg in base_segments]

    results = []
    for pre_sent in preprocessed_sentences:
        pre_tokens = _tokenize(pre_sent)
        pre_len    = len(pre_tokens)

        # Step 2b: 依 pre_len 合併短句段，避免 segment 切太細
        merged_candidates = _merge_short_segments(base_segments, base_seg_tokens, pre_len)
        # merged_candidates: [(tokens, seg_dict), ...]

        # Step 3: cosine top-k 粗篩（直接使用合併後已累積的 tokens）
        scored = sorted(
            [(_cosine_similarity(pre_tokens, toks), seg) for toks, seg in merged_candidates],
            key=lambda x: x[0],
            reverse=True,
        )[:top_k]

        # Step 4: sliding window 精確定位
        best_span    = None
        best_cos_sim = scored[0][0] if scored else 0.0
        best_final   = -1.0

        for cos_sim, cand_seg in scored:
            span  = _best_span_in_candidate(pre_tokens, cand_seg)
            final = 0.5 * cos_sim + 0.5 * span["span_similarity"]
            if final > best_final:
                best_final   = final
                best_cos_sim = cos_sim
                best_span    = span

        if best_span is None or best_final < min_similarity:
            results.append({
                "preprocessed":    pre_sent,
                "matched_text":    None,
                "matched":         None,
                "start_index":     None,
                "end_index":       None,
                "cos_similarity":  round(best_cos_sim, 4),
                "span_similarity": 0.0,
                "similarity":      0.0,
            })
        else:
            results.append({
                "preprocessed":    pre_sent,
                "matched_text":    best_span["matched_text"],
                "matched":         best_span["matched_text"],
                "start_index":     best_span["start"],
                "end_index":       best_span["end"],
                "cos_similarity":  round(best_cos_sim, 4),
                "span_similarity": best_span["span_similarity"],
                "similarity":      round(best_final, 4),
            })

    return results


# =========================
# Builders
# =========================
def pick_content_fields(doc: dict) -> dict:
    raw_content = doc.get("raw_content") or ""
    content = (
        raw_content
        or doc.get("content")
        or doc.get("cleaned_content")
        or doc.get("article_content")
        or doc.get("text")
        or ""
    )
    cleaned_content = doc.get("cleaned_content") or content

    return {
        "raw_content": raw_content,
        "content": content,
        "cleaned_content": cleaned_content
    }


def build_result_from_analysis(doc: dict, requested_url: str) -> dict:
    normalized_url = doc.get("normalized_url") or normalize_url(requested_url)
    domain = doc.get("domain") or extract_domain(normalized_url)

    # url_analyses 存的是 raw_label / confidence_level，
    # 但不一定有 risk_level / risk_label / short_reason，
    # 所以從 map_label_to_risk 補齊
    raw_label = doc.get("raw_label") or doc.get("confidence_level") or ""
    mapped = map_label_to_risk(raw_label) if raw_label else {}

    risk_level   = doc.get("risk_level")   or mapped.get("risk_level",  3)
    risk_label   = doc.get("risk_label")   or mapped.get("risk_label",  "未知")
    short_reason = doc.get("short_reason") or mapped.get("short_reason", "資料已存在於分析資料庫")

    return {
        "requested_url": requested_url,
        "normalized_url": normalized_url,
        "domain": domain,
        "risk_level": risk_level,
        "risk_label": risk_label,
        "data_status": doc.get("data_status", "recorded"),
        "data_status_label": doc.get("data_status_label", "已收錄"),
        "short_reason": short_reason,
        "detailed_reason": doc.get("detailed_reason", short_reason),
        "risk_factors": doc.get("risk_factors", []),
        "suggested_actions": doc.get("suggested_actions", []),
        **pick_content_fields(doc),
        "selected_sentences": doc.get("selected_sentences", []),   # ← 補上
        "selected_scores": doc.get("selected_scores", []),         # ← 補上
        "matched_sentences": doc.get("matched_sentences", []),     # ← 模糊比對結果
        "analysis_metadata": doc.get("analysis_metadata", {
            "source": ANALYSIS_COLLECTION_NAME
        }),
        "analysis_time": doc.get("analysis_time", utc_now_iso()),
        "detail_available": True,
        "match_scope": doc.get("_match_scope", "url"),
        "matched_by": doc.get("_matched_by", "normalized_url"),
        "raw_label": raw_label or None,
        "title": doc.get("title", "")
    }



def build_result_not_found(requested_url: str) -> dict:
    normalized_url = normalize_url(requested_url)

    unknown_doc = unknown_urls_collection.find_one({"normalized_url": normalized_url})
    crawl_status = unknown_doc.get("crawl_status") if unknown_doc else None

    # 爬蟲仍在進行中 → 讓前端繼續輪詢
    if crawl_status in ["queued", "crawling", "processing"]:
        return {
            "requested_url": requested_url,
            "normalized_url": normalized_url,
            "domain": extract_domain(normalized_url),
            "risk_level": 3,
            "risk_label": "分析中",
            "data_status": "processing",
            "data_status_label": "處理中",
            "short_reason": "系統正在背景爬取並分析此網頁，請稍候...",
            "detailed_reason": "此 URL 已進入背景分析排程，請稍後重新確認。",
            "risk_factors": [],
            "suggested_actions": ["請稍候片刻等待分析完成"],
            "analysis_metadata": {"source": "queued"},
            "analysis_time": utc_now_iso(),
            "detail_available": False,
            "match_scope": "none",
            "matched_by": "none",
            "raw_label": None
        }

    # 爬蟲已完成（含部分失敗）→ 嘗試從 analysis_collection 取得結果
    if crawl_status in ["content_saved", "partial_done", "analysis_done", "failed"]:
        analysis_doc = find_analysis_doc(requested_url)
        if analysis_doc:
            return build_result_from_analysis(analysis_doc, requested_url)
        # 爬取或分析失敗，沒有結果
        return {
            "requested_url": requested_url,
            "normalized_url": normalized_url,
            "domain": extract_domain(normalized_url),
            "risk_level": 3,
            "risk_label": "無法分析",
            "data_status": "analysis_failed",
            "data_status_label": "分析失敗",
            "short_reason": "此 URL 已嘗試爬取但未能取得有效內容，無法完成分析。",
            "detailed_reason": f"爬蟲狀態：{crawl_status}，但 {ANALYSIS_COLLECTION_NAME} 中無對應結果。",
            "risk_factors": [],
            "suggested_actions": ["請確認此 URL 是否可正常存取"],
            "analysis_metadata": {"source": "crawler_failed"},
            "analysis_time": utc_now_iso(),
            "detail_available": False,
            "match_scope": "none",
            "matched_by": "none",
            "raw_label": None
        }

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
# Unknown URL helpers
# =========================
def upsert_unknown_urls(urls, source="check_urls_batch"):
    now = utc_now_iso()
    pushable_urls = []

    for url in urls:
        raw_url = str(url).strip()
        if not raw_url:
            continue

        normalized_url = normalize_url(raw_url)
        domain = extract_domain(normalized_url)

        existing = unknown_urls_collection.find_one(
            {"normalized_url": normalized_url},
            {"_id": 0, "crawler_pushed": 1}
        )

        unknown_urls_collection.update_one(
            {"normalized_url": normalized_url},
            {
                "$set": {
                    "url": raw_url,
                    "normalized_url": normalized_url,
                    "domain": domain,
                    "last_seen_at": now,
                    "last_source": source,
                    "crawl_status": "pending"
                },
                "$setOnInsert": {
                    "first_seen_at": now,
                    "crawler_pushed": False
                },
                "$inc": {
                    "seen_count": 1
                }
            },
            upsert=True
        )

        if not existing or existing.get("crawler_pushed") is not True:
            pushable_urls.append(raw_url)

    return pushable_urls


def is_supported_url_for_worker(url: str) -> bool:
    """
    判斷 URL 是否屬於爬蟲支援的目標網站。
    只要 domain 在支援清單或符合 suffix 規則就放行。
    導覽列、首頁、搜尋頁等非文章 URL 由
    fccna_worker 的 prepare_urls / is_valid_article_url 負責二次過濾。
    """
    SUPPORTED_DOMAINS = {
        "www.chinatimes.com",
        "www.cna.com.tw", "cna.com.tw",
        "www.ettoday.net",
        "news.tvbs.com.tw",
        "ctinews.com",
        "news.ebc.net.tw",
        "www.setn.com",

        "news.cts.com.tw",

        "news.ttv.com.tw",
        # Yahoo 新聞
        "tw.news.yahoo.com",
        # Yahoo 股市新聞
        "tw.stock.yahoo.com",
        # 國外英語媒體（右翼/另類）
        "www.breitbart.com",
        "www.zerohedge.com",
        "nypost.com",
        "www.newsmax.com",

        # 台灣內容農場（與 crawler.py 的 TARGET_SITES 同步）
        "www.teepr.com",
        "kknews.cc",
        "www.mission-tw.com",    # 密訊（域名會輪換，前身 pplomo.best）
        "www.zanliv.com",
        "toments.com",           # 觸電網（中文內容農場）

        # 英文內容農場 / 另類媒體
        "www.upworthy.com",
        "www.distractify.com",
        "www.thethings.com",
        "www.naturalnews.com",
        "beforeitsnews.com",
    }
    # 自由時報使用 suffix 匹配，支援所有 subdomain（news/ec/ent/sports/talk 等）
    SUPPORTED_SUFFIXES = {
        ".ltn.com.tw",
    }
    try:
        normalized = normalize_url(url)
        parsed = urlparse(normalized)
        host = (parsed.netloc or "").lower()
        if host in SUPPORTED_DOMAINS:
            # 密訊 mission-tw：只放行文章頁 /article/分類/數字，
            # 分類/導覽/列表/搜尋頁（/main_category、/category、/articlelist、/search、首頁…）不排進背景爬取
            if host == "www.mission-tw.com":
                return bool(_re.match(r"^/article/[^/]+/\d+", parsed.path or ""))
            return True
        # suffix 匹配
        for suffix in SUPPORTED_SUFFIXES:
            if host.endswith(suffix) or host == suffix.lstrip("."):
                return True
        return False
    except Exception:
        return False


def build_worker_job_payload(urls):
    return {
        "job_id": datetime.now(UTC_PLUS_8).strftime("%Y%m%d_%H%M%S") + "_" + str(uuid.uuid4())[:8],
        "source": "backend_unknown_urls",
        "created_at": utc_now_iso(),
        "urls": urls,
    }


def write_worker_job_file(payload):
    jobs_dir = Path(CRAWLER_JOBS_DIR)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    job_path = jobs_dir / f"{payload['job_id']}.json"
    with job_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    return job_path


def trigger_fccna_worker(job_path: Path):
    Path(CRAWLER_EXPORT_DIR).mkdir(parents=True, exist_ok=True)

    process = subprocess.Popen(
        [
            sys.executable,
            CRAWLER_WORKER_PY,
            "--job",
            str(job_path),
            "--export-dir",
            CRAWLER_EXPORT_DIR,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return process.pid


def queue_urls_for_worker(urls):
    """
    測試串接用：
    暫時不做網站分流，所有 unknown URLs 都直接送進 fccna_worker.py。
    """
    start_time = time.perf_counter()
    print(f"開始處理：{len(urls)} 筆網址")

    if not ENABLE_CRAWLER_TRIGGER:
        return {
            "queued_count": 0,
            "job_id": None,
            "pid": None,
            "reason": "crawler trigger disabled"
        }

    # 只送支援網站的 URL 給爬蟲，過濾掉首頁、搜尋頁、社群媒體等無關連結
    worker_urls = [
        str(u).strip() for u in urls
        if str(u).strip() and is_supported_url_for_worker(str(u).strip())
    ]
    if not worker_urls:
        return {
            "queued_count": 0,
            "job_id": None,
            "pid": None,
            "reason": "no urls"
        }

    payload = build_worker_job_payload(worker_urls)
    job_id = payload["job_id"]

    pipeline_jobs[job_id] = {
        "job_id": job_id,
        "status": "processing",
        "stage": "init"
    }

    thread = threading.Thread(target=run_pipeline_background, args=(job_id, worker_urls))
    thread.start()

    normalized_urls = [normalize_url(u) for u in worker_urls]

    unknown_urls_collection.update_many(
        {"normalized_url": {"$in": normalized_urls}},
        {
            "$set": {
                "crawl_status": "queued",
                "crawler_pushed": True,
                "crawler_pushed_at": utc_now_iso(),
                "last_job_id": job_id,
            }
        }
    )
    end_time = time.perf_counter()
    print(f"排程總共花費: {end_time - start_time} 秒")

    return {
        "queued_count": len(worker_urls),
        "job_id": job_id,
        "pid": None,
        "reason": "ok"
    }


# =========================
# Service-ish helpers
# =========================

# 各網站已知的付費牆 / 贊助內容路徑前綴
_PAYWALLED_PATHS = {
    "www.chinatimes.com": {
        "/album/memberarticles": "此為中時新聞網會員付費專區文章，無法爬取內容。",
    },
    "www.zerohedge.com": {
        "/sponsored-post": "此為 ZeroHedge 贊助廣告內容，非新聞文章，不進行分析。",
    },
}

def _check_paywalled(url: str):
    """
    檢查 URL 是否為已知付費牆或贊助內容。
    回傳原因字串；若非付費內容則回傳 None。
    """
    try:
        normalized = normalize_url(url)
        parsed = urlparse(normalized)
        domain = (parsed.netloc or "").lower()
        path_lower = (parsed.path or "").lower()
        for prefix, reason in _PAYWALLED_PATHS.get(domain, {}).items():
            if path_lower.startswith(prefix):
                return reason
    except Exception:
        pass
    return None


def build_result_skipped_non_article(requested_url: str) -> dict:
    """密訊等站的分類／導覽／列表／搜尋／首頁等非文章頁：不爬取、不分析，直接回報跳過。"""
    normalized_url = normalize_url(requested_url)
    return {
        "requested_url": requested_url,
        "normalized_url": normalized_url,
        "domain": extract_domain(normalized_url),
        "risk_level": 3,
        "risk_label": "不分析",
        "data_status": "skipped_non_article",
        "data_status_label": "非文章頁",
        "short_reason": "此為分類／導覽頁，非單篇文章，系統不予爬取與分析。",
        "detailed_reason": "此 URL 屬於分類、導覽列、列表或搜尋頁，不會排入背景爬取。請改用單篇文章網址。",
        "risk_factors": [],
        "suggested_actions": ["請改用單篇文章的網址進行查詢"],
        "analysis_metadata": {"source": "skipped_non_article"},
        "analysis_time": utc_now_iso(),
        "detail_available": False,
        "match_scope": "none",
        "matched_by": "none",
        "raw_label": None,
    }


def resolve_url_result(url: str) -> dict:
    # ── 密訊 mission-tw：分類/導覽/列表/搜尋/首頁等非文章頁 → 不爬不分析 ──
    _norm = normalize_url(url)
    if extract_domain(_norm) == "www.mission-tw.com" \
            and not _re.match(r"^/article/[^/]+/\d+", urlparse(_norm).path or ""):
        return build_result_skipped_non_article(url)
    # ─────────────────────────────────────────────────────────
    # ── 付費牆 / 贊助內容檢查 ─────────────────────────────────
    paywall_reason = _check_paywalled(url)
    if paywall_reason:
        normalized_url = normalize_url(url)
        return {
            "requested_url": url,
            "normalized_url": normalized_url,
            "domain": extract_domain(normalized_url),
            "risk_level": 3,
            "risk_label": "無法分析",
            "data_status": "paywalled",
            "data_status_label": "付費/贊助內容",
            "short_reason": paywall_reason,
            "detailed_reason": paywall_reason,
            "risk_factors": [],
            "suggested_actions": ["請直接前往原始網站閱讀此內容"],
            "analysis_metadata": {"source": "paywalled"},
            "analysis_time": utc_now_iso(),
            "detail_available": False,
            "match_scope": "none",
            "matched_by": "none",
            "raw_label": None,
            "title": "",
        }
    # ─────────────────────────────────────────────────────────

    analysis_doc = find_analysis_doc(url)
    if analysis_doc:
        return build_result_from_analysis(analysis_doc, url)

    return build_result_not_found(url)


def build_batch_label_map(urls):
    """
    回傳格式維持你目前前端想要的 url -> label map。
    若找不到，該 URL 不放進 map。

    額外流程：
    - unknown URL 寫進 unknown_urls
    - 後端目前只分流 CNA URLs 給 fccna_worker.py
    """
    response_map = {}
    still_unknown_urls = []

    if not urls:
        return response_map

    # 查詢 analysis_collection
    for url in urls:
        analysis_doc = find_analysis_doc(url)
        if analysis_doc:
            analysis_label = analysis_doc.get("raw_label") or analysis_doc.get("confidence_level") or "Unknown"
            response_map[url] = analysis_label
        else:
            still_unknown_urls.append(url)

    if still_unknown_urls:
        real_unknown = []
        for url in still_unknown_urls:
            norm = normalize_url(url)
            unknown_doc = unknown_urls_collection.find_one({"normalized_url": norm})
            if unknown_doc:
                status = unknown_doc.get("crawl_status")
                if status in ["queued", "crawling", "processing"]:
                    response_map[url] = "processing"
                elif status in ["content_saved", "partial_done", "analysis_done", "failed"]:
                    response_map[url] = "analysis_failed"
                else:
                    real_unknown.append(url)
            else:
                real_unknown.append(url)

        if real_unknown:
            pushable_urls = upsert_unknown_urls(real_unknown, source="check_urls_batch")
            # 全部 URL 一次送進單一 worker，避免多個 worker 並發打同一網站觸發 rate limit。
            # fccna_worker 內部已有 CRAWL_DELAY_MIN/MAX 控制請求間隔（1.5–3 秒），
            # 單一進程序列爬取不會並發，CNA 不會被爆打。
            # 前端輪詢機制不受影響（每篇爬完分析完即寫入 MongoDB，前端拿到就顯示）。
            if pushable_urls:
                queue_urls_for_worker(pushable_urls)
            for url in real_unknown:
                response_map[url] = "processing"

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
        "analysis_collection": ANALYSIS_COLLECTION_NAME,
        "unknown_urls_collection": UNKNOWN_URLS_COLLECTION_NAME,
        "crawler_worker_py": CRAWLER_WORKER_PY,
        "crawler_jobs_dir": CRAWLER_JOBS_DIR,
        "crawler_export_dir": CRAWLER_EXPORT_DIR,
        "crawler_trigger_enabled": ENABLE_CRAWLER_TRIGGER,
    })


@app.route("/check_urls_batch", methods=["POST"])
def check_urls_batch():
    """
    與你目前前端相容的批次查詢：
    input:  {"urls": ["https://a.com", "https://b.com"]}
    output: {"https://a.com": "Real", "https://b.com": "Fake"}

    查不到的 URL：
    - 不放進 response_map
    - 但會寫入 unknown_urls
    - 目前先只分流 CNA URLs 給 fccna_worker.py
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


@app.route("/api/unknown-urls", methods=["GET"])
def get_unknown_urls():
    limit = request.args.get("limit", default=100, type=int)
    status = request.args.get("status", default="", type=str).strip()

    query = {}
    if status:
        query["crawl_status"] = status

    docs = list(
        unknown_urls_collection
        .find(query, {"_id": 0})
        .sort("last_seen_at", ASCENDING)
        .limit(limit)
    )

    return jsonify({
        "ok": True,
        "data": docs
    })


@app.route("/api/unknown-urls/mark-crawled", methods=["POST"])
def mark_unknown_urls_crawled():
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])

    if not isinstance(urls, list):
        return jsonify({
            "ok": False,
            "error": "urls must be a list"
        }), 400

    normalized_urls = [normalize_url(url) for url in urls if str(url).strip()]

    if not normalized_urls:
        return jsonify({
            "ok": True,
            "updated_count": 0
        })

    result = unknown_urls_collection.update_many(
        {"normalized_url": {"$in": normalized_urls}},
        {
            "$set": {
                "crawl_status": "crawled",
                "crawled_at": utc_now_iso()
            }
        }
    )

    return jsonify({
        "ok": True,
        "updated_count": result.modified_count
    })


# =========================
# Pipeline API (Async)
# =========================
pipeline_jobs = {}


# =========================
# Model Inference Wrapper
# =========================
def analyze_csv_with_inference(cleaned_csv: str, results_json_path: str):
    """
    以 inference.py 的 analyze_fake_news() 逐列推論，
    回傳：
      predictions     : {url: confidence_level_str}   供 pipeline 判斷 label
      prediction_details : {url: full_result_dict}    供 MongoDB 存入完整細節
    """
    import pandas as pd

    if analyze_fake_news is None or _inf_model is None:
        raise RuntimeError("inference module not loaded; check model preload log")

    df = pd.read_csv(cleaned_csv)

    url_col = "url" if "url" in df.columns else "normalized_url"
    text_col = "cleaned_content" if "cleaned_content" in df.columns else "text"

    predictions = {}
    prediction_details = {}

    for _, row in df.iterrows():
        url = row.get(url_col)
        text = row.get(text_col)

        if not url or (hasattr(text, '__class__') and str(type(text)) == "<class 'float'>") or not str(text).strip():
            continue

        try:
            result = analyze_fake_news(
                str(text), _inf_model, _inf_tokenizer, _inf_device
            )
            predictions[url] = result["confidence_level"]
            prediction_details[url] = result
        except Exception as e:
            #print(f"[INFERENCE ERROR] url={url}: {e}")
            predictions[url] = "不確定 (Uncertain)"
            prediction_details[url] = {"confidence_level": "不確定 (Uncertain)", "error": str(e)}

    if results_json_path:
        with open(results_json_path, "w", encoding="utf-8") as f:
            json.dump(prediction_details, f, ensure_ascii=False, indent=2)

    return predictions, prediction_details


def run_pipeline_background(job_id: str, urls: list):
    _t0 = time.perf_counter()
    #print(f"[PIPELINE DEBUG] job_id={job_id}: Background thread started with {len(urls)} URLs.")
    #print(f"[PERF] ① Pipeline 開始 (job_id={job_id})")

    # ── 寫入 lock，通知 run_all.py 在此期間暫停 ──────────────
    try:
        import os as _os_lock
        _PIPELINE_LOCK.write_text(
            json.dumps({
                "pid":        _os_lock.getpid(),
                "job_id":     job_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }),
            encoding="utf-8",
        )
    except Exception as _le:
        print(f"[LOCK] 建立 lock 失敗（不影響執行）：{_le}")
    # ─────────────────────────────────────────────────────────

    try:
        pipeline_jobs[job_id]["status"] = "processing"

        # 1. Crawler
        pipeline_jobs[job_id]["stage"] = "crawler"
        #print(f"[PIPELINE DEBUG] job_id={job_id}: Starting crawler via subprocess...")
        job_payload = build_worker_job_payload(urls)
        job_payload["job_id"] = job_id
        job_path = write_worker_job_file(job_payload)

        _t1 = time.perf_counter()
        print(f"[PERF] ② Job 建立完成，耗時 {_t1 - _t0:.2f} 秒 ({len(urls)} 筆 URL 寫入 job.json)")

        # 爬蟲改為非阻塞（Popen），同時啟動監控 thread 即時處理每筆爬完的 CSV
        export_dir_path = Path(CRAWLER_EXPORT_DIR)
        processed_single_csvs = set()

        def process_single_csv(single_csv: Path):
            """爬完一筆立刻跑前處理 + 模型 + 寫入資料庫"""
            try:
                import pandas as pd
                df = pd.read_csv(single_csv)
                if df.empty:
                    return
                # 只處理成功爬取的
                df = df[df["status"] == "content_saved"]
                if df.empty:
                    return

                single_cleaned = str(PROCESSED_DIR / f"{single_csv.stem}_cleaned.csv")
                total_preprocess.process_csv_file(str(single_csv), single_cleaned)

                preds, details = analyze_csv_with_inference(single_cleaned, None)
                now = utc_now_iso()

                url_col = "url" if "url" in df.columns else "normalized_url"
                text_col = "text" if "text" in df.columns else "cleaned_content"
                title_col = "title"

                # 原始文章（未預處理）內文 map
                raw_content_map = {}
                title_map = {}
                for _, row in df.iterrows():
                    u = str(row.get(url_col) or "").strip()
                    if u:
                        raw_content_map[u] = str(row.get(text_col) or "")
                        title_map[u] = str(row.get(title_col) or "")

                # 預處理後內文 map（從 cleaned CSV 讀取）
                cleaned_content_map = {}
                try:
                    cleaned_df = pd.read_csv(single_cleaned)
                    c_url_col = "url" if "url" in cleaned_df.columns else "normalized_url"
                    c_text_col = "cleaned_content" if "cleaned_content" in cleaned_df.columns else "text"
                    for _, row in cleaned_df.iterrows():
                        u = str(row.get(c_url_col) or "").strip()
                        if u:
                            cleaned_content_map[u] = str(row.get(c_text_col) or "")
                except Exception as e:
                    print(f"[INSTANT WARN] cleaned CSV 讀取失敗，將 fallback 至原文: {e}")

                for url, label in preds.items():
                    normalized = normalize_url(url)
                    domain = extract_domain(normalized)
                    try:
                        risk = map_label_to_risk(label)
                        detail = details.get(url, {})
                        raw_text = raw_content_map.get(url, "")
                        cleaned_text = cleaned_content_map.get(url, raw_text)
                        selected_str = detail.get("selected_sentences") or ""

                        # 模糊比對：將預處理關鍵句映射回原文句段
                        matched = fuzzy_match_sentences_to_original(selected_str, raw_text)

                        analysis_collection.update_one(
                            {"normalized_url": normalized},
                            {"$set": {
                                "requested_url": url,
                                "domain": domain,
                                "raw_label": label,
                                "confidence_level": label,
                                "risk_label": risk["risk_label"],
                                "risk_level": risk["risk_level"],
                                "short_reason": risk["short_reason"],
                                "probability_real": detail.get("probability_real"),
                                "selected_sentences": selected_str,
                                "selected_scores": detail.get("selected_scores"),
                                "matched_sentences": matched,
                                "analysis_time": now,
                                "job_id": job_id,
                                "title": title_map.get(url, ""),
                                "raw_content": raw_text,
                                "content": raw_text,
                                "cleaned_content": cleaned_text,
                            }},
                            upsert=True
                        )
                        unknown_urls_collection.update_one(
                            {"normalized_url": normalized},
                            {"$set": {"crawl_status": "analysis_done", "analysis_completed_at": now}}
                        )
                        print(f"[INSTANT] ✓ {url} → {label} | 模糊比對 {len(matched)} 句完成")
                    except Exception as e:
                        print(f"[INSTANT ERROR] {url}: {e}")
            except Exception as e:
                print(f"[INSTANT PROCESS ERROR] {single_csv}: {e}")

        def watch_exports():
            """監控 exports 資料夾，發現新的個別 CSV 就立刻處理"""
            while not crawler_done.is_set():
                try:
                    for csv_file in export_dir_path.glob(f"{job_id}_*.csv"):
                        if csv_file not in processed_single_csvs:
                            processed_single_csvs.add(csv_file)
                            t = threading.Thread(target=process_single_csv, args=(csv_file,))
                            t.daemon = True
                            t.start()
                except Exception as e:
                    print(f"[WATCH ERROR] {e}")
                time.sleep(1)

        crawler_done = threading.Event()
        watch_thread = threading.Thread(target=watch_exports)
        watch_thread.daemon = True
        watch_thread.start()

        process = subprocess.run(
            [
                sys.executable,
                CRAWLER_WORKER_PY,
                "--job",
                str(job_path),
                "--export-dir",
                CRAWLER_EXPORT_DIR,
            ],
            capture_output=True,
            text=True
        )

        crawler_done.set()
        watch_thread.join(timeout=5)

        # 最後再掃一次，確保沒有漏掉的 CSV
        for csv_file in export_dir_path.glob(f"{job_id}_*.csv"):
            if csv_file not in processed_single_csvs:
                process_single_csv(csv_file)

        if process.returncode != 0:
            raise Exception(f"Crawler failed: {process.stderr}")

        try:
            stdout = process.stdout
            crawler_result = None
            end = stdout.rfind('}')
            if end != -1:
                depth = 0
                start = -1
                for i in range(end, -1, -1):
                    if stdout[i] == '}':
                        depth += 1
                    elif stdout[i] == '{':
                        depth -= 1
                        if depth == 0:
                            start = i
                            break
                if start != -1:
                    json_str = stdout[start:end+1]
                    crawler_result = json.loads(json_str)
            if crawler_result is None:
                raise Exception("No JSON object found in output")
        except Exception as e:
            raise Exception(f"Failed to parse crawler output: {e}. Output was: {process.stdout}")

        raw_csv = crawler_result.get("csv_path")
        _t2 = time.perf_counter()
        print(f"[PERF] ③ 爬蟲完成，耗時 {_t2 - _t1:.2f} 秒")

        # 2. Preprocess（整批 fallback，處理個別 CSV 沒抓到的）
        pipeline_jobs[job_id]["stage"] = "preprocess"
        cleaned_csv = str(PROCESSED_DIR / f"{job_id}_cleaned.csv")
        #print(f"[PIPELINE DEBUG] job_id={job_id}: Starting preprocess.")
        #print(f"[PIPELINE DEBUG] raw_csv={raw_csv}, cleaned_csv={cleaned_csv}")
        try:
            total_preprocess.process_csv_file(raw_csv, cleaned_csv)
            #print(f"[PIPELINE DEBUG] job_id={job_id}: Preprocess completed.")
        except Exception as e:
            #print(f"[PIPELINE ERROR] job_id={job_id}: Preprocess failed: {e}")
            raise e

        _t3 = time.perf_counter()
        print(f"[PERF] ④ 前處理完成，耗時 {_t3 - _t2:.2f} 秒 (cleaned: {cleaned_csv})")

        # 3. Model Inference
        pipeline_jobs[job_id]["stage"] = "model"
        results_json = str(RESULTS_DIR / f"{job_id}_predictions.json")

        # 讀取原始（未預處理）內文
        raw_content_by_url = {}
        cleaned_content_by_url = {}
        title_by_url = {}
        try:
            import pandas as pd
            # 原始爬蟲 CSV（text 欄位）
            raw_df = pd.read_csv(raw_csv)
            raw_url_col = "url" if "url" in raw_df.columns else "normalized_url"
            raw_text_col = "text" if "text" in raw_df.columns else "content"
            for _, row in raw_df.iterrows():
                row_url = str(row.get(raw_url_col) or "").strip()
                if row_url:
                    raw_content_by_url[row_url] = str(row.get(raw_text_col) or "")
                    if "title" in row:
                        title_by_url[row_url] = str(row.get("title") or "")
        except Exception as e:
            print(f"[WARN] Failed to load raw content map: {e}")

        try:
            import pandas as pd
            # 預處理後 CSV（cleaned_content 欄位）
            cleaned_df = pd.read_csv(cleaned_csv)
            url_col = "url" if "url" in cleaned_df.columns else "normalized_url"
            text_col = "cleaned_content" if "cleaned_content" in cleaned_df.columns else "text"
            title_col = "title"
            for _, row in cleaned_df.iterrows():
                row_url = str(row.get(url_col) or "").strip()
                if row_url:
                    cleaned_content_by_url[row_url] = str(row.get(text_col) or "")
                    if title_col in row and row_url not in title_by_url:
                        title_by_url[row_url] = str(row.get(title_col) or "")
        except Exception as e:
            print(f"[WARN] Failed to load cleaned content/title map: {e}")

        #print(f"[PIPELINE DEBUG] job_id={job_id}: Starting model inference.")
        try:
            predictions, prediction_details = analyze_csv_with_inference(cleaned_csv, results_json)
            #print(f"[PIPELINE DEBUG] job_id={job_id}: Model inference completed. Predictions: {predictions}")
        except Exception as e:
            #print(f"[PIPELINE ERROR] job_id={job_id}: Model inference failed: {e}")
            raise e

        _t4 = time.perf_counter()
        print(f"[PERF] ⑤ 模型推論完成，耗時 {_t4 - _t3:.2f} 秒 ({len(predictions)} 筆 URL 已標記)")

        # 4. Save to MongoDB
        pipeline_jobs[job_id]["stage"] = "database"
        now = utc_now_iso()
        inserted_or_updated = 0
        failed = 0

        for url, label in predictions.items():
            normalized = normalize_url(url)
            domain = extract_domain(normalized)
            try:
                risk = map_label_to_risk(label)
                detail = prediction_details.get(url, {})
                raw_text = raw_content_by_url.get(url, "")
                cleaned_text = cleaned_content_by_url.get(url, raw_text)
                selected_str = detail.get("selected_sentences") or ""

                # 模糊比對：將預處理關鍵句映射回原文句段
                matched = fuzzy_match_sentences_to_original(selected_str, raw_text)

                analysis_collection.update_one(
                    {"normalized_url": normalized},
                    {
                        "$set": {
                            "requested_url": url,
                            "domain": domain,
                            "raw_label": label,
                            "confidence_level": label,
                            "risk_label": risk["risk_label"],
                            "risk_level": risk["risk_level"],
                            "short_reason": risk["short_reason"],
                            "probability_real": detail.get("probability_real"),
                            "selected_sentences": selected_str,
                            "selected_scores": detail.get("selected_scores"),
                            "matched_sentences": matched,
                            "analysis_time": now,
                            "source_csv": raw_csv,
                            "cleaned_csv": cleaned_csv,
                            "job_id": job_id,
                            "title": title_by_url.get(url, ""),
                            "raw_content": raw_text,
                            "content": raw_text,
                            "cleaned_content": cleaned_text,
                        }
                    },
                    upsert=True
                )
                urls_collection.update_one(
                    {"url": url},
                    {
                        "$set": {
                            "label": label,
                            "raw_label": label,
                            "confidence_level": label,
                            "risk_label": risk["risk_label"],
                            "risk_level": risk["risk_level"],
                            "short_reason": risk["short_reason"],
                            "probability_real": detail.get("probability_real"),
                            "created_at": now,
                            "analysis_time": now,
                            "raw_content": raw_text,
                            "content": raw_text,
                            "cleaned_content": cleaned_text,
                            "selected_sentences": selected_str,
                            "selected_scores": detail.get("selected_scores"),
                            "matched_sentences": matched,
                            "title": title_by_url.get(url, ""),
                            "job_id": job_id,
                            "normalized_url": normalized
                        }
                    },
                    upsert=True
                )
                inserted_or_updated += 2
            except Exception as e:
                print(f"[ERROR] Failed to save {url} to mongo: {e}")
                failed += 1

        # 回寫 unknown_urls_collection：標記已完成分析
        for _url in urls:
            _norm = normalize_url(_url)
            if _norm:
                unknown_urls_collection.update_one(
                    {"normalized_url": _norm},
                    {"$set": {"crawl_status": "analysis_done", "analysis_completed_at": now}}
                )

        pipeline_jobs[job_id].update({
            "status": "completed",
            "raw_csv": raw_csv,
            "cleaned_csv": cleaned_csv,
            "result_file": results_json,
            "results": predictions,
            "database": {
                "inserted_or_updated": inserted_or_updated,
                "failed": failed
            }
        })
        _t5 = time.perf_counter()
        print(f"[PERF] Pipeline 全部完成，總耗時 {_t5 - _t0:.2f} 秒 (job_id={job_id})")


        # ── 清理暫存檔案 ──────────────────────────────────────
        # 資料已寫入 MongoDB，jobs/exports/processed/results 的檔案都是暫存

        try:
            import glob, os
            patterns = [
                str(JOBS_DIR / f"{job_id}*.json"),
                str(EXPORTS_DIR / f"{job_id}*.csv"),
                str(EXPORTS_DIR / f"{job_id}*.jsonl"),
                str(PROCESSED_DIR / f"{job_id}*.csv"),
                str(RESULTS_DIR / f"{job_id}*.json"),
            ]
            
            removed = 0
            for pattern in patterns:
                for f in glob.glob(pattern):
                    try:
                        os.remove(f)
                        removed += 1
                    except Exception:
                        pass
            print(f"[CLEANUP] job_id={job_id} 清理完成，刪除 {removed} 個暫存檔")
        except Exception as e:
            print(f"[CLEANUP WARN] job_id={job_id} 清理失敗（不影響結果）：{e}")
        # ─────────────────────────────────────────────────────

    except Exception as e:
        #print(f"[PIPELINE FATAL ERROR] job_id={job_id}: Pipeline failed at stage '{pipeline_jobs[job_id].get('stage')}': {e}")
        pipeline_jobs[job_id]["status"] = "failed"
        pipeline_jobs[job_id]["error"] = str(e)
    finally:
        # ── 釋放 lock ─────────────────────────────────────────
        try:
            if _PIPELINE_LOCK.exists():
                _PIPELINE_LOCK.unlink()
                print(f"[LOCK] Pipeline lock 已釋放 (job_id={job_id})")
        except Exception as _le:
            print(f"[LOCK] 釋放 lock 失敗：{_le}")
        # ──────────────────────────────────────────────────────


@app.route("/api/pipeline/run", methods=["POST"])
def pipeline_run():
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])

    if not isinstance(urls, list) or not urls:
        return jsonify({"success": False, "error": "urls must be a non-empty list"}), 400

    job_id = datetime.now(UTC_PLUS_8).strftime("%Y%m%d_%H%M%S") + "_" + str(uuid.uuid4())[:8]

    pipeline_jobs[job_id] = {
        "job_id": job_id,
        "status": "processing",
        "stage": "init"
    }

    thread = threading.Thread(target=run_pipeline_background, args=(job_id, urls))
    thread.start()

    return jsonify({
        "success": True,
        "job_id": job_id,
        "status": "processing",
        "message": "Pipeline started in background."
    })


@app.route("/api/pipeline/status/<job_id>", methods=["GET"])
def pipeline_status(job_id):
    job = pipeline_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "job not found"}), 404

    if job["status"] == "completed":
        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "completed",
            "raw_csv": job.get("raw_csv"),
            "cleaned_csv": job.get("cleaned_csv"),
            "result_file": job.get("result_file"),
            "results": job.get("results"),
            "database": job.get("database")
        })
    elif job["status"] == "failed":
        return jsonify({
            "success": False,
            "job_id": job_id,
            "status": "failed",
            "stage": job.get("stage"),
            "error": job.get("error")
        })
    else:
        return jsonify({
            "success": True,
            "job_id": job_id,
            "status": "processing",
            "stage": job.get("stage")
        })


if __name__ == "__main__":
    # 啟動 heartbeat thread
    def _heartbeat():
        while True:
            time.sleep(5)
            now = datetime.now(UTC_PLUS_8).strftime("%H:%M:%S")
            #print(f"[HEARTBEAT] {now} | 服務運作中 | 已處理請求：{_request_counter} 次")

    hb = threading.Thread(target=_heartbeat, daemon=True)
    hb.start()

    app.run(host=HOST, port=PORT, debug=DEBUG, use_reloader=True, threaded=True)