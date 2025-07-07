from flask import Flask, request
from pymongo import MongoClient
import csv
import os
app = Flask(__name__)

client=MongoClient('mongodb://Localhost:27017/')
db=client['fake_news_detector']
collection=db['whitelist_urls']

@app.route("/save_url", methods=["POST"])
def save_url():
    data = request.get_json()
    urls=data["urls"]
    collection.insert_many([{"url": u} for u in urls])
    return {"status": "urls saved to MOngodb to csv"}


if __name__ == "__main__":
     app.run(debug=True)
