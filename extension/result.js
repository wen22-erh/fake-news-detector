document.addEventListener("DOMContentLoaded", function () {
    const text = localStorage.getItem("lastContent");
    const div = document.getElementById("resultText");
    if (text) {
        div.innerText = text;
    } else {
        div.innerText = "no content found";
    }
});
