# PROMPT.md - 專案開發指令設計與成效報告

## ✅ 實際使用過的關鍵 Prompt (節錄自對話紀錄)

### 1. 初始系統啟動與規劃 (Phase 0)
**場景**：專案啟動，為了避免 AI 直接亂寫 code，我下達了框架定義指令，強制進入 Orchestration 模式。

**我下達的指令：**
> 「你現在同時擔任此專案的 Lead Architect (架構師)、Project Manager (專案經理) 以及 Feature Developer (開發團隊)。... 嚴禁直接輸出代碼。請先提出一個多階段的執行計畫（Roadmap），並將任務拆解為細粒度的子任務。」

**Roo Code 的反應：**
列出了從 Phase 0 到 Phase 6 的 17 個細分子任務，包含環境配置、PubMed/Qdrant 封裝、LangGraph 實作及測試計畫，並主動建立 TODO List 追蹤進度。

---
### 2. 技術選型與架構細化 (Phase 1 & 2)
**場景**：在 Roadmap 批准後，要求 AI 針對醫療系統的高複雜度進行技術選型與環境基礎建設。

**我下達的指令：**
> 「進入 Phase 1 & 2：系統架構細化與基礎設施落地。請根據 `DESIGN.md` 定義的『多 Agent 協作』需求，選定適用的 Framework。同時，實作 Qdrant 向量資料庫與 PubMed API 的底層 Wrapper。所有 API Key 必須透過 `.env` 讀取。」

**Roo Code 的反應：**
選定 **LangGraph** 作為核心框架，理由是其具備循環邏輯與狀態管理能力。隨後建立了 `pubmed_wrapper.py` 與 `qdrant_wrapper.py`，實作了非同步 (Async) 單例模式 Client，並主動生成了 `.env.example`。

---

### 3. 工具封裝與防禦性編程 (Phase 3)
**場景**：開發外部 API 工具，特別強調 Error Handling 與 Rate Limit。

**我下達的指令：**
> 「依據建築師規劃，在 `src/tools/pubmed.py` 與 `src/tools/qdrant.py` 實作工具呼叫。請確保：1. 支援關鍵字優化。2. 捕捉錯誤訊號（如回傳空集合時包含 retry flag）。3. 實作完後執行單元測試驗證連通性。」

**Roo Code 的反應：**
實作了具備錯誤處理機制的 Tool 類別，並在開發後自主執行 `pytest` 指令確認 PubMed API 能正確回傳數據，確保地基穩固。

---

### 4. LangGraph 狀態機邏輯實作 (Phase 4)
**場景**：定義系統的「大腦」，處理搜尋失敗與內容審查的複雜分支邏輯。

**我下達的指令：**
> 「[System Directive: LangGraph State Machine Implementation] 進入 Phase 4。在動手寫代碼前，請先定義 LangGraph 的 State、Nodes 與 Conditional Edges。請明確說明如何處理：情境 A (搜尋失敗重試) 與 情境 B (醫學可信度審查與回滾)。」

**Roo Code 的反應：**
使用 Mermaid 繪製了流程圖，定義了 `Planner` -> `Researcher` -> `Critic` 的循環路徑。當 Critic 判定內容不足時，State 會回流至 Planner 重新優化，並在每個節點完成後自動執行 git commit（如：`feat(agent): implement planner node`）。

---

### 5. 系統整合、觀測性與錯誤修正 (Phase 5)
**場景**：測試時發現端到端流程出現 422 Error（數據格式錯誤）。

**我下達的指令：**
> 「負責 Phase 5.3（建立端到端測試與觀測性）。目標為：1. 為 `/api/research` 建立 E2E 測試。2. 確保 Correlation ID、Telemetry 指標可透過 API Streaming 取得。」

**Roo Code 的反應：**
在測試中自主發現 `TaskStatus` Enum 無法被 JSON 序列化的錯誤，隨即修正了 `schemas.py`。同時加入了結構化日誌（Structured Logging）與 Trace ID，讓每個任務請求都能被追蹤。

---

### 6. 部署準備與品質審查 (Phase 6)
**場景**：專案進入尾聲，要求 AI 進行最後的代碼清理、文檔彙整與專案回顧。

**我下達的指令：**
> 「進入最後階段 Phase 6：部署準備與品質審查。
> 1. 請自動生成完整的 `README.md`，包含環境安裝步驟與 Orchestration 邏輯說明。
> 2. 進行 Final Review：掃描所有程式碼，移除硬編碼的調試訊息。
> 3. 彙整本次開發的歷史提示詞至 `PROMPT.md`，並完成最終的 Commit。」

