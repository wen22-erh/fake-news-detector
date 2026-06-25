import os
import time
import re
import sys
from html import unescape
from pathlib import Path

import pandas as pd

# backend 的上一層，也就是 integrate/
BASE_DIR = Path(__file__).resolve().parent.parent

# testing_mbert 資料夾
MODEL_DIR = BASE_DIR / "testing_mbert"

# 讓 Python 找得到 testing_mbert/analize_model_backend.py 和 model.py
sys.path.append(str(MODEL_DIR))

from analize_model_backend import predict_text

# =======================
# total_preprocess 清理邏輯（從 total_preprocess.py 移植）
# inference 時沒有 label，READ_MORE_RE 一律套用
# =======================
AGENCIES = r"(Reuters|AP|Associated Press|AFP|Bloomberg|BBC|CNN|NPR|WSJ|NYT|CNBC|The Guardian)"
MONTHS = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?"
AGENCY_ALL = re.compile(rf"\b{AGENCIES}\b", re.I)

CORRECTION = re.compile(r'^\([^)]*\)\s*')
LEADING_QUOTES = re.compile(r'^[\"""\']+\s*')
REPEAT_AGENCY = re.compile(rf'^(?:\s*\({AGENCIES}\)\s*(?:[-–—]\s*)?)+', re.I)
LEADING_DASH = re.compile(r'^\s*[-–—]\s*')

GENERIC_DATELINE = re.compile(rf"""
    ^\s*[\"""']?
    [A-Z0-9][A-Z0-9 .,'/-]*
    (?:/[A-Z0-9][A-Z0-9 .,'/-]*)*
    (?:,\s*[A-Za-z.]+(?:\s+[A-Za-z.]+)*)?
    (?:,\s*{MONTHS}\s+\d{{1,2}}(?:,\s*\d{{4}})?)?
    \s*\({AGENCIES}\)\s*(?:[-–—]\s*)?
""", re.VERBOSE | re.I)

CITY_CHAIN = r"(?:[A-Z0-9][A-Za-z0-9.'-]*(?:\s+[A-Z0-9][A-Za-z0-9.'-]*)*(?:/\s*[A-Z0-9][A-Za-z0-9.'-]*(?:\s+[A-Z0-9][A-Za-z0-9.'-]*)*)*)"

STRICT_CITY = re.compile(
    rf'^\s*[\"""\']?{CITY_CHAIN}(?:,\s*[A-Za-z.]+)?\s*\({AGENCIES}\)\s*(?:[-–—]\s*)?',
    re.I
)

STRICT_CITY_DATE = re.compile(
    rf'^\s*[\"""\']?{CITY_CHAIN}\s*,\s*{MONTHS}\s+\d{{1,2}}(?:,\s*\d{{4}})?\s*\({AGENCIES}\)\s*(?:[-–—]\s*)?',
    re.I
)

BYLINE = re.compile(r"""
    ^\s*By\s+
    [A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+)*
    (?:\s+and\s+[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+)*)?
    (?:\s+in\s+[A-Za-z0-9 .,'/-]+)?
    \s*(?:[-–—]\s*)?
""", re.VERBOSE)

SAFETY_NET = re.compile(rf'^\s*.{{0,120}}?\({AGENCIES}\)\s*(?:[-–—]\s*)?', re.I)

URL_RE = re.compile(r'https?://\S+|www\.\S+', re.I)
DOMAIN_RE = re.compile(r"\b[a-zA-Z0-9.-]+\.(com|org|net|gov|edu|co|io)\b(/\S*)?", re.I)
HANDLE_RE = re.compile(r'\(@[A-Za-z0-9_]+\)|@[A-Za-z0-9_]+')
PIC_TW_RE = re.compile(r'pic\.twitter\.com/\S+', re.I)
PHOTO_BY_RE = re.compile(r'Photo by .*', re.I)

