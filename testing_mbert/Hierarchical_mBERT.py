import os
import sys
import json
import random
import shutil
from pathlib import Path
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import matplotlib.pyplot as plt
import transformers
from matplotlib.ticker import MaxNLocator, MultipleLocator
from transformers import EarlyStoppingCallback

from torch.utils.data import Dataset
from transformers import (
    BertTokenizerFast, 
    Trainer, 
    TrainingArguments, 
    TrainerCallback,
    set_seed,
    PreTrainedModel,
    BertModel,
    BertConfig
)
from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support,
    classification_report, confusion_matrix, ConfusionMatrixDisplay
)

# =======================
# 路徑設定與環境清理 (保留原設定)
# =======================
HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE / "gemma4_bbc500_4300_dp0.3"
CKPT_DIR  = MODEL_DIR / "checkpoints"
LOGS_DIR  = MODEL_DIR / "logs"
TB_DIR    = MODEL_DIR / "tb_logs"
LABEL_NAMES = ["Fake", "Real"]   # label 0 = Fake, label 1 = Real

CKPT_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
TB_DIR.mkdir(parents=True, exist_ok=True)
os.environ["TENSORBOARD_LOGGING_DIR"] = str(TB_DIR)

# 每次訓練前清掉舊 checkpoint，避免混入上次結果
shutil.rmtree(CKPT_DIR, ignore_errors=True)
CKPT_DIR.mkdir(parents=True, exist_ok=True)

# =======================
# 固定 Seed (保留原設定)
# =======================
SEED = 42
random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)
if torch.cuda.is_available():
    torch.cuda.manual_seed_all(SEED)
set_seed(SEED)

# =======================
# 自定義 Document-level 評估指標
# =======================
def compute_metrics(pred):
    labels = pred.label_ids
    preds  = pred.predictions.argmax(-1)

    precision, recall, f1, _ = precision_recall_fscore_support(
        labels, preds, average="binary", zero_division=0
    )
    macro_precision, macro_recall, macro_f1, _ = precision_recall_fscore_support(
        labels, preds, average="macro", zero_division=0
    )
    acc = accuracy_score(labels, preds)

    return {
        "accuracy": float(acc),
        "f1": float(f1),
        "precision": float(precision),
        "recall": float(recall),
        "macro_f1": float(macro_f1),
        "macro_precision": float(macro_precision),
        "macro_recall": float(macro_recall),
    }

# =======================
# 讀資料與預處理 (完全保留原邏輯)
# =======================
CSV_NAME = "gemma4_bbc500_4000.csv"
CSV_PATH = HERE / CSV_NAME

df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")

use_col = "content" if "content" in df.columns else "text"
df = df[[use_col, "label"]].rename(columns={use_col: "text"}).dropna().copy()

df["label"] = df["label"].astype(int)
df["text"] = (
    df["text"].astype(str)
      .str.replace("\u200b", "", regex=False)
      .str.replace("\xa0", " ", regex=False)
      .str.strip()
)

# 移除空字串
df = df[df["text"].ne("")].reset_index(drop=True)
df["doc_id"] = [f"d{i}" for i in range(len(df))]

# 以文件為單位切分 (Stratified Split)
doc_tbl = df[["doc_id", "label"]].drop_duplicates()
train_docs, tmp_docs = train_test_split(
    doc_tbl, test_size=0.10, random_state=SEED, stratify=doc_tbl["label"]
)
val_docs, test_docs = train_test_split(
    tmp_docs, test_size=0.50, random_state=SEED, stratify=tmp_docs["label"]
)

train_df = df[df["doc_id"].isin(train_docs["doc_id"])].reset_index(drop=True)
val_df = df[df["doc_id"].isin(val_docs["doc_id"])].reset_index(drop=True)
test_df  = df[df["doc_id"].isin(test_docs["doc_id"])].reset_index(drop=True)

# =======================
# 外部驗證集：LIAR（只驗證，不參與訓練）
# =======================
LIAR_CSV_NAME = "sample_2000_liar_nojs.csv"   # 改成你的檔名
LIAR_CSV_PATH = HERE / LIAR_CSV_NAME

liar_df = pd.read_csv(LIAR_CSV_PATH, encoding="utf-8-sig")

liar_use_col = "content" if "content" in liar_df.columns else "text"
liar_df = liar_df[[liar_use_col, "label"]].rename(columns={liar_use_col: "text"}).dropna().copy()

