document.addEventListener("DOMContentLoaded", function () {
    chrome.tabs.query({ active: true, lastFocusedWindow: true }, function (tabs) {
        var url = tabs[0].url;
        document.getElementById("msgLabel").innerText = "URL : " + url;

        fetch("http://localhost:5000/save_url", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url: url }),
        });

        chrome.scripting.executeScript({
            target: { tabId: tabs[0].id },
            files: ["content.js"],
        });
    });
});

// chrome.runtime.onMessage.addListener((message, sender) => {
//     if (message.action === "updateText") {
//         // const div = document.createElement("div");
//         // div.innerText = "Content :\n" + message.text;
//         // document.body.appendChild(div);

//         fetch("http://localhost:5000/save_content", {
//             method: "POST",
//             headers: { "Content-Type": "application/json" },
//             body: JSON.stringify({ content: message.text }),
//         });

//     }
// });
