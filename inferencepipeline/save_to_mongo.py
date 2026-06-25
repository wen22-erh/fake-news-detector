# -*- coding: utf-8 -*-
"""
save_to_mongo.py
將模型推論結果存入 fake_news_detector：
  - url_analyses（upsert by normalized_url）
  - urls        （upsert by url）

欄位對齊 run.py 的 run_pipeline_background() 寫法。
"""

import difflib as _difflib
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, unquote, quote
from pymongo import MongoClient
import re as _re


# =============================================================
# 模糊比對（對齊最新版 run.py fuzzy_match_sentences_to_original）
# =============================================================
def fuzzy_match_sentences_to_original(
    selected_sentences_str: str,
    raw_text: str,
    tokenizer=None,          # BertTokenizerFast，從 run_all.py 傳入
    min_similarity: float = 0.30,
    top_k: int = 5,
) -> list:
    """
    將推論模型輸出的預處理關鍵句（selected_sentences，以 ' | ' 分隔）
    精準定位至原始文章中的對應片段，並回傳字元層級的 start/end offset。

    Pipeline:
    1. 用 BertTokenizerFast 對預處理句分詞（normalize）
    2. 將 raw_text 以標點斷句，保留每句在 raw_text 中的字元 offset
    3. 合併過短的相鄰 segment，確保候選句長度 >= pre_len
    4. 用 bag-of-tokens 餘弦相似度快速篩選 top-k 候選句
    5. 僅在 top-k 候選句內做 sliding window（Normalized Levenshtein）
       找出最佳匹配片段，並回傳字元層級 start/end offset
    """
    if not selected_sentences_str or not raw_text:
        return []

    preprocessed_sentences = [s.strip() for s in selected_sentences_str.split(" | ") if s.strip()]
    if not preprocessed_sentences:
        return []

    # ── 工具函數 ────────────────────────────────────────────────────

    def _tokenize(text: str) -> list:
        lowered = text.lower()
        if tokenizer is not None:
            try:
                return tokenizer.tokenize(lowered)
            except Exception:
                pass
        return lowered.split()

    def _tokenize_with_offsets(text: str):
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
        tokens, offsets = [], []
        pos = 0
        for tok in lowered.split():
            start = lowered.find(tok, pos)
            offsets.append((start, start + len(tok)))
            tokens.append(tok)
            pos = start + len(tok)
        return tokens, offsets

    def _cosine_similarity(tokens_a: list, tokens_b: list) -> float:
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

    def _split_with_offsets(text: str, min_len: int = 3) -> list:
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

    # ── Step 2b: 合併過短的相鄰 segment ────────────────────────────

    def _merge_short_segments(base_segs, base_tokens, pre_len):
        if not base_segs:
            return []
        min_toks = pre_len
        max_toks = int(pre_len * 2.0)
        result = []
        i, n = 0, len(base_segs)
        while i < n:
            grp_toks  = list(base_tokens[i])
            grp_start = base_segs[i]["start"]
            grp_end   = base_segs[i]["end"]
            i += 1
            while (
                len(grp_toks) < min_toks
                and i < n
                and len(grp_toks) + len(base_tokens[i]) <= max_toks
            ):
                grp_toks += base_tokens[i]
                grp_end   = base_segs[i]["end"]
                i += 1
            grp_text = raw_text[grp_start:grp_end].strip()
            if grp_text:
                result.append((
                    grp_toks,
                    {"text": grp_text, "start": grp_start, "end": grp_end},
                ))
        return result

    # ── Step 4: 候選句內 sliding window 精確定位 ────────────────────

    def _best_span_in_candidate(pre_tokens, cand_seg):
        cand_text         = cand_seg["text"]
        cand_global_start = cand_seg["start"]
        cand_tokens, offsets = _tokenize_with_offsets(cand_text)
        pre_len  = len(pre_tokens)
        cand_len = len(cand_tokens)

        def _lev_sim(a, b):
            la, lb = len(a), len(b)
            if la == 0 and lb == 0: return 1.0
            if la == 0 or lb == 0:  return 0.0
            prev = list(range(lb + 1))
            for tok_a in a:
                curr = [prev[0] + 1] + [0] * lb
                for j, tok_b in enumerate(b, 1):
                    curr[j] = prev[j-1] if tok_a == tok_b else 1 + min(prev[j], curr[j-1], prev[j-1])
                prev = curr
            return 1.0 - prev[lb] / max(la, lb)

        if cand_len == 0 or cand_len <= pre_len:
            sim = _lev_sim(pre_tokens, cand_tokens)
            return {
                "matched_text":    cand_text,
                "start":           cand_global_start,
                "end":             cand_global_start + len(cand_text),
                "span_similarity": round(sim, 4),
            }

        min_win = max(1,        int(pre_len * 1.0))
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

    base_segments   = _split_with_offsets(raw_text)
    if not base_segments:
        return []
    base_seg_tokens = [_tokenize(seg["text"]) for seg in base_segments]

    results = []
    for pre_sent in preprocessed_sentences:
        pre_tokens = _tokenize(pre_sent)
        pre_len    = len(pre_tokens)

        merged_candidates = _merge_short_segments(base_segments, base_seg_tokens, pre_len)

        scored = sorted(
            [(_cosine_similarity(pre_tokens, toks), seg) for toks, seg in merged_candidates],
            key=lambda x: x[0],
            reverse=True,
        )[:top_k]

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
        path   = quote(unquote(parsed.path or ""), safe="/-._~!$&'()*+,;=:@")
        if path != "/":
            path = path.rstrip("/")
        q_pairs = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in {
                "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                "gclid", "fbclid", "igshid", "mc_cid", "mc_eid", "topic",
            }
        ]
        sorted_query = urlencode(sorted(q_pairs), doseq=True)
        normalized   = f"{scheme}://{netloc}{path}"
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


