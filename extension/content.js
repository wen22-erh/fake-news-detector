const text = document.body.innerText;
chrome.runtime.sendMessage({
    action: "updateText",
    text: text,
});
