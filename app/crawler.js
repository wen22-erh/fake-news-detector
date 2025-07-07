// app/crawler.js
require('dotenv').config()
const axios = require('axios')
const cheerio = require('cheerio')

// 你可以從 .env 讀入要爬的 URL
const TARGET_URL = process.env.CRAWL_URL || 'https://example.com'

async function crawl(url) {
  try {
    // 1. 發 HTTP GET 請求
    const { data: html } = await axios.get(url, {
      headers: { 'User-Agent': 'MyBasicCrawler/1.0' }
    })

    // 2. 載入 cheerio 解析 HTML
    const $ = cheerio.load(html)

    // 3. 找出所有 <a> 標籤，並印出 href
    const links = []
    $('a').each((_, el) => {
      const href = $(el).attr('href')
      if (href) links.push(href)
    })

    console.log(`在 ${url} 找到 ${links.length} 個連結：`)
    links.forEach((link, i) => console.log(`${i + 1}. ${link}`))
  } catch (err) {
    console.error('爬取失敗：', err.message)
  }
}

crawl(TARGET_URL)
