const DEFAULT_API_BASE_URL = "http://120.105.129.171:5050";

document.addEventListener("DOMContentLoaded", init);

async function init() {
  const els = getElements();

  bindEvents(els);
  await loadSettings(els);
}

function getElements() {
  return {
    apiBaseUrlInput:
      document.getElementById("apiBaseUrl") ||
      document.getElementById("api-base-url") ||
      document.querySelector('[data-role="api-base-url"]'),

    saveButton:
      document.getElementById("saveSettingsBtn") ||
      document.getElementById("save-settings-btn") ||
      document.querySelector('[data-role="save-settings"]'),

    clearCacheButton:
      document.getElementById("clearCacheBtn") ||
      document.getElementById("clear-cache-btn") ||
      document.querySelector('[data-role="clear-cache"]'),

    statusText:
      document.getElementById("statusText") ||
      document.getElementById("status-text") ||
      document.querySelector('[data-role="status-text"]')
  };
}

function bindEvents(els) {
  if (els.saveButton) {
    els.saveButton.addEventListener("click", async () => {
      const rawValue = els.apiBaseUrlInput?.value || DEFAULT_API_BASE_URL;
      const normalizedValue = normalizeBaseUrl(rawValue);

      const response = await sendMessage({
        type: "SAVE_SETTINGS",
        apiBaseUrl: normalizedValue
      });

      if (!response.ok) {
        showStatus(els, `儲存失敗：${response.error || "未知錯誤"}`, true);
        return;
      }

      if (els.apiBaseUrlInput) {
        els.apiBaseUrlInput.value = normalizedValue;
      }

      showStatus(els, "設定已儲存");
    });
  }

  if (els.clearCacheButton) {
    els.clearCacheButton.addEventListener("click", async () => {
      const response = await sendMessage({
        type: "CLEAR_CACHE"
      });

      if (!response.ok) {
        showStatus(els, `清除快取失敗：${response.error || "未知錯誤"}`, true);
        return;
      }

      showStatus(els, "快取已清除");
    });
  }
}

async function loadSettings(els) {
  const response = await sendMessage({
    type: "GET_SETTINGS"
  });

  if (!response.ok) {
    if (els.apiBaseUrlInput) {
      els.apiBaseUrlInput.value = DEFAULT_API_BASE_URL;
    }
    showStatus(els, "讀取設定失敗，已使用預設值", true);
    return;
  }

  const savedBaseUrl = response.data?.apiBaseUrl || DEFAULT_API_BASE_URL;
  const normalizedValue = normalizeBaseUrl(savedBaseUrl);

  if (els.apiBaseUrlInput) {
    els.apiBaseUrlInput.value = normalizedValue;
  }

  // 若使用者之前存的是 /api 版本，這裡順手修正一次
  if (normalizedValue !== savedBaseUrl) {
    await sendMessage({
      type: "SAVE_SETTINGS",
      apiBaseUrl: normalizedValue
    });

    showStatus(els, "已自動修正 API Base URL");
  }
}

function normalizeBaseUrl(value) {
  let url = String(value || "").trim();

  if (!url) {
    return DEFAULT_API_BASE_URL;
  }

  // 去掉尾端斜線
  url = url.replace(/\/+$/, "");

  // 若舊設定誤存成 /api，這裡自動拿掉
  if (url.endsWith("/api")) {
    url = url.slice(0, -4);
  }

  return url || DEFAULT_API_BASE_URL;
}

function showStatus(els, message, isError = false) {
  if (!els.statusText) return;

  els.statusText.textContent = message;
  els.statusText.dataset.state = isError ? "error" : "success";

  window.clearTimeout(showStatus._timer);
  showStatus._timer = window.setTimeout(() => {
    if (els.statusText) {
      els.statusText.textContent = "";
      els.statusText.dataset.state = "";
    }
  }, 2500);
}

function sendMessage(message) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        resolve({
          ok: false,
          error: chrome.runtime.lastError.message
        });
        return;
      }

      resolve(
        response || {
          ok: false,
          error: "Background 沒有回傳 response"
        }
      );
    });
  });
}