liar_df["label"] = liar_df["label"].astype(int)
liar_df["text"] = (
    liar_df["text"].astype(str)
        .str.replace("\u200b", "", regex=False)
        .str.replace("\xa0", " ", regex=False)
        .str.strip()
)

liar_df = liar_df[liar_df["text"].ne("")].reset_index(drop=True)
liar_df["doc_id"] = [f"liar_{i}" for i in range(len(liar_df))]

print("external liar docs →", len(liar_df))

# # =======================
# # 只抽 2000 筆 training docs 做實驗
# # =======================
# TRAIN_SAMPLE_SIZE = 2000  # 想改 5000 / 10000 就改這裡

# train_doc_tbl = train_df[["doc_id", "label"]].drop_duplicates()

# if TRAIN_SAMPLE_SIZE < len(train_doc_tbl):
#     sampled_train_docs, _ = train_test_split(
#         train_doc_tbl,
#         train_size=TRAIN_SAMPLE_SIZE,
#         random_state=SEED,
#         stratify=train_doc_tbl["label"]
#     )

#     train_df = train_df[train_df["doc_id"].isin(sampled_train_docs["doc_id"])].reset_index(drop=True)



print("documents →", {
    "train_docs": len(train_df),
    "val_docs": len(val_df),
    "test_docs": len(test_df),
})

# =======================
# Tokenizer
# =======================
MODEL_NAME = "bert-base-multilingual-cased"
tokenizer = BertTokenizerFast.from_pretrained(MODEL_NAME)

# =======================
# 階層式資料集與 3D Collator (新架構核心 1)
# =======================
MAX_LEN = 512
STRIDE  = 128
MAX_POSITION_CHUNKS = 512

class DocumentDataset(Dataset):
    def __init__(self, df, tokenizer, max_seq_len=512, stride=128):
        self.labels = df["label"].tolist()
        self.docs = df["text"].tolist()
        self.doc_ids = df["doc_id"].tolist()
        self.tokenizer = tokenizer
        self.max_seq_len = max_seq_len
        self.stride = stride

    def __len__(self):
        return len(self.docs)

    def __getitem__(self, idx):
        text = self.docs[idx]
        encoding = self.tokenizer(
            text,
            truncation=True,
            max_length=self.max_seq_len,
            stride=self.stride,
            return_overflowing_tokens=True,
            return_attention_mask=True,
            padding=False
        )

        return {
            "input_ids": encoding["input_ids"],              # 不再 [:MAX_CHUNKS]
            "attention_mask": encoding["attention_mask"],    # 不再 [:MAX_CHUNKS]
            "label": self.labels[idx],
            "doc_id": self.doc_ids[idx]
        }

def document_collate_fn(batch):
    labels = torch.tensor([item["label"] for item in batch], dtype=torch.long)
    max_chunks = max(len(item["input_ids"]) for item in batch)
    
    batch_input_ids, batch_attention_mask, batch_chunk_mask = [], [], []
    pad_token_id = tokenizer.pad_token_id
    
    for item in batch:
        num_chunks = len(item["input_ids"])
        # chunk_mask: 1 為真實 chunk，0 為對齊用的 padding chunk
        batch_chunk_mask.append([1] * num_chunks + [0] * (max_chunks - num_chunks))
        
        doc_input_ids, doc_attention_mask = [], []
        for i in range(max_chunks):
            if i < num_chunks:
                ids = item["input_ids"][i]
                mask = item["attention_mask"][i]
                pad_len = MAX_LEN - len(ids)
                doc_input_ids.append(ids + [pad_token_id] * pad_len)
                doc_attention_mask.append(mask + [0] * pad_len)
            else:
                doc_input_ids.append([pad_token_id] * MAX_LEN)
                doc_attention_mask.append([0] * MAX_LEN)
                
        batch_input_ids.append(doc_input_ids)
        batch_attention_mask.append(doc_attention_mask)
        
    return {
        "input_ids": torch.tensor(batch_input_ids, dtype=torch.long),
        "attention_mask": torch.tensor(batch_attention_mask, dtype=torch.long),
        "chunk_mask": torch.tensor(batch_chunk_mask, dtype=torch.long),
        "labels": labels
    }
