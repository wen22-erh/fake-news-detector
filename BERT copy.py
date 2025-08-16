from sklearn.model_selection import train_test_split
from transformers import BertTokenizer, BertForSequenceClassification, Trainer, TrainingArguments
import pandas as pd 
import torch
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
from transformers import TrainingArguments
from datasets import Dataset
import matplotlib.pyplot as plt
from transformers import BertTokenizerFast
from datasets import Value
from transformers import DataCollatorWithPadding
import os, json

def compute_metrics(pred):
    labels = pred.label_ids
    preds = pred.predictions.argmax(-1)
    precision, recall, f1, _ = precision_recall_fscore_support(labels, preds, average='binary')
    acc = accuracy_score(labels, preds)
    return {
        'accuracy': acc,
        'f1': f1,
        'precision': precision,
        'recall': recall
    }

df = pd.read_csv("bert_dataset_eng.csv")  
df = df[["content", "label"]]
df = df.dropna()

train_texts, val_texts, train_labels, val_labels=train_test_split(
    df["content"].tolist(),df["label"].tolist(),test_size=0.2,random_state=42
)

# small_frac = 0.05
# small_train_n = max(1, int(len(train_texts) * small_frac))
# small_val_n   = max(1, int(len(val_texts)   * small_frac))

# train_texts  = train_texts[:small_train_n]
# train_labels = train_labels[:small_train_n]
# val_texts    = val_texts[:small_val_n]
# val_labels   = val_labels[:small_val_n]

# --- 正確建立 Dataset（要放列表，不能放整數）---
train_dataset = Dataset.from_dict({"text": train_texts, "label": train_labels})
val_dataset   = Dataset.from_dict({"text": val_texts,   "label": val_labels})
train_dataset = train_dataset.cast_column("label", Value("int64"))
val_dataset   = val_dataset.cast_column("label", Value("int64"))
MODEL_NAME = "bert-base-multilingual-cased"
tokenizer = BertTokenizerFast.from_pretrained(MODEL_NAME)
data_collator = DataCollatorWithPadding(tokenizer=tokenizer, pad_to_multiple_of=8)
def tokenize(batch):
    return tokenizer(batch["text"], truncation=True, max_length=512)

train_dataset = train_dataset.map(tokenize, batched=True)
val_dataset = val_dataset.map(tokenize, batched=True)


train_dataset.set_format(type="torch", columns=["input_ids", "attention_mask", "label"])
val_dataset.set_format(type="torch", columns=["input_ids", "attention_mask", "label"])

model = BertForSequenceClassification.from_pretrained(MODEL_NAME, num_labels=2)
model.to("cuda" if torch.cuda.is_available() else "cpu")

training_args=TrainingArguments(
    output_dir="./results",
    num_train_epochs=3,
    learning_rate=2e-5,
    per_device_train_batch_size=32,
    per_device_eval_batch_size=32,
    dataloader_pin_memory=True,
    # === 新增：logging / eval / save 策略 ===
    logging_dir="./results/tb_logs",
    logging_strategy="steps",
    logging_steps=100,              # 可依資料量調大/調小
    evaluation_strategy="steps",
    eval_steps=100,
    save_strategy="steps",
    save_steps=100,
    save_total_limit=2,             # 僅保留最近 2 個 checkpoint 以省空間
    load_best_model_at_end=True,    # 以最佳指標自動載回
    metric_for_best_model="accuracy",
    greater_is_better=True,
    report_to=["tensorboard"],      # 直接輸出到 TensorBoard

    weight_decay=0.01,# 正則化 防止過擬合
    fp16=True,#加速 
    no_cuda=False#使用 GPU
)
#Create a Trainer instance and pass it the model, training arguments, training and test datasets, and evaluation function. Call train() to start training.
trainer=Trainer(
    model=model,
    args=training_args,
    train_dataset=train_dataset,
    eval_dataset=val_dataset,
    data_collator=data_collator,
    compute_metrics=compute_metrics  
)
# 開訓練前加一行
model.config.use_cache = False

print(f"Trainer 使用裝置： {trainer.args.device}")
print(torch.cuda.is_available())
print(torch.cuda.get_device_name(0))
print(torch.version.cuda)

train_result = trainer.train()


