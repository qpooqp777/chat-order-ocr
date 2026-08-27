# 多格式聊天室訂單測試報告

## 測試摘要

本次使用 `references/multi_format_test_fixture.txt` 測試 8 類聊天室訊息格式，並另外驗證 OCR 來源與品項字典。文字來源測試共產生 11 筆訂單列、15 份數量；OCR 來源則正確標示 `confidence: check`。

| 測試類型 | 測試內容 | 結果 |
|---|---|---|
| 標準格式 | 姓名獨立一行，品項加 `+1` 或全形 `＋2` | 通過 |
| 中文數量 | `一份`、`兩盒` | 通過 |
| 規格保留 | `櫻桃 2公斤 +1`、`青龍6粒198 — 一份` | 通過，規格保留 |
| 同名分散 | 同一姓名在不同訊息段落再次出現 | 通過，分別歸入同名訂購人 |
| 聊天噪音 | 時間、閒聊、@詢問 | 多數正確略過 |
| 同行姓名品項 | `小強 西瓜+1` | 需人工確認，規則式解析無法可靠拆分 |
| 特殊分隔 | `淑仔：文旦+1` | 需人工確認，姓名與品項可能被視為一體 |
| 無數量訊息 | `想要草莓`、`請留芭樂` | 正確不產生訂單 |
| OCR 來源 | 透過 `--source ocr` 輸入 | 正確標示 `confidence: check` |
| 品項字典 | 別名對應標準品項 | 可對應；未命中會標示需確認 |

## 實際使用範例

### 1. 貼上文字並輸出 JSON

```bash
python3 scripts/order_ocr_cli.py \
  --input messages.txt \
  --source text \
  --group-by person \
  --format json \
  --output orders.json
```

### 2. 從標準輸入讀取聊天文字並輸出 CSV

```bash
cat messages.txt | python3 scripts/order_ocr_cli.py \
  --source text \
  --sort original \
  --format csv \
  --output orders.csv
```

### 3. OCR 文字輸入

```bash
cat ocr-result.txt | python3 scripts/order_ocr_cli.py \
  --source ocr \
  --sort name-desc \
  --format json
```

此模式下每筆結果都應視為草稿，因為姓名、品項與數量可能受到 OCR 辨識錯誤影響。

### 4. 套用品項字典

```bash
python3 scripts/order_ocr_cli.py \
  --input messages.txt \
  --catalog references/catalog.example.json \
  --group-by person \
  --format json
```

## 重要限制

工具會沿用最近一個看起來像姓名的獨立訊息，因此聊天中的一般句子若符合姓名啟發式，可能被暫時當作訂購人。例如測試中的「今天到貨囉～」可能被套用到後續同行姓名品項。工具不會自行判斷同一行中的姓名與品項邊界，也不會把「沙糖桔」與「砂糖橘」等不同文字自動合併。

建議實務上讓訂購人單獨一行，下一行再輸入訂單，例如：

```text
小強
西瓜+1
```

若使用價格、重量、包裝或特殊符號，應在出貨前檢查 `item` 原文、`matched_item`、`qty` 與 `confidence`。任何 `check` 或「未對應，請確認」的列，都不應直接視為已核准訂單。
