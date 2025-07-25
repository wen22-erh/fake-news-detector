from firecrawl import FirecrawlApp, ScrapeOptions
from pymongo import MongoClient
from dataclasses import asdict
import json
client=MongoClient('mongodb://Localhost:27017/')
db=client['firecrawl']
collection=db['articles']
app = FirecrawlApp(
    api_key=None,                         # 本地無須真正金鑰
    api_url="http://localhost:3002/"      # ← Firecrawl API 服務的網址
)

    
try:
    crawl_result = app.crawl_url('https://edition.cnn.com/2025/07/24/sport/venus-williams-frech-dc-open-spt', 
  limit=1, 
  scrape_options=ScrapeOptions(
  formats=['markdown',],
  onlyMainContent=True,
  blockAds=True,

  ))
    print(crawl_result)
except Exception as e:
    print("Error:", e)
    
collection.insert_one({
    "url":"https://edition.cnn.com/2025/07/24/sport/venus-williams-frech-dc-open-spt",
    "content":str(crawl_result)
    })