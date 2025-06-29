# MongoDB 指令整理

collection:資料表
field:欄位

## 基本指令

```shell
show dbs
use <資料庫名>
cls
db.dropDatabase()
```

---

## 新增資料

```js
db.<資料表名>.insertOne({})
db.<資料表名>.insertMany([{},{},...])
```

---

## 資料型態

-   string: `"文字"`
-   int, double, bool
-   date: `new Date()`
-   array: `["a", "b", "c"]`
-   nested document:
    ```js
    {
      street: "",
      city: "",
      number: 0
    }
    ```

---

## 查詢與排序

```js
db.<資料表名>.find()
db.<資料表名>.find().sort({documentname: 1}) // 1:升序, -1:降序
db.<資料表名>.find().limit(1)
db.<資料表名>.find().sort({documentname: 1}).limit(1)
```

### 條件查詢與投影

```js
db.<資料表名>.find({documentname: "想找的資料"})
// 投影
db.<資料表名>.find({}, {_id: false, document: true})
```

---

## 更新

```js
db.<資料表名>.update({name: ""}, {$set: {documentname: ""}})
```

-   `$set`: 設定欄位
-   `$unset`: 移除欄位
-   `$exists`: 判斷欄位是否存在

---

## 刪除

```js
db.<資料表名>.deleteOne({})
db.<資料表名>.deleteMany({})
```

---

## 比較運算子

-   `$ne`: 不等於
-   `$lt`/`$lte`: 小於/小於等於
-   `$gt`/`$gte`: 大於/大於等於
-   `$in`/`$nin`: 包含/不包含於陣列

---

## 邏輯運算子

-   `$and: []`
-   `$or: []`
-   `$nor: []`
-   `$not: []`

---

## 索引

```js
db.<資料表名>.explain("executionStats")
db.<資料表名>.createIndexes({name: 1})
db.<資料表名>.getIndexes()
```

---

## 建立 Collection

```js
db.createCollection("資料表名", {
    capped: true / false,
    size: 10000000, // bytes
    max: 100, // 最大文件數
    autoIndexId: false,
});
```