eval_results = trainer.evaluate()
print("驗證結果：")
for k, v in eval_results.items():
    if isinstance(v, float):
        print(f"{k}: {v:.4f}")
    else:
        print(f"{k}: {v}")


# 繪製訓練曲線
train_loss = train_result.training_loss
history = trainer.state.log_history

train_loss_list = []
eval_loss_list = []
eval_acc_list = []

for record in history:
    if "loss" in record and "epoch" in record:  
        train_loss_list.append((record["epoch"], record["loss"]))
    if "eval_loss" in record:  
        eval_loss_list.append((record["epoch"], record["eval_loss"]))
    if "eval_accuracy" in record:  
        eval_acc_list.append((record["epoch"], record["eval_accuracy"]))
        
print("驗證結果：")
for k, v in eval_results.items():
    print(f"{k}: {v:.4f}")
    # 儲存 finetuned 模型與 tokenizer
    


os.makedirs("./results/logs", exist_ok=True)

history = trainer.state.log_history  # list[dict]
with open("./results/logs/log_history.json", "w", encoding="utf-8") as f:
    json.dump(history, f, ensure_ascii=False, indent=2)

# 也存成 CSV，方便用 Excel / Pandas 看
pd.DataFrame(history).to_csv("./results/logs/log_history.csv", index=False, encoding="utf-8")
print("✅ 已保存 log history 到 ./results/logs/log_history.{json,csv}")

# === （可選）把既有歷史回填到另一個 TensorBoard 目錄 ===
try:
    from torch.utils.tensorboard import SummaryWriter
    writer = SummaryWriter(log_dir="./results/tb_logs_backfill")
    global_step = 0
    for rec in history:
        # 優先使用原始 step；沒有就自增
        if "step" in rec:
            global_step = rec["step"]
        else:
            global_step += 1

        if "loss" in rec:
            writer.add_scalar("train/loss", rec["loss"], global_step)
        if "learning_rate" in rec:
            writer.add_scalar("train/learning_rate", rec["learning_rate"], global_step)
        if "eval_loss" in rec:
            writer.add_scalar("eval/loss", rec["eval_loss"], global_step)
        if "eval_accuracy" in rec:
            writer.add_scalar("eval/accuracy", rec["eval_accuracy"], global_step)
        if "eval_f1" in rec:
            writer.add_scalar("eval/f1", rec["eval_f1"], global_step)
        if "eval_precision" in rec:
            writer.add_scalar("eval/precision", rec["eval_precision"], global_step)
        if "eval_recall" in rec:
            writer.add_scalar("eval/recall", rec["eval_recall"], global_step)
    writer.close()
    print("✅ 已回填 TensorBoard：./results/tb_logs_backfill （tensorboard --logdir 指到這裡即可）")
except Exception as e:
    print(f"（略過回填 TB）{e}")
    
    
# Loss
plt.figure(figsize=(8, 5))
plt.plot([x[0] for x in train_loss_list], [x[1] for x in train_loss_list], label="Train Loss")
plt.plot([x[0] for x in eval_loss_list],  [x[1] for x in eval_loss_list],  label="Eval Loss")
plt.xlabel("Epoch"); plt.ylabel("Loss"); plt.title("Training and Validation Loss")
plt.legend(); plt.grid(True)
plt.savefig("./results/logs/loss.png", dpi=150, bbox_inches="tight"); plt.close()

# Accuracy
plt.figure(figsize=(8, 5))
plt.plot([x[0] for x in eval_acc_list], [x[1] for x in eval_acc_list], marker="o", label="Eval Accuracy")
plt.xlabel("Epoch"); plt.ylabel("Accuracy"); plt.title("Validation Accuracy")
plt.legend(); plt.grid(True)
plt.savefig("./results/logs/accuracy.png", dpi=150, bbox_inches="tight"); plt.close()

print("✅ 已輸出 loss/accuracy 圖檔到 ./results/logs/")



# 評估結果

print("驗證結果：")
for k, v in eval_results.items():
    print(f"{k}: {v:.4f}")
    # 儲存 finetuned 模型與 tokenizer
    
trainer.save_model("./my_finetuned_mbert")
trainer.save_state()
tokenizer.save_pretrained("./my_finetuned_mbert")
print("模型與 tokenizer 已儲存至 ./my_finetuned_mbert")
