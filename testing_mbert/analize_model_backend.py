import math
import torch
from transformers import BertTokenizerFast
from model2 import HierarchicalDocumentModel, HierarchicalBertConfig

_model = None
_tokenizer = None
_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def load_model(model_folder):
    """
    載入已經訓練好的模型與 tokenizer。
    model_folder 要指向 testing_mbert 資料夾。
    """
    global _model, _tokenizer

    if _model is None or _tokenizer is None:
        print(f"載入模型資料夾：{model_folder}")
        _tokenizer = BertTokenizerFast.from_pretrained(model_folder)
        _model = HierarchicalDocumentModel.from_pretrained(model_folder)
        _model.to(_device)
        _model.eval()

    return _model, _tokenizer


@torch.no_grad()
def predict_text(text, model_folder, T=3.0, max_len=512, stride=128):
    """
    接收一篇文章文字，回傳完整分析結果。

    回傳 dict，包含：
    - prediction        : 0（假新聞）或 1（真實新聞）
    - probability_real  : P(Real)，0.0 ~ 1.0
    - confidence_level  : 五級文字說明
    - selected_sentences: 關鍵句子（以 ' | ' 分隔）
    - selected_scores   : 對應句子的平均得票數（以 ', ' 分隔）
    """
    model, tokenizer = load_model(model_folder)

    if not isinstance(text, str):
        text = str(text)

    # 1. Tokenize
    encoding = tokenizer(
        text,
        truncation=True,
        max_length=max_len,
        stride=stride,
        return_overflowing_tokens=True,
        return_attention_mask=True,
        padding=False
    )

    num_chunks = len(encoding["input_ids"])
    pad_token_id = tokenizer.pad_token_id

    doc_input_ids, doc_attention_mask = [], []
    for i in range(num_chunks):
        ids = encoding["input_ids"][i]
        mask = encoding["attention_mask"][i]
        pad_len = max_len - len(ids)
        doc_input_ids.append(ids + [pad_token_id] * pad_len)
        doc_attention_mask.append(mask + [0] * pad_len)

    input_ids = torch.tensor([doc_input_ids], dtype=torch.long).to(_device)
    attention_mask = torch.tensor([doc_attention_mask], dtype=torch.long).to(_device)
    chunk_mask = torch.ones((1, num_chunks), dtype=torch.long).to(_device)

    # 2. 呼叫 extract_features（與 inference.py 完全相同）
    outputs = model.extract_features(
        input_ids=input_ids,
        attention_mask=attention_mask,
        chunk_mask=chunk_mask
    )

    attn_mat = outputs["attention_matrix"][0]
    chunk_indices = outputs["chunk_indices"][0]
    logits = outputs["logits"][0]

    # 3. Temperature Scaling 與五級分類
    z_F = logits[0].item()
    z_R = logits[1].item()
    delta_z = z_R - z_F

    try:
        p_real = 1.0 / (1.0 + math.exp(-delta_z / T))
    except OverflowError:
        p_real = 0.0 if (-delta_z / T) > 0 else 1.0

    if p_real >= 0.8:
        confidence_level = "高度真實 (Highly Real)"
    elif p_real >= 0.6:
        confidence_level = "可能真實 (Likely Real)"
    elif p_real >= 0.4:
        confidence_level = "不確定 (Uncertain)"
    elif p_real >= 0.2:
        confidence_level = "可能假新聞 (Likely Fake)"
    else:
        confidence_level = "高度假新聞 (Highly Fake)"

    predicted_class = 1 if p_real >= 0.5 else 0

    # 4. 找關鍵 chunk 與 token 頻率
    column_sums = attn_mat.sum(dim=0)
    best_chunk_idx = torch.argmax(column_sums).item()
    best_chunk_token_indices = chunk_indices[best_chunk_idx]

    unique_tokens, counts = torch.unique(best_chunk_token_indices, return_counts=True)
    token_score_dict = {
        token.item(): count.item()
        for token, count in zip(unique_tokens, counts)
    }

    # 5. 還原文本並計算句子分數
    chunk_token_ids = encoding["input_ids"][best_chunk_idx]
    punctuations = {'.', '?', '!', ',', '\u3002', '\uff1f', '\uff01', '\uff0c'}
    special_token_ids = {tokenizer.cls_token_id, tokenizer.sep_token_id, tokenizer.pad_token_id}

    sentences_info = []
    current_tokens = []
    current_token_scores = []

    for idx, token_id in enumerate(chunk_token_ids):
        if token_id in special_token_ids:
            continue

        token_str = tokenizer.convert_ids_to_tokens(token_id)
        token_score = token_score_dict.get(idx, 0)

        current_tokens.append(token_str)
        current_token_scores.append(token_score)

        if token_str in punctuations:
            valid_length = len(current_tokens)

            is_abbreviation = False
            if token_str == '.' and valid_length >= 2:
                prev_tok = current_tokens[-2].replace('##', '')
                if len(prev_tok) <= 2 and prev_tok.isalpha():
                    is_abbreviation = True

            if valid_length >= 8 and not is_abbreviation:
                K = min(7, valid_length)
                top_k_scores = sorted(current_token_scores, reverse=True)[:K]
                top_k_avg_score = sum(top_k_scores) / K

                sentence_text = tokenizer.convert_tokens_to_string(current_tokens)
                sentences_info.append({
                    "text": sentence_text.strip(),
                    "score": top_k_avg_score,
                    "length": valid_length
                })

                current_tokens = []
                current_token_scores = []

    # 收尾處理
    if len(current_tokens) >= 8:
        K = min(7, len(current_tokens))
        top_k_scores = sorted(current_token_scores, reverse=True)[:K]
        top_k_avg_score = sum(top_k_scores) / K
        sentence_text = tokenizer.convert_tokens_to_string(current_tokens)
        sentences_info.append({
            "text": sentence_text.strip(),
            "score": top_k_avg_score,
            "length": len(current_tokens)
        })

    # 6. 動態相對門檻過濾
    final_selected_sentences = []

    if sentences_info:
        sorted_sentences = sorted(sentences_info, key=lambda x: x["score"], reverse=True)
        MAX_K = 3
        ALPHA = 0.35
        best_score = sorted_sentences[0]["score"]
        threshold = best_score * ALPHA

        for i, sent in enumerate(sorted_sentences):
            if i >= MAX_K:
                break
            if sent["score"] >= threshold:
                final_selected_sentences.append(sent)
            else:
                break
    else:
        all_text = tokenizer.convert_tokens_to_string(
            tokenizer.convert_ids_to_tokens(
                [t for t in chunk_token_ids if t not in special_token_ids]
            )
        )
        final_selected_sentences = [{"text": all_text, "score": 0, "length": len(chunk_token_ids)}]

    best_sentences_combined = " | ".join([s["text"] for s in final_selected_sentences])
    best_scores_combined = ", ".join([f"{s['score']:.2f}" for s in final_selected_sentences])

    return {
        "prediction": predicted_class,
        "probability_real": round(p_real, 4),
        "confidence_level": confidence_level,
        "selected_sentences": best_sentences_combined,
        "selected_scores": best_scores_combined,
    }


if __name__ == "__main__":
    test_text = "This is a test news article."
    result = predict_text(test_text, ".")
    print("預測結果：", result["prediction"])
    print("真實機率：", result["probability_real"])
    print("信心等級：", result["confidence_level"])
    print("關鍵句子：", result["selected_sentences"])