train_dataset = DocumentDataset(train_df, tokenizer, MAX_LEN, STRIDE)
val_dataset   = DocumentDataset(val_df, tokenizer, MAX_LEN, STRIDE)
test_dataset  = DocumentDataset(test_df, tokenizer, MAX_LEN, STRIDE)
liar_dataset  = DocumentDataset(liar_df, tokenizer, MAX_LEN, STRIDE)

# =======================
# 階層式模型架構 (新架構核心 2)
# =======================
def masked_max_pooling_with_indices(tensor, mask, dim):
    mask_expanded = mask.unsqueeze(-1).expand_as(tensor)
    fill_value = torch.finfo(tensor.dtype).min
    tensor_masked = tensor.masked_fill(mask_expanded == 0, fill_value)
    # 這裡不要用 _，把 indices 接下來
    max_values, indices = torch.max(tensor_masked, dim=dim)
    return max_values, indices

def masked_max_pooling(tensor, mask, dim):
    mask_expanded = mask.unsqueeze(-1).expand_as(tensor)
    fill_value = torch.finfo(tensor.dtype).min
    tensor_masked = tensor.masked_fill(mask_expanded == 0, fill_value)
    max_values, _ = torch.max(tensor_masked, dim=dim)
    return max_values

class HierarchicalBertConfig(BertConfig):
    def __init__(self, max_chunks=512, **kwargs):
        super().__init__(**kwargs)
        self.max_chunks = max_chunks

