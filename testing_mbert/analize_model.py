import pandas as pd
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer, DataCollatorWithPadding
from sklearn.metrics import accuracy_score, confusion_matrix, ConfusionMatrixDisplay
import matplotlib.pyplot as plt
from datasets import Dataset as HFDataset,Value
# 讀 CSV（content / label）
df = pd.read_csv("gnews_normal_noquo.csv")
texts = df["text"].astype(str).tolist()
y_true = df["label"].astype(int).tolist()

# 在模型資料夾執行，直接從當前目錄載入
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = AutoModelForSequenceClassification.from_pretrained(".").to(device)
tokenizer = AutoTokenizer.from_pretrained(".")

MAX_LEN = 512
STRIDE  = 128
@torch.no_grad()
def predict(texts,batch_size=32,max_len=512,stride=128,sliding=True):
    model.eval()
    preds=[]
    for t in texts:
        enc=tokenizer(
            t,
            return_tensors="pt",
            truncation=True,
            max_length=max_len,
            stride=stride,
            return_overflowing_tokens=True,
            padding=True
    )
        # 只保留模型會用到的鍵，再丟到裝置
        inputs = {k: v.to(device) for k, v in enc.items()
                  if k in ("input_ids", "attention_mask", "token_type_ids", "position_ids")}
        logits = model(**inputs).logits
        agg=logits.mean(dim=0)
        preds.append(int(agg.argmax().item()))
    return preds


# 推論 + 評估
y_pred = predict(texts,batch_size=32,max_len=MAX_LEN,stride=STRIDE,sliding=True)
acc = accuracy_score(y_true, y_pred)
cm = confusion_matrix(y_true, y_pred, labels=[0, 1])

print(f"Accuracy: {acc:.4f}")
print("Confusion matrix (rows=true 0/1, cols=pred 0/1):")
print(cm)

# 畫混淆矩陣並顯示（含 accuracy）
disp = ConfusionMatrixDisplay(confusion_matrix=cm, display_labels=[0, 1])
disp.plot(values_format="d")
plt.title(f"nomalized no comma Confusion Matrix (Accuracy = {acc:.4f})")
plt.tight_layout()
plt.show()
