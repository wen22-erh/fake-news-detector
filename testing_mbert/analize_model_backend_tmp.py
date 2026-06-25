import json
import pandas as pd
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

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

        _model = AutoModelForSequenceClassification.from_pretrained(model_folder)
        _tokenizer = AutoTokenizer.from_pretrained(model_folder)

        _model.to(_device)
        _model.eval()

    return _model, _tokenizer


@torch.no_grad()
def predict_text(text, model_folder, max_len=512, stride=128):
    """
    接收一篇文章文字，回傳預測結果。
    回傳值通常是 0 或 1。
    """

    model, tokenizer = load_model(model_folder)

    enc = tokenizer(
        str(text),
        return_tensors="pt",
        truncation=True,
        max_length=max_len,
        stride=stride,
        return_overflowing_tokens=True,
        padding=True,
    )

    inputs = {
        k: v.to(_device)
        for k, v in enc.items()
        if k in (
            "input_ids",
            "attention_mask",
            "token_type_ids",
            "position_ids",
        )
    }

    logits = model(**inputs).logits

    # 文章如果被切成多段，取平均
    avg_logits = logits.mean(dim=0)

    pred = int(avg_logits.argmax().item())

    return pred


def analyze_csv(input_csv_path: str, model_folder: str, results_json_path: str = None) -> dict:
    """
    批次處理 CSV 檔案進行模型推論。
    回傳 URL 到 label 的對應字典。
    """
    df = pd.read_csv(input_csv_path)
    
    url_col = "url"
    if "url" not in df.columns and "normalized_url" in df.columns:
        url_col = "normalized_url"
        
    content_col = "cleaned_content"
    if content_col not in df.columns:
        content_col = "text"
        
    results = {}
    
    for idx, row in df.iterrows():
        url = row.get(url_col)
        text = row.get(content_col)
        
        if pd.isna(text) or not str(text).strip() or not url:
            continue
            
        pred = predict_text(str(text), model_folder)
        # 0 = fake, 1 = real 
        label = "fake" if pred == 0 else "real"
        results[url] = label

    if results_json_path:
        with open(results_json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    return results

if __name__ == "__main__":
    # 單獨測試用
    test_text = "This is a test news article."

    # 這裡用目前資料夾，也就是 testing_mbert
    result = predict_text(test_text, ".")

    print("預測結果：", result)