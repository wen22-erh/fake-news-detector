const express = require("express");
const app = express();

app.get("/", (req, res) => {
    res.send("<p>歡迎來到docker網站</p>");
});
app.listen(3000, () => {
    console.log("開啟網站");
});
