import torch
import pandas as pd
import math
from transformers import BertTokenizerFast
from model2 import HierarchicalDocumentModel, HierarchicalBertConfig

# ============================================================
# 1. 新增：取得最終分類器與權重的 Helper 函數
# ============================================================
def find_final_classifier_linear(model, num_labels=2, expected_in_features=None):
    candidates = []
    for name, module in model.named_modules():
        if isinstance(module, torch.nn.Linear):
            if module.out_features == num_labels:
                if expected_in_features is None or module.in_features == expected_in_features:
                    candidates.append((name, module))

    if len(candidates) == 0:
        raise RuntimeError("找不到符合條件的最終分類器 Linear layer。")
    return candidates[-1]

def get_classifier_dim_weights(model, pred_label, hidden_dim, mode="margin_positive", num_labels=2):
    layer_name, classifier_layer = find_final_classifier_linear(
        model=model, num_labels=num_labels, expected_in_features=hidden_dim
    )
    W = classifier_layer.weight.detach().float().cpu()
    
    pred_label = int(pred_label)

    if mode == "margin_positive":
        other_label = 1 - pred_label
        dim_weights = W[pred_label] - W[other_label]
        dim_weights = torch.clamp(dim_weights, min=0.0)  # 只保留正貢獻
    elif mode == "pred_positive":
        dim_weights = W[pred_label]
        dim_weights = torch.clamp(dim_weights, min=0.0)
    elif mode == "abs_margin":
        other_label = 1 - pred_label
        dim_weights = torch.abs(W[pred_label] - W[other_label])
    else:
        raise ValueError(f"未知的 mode={mode}")

    return dim_weights, layer_name

# ============================================================
# 2. 原本的模型載入函數
# ============================================================
def load_model_and_tokenizer(model_path):
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tokenizer = BertTokenizerFast.from_pretrained(model_path)
    model = HierarchicalDocumentModel.from_pretrained(model_path)
    model.to(device)
    model.eval()
    return model, tokenizer, device

# ============================================================
# 3. 整合 Classifier Weight 的推論函數
# ============================================================
def analyze_fake_news(text, model, tokenizer, device, T=0.88, max_len=512, stride=128):
    if not isinstance(text, str):
        text = str(text)

    # 1. Tokenize
    encoding = tokenizer(
        text, truncation=True, max_length=max_len, stride=stride,
        return_overflowing_tokens=True, return_attention_mask=True, padding=False
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
        
    input_ids = torch.tensor([doc_input_ids], dtype=torch.long).to(device)
    attention_mask = torch.tensor([doc_attention_mask], dtype=torch.long).to(device)
    chunk_mask = torch.ones((1, num_chunks), dtype=torch.long).to(device)

    # 2. 呼叫特徵提取
    with torch.no_grad():
        outputs = model.extract_features(
            input_ids=input_ids,
            attention_mask=attention_mask,
            chunk_mask=chunk_mask
        )

    attn_mat = outputs["attention_matrix"][0]
    chunk_indices = outputs["chunk_indices"][0].cpu()  # 移至 CPU 處理權重
    logits = outputs["logits"][0]

    # 3. Temperature Scaling 與預測結果 (已優化數值穩定性)
    z_F = logits[0].item()
    z_R = logits[1].item()
    delta_z = z_R - z_F
    
    p_real = torch.sigmoid(torch.tensor(delta_z / T)).item()

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

    # 4. 找出關鍵 Chunk
    column_sums = attn_mat.sum(dim=0)
    best_chunk_idx = torch.argmax(column_sums).item()
    
    # ========================================================
    # 5. [核心修改] 替換為 Classifier Weight 加權計分
    # ========================================================
    hidden_dim = int(chunk_indices.shape[-1])
    dim_weights, _ = get_classifier_dim_weights(
        model=model,
        pred_label=predicted_class,
        hidden_dim=hidden_dim,
        mode="margin_positive"
    )

    selected_token_positions = chunk_indices[best_chunk_idx].view(-1)
    token_score_dict = {}

    for dim_idx, token_pos_tensor in enumerate(selected_token_positions):
        pos = int(token_pos_tensor.item())
        dim_weight = float(dim_weights[dim_idx].item())
        
        # 只累加對目前預測類別有正向貢獻的權重
        if dim_weight > 0:
            token_score_dict[pos] = token_score_dict.get(pos, 0.0) + dim_weight
    # ========================================================

    # 6. 還原文本並計算句子分數
    chunk_token_ids = encoding["input_ids"][best_chunk_idx]
    punctuations = {'.', '?', '!', ',', '。', '？', '！', '，'}
    special_token_ids = {tokenizer.cls_token_id, tokenizer.sep_token_id, tokenizer.pad_token_id}

    sentences_info = []
    current_tokens = []
    current_token_scores = []

    for idx, token_id in enumerate(chunk_token_ids):
        if token_id in special_token_ids:
            continue

        token_str = tokenizer.convert_ids_to_tokens(token_id)
        # 取出權重分數，若無則為 0
        token_score = token_score_dict.get(idx, 0.0) 

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

    # 直接取前三高的句子，過濾掉分數為 0 的句子
    sentences_info = [s for s in sentences_info if s["score"] > 0]
    if sentences_info:
        sorted_sentences = sorted(sentences_info, key=lambda x: x["score"], reverse=True)
        final_selected_sentences = sorted_sentences[:3]
    else:
        all_text = tokenizer.convert_tokens_to_string(
            tokenizer.convert_ids_to_tokens([t for t in chunk_token_ids if t not in special_token_ids])
        )
        final_selected_sentences = [{"text": all_text, "score": 0, "length": len(chunk_token_ids)}]

    best_sentences_combined = " | ".join([s["text"] for s in final_selected_sentences])
    best_scores_combined = ", ".join([f"{s['score']:.4f}" for s in final_selected_sentences])

    return {
        "prediction": predicted_class,
        "probability_real": p_real,
        "confidence_level": confidence_level,
        "best_chunk_index": best_chunk_idx,
        "selected_sentences": best_sentences_combined,
        "selected_scores": best_scores_combined,
        "T_used": T  # 新增回傳實際使用的 T 值
    }

if __name__ == "__main__":
    MODEL_PATH = "./final_model"
    model, tokenizer, device = load_model_and_tokenizer(MODEL_PATH)
    
    INPUT_CSV = "test.csv"
    TEMPERATURE_SETTING = 0.88 # 統一在此設定 Temperature
    
    print(f"載入模型成功 (設備: {device})")
    print(f"開始讀取 {INPUT_CSV}...\n")
    
    df = pd.read_csv(INPUT_CSV)
    TEXT_COLUMN = "text"
    total_rows = len(df)
    
    for index, row in df.iterrows():
        text_content = str(row[TEXT_COLUMN])
        
        result = analyze_fake_news(text_content, model, tokenizer, device, T=TEMPERATURE_SETTING)
        
        sentences = result["selected_sentences"].split(" | ")
        scores = result["selected_scores"].split(", ")
        
        print(f"========== 第 {index + 1} / {total_rows} 筆新聞 ==========")
        print(f"系統判定 : {result['confidence_level']}")
        # 修正：改為動態讀取實際使用的 T 值
        print(f"真實機率 : {result['probability_real']:.4f} (T={result['T_used']})")
        print(f"關鍵特徵 :")
        
        for i, (sent, score) in enumerate(zip(sentences, scores)):
            if sent.strip():
                print(f"  {i+1}. {sent}")
                print(f"     => 平均加權分數: {score}")
                
        print("=========================================\n")