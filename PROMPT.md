# Prompt 紀錄

- **Phase 0 系統規劃**
  - **Phase 0.1 分階段開發約束**：規定整體開發必須依序完成 Phase 1~5，禁止一次性交付，並要求各階段間維持可追蹤關聯 [`DESIGN.md`](DESIGN.md:35-41)。
  - **Phase 0.2 通用工程規範**：強調非同步通訊、模組去耦、防禦性編程與自動化測試覆蓋，作為後續所有實作的共同準則 [`DESIGN.md`](DESIGN.md:47-52)。

- **Phase 1 基礎設施準備**
  - **Phase 1.1 Docker Compose 服務配置**：需設定 Qdrant、Postgres、Redis 等核心服務並驗證連通性，提供後續階段開發環境 [`DESIGN.md`](DESIGN.md:35-38)。

- **Phase 2 數據 Schema 設計**
  - **Phase 2.1 Qdrant Collection 契約**：定義 `medical_articles` collection 之向量維度、距離度量與 shards/replicas 預設值，所有資料操作需遵循 [`plans/phase2_schema_design.md`](plans/phase2_schema_design.md:3-8)。
  - **Phase 2.2 Payload 與索引規範**：規範 `pmid`、`abstract`、`mesh_terms` 等欄位型別與 Hybrid Search 過濾策略，並要求時間欄位採 UTC ISO8601 格式 [`plans/phase2_schema_design.md`](plans/phase2_schema_design.md:9-33)。
  - **Phase 2.3 Upsert 與系統整合**：指示使用 PMID 作為 point id、維持摘要清洗規則，並確保 `src/settings.py` 的向量設定與 schema 一致以利 Wrapper 測試 [`plans/phase2_schema_design.md`](plans/phase2_schema_design.md:34-43)。

- **Phase 3 核心工具封裝**
  - **Phase 3.1 PubMed Wrapper 要求**：提供 `search`、`fetch_details`、`fetch_summaries`、`warm_up` 非同步方法，整合 `AsyncRateLimiter`、`ToolingError` 階層與重試/降級策略 [`plans/phase3_wrapper_design.md`](plans/phase3_wrapper_design.md:31-58)。
  - **Phase 3.2 Qdrant Wrapper 要求**：啟動時驗證 collection schema，實作 `ensure_collection`、`upsert`、`query`、`delete`、`healthcheck`，並支援批次切分與錯誤映射機制 [`plans/phase3_wrapper_design.md`](plans/phase3_wrapper_design.md:84-103)。
  - **Phase 3.3 觀測性與測試規範**：統一 structlog/Otel 遙測欄位，規劃 pytest-asyncio、respx/qdrant mock、VCR 錄製，以及依賴與設定檔管理策略 [`plans/phase3_wrapper_design.md`](plans/phase3_wrapper_design.md:59-118)。

- **Phase 4 LangGraph 狀態機構築**
  - **Phase 4.1 狀態結構設計**：定義 `user_query`、`planning`、`pubmed`、`qdrant`、`rag`、`critic`、`telemetry`、`fallback`、`ui` 等欄位，建議以 Pydantic/TypedDict 實作 [`plans/phase4_langgraph_plan.md`](plans/phase4_langgraph_plan.md:9-23)。
  - **Phase 4.2 節點職責與資料流**：描述 Planner、PubMed Search、Result Normalizer、Qdrant Upsert/Search、RAG Synthesizer、Medical Critic、Fallback/Final Responder 的輸入輸出與狀態更新流程 [`plans/phase4_langgraph_plan.md`](plans/phase4_langgraph_plan.md:25-35)。
  - **Phase 4.3 Conditional Edge 與回滾策略**：Scenario A（PubMed 無結果）與 Scenario B（Critic 可信度不足）需定義回滾條件、降級觸發與 `telemetry.error_flags` 記錄方式 [`plans/phase4_langgraph_plan.md`](plans/phase4_langgraph_plan.md:36-47)。
  - **Phase 4.4 非同步併發與遙測**：採用 `asyncio.gather`/`TaskGroup` 控制工具並行、追蹤任務狀態與取消卡住任務，確保 streaming 響應與遙測可視性 [`plans/phase4_langgraph_plan.md`](plans/phase4_langgraph_plan.md:49-54)。

- **Phase 5 UI 與端到端整合**
  - **Phase 5.1 UI/API 介面實作**：建立 `/ui`、`/ui/query`（Jinja + Streaming）及 `/api/research` NDJSON API，串接 LangGraph streaming updates 與 telemetry [`plans/phase5_ui_integration_plan.md`](plans/phase5_ui_integration_plan.md:3-43)。
  - **Phase 5.2 E2E 測試與 CI**：使用 httpx.AsyncClient 與 orchestrator stubs 覆蓋成功/降級情境，並整合進 CI 流程 [`plans/phase5_ui_integration_plan.md`](plans/phase5_ui_integration_plan.md:44-53)。
  - **Phase 5.3 作業準備與觀測性擴充**：實作前確認 `git status` 乾淨、建立專用分支，並規劃 streaming 格式協議與遙測面板呈現 [`plans/phase5_ui_integration_plan.md`](plans/phase5_ui_integration_plan.md:64-88)。

- **Phase 6 部署與品質審查**
  - **Phase 6.1 部署前檢查清單**：整合前期 Async 需求、遙測欄位與工具錯誤分類，作為部署文件與驗證步驟的基準 [`plans/phase6_deployment_quality_plan.md`](plans/phase6_deployment_quality_plan.md:3-21)。
  - **Phase 6.2 工作項目矩陣**：涵蓋 Docker 檢查、映像建置、發佈回滾、CI/CD 安全掃描、QA 測試矩陣、觀測性與安全治理等項目，需產出對應文件與腳本以通過 Go/No-Go 審查 [`plans/phase6_deployment_quality_plan.md`](plans/phase6_deployment_quality_plan.md:23-60)。
