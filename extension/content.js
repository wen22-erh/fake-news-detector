const text = document.body.innerText;
let lastUrl = null;
let result = null;

let box = document.createElement("div");
box.id = "box";
box.textContent = "content";
document.body.appendChild(box);
let checkimg = document.createElement("img");
checkimg.id = "checkimg";
checkimg.src = chrome.runtime.getURL("check.png");
let crossimg = document.createElement("img");
crossimg.id = "crossimg";
crossimg.src = chrome.runtime.getURL("cross.jpg");
checkimg.className = "result-icon";
crossimg.className = "result-icon";
document.addEventListener("mousemove", function (event) {
    box.style.left = event.clientX + 10 + "px";
    box.style.top = event.clientY + 10 + "px";
    const target = event.target;
    if (target.tagName === "A" && target.href) {
        if (target.href !== lastUrl) {
            lastUrl = target.href;
            console.log("查詢網址：", target.href);
            fetch("http://localhost:5000/checkurl", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url: target.href }),
            })
                .then((res) => res.json())
                .then((data) => {
                    result = data.found;
                    if (data.found) {
                        box.innerHTML = lastUrl + checkimg.outerHTML;
                    } else {
                        box.innerHTML = lastUrl + crossimg.outerHTML;
                    }
                });
        } else {
            if (result === true) {
                box.innerHTML = target.href + checkimg.outerHTML;
            } else {
                box.innerHTML = target.href + crossimg.outerHTML;
            }
        }
    } else {
        box.innerHTML = "";
    }
});