def map_label_to_risk(label: str) -> dict:
    n = str(label or "").strip().lower()

    if "highly real" in n:
        return {"risk_level": 1, "risk_label": "高度可信",
                "short_reason": "模型判定此文章為高度真實。"}
    if "likely real" in n:
        return {"risk_level": 2, "risk_label": "可能可信",
                "short_reason": "模型判定此文章可能為真實。"}
    if "uncertain" in n or "不確定" in n:
        return {"risk_level": 3, "risk_label": "不確定",
                "short_reason": "模型對此文章真假判斷不確定。"}
    if "likely fake" in n:
        return {"risk_level": 4, "risk_label": "可能假新聞",
                "short_reason": "模型判定此文章可能為假新聞，建議提高警覺。"}
    if "highly fake" in n:
        return {"risk_level": 5, "risk_label": "高度假新聞",
                "short_reason": "模型判定此文章為高度假新聞，建議避免互動。"}

    return {"risk_level": 3, "risk_label": "不確定",
            "short_reason": "模型判定結果無法對應已知風險等級。"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================
# 主寫入函式
# =============================================================
def save_results_to_mongo(
    mongo_uri: str,
    db_name: str,
    predictions: dict,
    prediction_details: dict,
    raw_content_by_url: dict,
    cleaned_content_by_url: dict,
    title_by_url: dict,
    job_id: str = "batch",
    urls_collection_name: str = "urls",
    analysis_collection_name: str = "url_analyses",
    source_csv: str = "",
    cleaned_csv: str = "",
    tokenizer=None,          # BertTokenizerFast，從 run_all.py 傳入
) -> dict:
    """
    將模型推論結果寫入 fake_news_detector.url_analyses + fake_news_detector.urls。

    Parameters
    ----------
    mongo_uri               : MongoDB 連線字串
    db_name                 : 目標 DB（fake_news_detector）
    predictions             : { url → confidence_level }
    prediction_details      : { url → { confidence_level, probability_real,
                                        selected_sentences, selected_scores, ... } }
    raw_content_by_url      : { url → 原始爬蟲文字 }
    cleaned_content_by_url  : { url → 預處理後文字 }
    title_by_url            : { url → 標題 }
    job_id                  : 這批 job 的識別 ID
    urls_collection_name    : 通常是 "urls"
    analysis_collection_name: 通常是 "url_analyses"

    Returns
    -------
    dict : { "inserted_or_updated": int, "failed": int }
    """

    print("========== save_to_mongo.py 開始 ==========")

    client = MongoClient(mongo_uri)
    db = client[db_name]
    analysis_col = db[analysis_collection_name]
    urls_col     = db[urls_collection_name]

    now = utc_now_iso()
    inserted_or_updated = 0
    failed = 0

    for url, label in predictions.items():
        normalized = normalize_url(url)
        domain     = extract_domain(normalized)

        try:
            risk          = map_label_to_risk(label)
            detail        = prediction_details.get(url, {})
            raw_text      = raw_content_by_url.get(url, "")
            cleaned_text  = cleaned_content_by_url.get(url, raw_text)
            title         = title_by_url.get(url, "")
            selected_str    = detail.get("selected_sentences") or ""
            selected_scores = detail.get("selected_scores")
            prob_real       = detail.get("probability_real")

            # 模糊比對：將預處理關鍵句映射回原文（與 run.py 相同）
            matched_sents = fuzzy_match_sentences_to_original(
                selected_str, raw_text, tokenizer=tokenizer
            )

            # ── url_analyses（前端詳細分析頁使用）──────────────
            analysis_col.update_one(
                {"normalized_url": normalized},
                {"$set": {
                    "requested_url":    url,
                    "normalized_url":   normalized,
                    "domain":           domain,

                    "raw_label":        label,
                    "confidence_level": label,
                    "risk_label":       risk["risk_label"],
                    "risk_level":       risk["risk_level"],
                    "short_reason":     risk["short_reason"],

                    "probability_real":   prob_real,
                    "selected_sentences": selected_str,
                    "selected_scores":    selected_scores,
                    "matched_sentences":  matched_sents,

                    "title":            title,
                    "raw_content":      raw_text,
                    "content":          raw_text,
                    "cleaned_content":  cleaned_text,

                    "analysis_time":    now,
                    "job_id":           job_id,
                    "source_csv":       source_csv,
                    "cleaned_csv":      cleaned_csv,
                    "data_status":      "recorded",
                    "data_status_label": "已標記",
                }},
                upsert=True,
            )

            # ── urls（前端快速查詢使用）─────────────────────────
            urls_col.update_one(
                {"url": url},
                {"$set": {
                    "url":              url,
                    "normalized_url":   normalized,
                    "domain":           domain,

                    "label":            label,
                    "raw_label":        label,
                    "confidence_level": label,
                    "risk_label":       risk["risk_label"],
                    "risk_level":       risk["risk_level"],
                    "short_reason":     risk["short_reason"],

                    "probability_real":   prob_real,
                    "selected_sentences": selected_str,
                    "selected_scores":    selected_scores,
                    "matched_sentences":  matched_sents,

                    "title":            title,
                    "raw_content":      raw_text,
                    "content":          raw_text,
                    "cleaned_content":  cleaned_text,

                    "analysis_time":    now,
                    "created_at":       now,
                    "job_id":           job_id,
                    "source_csv":       source_csv,
                    "cleaned_csv":      cleaned_csv,
                    "data_status":      "recorded",
                    "data_status_label": "已標記",
                }},
                upsert=True,
            )

            inserted_or_updated += 2   # analysis + urls 各一筆
            print(f"  ✓ {url[:80]} → {label}")

        except Exception as e:
            print(f"  ✗ 存入失敗 {url}: {e}")
            failed += 1

    client.close()

    summary = {
        "inserted_or_updated": inserted_or_updated,
        "failed": failed,
    }
    print(f"寫入完成：{summary}")
    print("========== save_to_mongo.py 完成 ==========")
    return summary