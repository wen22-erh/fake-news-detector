const text = document.body.innerText;
chrome.runtime.sendMessage({
    action: "updateText",
    text: text,
});
let box = document.createElement("div");
box.id = "box";
box.textContent = "content";
document.body.appendChild(box);
document.addEventListener("mousemove", function (event) {
    box.style.left = event.clientX + 10 + "px";
    box.style.top = event.clientY + 10 + "px";
    const target = event.target;
    let url = null;
    if (target.href) {
        url = target.href;
        box.textContent = url;
    } else {
        box.textContent = "";
    }
});