class HierarchicalDocumentModel(PreTrainedModel):

    config_class = HierarchicalBertConfig

    def __init__(self, config):
        super().__init__(config)

        self.num_labels = config.num_labels
        self.bert = BertModel(config)

        # position encoding
        self.position_embeddings = nn.Embedding(config.max_chunks, config.hidden_size)

        # Dropout：降低 overfitting
        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        # LayerNorm：穩定 chunk representation
        self.chunk_layer_norm = nn.LayerNorm(config.hidden_size)
        self.doc_layer_norm = nn.LayerNorm(config.hidden_size)

        # document-level multi-head self-attention
        self.chunk_attention = nn.MultiheadAttention(
            embed_dim=config.hidden_size,
            num_heads=8,
            dropout=config.hidden_dropout_prob,
            batch_first=True
        )

        # classifier 前再加一層 dropout
        self.classifier = nn.Linear(config.hidden_size, self.num_labels)

        self.post_init()

    def forward(self, input_ids, attention_mask, chunk_mask=None, labels=None, **kwargs):
        batch_size, num_chunks, seq_len = input_ids.size()

        # 1. mBERT & chunk-level masked max pooling
        flat_input_ids = input_ids.view(-1, seq_len)
        flat_attention_mask = attention_mask.view(-1, seq_len)

        bert_outputs = self.bert(
            input_ids=flat_input_ids,
            attention_mask=flat_attention_mask
        )

        chunk_embeddings = masked_max_pooling(
            bert_outputs.last_hidden_state,
            flat_attention_mask,
            dim=1
        )

        chunk_embeddings = chunk_embeddings.view(batch_size, num_chunks, -1)

        # 2. position encoding
        if num_chunks > self.config.max_chunks:
            raise ValueError(
                f"本批次文件的 chunk 數 ({num_chunks}) 超過目前 position embedding 上限 "
                f"({self.config.max_chunks})，請把 MAX_POSITION_CHUNKS 調大。"
            )

        position_ids = torch.arange(
            num_chunks,
            dtype=torch.long,
            device=chunk_embeddings.device
        ).unsqueeze(0).expand(batch_size, -1)

        chunk_embeddings = chunk_embeddings + self.position_embeddings(position_ids)

        # 這裡加 LayerNorm + Dropout
        chunk_embeddings = self.chunk_layer_norm(chunk_embeddings)
        chunk_embeddings = self.dropout(chunk_embeddings)

        if chunk_mask is None:
            chunk_mask = torch.ones(
                (batch_size, num_chunks),
                dtype=torch.long,
                device=chunk_embeddings.device
            )

        # 3. document-level multi-head self-attention
        attn_output, _ = self.chunk_attention(
            query=chunk_embeddings,
            key=chunk_embeddings,
            value=chunk_embeddings,
            key_padding_mask=(chunk_mask == 0),
            need_weights=False
        )

        # 加 residual connection + LayerNorm
        attn_output = self.chunk_layer_norm(chunk_embeddings + self.dropout(attn_output))

        # 4. document-level masked max pooling
        doc_embedding = masked_max_pooling(attn_output, chunk_mask, dim=1)

        # classifier 前加 LayerNorm + Dropout
        doc_embedding = self.doc_layer_norm(doc_embedding)
        doc_embedding = self.dropout(doc_embedding)

        # 5. classifier
        logits = self.classifier(doc_embedding)

        loss = None
        if labels is not None:
            loss = nn.CrossEntropyLoss()(
                logits.view(-1, self.num_labels),
                labels.view(-1)
            )

        return {"loss": loss, "logits": logits} if loss is not None else {"logits": logits}
    @torch.no_grad()
    def extract_features(self, input_ids, attention_mask, chunk_mask=None):
        batch_size, num_chunks, seq_len = input_ids.size()

        flat_input_ids = input_ids.view(-1, seq_len)
        flat_attention_mask = attention_mask.view(-1, seq_len)

        bert_outputs = self.bert(
            input_ids=flat_input_ids,
            attention_mask=flat_attention_mask
        )

        chunk_embeddings, chunk_indices = masked_max_pooling_with_indices(
            bert_outputs.last_hidden_state,
            flat_attention_mask,
            dim=1
        )

        chunk_embeddings = chunk_embeddings.view(batch_size, num_chunks, -1)
        chunk_indices = chunk_indices.view(batch_size, num_chunks, -1)

        if num_chunks > self.config.max_chunks:
            raise ValueError(
                f"本批次文件的 chunk 數 ({num_chunks}) 超過目前 position embedding 上限 "
                f"({self.config.max_chunks})，請把 MAX_POSITION_CHUNKS 調大。"
            )

        position_ids = torch.arange(
            num_chunks,
            dtype=torch.long,
            device=chunk_embeddings.device
        ).unsqueeze(0).expand(batch_size, -1)

        chunk_embeddings = chunk_embeddings + self.position_embeddings(position_ids)

        # 與正式 forward 對齊
        chunk_embeddings = self.chunk_layer_norm(chunk_embeddings)
        chunk_embeddings = self.dropout(chunk_embeddings)

        if chunk_mask is None:
            chunk_mask = torch.ones(
                (batch_size, num_chunks),
                dtype=torch.long,
                device=chunk_embeddings.device
            )

        attn_output, attn_weights = self.chunk_attention(
            query=chunk_embeddings,
            key=chunk_embeddings,
            value=chunk_embeddings,
            key_padding_mask=(chunk_mask == 0),
            need_weights=True,
            average_attn_weights=True   # 保留：輸出 [B, N, N]
        )

        # 與正式 forward 對齊：residual + LayerNorm
        attn_output = self.chunk_layer_norm(
            chunk_embeddings + self.dropout(attn_output)
        )

        doc_embedding, doc_indices = masked_max_pooling_with_indices(
            attn_output,
            chunk_mask,
            dim=1
        )

        doc_embedding = self.doc_layer_norm(doc_embedding)
        doc_embedding = self.dropout(doc_embedding)

        logits = self.classifier(doc_embedding)

        return {
            "logits": logits,
            "chunk_indices": chunk_indices,          # [B, N, 768]
            "doc_indices": doc_indices,              # [B, 768]
            "attention_matrix": attn_weights,        # [B, N, N]，已平均 heads
            "doc_embedding": doc_embedding           # [B, 768]
        }
# =======================
# Model & Trainer 初始化
# =======================
device = "cuda" if torch.cuda.is_available() else "cpu"

config = HierarchicalBertConfig.from_pretrained(
    MODEL_NAME,
    num_labels=2,
    max_chunks=MAX_POSITION_CHUNKS,
    hidden_dropout_prob=0.3,
    attention_probs_dropout_prob=0.2
)

model = HierarchicalDocumentModel.from_pretrained(
    MODEL_NAME,
    config=config,
    ignore_mismatched_sizes=True
)

model.to(device)

print("PY:", sys.executable)
print("TRANSFORMERS_VER:", transformers.__version__)

