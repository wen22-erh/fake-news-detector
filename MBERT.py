import os, json, sys
from pathlib import Path
from datasets import Dataset as HFDataset, Value
import pandas as pd
import torch
import matplotlib.pyplot as plt
import sys, transformers
from transformers import (
    BertTokenizerFast, BertForSequenceClassification,
    DataCollatorWithPadding, Trainer, TrainingArguments
)
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support,
    classification_report, confusion_matrix, ConfusionMatrixDisplay
)
import shutil
HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE / "testing_mbert"   # 模型主資料夾
CKPT_DIR  = MODEL_DIR / "checkpoints"                # checkpoint 會放這
LOGS_DIR  = MODEL_DIR / "logs"                       # 圖片會放這
TB_DIR    = MODEL_DIR / "tb_logs"                    # TensorBoard logs

CKPT_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
TB_DIR.mkdir(parents=True, exist_ok=True)

# （建議）每次訓練前清掉舊的 checkpoint，避免上次的 epoch 混進來
shutil.rmtree(CKPT_DIR, ignore_errors=True)
CKPT_DIR.mkdir(parents=True, exist_ok=True)
def compute_metrics(pred):
    labels = pred.label_ids
    preds  = pred.predictions.argmax(-1)
    precision, recall, f1, _ = precision_recall_fscore_support(labels, preds, average='binary')
    acc = accuracy_score(labels, preds)
    return {'accuracy': acc, 'f1': f1, 'precision': precision, 'recall': recall}

# -----------------------
# 讀資料

CSV_NAME = "final_test_bert_dataset.csv"
CSV_PATH = HERE / CSV_NAME

df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")

# 清理
use_col = "content" if "content" in df.columns else "text"
df = df[[use_col, "label"]].rename(columns={use_col: "text"}).dropna()
df["label"] = df["label"].astype(int)
df["text"] = (df["text"].astype(str)
              .str.replace("\u200b","", regex=False)
              .str.replace("\xa0"," ", regex=False)
              .str.strip())

# 給 doc_id
df["doc_id"] = [f"d{i}" for i in range(len(df))]

# 以「文件」為單位切分
doc_tbl = df[["doc_id","label"]].drop_duplicates()
train_docs, tmp_docs = train_test_split(
    doc_tbl, test_size=0.10, random_state=42, stratify=doc_tbl["label"]
)
val_docs, test_docs = train_test_split(
    tmp_docs, test_size=0.50, random_state=42, stratify=tmp_docs["label"]
)

train_df = df[df["doc_id"].isin(train_docs["doc_id"])].reset_index(drop=True)
val_df   = df[df["doc_id"].isin(val_docs["doc_id"])].reset_index(drop=True)
test_df  = df[df["doc_id"].isin(test_docs["doc_id"])].reset_index(drop=True)

# -----------------------
# Tokenizer

MODEL_NAME = "bert-base-multilingual-cased"
tokenizer = BertTokenizerFast.from_pretrained(MODEL_NAME)

# -----------------------
# 滑動視窗

MAX_LEN = 512
STRIDE  = 128

def explode_df_to_windows_for_trainer(df_split, tokenizer, max_length=512, stride=128):
    d = HFDataset.from_pandas(df_split[["doc_id","text","label"]])

    def _tok(batch):
        out = tokenizer(
            batch["text"],
            truncation=True,
            max_length=max_length,
            stride=stride,
            return_overflowing_tokens=True,
            return_attention_mask=True,
            return_offsets_mapping=False,
        )
        mapping = out.pop("overflow_to_sample_mapping")
        out["label"]  = [batch["label"][i]  for i in mapping]
        out["doc_id"] = [batch["doc_id"][i] for i in mapping]
        return out

    dd = d.map(_tok, batched=True, remove_columns=["text"])
    dd = dd.cast_column("label", Value("int64"))
    return dd

train_dataset = explode_df_to_windows_for_trainer(train_df, tokenizer, MAX_LEN, STRIDE)
val_dataset   = explode_df_to_windows_for_trainer(val_df,   tokenizer, MAX_LEN, STRIDE)
test_dataset  = explode_df_to_windows_for_trainer(test_df,  tokenizer, MAX_LEN, STRIDE)

print("windows →", {
    "train": len(train_dataset),
    "val": len(val_dataset),
    "test": len(test_dataset)
})


# Model & Trainer

model = BertForSequenceClassification.from_pretrained(MODEL_NAME, num_labels=2)
device = "cuda" if torch.cuda.is_available() else "cpu"
model.to(device)

collator = DataCollatorWithPadding(tokenizer=tokenizer, pad_to_multiple_of=8)

print("PY:", sys.executable)
print("TRANSFORMERS_VER:", transformers.__version__)

training_args = TrainingArguments(
    output_dir=str(CKPT_DIR),
    num_train_epochs=3,
    learning_rate=2e-5,
    per_device_train_batch_size=16,
    per_device_eval_batch_size=16,
    dataloader_pin_memory=True,

    logging_dir=str(TB_DIR),
    logging_strategy="epoch",
    evaluation_strategy="epoch",
    save_strategy="epoch",
    save_total_limit=3,

    load_best_model_at_end=True,
    metric_for_best_model="eval_accuracy",
    greater_is_better=True,
    report_to=["tensorboard"],

    weight_decay=0.01,
    fp16=torch.cuda.is_available(),
    no_cuda=False
)
torch.cuda.empty_cache()
trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=val_dataset,
    data_collator=collator,
    compute_metrics=compute_metrics,
)
model.config.use_cache = False

