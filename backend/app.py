#測試版 目前只能看url是否有在db中 未加入label
from flask import Flask, request, jsonify
from pymongo import MongoClient
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
client = MongoClient('mongodb://localhost:27017/')
db = client['fake_news_detector']
whitelist_collection = db['urls']
@app.route("/check_urls_batch", methods=["POST"])
def check_urls_batch():
    data = request.get_json()
    urls = data.get("urls", [])
    
    if not urls:
        return jsonify({})

    # 在 MongoDB 中一次搜尋所有存在 list 內的 URL
    # 假設你的 document 結構有 'url' 和 'label' (Real/Fake)
    results = whitelist_collection.find({"url": {"$in": urls}})
    
    # 建立回傳的 Map (Dictionary)
    response_map = {}
    for doc in results:
        # 將資料庫的標籤取出，若無則預設為 Unknown
        response_map[doc["url"]] = True

    return jsonify(response_map)

if __name__ == "__main__":
    app.run(debug=True)