training_args = TrainingArguments(
    output_dir=str(CKPT_DIR),
    num_train_epochs=5,
    learning_rate=2e-5,

    # 5090 建議先從 1 開始，穩了再試 2
    per_device_train_batch_size=1,
    per_device_eval_batch_size=1,
    gradient_accumulation_steps=16,

    dataloader_pin_memory=True,

    seed=SEED,
    data_seed=SEED,

    logging_strategy="epoch",
    eval_strategy="epoch",
    save_strategy="epoch",
    save_total_limit=20,

    load_best_model_at_end=True,
    metric_for_best_model="eval_val_loss",
    greater_is_better=False,

    report_to=["tensorboard"],
    weight_decay=0.01,
    label_smoothing_factor=0.05,

    fp16=torch.cuda.is_available(),
)
class ExternalEvalCallback(TrainerCallback):
    def __init__(self, external_dataset, name="liar"):
        self.external_dataset = external_dataset
        self.name = name
        self.trainer = None
        self._running = False
        self.enabled = True

    def on_evaluate(self, args, state, control, metrics=None, **kwargs):
        if not self.enabled:
            return control

        if self.trainer is None or self._running:
            return control

        # 只在正式 validation(eval_) 觸發，不在 train_/val_ 回放時觸發
        if metrics is None or not any(k.startswith("eval_") for k in metrics.keys()):
            return control

        self._running = True
        try:
            ext_metrics = self.trainer.evaluate(
                eval_dataset=self.external_dataset,
                metric_key_prefix=self.name
            )

            print(
                f"\n[{self.name.upper()} External Eval] "
                f"epoch={state.epoch:.2f} | "
                f"accuracy={ext_metrics.get(f'{self.name}_accuracy', float('nan')):.4f} | "
                f"f1={ext_metrics.get(f'{self.name}_f1', float('nan')):.4f} | "
                f"macro_f1={ext_metrics.get(f'{self.name}_macro_f1', float('nan')):.4f}"
            )
        finally:
            self._running = False

        return control

trainer = Trainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset={
        "train": train_dataset, 
        "val": val_dataset
    },
    data_collator=document_collate_fn,
    compute_metrics=compute_metrics,
    callbacks=[EarlyStoppingCallback(early_stopping_patience=3)]
)
liar_callback = ExternalEvalCallback(liar_dataset, name="liar")
liar_callback.trainer = trainer
trainer.add_callback(liar_callback)
print(f"Trainer 使用裝置：{trainer.args.device}")

# =======================
# 訓練與驗證
# =======================
trainer.train()

# =======================
# 從 Trainer log_history 取得 eval-mode 的 train/val loss 與 accuracy
# =======================

log_history = trainer.state.log_history

eval_logs = []
train_loop_logs = []   # 可選：保留 training mode loss，不拿來和 val loss 畫同一張圖

for log in log_history:
    if "epoch" not in log:
        continue

    ep = int(round(float(log["epoch"])))

    # A. training loop loss：model.train()，dropout 開啟
    # 這個只留作參考，不建議拿來跟 eval_val_loss 直接比較
    if "loss" in log and not any(k.startswith(("eval_", "liar_")) for k in log.keys()):
        train_loop_logs.append({
            "Epoch": ep,
            "Train_Loss_TrainMode": float(log["loss"])
        })

    # B. evaluation loss / accuracy：model.eval()，dropout 關閉
    # 這些才適合畫 Train vs Val
    has_eval_data = any(
        k in log for k in [
            "eval_train_loss",
            "eval_train_accuracy",
            "eval_val_loss",
            "eval_val_accuracy"
        ]
    )

    if has_eval_data:
        eval_logs.append({
            "Epoch": ep,
            "Train_Loss": float(log.get("eval_train_loss", np.nan)),
            "Train_Accuracy": float(log.get("eval_train_accuracy", np.nan)),
            "Val_Loss": float(log.get("eval_val_loss", np.nan)),
            "Val_Accuracy": float(log.get("eval_val_accuracy", np.nan)),
        })

# evaluation logs
df_history = pd.DataFrame(eval_logs)

if df_history.empty:
    raise ValueError("沒有抓到 eval_train/eval_val 指標，請檢查 Trainer 是否有執行 evaluation。")

# 同一個 epoch 可能有多筆 log，合併成一筆
df_history = (
    df_history
    .groupby("Epoch", as_index=False)
    .last()
    .sort_values("Epoch")
    .reset_index(drop=True)
)

# optional：把 training mode loss 也併進 CSV，但不拿來畫主要 loss 圖
if train_loop_logs:
    df_train_loop = (
        pd.DataFrame(train_loop_logs)
        .drop_duplicates(subset=["Epoch"], keep="last")
    )
    df_history = pd.merge(df_history, df_train_loop, on="Epoch", how="left")

