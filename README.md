# fake-news-detector

新聞媒體辨識專題

[Flask 教學網址](https://devs.tw/post/448)

```mermaid
flowchart TD
    A["popup.html<br>載入 popup.js"] --> B["popup.js<br>抓取 URL"]
    B --> C["popup.js<br>傳訊息給 content.js"]
    C --> D["content.js<br>抓取網頁內文"]
    D --> E["content.js 回傳內文給 popup.js"]
    E --> F["popup.js<br>顯示/處理內文"]
    F --> G["跳轉 result.html<br>載入 result.js"]
    G --> H["result.js<br>POST 內文到 Flask"]
    H --> I["Flask (app.py)<br>寫入 csv"]
```
