# DESIGN.md - Med-Agentic Research & Monitoring Suite (MARS)

## 1. 系統目標與使用情境 (System Goals & Scenarios)
本系統旨在提供一個專業級的**醫療文獻研究與決策輔助平台**。系統不只是單純的聊天機器人，而是一個能主動規劃研究任務的 AI Agent 系統。

* **使用情境 A：自主醫學研究**
    使用者提出複雜的臨床問題（例如：`Analysis of Side Effects of New GLP-1 Medications`）。系統會自主拆解關鍵字、從 PubMed 抓取文獻、進行數據清洗、並存入 Qdrant 向量資料庫進行深度的 RAG (Retrieval-Augmented Generation) 問答。
* **使用情境 B：醫療專有名詞轉譯與可視化**
    針對文獻中艱澀的專有名詞（Medical Terms），系統提供對比解釋，並具備文字轉圖片（Text-to-Image）功能，將病理描述視覺化以輔助理解。

## 2. 功能模組切分 (Functional Modules)
為了符合工程複雜度，系統分為以下模組：
* **Agentic Orchestrator (核心編排)**：基於 **LangGraph** 實作的狀態機，控制任務流轉。
* **Data Pipeline (資料流水線)**：負責 PubMed XML 解析、Text Chunking (文本切片) 與清洗。
* **Vector Engine (Qdrant)**：負責高維度向量存儲與 **Metadata Filtering** (元數據過濾，如年份、期刊篩選)。
* **Knowledge Base (PostgreSQL)**：負責持久化儲存使用者對話紀錄、文獻元數據與系統權限設定。
* **Async Worker (背景任務)**：使用排程系統定期更新特定醫療領域的最新論文摘要。
* **Interactive UI (Gradio/Streamlit)**：提供流式輸出（Streaming）與醫學影像顯示的互動介面。

## 3. Orchestration 角色設計 (Agent Personas)
本系統採用多代理人協作模式：
1. **Planner Agent**：負責接收問題，將其拆解為搜尋關鍵字、需要檢索的年份範圍與研究重點。
2. **Researcher Agent (Tool Runner)**：操作 PubMed API 工具，負責精準檢索與資料採集。
3. **Librarian Agent (Qdrant Specialist)**：負責管理向量庫的 Upsert 與 Hybrid Search 檢索邏輯。
4. **Medical Critic Agent**：審核 RAG 回傳的內容，檢查是否包含過時資訊或醫學邏輯錯誤，並負責名詞解釋。

## 4. 資料流與控制流 (Data & Control Flow)
* **控制流 (Control Flow)**: 
  `User Query` -> `Planner` -> `Decision Node (Branching)` -> `Researcher` -> `Librarian` -> `Critic` -> `Final Answer`.
* **資料流 (Data Flow)**:
  1. `External API (PubMed)` -> `Processing` -> `Qdrant Payload`.
  2. `Qdrant Vector` + `PostgreSQL Metadata` -> `Context` -> `LLM Reasoning`.

## 5. 任務拆解與協作預期 (Roo Code Task Breakdown)
我預期 Roo Code 必須分階段執行，嚴禁一次性產出：
1. **Phase 1: Infra Setup**：配置 Docker-compose（Qdrant, Postgres, Redis），並驗證服務連通性。
2. **Phase 2: Data Schema Design**：定義 Qdrant Collection 結構（包含向量維度與 Payload 欄位）與 SQL 表架構。
3. **Phase 3: Core Tooling Development**：開發獨立的 PubMed API Wrapper 與 Qdrant 檢索工具，並附帶單元測試。
4. **Phase 4: Agent Logic Construction**：使用 LangGraph 串聯各代理人，實作條件分支（Conditional Edges）。
5. **Phase 5: UI & Integration Testing**：開發前端介面並進行端到端（E2E）測試，修正檢索不精準的問題。

## 6. 為什麼本系統合理需要 Orchestration 模式？
1. **異質資料庫整合**：同時涉及向量資料庫 (Qdrant) 與關聯式資料庫 (PostgreSQL) 的同步操作。
2. **動態規劃需求**：PubMed 的 API 回傳是不確定的，Agent 必須根據回傳結果數量決定「擴大搜索」或「直接總結」，這需要多步規劃能力。
3. **工程化規模**：包含背景非同步任務 (Celery)、錯誤重試機制 (Error Handling) 與多層級的對話狀態管理，超出了單一 Prompt 的處理範圍。

## 7. 工程約束與開發規範 (Engineering Constraints)
為確保系統穩定性與 Orchestration 模式的深度應用，開發時須遵守：
* **非同步通訊 (Asynchronous communication)**：PubMed 抓取與向量處理必須是非同步的，避免阻塞 API 響應。
* **模組去耦合 (Decoupling)**：Agent 邏輯、資料庫操作、API 路由必須完全分離，嚴禁將業務邏輯寫在單一文件中。
* **防禦性編程 (Defensive Programming)**：必須實作 API Rate Limit 處理（針對 NCBI API）以及向量檢索失敗的降級方案（Fallback）。
* **自動化測試要求**：所有 Tools (PubMed/Qdrant) 必須具備對應的 pytest 測試腳本，驗證資料格式的正確性。