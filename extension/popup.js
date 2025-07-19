document.addEventListener("DOMContentLoaded", function () {
    chrome.tabs.query({}, function (tabs) {
        var urls = tabs.map((tab) => tab.url);
        document.getElementById("msgLabel").innerText = "URLS : \n " + urls.join("\n");

        fetch("http://localhost:5000/save_url", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ urls: urls }),
        });

        tabs.forEach((tab) => {
            chrome.scripting.executeScript({
                target: { tabId: tab.id },
            });
        });
    });
});
