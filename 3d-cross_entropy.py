import numpy as np
import matplotlib.pyplot as plt

# 1. 建立模擬的驗證集 Logits (假設真實答案全部是 1 也就是假新聞)
np.random.seed(42)
# 假設驗證集有 100 筆資料，準確率高達 95%
# 95 筆猜對：模型極度自信給出很高的正向差值 (z1 - z0 = 5)
correct_delta_z = np.random.normal(loc=5.0, scale=1.0, size=95)
# 5 筆猜錯：模型極度自信但猜錯，給出很低的負向差值 (z1 - z0 = -5)
wrong_delta_z = np.random.normal(loc=-5.0, scale=1.0, size=5)

# 合併所有資料
delta_z = np.concatenate([correct_delta_z, wrong_delta_z])
y_true = np.ones(100) # 真實標籤都是 1

# 2. 測試不同的 T 值 (從 0.5 到 5.0)
T_values = np.linspace(0.5, 5.0, 100)
losses = []

# 3. 計算每個 T 對應的交叉熵損失 (NLL)
for T in T_values:
    # 套用帶有 T 的 Softmax (Sigmoid 形式)
    P = 1 / (1 + np.exp(-delta_z / T))
    # Clip 機率避免出現 log(0) 的數學錯誤
    P = np.clip(P, 1e-7, 1 - 1e-7)
    
    # 計算 Binary Cross-Entropy Loss
    loss = -np.mean(y_true * np.log(P) + (1 - y_true) * np.log(1 - P))
    losses.append(loss)

# 4. 找出最低點 (最佳的 T)
best_idx = np.argmin(losses)
best_T = T_values[best_idx]
min_loss = losses[best_idx]

# 5. 繪圖
plt.figure(figsize=(9, 6))
plt.plot(T_values, losses, lw=2, color='blue', label='NLL Curve')
plt.scatter([best_T], [min_loss], color='red', s=100, zorder=5, 
            label=f'Optimal $T^* \\approx {best_T:.2f}$\nMin Loss $\\approx {min_loss:.3f}$')
plt.axvline(x=1.0, color='gray', linestyle='--', 
            label=f'Uncalibrated (T=1.0)\nLoss $\\approx {losses[10]:.3f}$')

plt.xlabel('Temperature ($T$)', fontsize=12)
plt.ylabel('Average Cross-Entropy Loss (NLL)', fontsize=12)
plt.title('Optimization of Temperature $T$ by Minimizing NLL', fontsize=14)
plt.legend(fontsize=11)
plt.grid(True, linestyle='--', alpha=0.7)
plt.show()