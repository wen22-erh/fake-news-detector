const emptyStateEl = document.getElementById("emptyState");
const detailCardEl = document.getElementById("detailCard");
const badgeEl = document.getElementById("badge");
const dataStatusEl = document.getElementById("dataStatus");
const analysisTimeEl = document.getElementById("analysisTime");
const urlTextEl = document.getElementById("urlText");
const detailedReasonEl = document.getElementById("detailedReason");
const riskFactorsEl = document.getElementById("riskFactors");
const suggestedActionsEl = document.getElementById("suggestedActions");
const analysisMetaEl = document.getElementById("analysisMeta");
const pinBtn = document.getElementById("pinBtn");
const refreshBtn = document.getElementById("refreshBtn");
const closeBtn = document.getElementById("closeBtn");
const unpinAndCloseBtn = document.getElementById("unpinAndCloseBtn");

let currentItem = null;
let isPinned = false;

init();

async function init() {
  await loadCurrentItem();
  chrome.storage.onChanged.addListener(async (changes, areaName) => {
    if (areaName !== "local") return;

    if (changes.detailItem) {
      currentItem = changes.detailItem.newValue || null;
      await render();
    }

    if (changes.pinnedItem) {
      const newPinned = changes.pinnedItem.newValue || null;
      isPinned = buildItemKey(newPinned) === buildItemKey(currentItem);
      updatePinButton();
    }
  });
}

refreshBtn.addEventListener("click", async () => {
  if (!currentItem) return;
  const response = await sendMessage({
    type: "REFRESH_DETAILS",
    url: currentItem.requested_url || currentItem.normalized_url
  });

  if (response.ok) {
    currentItem = response.data;
    await chrome.storage.local.set({ detailItem: currentItem });
    await render();
  }
});

closeBtn.addEventListener("click", async () => {
  await sendMessage({ type: "CLEAR_SIDE_PANEL" });
});

pinBtn.addEventListener("click", async () => {
  if (!currentItem) return;

  const response = await sendMessage({
    type: "TOGGLE_PIN",
    item: currentItem
  });

  if (response.ok) {
    isPinned = response.data.pinned;
    updatePinButton();
  }
});

unpinAndCloseBtn.addEventListener("click", async () => {
  const { pinnedItem } = await chrome.storage.local.get(["pinnedItem"]);
  if (buildItemKey(pinnedItem) === buildItemKey(currentItem)) {
    await sendMessage({
      type: "TOGGLE_PIN",
      item: currentItem
    });
  }
  await sendMessage({ type: "CLEAR_SIDE_PANEL" });
});

async function loadCurrentItem() {
  const { detailItem, pinnedItem } = await chrome.storage.local.get(["detailItem", "pinnedItem"]);
  currentItem = detailItem || null;
  isPinned = buildItemKey(pinnedItem) === buildItemKey(currentItem);
  await render();
}

async function render() {
  if (!currentItem) {
    emptyStateEl.classList.remove("hidden");
    detailCardEl.classList.add("hidden");
    return;
  }

  emptyStateEl.classList.add("hidden");
  detailCardEl.classList.remove("hidden");

  badgeEl.textContent = `${currentItem.risk_level} - ${currentItem.risk_label}`;
  badgeEl.className = `badge ${riskClassName(currentItem.risk_level)}`;
  dataStatusEl.textContent = currentItem.data_status_label || currentItem.data_status || "未知";
  analysisTimeEl.textContent = formatTime(currentItem.analysis_time);
  urlTextEl.textContent = currentItem.requested_url || currentItem.normalized_url || "";
  detailedReasonEl.textContent = currentItem.detailed_reason || currentItem.short_reason || "無";
  analysisMetaEl.textContent = JSON.stringify(currentItem.analysis_metadata || {}, null, 2);

  renderList(riskFactorsEl, currentItem.risk_factors || []);
  renderList(suggestedActionsEl, currentItem.suggested_actions || []);
  updatePinButton();
}

function renderList(container, items) {
  container.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.textContent = "無";
    container.appendChild(li);
    return;
  }

  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item;
    container.appendChild(li);
  }
}

function updatePinButton() {
  pinBtn.textContent = isPinned ? "取消釘選" : "釘選";
}

function riskClassName(level) {
  switch (Number(level)) {
    case 1: return "risk-safe";
    case 2: return "risk-low";
    case 3: return "risk-unknown";
    case 4: return "risk-suspicious";
    case 5: return "risk-danger";
    default: return "risk-unknown";
  }
}

function buildItemKey(item) {
  if (!item) return null;
  return item.normalized_url || item.requested_url || item.url || null;
}

function formatTime(value) {
  if (!value) return "未知";
  try {
    return new Date(value).toLocaleString();
  } catch (_) {
    return value;
  }
}

function sendMessage(message) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(message, (response) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(response);
    });
  });
}