---

## ✅ Prompt 設計分析 (深度工程思維)

### 1. 結構化約束：防禦性設計與資安規範 (Phase 2)
* **目的**：確保系統具備生產等級的安全性，而非實驗室腳本。
* **設計分析**：透過強調「所有 Key 必須透過 `.env` 讀取」與「單例模式 (Singleton)」，強制 AI 執行 **資安最佳實踐**。這能防止 AI 為了方便而將敏感資訊硬編碼（Hardcoding），並優化資料庫連線池效率。
* **成效**：AI 自動生成了 `.env.example` 並實作了非同步 Client 封裝，降低了系統漏密風險。

### 2. 決策邊界定義：條件分支與重試機制 (Phase 4)
* **目的**：測試 AI 處理非線性流程與自我修復的能力。
* **設計分析**：這項 Prompt 的核心在於定義「情境 A/B」。這不是單純的指令，而是 **State Machine (狀態機)** 的邏輯注入。要求 AI 在動手前先畫圖，是為了確保其對「失敗路徑」的理解與我一致，這體現了工程上的 **協定優先 (Contract-First)** 思維。
* **成效**：AI 成功實作了當搜尋結果為空時，自動回流至 `Planner` 節點重新優化關鍵字的邏輯，展現了高度自主性。

### 3. 品質可觀測性：Telemetry 與 Trace ID 導入 (Phase 5)
* **目的**：解決多 Agent 系統中難以偵錯（Debug）的痛點。
* **設計分析**：要求加入「Correlation ID」與「Telemetry」。這在工程上是為了將整個非同步請求鏈路串聯起來。當系統出錯時，我們能精確追蹤是哪個節點（Node）產生了幻覺或超時。
* **成效**：AI 在日誌中實現了結構化 Logging，並在 API 回傳 422 錯誤時，能透過追蹤編號迅速定位至 `schemas.py` 的型別不匹配問題。

### 4. 環境適應與自動化驗收 (Phase 3 & 6)
* **目的**：降低人為介入程度，實作 CI/CD 式的開發循環。
* **設計分析**：要求 AI 「自主執行單元測試」與「清理硬編碼訊息」。這是在利用 AI 的 **Tool-use 能力** 來執行 QA 任務，確保最終交付的專案是「乾淨」且「可重現」的。
* **成效**：Roo Code 建立了自動化測試腳本並在完成後清理了 debug prints，確保代碼符合專業交付標準。
---

## ✅ 成效評估

* **哪些 Prompt 明顯提升了品質？**
    要求 AI 扮演「Lead Architect」並「先規劃後執行」的 Prompt 最為關鍵。這讓產出的代碼具備高度的解耦性（Decoupling），將 API 客戶端、業務邏輯與 Web 介面徹底分開。

* **哪些 Prompt 失敗或導致走偏？**
    在 Phase 3 實作 PubMed 封裝時，初次 Prompt 未強調 Rate Limit 的具體秒數，導致 AI 實作的 Telemetry（觀測數據）顯示等待時間為 0。後來透過「修正指令」才補齊了這部分的數據精準度。

* **Orchestration 是否真的有幫助？**
    **絕對有。** 若不使用 Orchestration，AI 常會在寫 B 功能時忘記 A 功能的規格。在本專案中，Roo Code 持續維護 TODO 狀態，並在每一階段實作後自我審查（Self-Review），這保證了系統在整合時沒有出現嚴重的接口不匹配問題。

* **如果不用 Orchestration，會卡在哪？**
    會卡在 **「狀態管理 (State Management)」**。醫療系統需要追蹤研究任務的當前進度（搜尋中、分析中、已完成），若不拆解角色與任務，AI 產出的狀態轉移邏輯會非常混亂且難以偵錯。

---

## 🧠 Commit 訊息特徵分析
本專案的 Git Commit 歷史完全由 Roo Code 產生，具有以下 AI Agent 特徵：

1.  **結構化前綴**：如 `feat(...)`, `fix(...)`, `refactor(...)`。
2.  **原子化變更**：每次 Commit 只專注於一個功能（如單獨 Commit `schemas.py`）。
3.  **無情緒描述**：訊息均為客觀的功能描述，無任何人類開發者常見的語氣詞。
---

## 🎓 總結與心得
本次作業展現了「AI as Engineer」的核心價值。透過高品質的 Prompt 設計，我不僅讓 Roo Code 寫出了程式碼，更引導它像一個資深開發團隊一樣，先進行架構規劃、定義規範、實作防禦性代碼，最後進行自動化驗證。