# 檢查主要欄位是否存在
required_cols = ["Train_Loss", "Val_Loss", "Train_Accuracy", "Val_Accuracy"]
missing_cols = [c for c in required_cols if c not in df_history.columns]

if missing_cols:
    raise ValueError(f"df_history 缺少欄位：{missing_cols}")

# 如果有 NaN，代表某些 epoch 沒有完整 eval_train/eval_val log
if df_history[required_cols].isna().any().any():
    print("⚠️ df_history 有缺值，請檢查 log_history：")
    print(df_history)

csv_save_path = LOGS_DIR / "model_A_metrics.csv"
df_history.to_csv(csv_save_path, index=False, encoding="utf-8-sig")

print(f"✅ Loss 與指標已匯出至：{csv_save_path}")
print(df_history)

liar_callback.enabled = False
eval_results_val = trainer.evaluate(eval_dataset=val_dataset)
print("\nValidation 結果（Document-level）：")
for k, v in eval_results_val.items():
    print(f"{k}: {v:.4f}" if isinstance(v, float) else f"{k}: {v}")

# =======================
# LIAR confusion matrix / classification report
# =======================
liar_pred_out = trainer.predict(liar_dataset)
liar_preds  = liar_pred_out.predictions.argmax(axis=-1)
liar_labels = liar_pred_out.label_ids
liar_metrics = compute_metrics(liar_pred_out)

print("\nLIAR 預測結果（Document-level）：")
print(
    f"accuracy: {liar_metrics['accuracy']:.4f} | "
    f"precision: {liar_metrics['precision']:.4f} | "
    f"recall: {liar_metrics['recall']:.4f} | "
    f"f1: {liar_metrics['f1']:.4f} | "
    f"macro_f1: {liar_metrics['macro_f1']:.4f}"
)

liar_cm = confusion_matrix(liar_labels, liar_preds, labels=[0, 1])
print("\nConfusion Matrix (LIAR, Document-level):")
print(liar_cm)

liar_report = classification_report(
    liar_labels,
    liar_preds,
    labels=[0, 1],
    target_names=LABEL_NAMES,
    digits=4,
    zero_division=0
)
print("\nClassification Report (LIAR, Document-level):\n", liar_report)

# =======================
# 測試集評估 (保留分類報表輸出)
# =======================
pred_out = trainer.predict(test_dataset)
test_preds  = pred_out.predictions.argmax(axis=-1)
test_labels = pred_out.label_ids
test_metrics = compute_metrics(pred_out)

print("\nTest 結果（Document-level）：")
print(
    f"accuracy: {test_metrics['accuracy']:.4f} | "
    f"precision: {test_metrics['precision']:.4f} | "
    f"recall: {test_metrics['recall']:.4f} | "
    f"f1: {test_metrics['f1']:.4f} | "
    f"macro_f1: {test_metrics['macro_f1']:.4f}"
)
report_doc = classification_report(
    test_labels,
    test_preds,
    labels=[0, 1],
    target_names=LABEL_NAMES,
    digits=4,
    zero_division=0
)
print("\nClassification Report (Test, Document-level):\n", report_doc)


# =======================
# 繪圖模組：Loss, Accuracy, Confusion Matrix
# =======================
# 將 df_history 的數據轉換為 list 供繪圖使用
epochs_curve = df_history["Epoch"].tolist()

# 這裡的 Train_Loss 是 eval_train_loss，也就是 dropout 關閉後在 train_dataset 上評估的 loss
train_loss_curve = df_history["Train_Loss"].tolist()
val_loss_curve = df_history["Val_Loss"].tolist()

train_acc_curve = df_history["Train_Accuracy"].tolist()
val_acc_curve = df_history["Val_Accuracy"].tolist()

LABEL_NAMES = ["Fake", "Real"]   # 如果你的 label 定義相反，就改成 ["Real", "Fake"]


LABEL_NAMES = ["Fake", "Real"]   # 如果你的 label 定義相反，就改成 ["Real", "Fake"]

# ---------- Loss 圖 ----------
fig_width = max(8, len(epochs_curve) * 0.8)

fig, ax = plt.subplots(figsize=(fig_width, 5))

ax.plot(
    epochs_curve,
    train_loss_curve,
    marker="o",
    label="Train Loss"
)

ax.plot(
    epochs_curve,
    val_loss_curve,
    marker="s",
    linestyle="--",
    label="Validation Loss"
)

ax.set_xlabel("Epoch")
ax.set_ylabel("Loss")
ax.set_title("Training and Validation Loss")

