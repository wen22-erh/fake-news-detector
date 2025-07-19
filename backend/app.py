from flask import Flask, request, jsonify
from pymongo import MongoClient
from flask_cors import CORS
app = Flask(__name__)
CORS(app)
client=MongoClient('mongodb://Localhost:27017/')
db=client['fake_news_detector']
collection=db['urls']

@app.route("/save_url", methods=["POST"])
def save_url():
    data = request.get_json()
    urls=data["urls"]
    collection.insert_many([{"url": u} for u in urls])
    return {"status": "urls saved to MOngodb to csv"}
collection=db['whiteList_urls']
@app.route("/checkurl", methods=["POST"])
def check_url():
    data=request.get_json()
    url=data.get("url")
    print("查詢的 URL：", repr(url))
    result=collection.find_one({"url":url})
    print("查詢結果：", result)
    if result:
        return jsonify({"found":True})
    else:
        return jsonify({"found":False})

if __name__ == "__main__":
     app.run(debug=True)
