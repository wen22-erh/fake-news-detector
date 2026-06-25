import torch
import torch.nn as nn
from transformers import PreTrainedModel, BertModel, BertConfig

# =======================
# 階層式模型架構所需函數
# =======================
def masked_max_pooling_with_indices(tensor, mask, dim):
    mask_expanded = mask.unsqueeze(-1).expand_as(tensor)
    fill_value = torch.finfo(tensor.dtype).min
    tensor_masked = tensor.masked_fill(mask_expanded == 0, fill_value)
    max_values, indices = torch.max(tensor_masked, dim=dim)
    return max_values, indices

def masked_max_pooling(tensor, mask, dim):
    mask_expanded = mask.unsqueeze(-1).expand_as(tensor)
    fill_value = torch.finfo(tensor.dtype).min
    tensor_masked = tensor.masked_fill(mask_expanded == 0, fill_value)
    max_values, _ = torch.max(tensor_masked, dim=dim)
    return max_values

def masked_top2_pooling_with_indices(tensor, mask, dim):
    """
    與 masked_max_pooling_with_indices 完全相同的 masked max pooling，
    但額外回傳 margin = top1 - top2（沿 dim 取前兩名）。

    - max_values：top1，數值上等同原本的 torch.max。
    - indices   ：top1 的位置，等同原本的 argmax indices。
    - margin    ：把 top1 那個位置拿掉後，該維度 max-pool 會下降多少
                  （= top1 - top2）。代表這個被選到的 token 的「不可取代性」。

    若某維度的有效 token 不足 2 個（top2 落在被遮蔽位置），margin 設為 0，
    避免 fill_value（極小值）造成 margin 爆值。
    """
    mask_expanded = mask.unsqueeze(-1).expand_as(tensor)
    fill_value = torch.finfo(tensor.dtype).min
    tensor_masked = tensor.masked_fill(mask_expanded == 0, fill_value)

    # topk 預設 largest=True, sorted=True：index 0 為最大、index 1 為次大
    topk_values, topk_indices = torch.topk(tensor_masked, k=2, dim=dim)

    max_values    = topk_values.select(dim, 0)    # top1，等同 torch.max 的數值
    indices       = topk_indices.select(dim, 0)   # top1 位置，等同原本 indices
    second_values = topk_values.select(dim, 1)    # top2

    margin = (max_values - second_values).clamp(min=0.0)

    # 若 top2 是被遮蔽位置（該維度有效 token < 2），margin 視為 0
    invalid = second_values <= fill_value
    margin = margin.masked_fill(invalid, 0.0)

    return max_values, indices, margin

# =======================
# 自定義 Config
# =======================
class HierarchicalBertConfig(BertConfig):
    def __init__(self, max_chunks=512, **kwargs):
        super().__init__(**kwargs)
        self.max_chunks = max_chunks

# =======================
# 模型主體
# =======================
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

        # ── 改用 top-2 pooling：top1 與原本一致，另外多吐 margin = top1 - top2 ──
        chunk_embeddings, chunk_indices, chunk_margins = masked_top2_pooling_with_indices(
            bert_outputs.last_hidden_state,
            flat_attention_mask,
            dim=1
        )

        chunk_embeddings = chunk_embeddings.view(batch_size, num_chunks, -1)
        chunk_indices = chunk_indices.view(batch_size, num_chunks, -1)
        chunk_margins = chunk_margins.view(batch_size, num_chunks, -1)

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

        # =====================================================
        # 手動從 in_proj_weight 取出 V matrix
        # V = X @ Mv^T + bv
        # in_proj_weight shape: [3 * hidden_size, hidden_size]
        # 排列順序：Q | K | V，所以 V 的 weight 在最後 1/3
        # =====================================================
        hidden_size = self.config.hidden_size
        v_weight = self.chunk_attention.in_proj_weight[2 * hidden_size:, :]  # [hidden, hidden]
        v_bias = (
            self.chunk_attention.in_proj_bias[2 * hidden_size:]
            if self.chunk_attention.in_proj_bias is not None
            else 0
        )

        # value_matrix shape: [B, N, hidden]
        value_matrix = chunk_embeddings @ v_weight.T + v_bias

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
            "chunk_indices": chunk_indices,     # [B, N, 768]
            "chunk_margins": chunk_margins,     # [B, N, 768] ← 新增：top1 - top2（移除感知）
            "doc_indices": doc_indices,         # [B, 768]
            "attention_matrix": attn_weights,   # [B, N, N]，已平均 heads
            "doc_embedding": doc_embedding,     # [B, 768]
            "value_matrix": value_matrix        # [B, N, 768]
        }