ax.set_xticks(epochs_curve)
ax.xaxis.set_major_locator(MaxNLocator(integer=True))
ax.yaxis.set_major_locator(MultipleLocator(0.25))
ax.grid(False)
ax.legend()

plt.tight_layout()
plt.savefig(LOGS_DIR / "loss.png", dpi=150)
plt.close(fig)


# ---------- Accuracy 圖 ----------
epochs_curve = df_history['Epoch'].tolist()
train_acc_curve = df_history['Train_Accuracy'].tolist()  # 新增這行
val_acc_curve = df_history['Val_Accuracy'].tolist()

fig_width = max(8, len(epochs_curve) * 0.8)
fig, ax = plt.subplots(figsize=(fig_width, 5))

# 畫 Train Accuracy (實線 + 圓點)
ax.plot(
    epochs_curve,
    train_acc_curve,
    marker="o",
    label="Train Accuracy",
    color="blue"
)

# 畫 Validation Accuracy (虛線 + 方塊)
ax.plot(
    epochs_curve,
    val_acc_curve,
    marker="s",
    linestyle="--",
    label="Validation Accuracy",
    color="orange"
)

ax.set_xlabel("Epoch")
ax.set_ylabel("Accuracy")
ax.set_title("Training and Validation Accuracy (Document-level)")

ax.set_xticks(epochs_curve)
ax.xaxis.set_major_locator(MaxNLocator(integer=True))
ax.yaxis.set_major_locator(MultipleLocator(0.025))
ax.grid(True, linestyle=":", alpha=0.6)  # 加上淺色網格比較好看重疊度
ax.legend()

plt.tight_layout()
plt.savefig(LOGS_DIR / "accuracy_overlap.png", dpi=150)  # 換個檔名
plt.close(fig)


# ---------- Test Confusion Matrix 圖 ----------
cm_doc = confusion_matrix(test_labels, test_preds, labels=[0, 1])

fig, ax = plt.subplots(figsize=(5, 4))

ConfusionMatrixDisplay(
    confusion_matrix=cm_doc,
    display_labels=LABEL_NAMES
).plot(
    ax=ax,
    values_format="d",
    colorbar=False
)

ax.set_title("Confusion Matrix (Test, Document-level)")
ax.set_xlabel("Predicted label")
ax.set_ylabel("Ground truth label")

plt.tight_layout()
plt.savefig(LOGS_DIR / "confusion_matrix_test_document_level.png", dpi=150)
plt.close(fig)


# ---------- LIAR Confusion Matrix 圖 ----------
fig, ax = plt.subplots(figsize=(5, 4))

ConfusionMatrixDisplay(
    confusion_matrix=liar_cm,
    display_labels=LABEL_NAMES
).plot(
    ax=ax,
    values_format="d",
    colorbar=False
)

ax.set_title("Confusion Matrix (LIAR, Document-level)")
ax.set_xlabel("Predicted label")
ax.set_ylabel("Ground truth label")

plt.tight_layout()
plt.savefig(LOGS_DIR / "confusion_matrix_liar_document_level.png", dpi=150)
plt.close(fig)

plt.close("all")

# =======================
# JSON 輸出 (簡化為僅輸出 Document-level)
# =======================
with open(LOGS_DIR / "test_metrics.json", "w", encoding="utf-8") as f:
    json.dump({
        "seed": SEED,
        "best_model_checkpoint": trainer.state.best_model_checkpoint,
        "validation_metrics": {
            k: float(v) if isinstance(v, (int, float, np.floating)) else v
            for k, v in eval_results_val.items()
        },
        "test_metrics": test_metrics,
        "num_test_docs": len(test_df)
    }, f, ensure_ascii=False, indent=2)

# =======================
# 儲存 Best Model
# =======================
best_ckpt = trainer.state.best_model_checkpoint
if best_ckpt is not None:
    print("\n重新載入最佳 checkpoint：", best_ckpt)
    trainer.model = HierarchicalDocumentModel.from_pretrained(best_ckpt, config=config).to(device)

trainer.save_model(MODEL_DIR)
tokenizer.save_pretrained(MODEL_DIR)

print("\n✅ 模型與 tokenizer 已儲存至", MODEL_DIR)
print("✅ 圖片與 TB/ckpt 皆位於：", LOGS_DIR, TB_DIR, CKPT_DIR)