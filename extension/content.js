// === 1. 初始化 UI 元件 ===
let box = document.createElement("div");
box.id = "box";
box.textContent = "content";
document.body.appendChild(box);

let checkimg = document.createElement("img");
checkimg.id = "checkimg";
checkimg.src = chrome.runtime.getURL("check.png");
checkimg.className = "result-icon";

let crossimg = document.createElement("img");
crossimg.id = "crossimg";
crossimg.src = chrome.runtime.getURL("cross.jpg");
crossimg.className = "result-icon";

// 新增：對應流程圖的「轉圈圖示」 (可用 GIF 或 CSS 取代)
let loadingIcon = document.createElement("span");
loadingIcon.textContent = " ⏳ (分析中...)";
loadingIcon.className = "result-icon loading-icon";

// === 2. 建立全域 Map 與批量查詢邏輯 ===
let urlMap = {};
let lastUrl = null;

// 網頁載入時：抓取所有 URL 並向後端批量查詢
window.addEventListener("load", async () => {
    // 取得網頁上所有合法的 http/https 連結並去重複
    const links = Array.from(document.querySelectorAll("a"))
        .map((a) => a.href)
        .filter((href) => href && href.startsWith("http"));
    const uniqueUrls = [...new Set(links)];

    if (uniqueUrls.length === 0) return;

    // 發送批量查詢至 Flask 後端 (對應你修改後的 /check_urls_batch API)
    try {
        const response = await fetch("http://localhost:5000/check_urls_batch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ urls: uniqueUrls }),
        });

        if (response.ok) {
            const resultData = await response.json();
            // 將後端回傳的結果寫入 Map (例如: {"url1": true, "url2": false})
            Object.assign(urlMap, resultData);
        }
    } catch (error) {
        console.error("無法連接後端：", error);
    }
});

// === 3. 滑鼠 Hover 查詢 Map 邏輯 ===
document.addEventListener("mousemove", async function (event) {
    const target = event.target.closest("a");

    if (target && target.href) {
        box.style.left = event.clientX + 10 + "px";
        box.style.top = event.clientY + 10 + "px";
        box.classList.add("visible");

        if (target.href !== lastUrl) {
            lastUrl = target.href;

            // 1. 先查 Map
            if (urlMap.hasOwnProperty(lastUrl)) {
                const status = urlMap[lastUrl];
                renderStatus(status);
            } else {
                // 2. 如果 Map 裡面沒有 (代表是動態產生的新連結)
                // 先顯示分析中
                box.innerHTML = lastUrl + loadingIcon.outerHTML;

                try {
                    // 發送單筆/批量請求去後端查 (這裡沿用你原本的批量 API 格式)
                    const response = await fetch("http://localhost:5000/check_urls_batch", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ urls: [lastUrl] }), // 只包裝這個新的 URL
                    });

                    if (response.ok) {
                        const resultData = await response.json();
                        // 把查到的結果更新到 Map 裡，下次 Hover 就不會再發 API 了
                        if (resultData[lastUrl] !== undefined) {
                            urlMap[lastUrl] = resultData[lastUrl];
                        } else {
                            urlMap[lastUrl] = "Unknown";
                        }

                        // 確認滑鼠還停留在同一個連結上，才更新畫面
                        if (lastUrl === target.href) {
                            renderStatus(urlMap[lastUrl]);
                        }
                    }
                } catch (error) {
                    console.error("即時查詢失敗:", error);
                }
            }
        }
    } else {
        box.innerHTML = "";
        box.classList.remove("visible");
        lastUrl = null;
    }
});

// 獨立出一個渲染圖示的 Function 讓程式碼比較乾淨
function renderStatus(status) {
    if (status === true) {
        box.innerHTML = lastUrl + checkimg.outerHTML;
    } else if (status === false) {
        box.innerHTML = lastUrl + crossimg.outerHTML;
    } else {
        box.innerHTML = lastUrl + loadingIcon.outerHTML;
    }
}
