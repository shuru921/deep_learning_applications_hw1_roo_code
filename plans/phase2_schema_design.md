# Phase 2 Qdrant Schema 設計

## 1. Collection 與資料契約
- **Collection 名稱**：`medical_articles`
- **向量維度**：`1536`（對齊 OpenAI text-embedding-3-large 設定）
- **距離度量**：`COSINE`
- **Shards / Replicas**：維持單節點預設，於 Qdrant 集群化部署時再調整。

## 2. Payload Schema 定義
| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `pmid` | `str` | PubMed 論文唯一識別碼 |
| `title` | `str` | 論文標題 |
| `abstract` | `str` | 論文摘要全文或分段組合 |
| `journal` | `str` | 期刊名稱 |
| `published_at` | `str` (ISO8601) | 出版日期，以 `YYYY-MM-DD` 或 `YYYY` 表示 |
| `authors` | `list[str]` | 作者清單 |
| `keywords` | `list[str]` | 自動抽取或人工定義之關鍵字 |
| `mesh_terms` | `list[str]` | PubMed MeSH 語彙，以利醫療領域檢索 |
| `language` | `str` | 文獻語言，例如 `en`, `zh` |
| `ingested_at` | `str` (ISO8601) | 系統寫入時間 |
| `source` | `str` | 來源標記（預設 `pubmed`） |

> 備註：所有時間欄位統一採 `UTC`，以 ISO8601 字串儲存，方便後續篩選。

## 3. Index 與 Filter 策略
- **Payload Index**：
  - `journal`, `language`, `mesh_terms` 設定為 keyword index。
  - `published_at` 轉換為 `int` 年份輔助篩選（另存欄位 `published_year`）。
- **Hybrid Search**：
  - Primary：vector similarity（COSINE）。
  - Filter：依期刊、年份、語言、MeSH 語彙等條件篩選。

## 4. Upsert 行為
- **Point ID**：使用 PubMed PMID 當 Qdrant point id，確保 upsert idempotent。
- **資料清洗**：
  - 抽取摘要時維持原段落順序，以 `\n\n` 相連。
  - Authors/keywords/mesh_terms 為已清理之 list[str]。

## 5. 設定與程式整合
- `src/settings.py` 保持 `vector_size=1536`、`collection_name="medical_articles"`。
- Phase 3 wrapper 以 `QdrantRecord` payload 驗證上述欄位型別。
- 後續將在測試中模擬 schema 驗證與部分 upsert 失敗情境。
