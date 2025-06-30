from flask import Flask, request
from pymongo import MongoClient
import csv
import os
app = Flask(__name__)

client=MongoClient('mongodb://Localhost:27017/')
db=client['fake-news-detector']
collection=db['whitelist_urls']

@app.route("/save_url", methods=["POST"])
def save_url():
    data = request.get_json()
    
    collection.insert_one({"url":data["url"]})
    
    with open("url_log.csv",'w',newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([data["url"]])
    return {"status": "url saved to MOngodb to csv"}

# @app.route("/save_content", methods=["POST"])
# def save_content():
#     data = request.get_json()
#     with open("content_log.csv", 'w', newline='', encoding='utf-8') as f:
#         writer = csv.writer(f)
#         writer.writerow([data["content"]])
#     return {"status": "content saved"}
if __name__ == "__main__":
     app.run(debug=True)
