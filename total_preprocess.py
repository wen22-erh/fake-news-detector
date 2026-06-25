import re
import pandas as pd
from html import unescape

INPUT_CSV = "gpt_300.csv"
OUTPUT_CSV = "gpt_300_normalize.csv"

TEXT_COL = "text"
LABEL_COL = "label"

FAKE_LABEL = 0
REAL_LABEL = 1

AGENCIES = r"(Reuters|AP|Associated Press|AFP|Bloomberg|BBC|CNN|NPR|WSJ|NYT|CNBC|The Guardian)"
MONTHS = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?"
AGENCY_ALL = re.compile(rf"\b{AGENCIES}\b", re.I)

CORRECTION = re.compile(r'^\([^)]*\)\s*')
LEADING_QUOTES = re.compile(r'^[\"“”\']+\s*')
REPEAT_AGENCY = re.compile(rf'^(?:\s*\({AGENCIES}\)\s*(?:[-–—]\s*)?)+', re.I)
LEADING_DASH = re.compile(r'^\s*[-–—]\s*')

GENERIC_DATELINE = re.compile(rf"""
    ^\s*[\"“”']?
    [A-Z0-9][A-Z0-9 .,'/-]*
    (?:/[A-Z0-9][A-Z0-9 .,'/-]*)*
    (?:,\s*[A-Za-z.]+(?:\s+[A-Za-z.]+)*)?
    (?:,\s*{MONTHS}\s+\d{{1,2}}(?:,\s*\d{{4}})?)?
    \s*\({AGENCIES}\)\s*(?:[-–—]\s*)?
""", re.VERBOSE | re.I)

CITY_CHAIN = r"(?:[A-Z0-9][A-Za-z0-9.'-]*(?:\s+[A-Z0-9][A-Za-z0-9.'-]*)*(?:/\s*[A-Z0-9][A-Za-z0-9.'-]*(?:\s+[A-Z0-9][A-Za-z0-9.'-]*)*)*)"

STRICT_CITY = re.compile(
    rf'^\s*[\"“”\']?{CITY_CHAIN}(?:,\s*[A-Za-z.]+)?\s*\({AGENCIES}\)\s*(?:[-–—]\s*)?',
    re.I
)

STRICT_CITY_DATE = re.compile(
    rf'^\s*[\"“”\']?{CITY_CHAIN}\s*,\s*{MONTHS}\s+\d{{1,2}}(?:,\s*\d{{4}})?\s*\({AGENCIES}\)\s*(?:[-–—]\s*)?',
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

RADIO_PROMO_KEYWORDS = [
    "alternate current radio network",
    "uncensored, uninterruptible talk radio",
    "social rejects club",
    "direct download episode",
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


def is_radio_promo(text):
    lower = str(text).lower()
    return any(kw in lower for kw in RADIO_PROMO_KEYWORDS)


def clean_one_row(row, text_col="text"):
    text = str(row.get(text_col, ""))

    text = text.replace("\u200b", "").replace("\xa0", " ").strip()
    text = clean_dateline_all(text)
    text = clean_social_url(text)
    text = AGENCY_ALL.sub("[AGENCY]", text)

    text = clean_said_on(text)
    text = clean_trailing_markers(text)
    text = clean_js_html_garbage(text)
    text = clean_boilerplate(text)
    text = remove_did_not_respond_sentence(text)

    if row.get(LABEL_COL) == FAKE_LABEL:
        text = READ_MORE_RE.sub("", text).strip()

    text = SOURCE_LINK_TAIL_RE.sub("", text).strip()

    text = re.sub(r"\b21wire\.tv\b", " ", text, flags=re.I)
    text = re.sub(r"\b21wire\b", " ", text, flags=re.I)

    text = normalize_space(text)
    text = text.lower()

    return text


def main():
    df = pd.read_csv(INPUT_CSV, keep_default_na=False, on_bad_lines="skip")

    if TEXT_COL not in df.columns:
        raise ValueError(f"找不到 text 欄位，目前欄位：{list(df.columns)}")
    if LABEL_COL not in df.columns:
        raise ValueError(f"找不到 label 欄位，目前欄位：{list(df.columns)}")

    before_count = len(df)
    before_label_dist = df[LABEL_COL].value_counts(dropna=False).sort_index()

    df[LABEL_COL] = pd.to_numeric(df[LABEL_COL], errors="coerce")
    df = df[df[LABEL_COL].isin([0, 1])].copy()
    df[LABEL_COL] = df[LABEL_COL].astype(int)

    df = df[df[TEXT_COL].astype(str).str.strip() != ""].copy()

    original_text = df[TEXT_COL].astype(str).copy()
    df[TEXT_COL] = df.apply(clean_one_row, axis=1)

    df = df[df[TEXT_COL].astype(str).str.strip() != ""].copy()
    df = df[~df[TEXT_COL].apply(is_radio_promo)].copy()
    df = df[df[TEXT_COL].apply(mixed_token_count) >= 5].copy()

    df = df[[TEXT_COL, LABEL_COL]].drop_duplicates(subset=[TEXT_COL]).reset_index(drop=True)

    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print("=" * 60)
    print("清理完成")
    print(f"輸入檔案：{INPUT_CSV}")
    print(f"輸出檔案：{OUTPUT_CSV}")
    print(f"原始筆數：{before_count}")
    print(f"清理後筆數：{len(df)}")
    print(f"刪除筆數：{before_count - len(df)}")
    print("=" * 60)

    print("\n原始 label 分布：")
    print(before_label_dist)

    print("\n清理後 label 分布：")
    print(df[LABEL_COL].value_counts(dropna=False).sort_index())

    print("\n前 5 筆：")
    print(df.head())


def process_csv_file(input_csv: str, output_csv: str):
    """
    提供給後端 Inference Pipeline 使用的清洗函式。
    會保留所有原始欄位，並新增/覆寫 cleaned_content 欄位。
    不會刪除筆數。
    """
    df = pd.read_csv(input_csv, keep_default_na=False, on_bad_lines="skip")
    
    # 尋找內容欄位
    text_col = None
    possible_cols = ["text", "content", "raw_content", "article"]
    for col in possible_cols:
        if col in df.columns:
            text_col = col
            break
            
    if not text_col:
        raise ValueError(f"找不到文章內容欄位，支援的名稱：{possible_cols}，目前欄位：{list(df.columns)}")
        
    df["cleaned_content"] = df.apply(lambda row: clean_one_row(row, text_col=text_col), axis=1)
    
    # 確保輸出目錄存在
    from pathlib import Path
    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    
    df.to_csv(output_csv, index=False, encoding="utf-8-sig")
    #print(f"✅ 已成功輸出清洗後檔案：{output_csv}")
    return output_csv


if __name__ == "__main__":
    main()