RE_SCRIPT = re.compile(r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>", re.I)
RE_STYLE = re.compile(r"<style\b[^<]*(?:(?!</style>)<[^<]*)*</style>", re.I)
RE_HTML = re.compile(r"<[^>]+>")
RE_WHITESPACE = re.compile(r"\s+")
RE_KEEP = re.compile(
    r"[^A-Za-z0-9\u4e00-\u9fff\s\.,!?;:'\"()\-\[\]{}，。！？；：「」『』】【（）《》、—…]"
)

SAID_ON_RE = re.compile(
    r'\bsaid\s+on\s+'
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|'
    r'Jan|January|Feb|February|Mar|March|Apr|April|May|'
    r'Jun|June|Jul|July|Aug|August|Sep|Sept|September|'
    r'Oct|October|Nov|November|Dec|December)'
    r'(?:\s+\d{1,2})?'
    r'(?:st|nd|rd|th)?'
    r'(?:\s+(?:morning|afternoon|evening|night))?\b',
    re.I
)

READ_MORE_RE = re.compile(
    r"read more(?:\s*\.\.\.)?\s*(?:.*?\s+news\s+)?(?:at\b:?|:).*",
    re.I | re.DOTALL
)

DID_NOT_RESPOND_RE = re.compile(
    r"\bdid not(?:\s+immediately)?\s+respond to\b",
    re.I
)

SOURCE_LINK_TAIL_RE = re.compile(r"""
    (
        (?:\[\s*\d{1,4}(?::\d{2})?\s*[a-zA-Z]{2,5}\s*\]\s*)?
        --\s*source\s+link\s*:
    )
    .*
""", re.I | re.VERBOSE | re.DOTALL)

BOILERPLATE_RE_LIST = [
    re.compile(r"\(\s*reporting by .*?\)", re.I),
    re.compile(r"\(\s*editing by .*?\)", re.I),
    re.compile(r"\(\s*reporting by .*?;\s*editing by .*?\)", re.I),
    re.compile(r"this article was funded in part by .*?\.", re.I),
    re.compile(r"it was independently created by the .*? editorial staff\.", re.I),
    re.compile(r".*? had no editorial involvement in its creation or production\.", re.I),
    re.compile(r"did not immediately respond to a request for comment\.", re.I),
    re.compile(r"did not respond to a request for comment\.", re.I),
    re.compile(r"respond to a request for comment\.", re.I),
]

JS_HINTS = [
    "getelementbyid", "getelementsbytagname", "createelement",
    "removeeventlistener", "detachevent", "addeventlistener",
    "settimeout", "setinterval", "onload", "onerror",
    "data:image", "base64", "decodeuricomponent",
    "appendchild", "insertbefore", "parentnode",
    "javascript", "math.max", "parsefloat", "indexof",
    "google image requests", "document", "window",
    "pcode", "playerbrandingid", "oo.ready",
    "oo.player.create", "playerparam", "container"
]

JS_CHUNK_PATTERNS = [
    re.compile(r"[^\n]{0,300}(?:getelementbyid|getelementsbytagname|createelement|appendchild|insertbefore)[^\n]{0,300}", re.I),
    re.compile(r"[^\n]{0,300}(?:removeeventlistener|detachevent|addeventlistener|onload|onerror)[^\n]{0,300}", re.I),
    re.compile(r"[^\n]{0,300}(?:data:image|base64|decodeuricomponent|parsefloat|math\.max|indexof)[^\n]{0,300}", re.I),
    re.compile(r"[^\n]{0,300}(?:javascript|parentnode|settimeout|setinterval|google image requests)[^\n]{0,300}", re.I),
    re.compile(r"[^\n]{0,400}(?:pcode|playerbrandingid|oo\.ready|oo\.player\.create|playerparam|container)[^\n]{0,400}", re.I),
]

CODE_TOKEN_RE = re.compile(r"\b(?:function|var|return|throw|try|catch|finally|case|switch|break|null|true|false)\b", re.I)
SYMBOL_RE = re.compile(r"[{}();=!<>]")

RADIO_PROMO_KEYWORDS = [
    "alternate current radio network",
    "uncensored, uninterruptible talk radio",
    "social rejects club",
    "direct download episode",
]


def normalize_space(text):
    return RE_WHITESPACE.sub(" ", str(text)).strip()


def clean_dateline_all(text):
    text = str(text).strip()
    text = CORRECTION.sub("", text)
    text = LEADING_QUOTES.sub("", text)
    text = REPEAT_AGENCY.sub("", text)

    changed = True
    while changed:
        old = text
        text = GENERIC_DATELINE.sub("", text)
        text = STRICT_CITY_DATE.sub("", text)
        text = STRICT_CITY.sub("", text)
        text = BYLINE.sub("", text)
        text = LEADING_QUOTES.sub("", text)
        text = LEADING_DASH.sub("", text)
        changed = text != old

    text = SAFETY_NET.sub("", text, count=1)
    text = LEADING_DASH.sub("", text)
    return normalize_space(text)


def clean_social_url(text):
    text = URL_RE.sub(" ", text)
    text = DOMAIN_RE.sub(" ", text)
    text = HANDLE_RE.sub(" ", text)
    text = PIC_TW_RE.sub(" ", text)
    text = PHOTO_BY_RE.sub(" ", text)
    return normalize_space(text)


def clean_said_on(text):
    return normalize_space(SAID_ON_RE.sub(" ", text))


def clean_trailing_markers(text):
    strong_tail_markers = [
        r"featured image via",
        r"feature via image",
        r"image via",
        r"for entire story:"
    ]

    for marker in strong_tail_markers:
        m = re.search(marker, text, flags=re.I)
        if m:
            ratio = m.start() / max(len(text), 1)
            if ratio >= 0.7:
                text = text[:m.start()]
            else:
                text = re.sub(marker, " ", text, flags=re.I)
            return normalize_space(text)

    m = re.search(r"via:", text, flags=re.I)
    if m:
        ratio = m.start() / max(len(text), 1)
        if ratio >= 0.7:
            text = text[:m.start()]
        else:
            text = re.sub(r"via:", " ", text, flags=re.I, count=1)

    return normalize_space(text)


def remove_did_not_respond_sentence(text):
    sentences = re.split(r"(?<=[.!?])\s+", str(text))
    kept = [s for s in sentences if not DID_NOT_RESPOND_RE.search(s)]
    return normalize_space(" ".join(kept))


def clean_boilerplate(text):
    for pat in BOILERPLATE_RE_LIST:
        text = pat.sub(" ", text)
    return normalize_space(text)


def clean_js_html_garbage(text):
    text = unescape(str(text))
    text = RE_SCRIPT.sub(" ", text)
    text = RE_STYLE.sub(" ", text)
    text = RE_HTML.sub(" ", text)

    lower = text.lower()

    if any(h in lower for h in JS_HINTS):
        for pat in JS_CHUNK_PATTERNS:
            text = pat.sub(" ", text)

    lower = text.lower()
    keyword_hits = sum(h in lower for h in JS_HINTS)
    code_token_hits = len(CODE_TOKEN_RE.findall(lower))
    symbol_hits = len(SYMBOL_RE.findall(lower))

    if keyword_hits >= 3 or (keyword_hits >= 2 and symbol_hits >= 8) or (code_token_hits >= 4 and symbol_hits >= 10):
        return ""

    text = RE_KEEP.sub(" ", text)
    return normalize_space(text)


def mixed_token_count(text):
    text = str(text)
    zh_tokens = re.findall(r"[\u4e00-\u9fff]", text)
    en_tokens = re.findall(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*", text)
    return len(zh_tokens) + len(en_tokens)


def mixed_token_count(text):
    text = str(text)
    zh_tokens = re.findall(r"[一-鿿]", text)
    en_tokens = re.findall(r"[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*", text)
    return len(zh_tokens) + len(en_tokens)


def preprocess_text(text):
    """
    對單篇文章套用 total_preprocess.py 的完整清理流程。
    inference 時沒有 label，READ_MORE_RE 一律套用。
    清理後為空、為 radio promo、或 token 數不足 5 則回傳 None。
    """
    text = str(text)
    text = text.replace("\u200b", "").replace("\xa0", " ").strip()
    text = clean_dateline_all(text)
    text = clean_social_url(text)
    text = AGENCY_ALL.sub("[AGENCY]", text)
    text = clean_said_on(text)
    text = clean_trailing_markers(text)
    text = clean_js_html_garbage(text)
    text = clean_boilerplate(text)
    text = remove_did_not_respond_sentence(text)

    # inference 時一律套用 READ_MORE_RE（訓練時只對 fake label 套用）
    text = READ_MORE_RE.sub("", text).strip()
    text = SOURCE_LINK_TAIL_RE.sub("", text).strip()

    text = re.sub(r"\b21wire\.tv\b", " ", text, flags=re.I)
    text = re.sub(r"\b21wire\b", " ", text, flags=re.I)

    text = normalize_space(text)
    text = text.lower()

    # 清理後為空則視為無效
    if not text:
        return None
    # radio promo 過濾
    if any(kw in text for kw in RADIO_PROMO_KEYWORDS):
        return None
    # token 數不足 5 過濾
    if mixed_token_count(text) < 5:
        return None
    return text


# =======================
# 主推論流程
# =======================
def run_inference(csv_folder, model_folder):
    """
    讀取 CSV → 預處理 → 模型推論 → 回傳 df, results
    """
    print("開始執行 process_csv.py")

    csv_filename = os.path.join(csv_folder, "bbc_label_1.csv")

    if not os.path.exists(csv_filename):
        raise FileNotFoundError(
            f"找不到 CSV 檔案：{csv_filename}\n"
            "請確認 export_to_csv.py 是否已成功執行，且輸出至正確資料夾。"
        )

    print(f"讀取 CSV 檔案：{csv_filename}")

    df = pd.read_csv(csv_filename, encoding="utf-8-sig")

    print(f"CSV 欄位：{df.columns.tolist()}")
    print(f"CSV 資料筆數：{len(df)}")

    if df.empty:
        raise ValueError(
            f"CSV 檔案沒有任何資料列：{csv_filename}。"
            "請確認 MongoDB 來源 collection 是否有資料，或 export_to_csv.py 是否匯出成功。"
        )

    if "content" in df.columns:
        text_col = "content"
    elif "text" in df.columns:
        text_col = "text"
    else:
        raise ValueError(
            f"CSV 中找不到 content 或 text 欄位，實際欄位為：{df.columns.tolist()}"
        )

    texts = df[text_col].astype(str).tolist()

    print("開始預處理文章...")

    cleaned_texts = []
    for text in texts:
        cleaned = preprocess_text(text)
        cleaned_texts.append(cleaned)

    # 重複內容去除：cleaned 相同的保留第一筆，其餘標記為 None
    seen = set()
    deduped = []
    for t in cleaned_texts:
        if t is None:
            deduped.append(None)
        elif t in seen:
            deduped.append(None)
        else:
            seen.add(t)
            deduped.append(t)
    cleaned_texts = deduped

    skipped = sum(1 for t in cleaned_texts if t is None)
    print(f"預處理完成：有效 {len(texts) - skipped} 筆，跳過 {skipped} 筆（清理後為空或重複）")

    # 有效文章的字數統計
    valid_texts = [t for t in cleaned_texts if t is not None]
    if valid_texts:
        lengths = [len(t) for t in valid_texts]
        avg_len = sum(lengths) / len(lengths)
        min_len = min(lengths)
        max_len = max(lengths)
        print(f"有效文章字數統計：平均 {avg_len:.0f} 字，最短 {min_len} 字，最長 {max_len} 字")

    print("開始把 CSV 內容送進 analize_model_backend.py 做預測...")

    results = []
    inference_times = []

    for index, (text, cleaned) in enumerate(zip(texts, cleaned_texts), start=1):
        if cleaned is None:
            print(f"第 {index}/{len(texts)} 筆：跳過（預處理後為空或重複）")
            results.append({
                "prediction": -1,
                "probability_real": 0.5,
                "confidence_level": "無法分析 (Empty after preprocessing)",
                "selected_sentences": "",
                "selected_scores": "",
            })
        else:
            t_start = time.time()
            result = predict_text(cleaned, str(model_folder))
            elapsed = time.time() - t_start
            inference_times.append(elapsed)
            print(f"第 {index}/{len(texts)} 筆：{elapsed:.2f}s　判定：{result['confidence_level']}")
            results.append(result)

    if inference_times:
        avg_t = sum(inference_times) / len(inference_times)
        print(f"推論完成：每筆平均耗時 {avg_t:.2f}s（共 {len(inference_times)} 筆有效）")

    print("模型預測完成")

    df["predicted_label"]    = [r["prediction"]         for r in results]
    df["probability_real"]   = [r["probability_real"]   for r in results]
    df["confidence_level"]   = [r["confidence_level"]   for r in results]
    df["selected_sentences"] = [r["selected_sentences"] for r in results]
    df["selected_scores"]    = [r["selected_scores"]    for r in results]

    return df, results


if __name__ == "__main__":
    CSV_FOLDER = BASE_DIR / "csvfiles"
    MODEL_FOLDER = BASE_DIR / "testing_mbert"

    df, results = run_inference(
        str(CSV_FOLDER),
        str(MODEL_FOLDER)
    )

    print("前 5 筆結果：")
    print(df[["url", "predicted_label", "probability_real", "confidence_level", "selected_sentences"]].head())