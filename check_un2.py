from pymongo import MongoClient
import json
client = MongoClient('mongodb://localhost:27017/')
db = client['fake_news_detector']
url = "https://www.cna.com.tw/news/ait/202605130089.aspx"
doc = db.unknown_urls.find_one({"normalized_url": url})
if doc:
    doc['_id'] = str(doc['_id'])
    print(json.dumps(doc, indent=2, ensure_ascii=False))
else:
    print("Not found in unknown_urls")