print(f"Trainer 使用裝置：{trainer.args.device}")
if torch.cuda.is_available():
    print("CUDA 可用：", torch.cuda.get_device_name(0))
    print("CUDA 版本：", torch.version.cuda)
else:
    print("CUDA 不可用，使用 CPU")

# 訓練
train_result = trainer.train()

# 驗證集
eval_results_val = trainer.evaluate(eval_dataset=val_dataset)
print("Validation 結果：")
for k, v in eval_results_val.items():
    print(f"{k}: {v:.4f}" if isinstance(v, float) else f"{k}: {v}")

# 測試集
pred_out = trainer.predict(test_dataset)
test_logits = pred_out.predictions
test_preds  = test_logits.argmax(axis=-1)
test_labels = pred_out.label_ids

test_precision, test_recall, test_f1, _ = precision_recall_fscore_support(
    test_labels, test_preds, average='binary'
)
test_acc = accuracy_score(test_labels, test_preds)
print("\nTest 結果：")
print(f"accuracy: {test_acc:.4f} | precision: {test_precision:.4f} | recall: {test_recall:.4f} | f1: {test_f1:.4f}")

report = classification_report(test_labels, test_preds, digits=4)
print("\nClassification Report (Test):\n", report)

# 逐 epoch 回放 Acc 曲線
from pathlib import Path as _Path
ckpt_paths = sorted(_Path(CKPT_DIR).glob("checkpoint-*"),key=lambda p: int(p.name.split("-")[-1]))
epochs_curve = list(range(1, len(ckpt_paths) + 1))

train_acc_curve, val_acc_curve = [], []
for ep, ckpt in zip(epochs_curve, ckpt_paths):
    m = BertForSequenceClassification.from_pretrained(ckpt).to(device)
    trainer.model = m
    train_metrics = trainer.evaluate(eval_dataset=train_dataset, metric_key_prefix="train")
    val_metrics   = trainer.evaluate(eval_dataset=val_dataset,   metric_key_prefix="val")
    train_acc_curve.append(float(train_metrics.get("train_accuracy", float("nan"))))
    val_acc_curve.append(float(val_metrics.get("val_accuracy", float("nan"))))
    print(f"[epoch {ep}] train_acc={train_acc_curve[-1]:.4f}  val_acc={val_acc_curve[-1]:.4f}")

history = trainer.state.log_history
train_loss_list = [(r["epoch"], r["loss"]) for r in history if "loss" in r and "epoch" in r]
eval_loss_list  = [(r["epoch"], r["eval_loss"]) for r in history if "eval_loss" in r and "epoch" in r]

plt.figure(figsize=(8,5))
if train_loss_list:
    plt.plot([x[0] for x in train_loss_list], [x[1] for x in train_loss_list], label="Train Loss")
if eval_loss_list:
    xs = [x[0] for x in eval_loss_list]; ys = [x[1] for x in eval_loss_list]
    plt.plot(xs, ys, linestyle="--", linewidth=2, label="Eval Loss")
    plt.scatter(xs, ys, s=30)
plt.xlabel("Epoch"); plt.ylabel("Loss"); plt.title("Training and Validation Loss")
plt.legend(); plt.grid(True); plt.tight_layout()
plt.savefig(LOGS_DIR / "loss.png", dpi=150)

if train_acc_curve and val_acc_curve:
    plt.figure(figsize=(8,5))
    plt.plot(epochs_curve, train_acc_curve, marker="o", label="Train Accuracy")
    plt.plot(epochs_curve, val_acc_curve, marker="s", label="Validation Accuracy")
    plt.xlabel("Epoch"); plt.ylabel("Accuracy"); plt.title("Train vs Validation Accuracy")
    plt.legend(); plt.grid(True); plt.tight_layout()
    plt.savefig(LOGS_DIR / "train_vs_val_accuracy.png", dpi=150)

cm = confusion_matrix(test_labels, test_preds, labels=[0,1])
fig1, ax1 = plt.subplots(figsize=(5,4))
ConfusionMatrixDisplay(confusion_matrix=cm, display_labels=[0,1]).plot(ax=ax1, values_format='d', colorbar=False)
ax1.set_title("Confusion Matrix (Test)")
plt.tight_layout()
plt.savefig(LOGS_DIR / "confusion_matrix_test.png", dpi=150)
plt.close('all')

with open(LOGS_DIR / "test_metrics.json", "w", encoding="utf-8") as f:
    json.dump({
        "val": {k: float(v) if isinstance(v, (int, float)) else v for k, v in eval_results_val.items()},
        "test": {"accuracy": float(test_acc), "precision": float(test_precision), "recall": float(test_recall), "f1": float(test_f1)}
    }, f, ensure_ascii=False, indent=2)

trainer.save_model(MODEL_DIR)          # 這會存 best（因為 load_best_model_at_end=True）
tokenizer.save_pretrained(MODEL_DIR)
print("\n✅ 模型與 tokenizer 已儲存至", MODEL_DIR)
print("✅ 圖片與 TB/ckpt 皆位於：", LOGS_DIR, TB_DIR, CKPT_DIR)

