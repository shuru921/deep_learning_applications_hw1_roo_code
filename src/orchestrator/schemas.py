"""LangGraph Orchestrator 狀態機資料結構定義。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, Field

try:  # pragma: no cover - pydantic v1 相容處理
    from pydantic import ConfigDict
except ImportError:  # pragma: no cover - pydantic v1 沒有 ConfigDict
    ConfigDict = None  # type: ignore[misc, assignment]

try:
    from ..clients.pubmed_wrapper import PubMedQuery
except ImportError:  # pragma: no cover - 直行模組時退回絕對匯入
    from src.clients.pubmed_wrapper import PubMedQuery


class OrchestratorBaseModel(BaseModel):
    """提供共用的 pydantic 設定以支援狀態物件。"""

    if ConfigDict is not None:  # pragma: no branch - 判斷 pydantic 版本
        model_config = ConfigDict(  # type: ignore[assignment]
            arbitrary_types_allowed=True,
            populate_by_name=True,
            validate_assignment=True,
            extra="ignore",
        )
    else:  # pragma: no cover - pydantic v1 配置

        class Config:  # type: ignore[too-few-public-methods]
            arbitrary_types_allowed = True
            allow_population_by_field_name = True
            validate_assignment = True
            extra = "ignore"


class PlanStep(OrchestratorBaseModel):
    """Planner 子任務計畫的結構化描述。"""

    step_id: str
    objective: str
    status: Literal[
        "pending", "running", "succeeded", "degraded", "skipped", "failed"
    ] = "pending"
    owner: Literal[
        "planner",
        "researcher",
        "librarian",
        "critic",
        "fallback",
        "responder",
        "system",
    ] = "planner"
    dependencies: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class PubMedDocument(OrchestratorBaseModel):
    """標準化的 PubMed 文獻資料。"""

    pmid: str
    title: str | None = None
    abstract: str | None = None
    journal: str | None = None
    published_at: str | None = None
    mesh_terms: list[str] = Field(default_factory=list)
    authors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    score: float | None = None
    source: str = "pubmed"
    metadata: dict[str, Any] = Field(default_factory=dict)


class PubMedQueryLog(OrchestratorBaseModel):
    """紀錄 PubMed 查詢歷程與結果統計。"""

    query: PubMedQuery
    issued_at: datetime = Field(default_factory=datetime.utcnow)
    status: Literal["pending", "sent", "succeeded", "empty", "failed", "degraded"] = (
        "pending"
    )
    result_count: int | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCallMetric(OrchestratorBaseModel):
    """工具呼叫遙測資料。"""

    tool: str
    action: str
    status: Literal[
        "pending", "running", "success", "warning", "error", "timeout", "cancelled"
    ] = "pending"
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None
    latency_ms: float | None = None
    retries: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class VectorHit(OrchestratorBaseModel):
    """向量檢索命中結果。"""

    point_id: str
    score: float
    payload: Mapping[str, Any] | None = None
    source: str | None = None


class BatchTelemetry(OrchestratorBaseModel):
    """Qdrant upsert 批次遙測紀錄。"""

    batch_size: int
    processed: int
    latency_ms: float | None = None
    retry_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    detail: dict[str, Any] = Field(default_factory=dict)


class QdrantSearchRecord(OrchestratorBaseModel):
    """Qdrant 檢索請求與結果封裝。"""

    query_vector: list[float] | None = None
    filter: dict[str, Any] | None = None
    hits: list[VectorHit] = Field(default_factory=list)
    executed_at: datetime = Field(default_factory=datetime.utcnow)
    latency_ms: float | None = None
    degraded: bool = False
    notes: list[str] = Field(default_factory=list)


class ContextChunk(OrchestratorBaseModel):
    """RAG 合成所需的上下文切片。"""

    chunk_id: str
    content: str
    source: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CriticFeedback(OrchestratorBaseModel):
    """醫療審查節點的發現與建議。"""

    issue: str
    severity: Literal["info", "minor", "major", "critical"] = "major"
    suggestion: str | None = None
    supporting_evidence: list[str] = Field(default_factory=list)
    requires_revision: bool = True
    source_nodes: list[str] = Field(default_factory=list)


class TaskStatus(OrchestratorBaseModel):
    """非同步任務執行情況，用於監控長時操作。"""

    task_id: str
    status: Literal[
        "pending", "running", "completed", "failed", "cancelled", "timeout", "degraded"
    ] = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ErrorSignal(OrchestratorBaseModel):
    """狀態機條件分支所需的錯誤旗標。"""

    source: str
    code: str
    message: str
    severity: Literal["info", "warning", "error", "critical"] = "error"
    raised_at: datetime = Field(default_factory=datetime.utcnow)
    data: dict[str, Any] = Field(default_factory=dict)


class FallbackEvent(OrchestratorBaseModel):
    """降級策略啟動紀錄。"""

    trigger: str
    action: str
    reason: str | None = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StreamUpdate(OrchestratorBaseModel):
    """UI streaming 的增量輸出。"""

    segment: str
    content: str
    channel: Literal["text", "citation", "telemetry", "alert"] = "text"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    final: bool = False


class UserQueryState(OrchestratorBaseModel):
    """使用者提問的原始與正規化資料。"""

    raw_prompt: str = ""
    normalized_terms: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)


class PlanningState(OrchestratorBaseModel):
    """Planner 節點的執行狀態。"""

    iteration: int = 0
    plan_steps: list[PlanStep] = Field(default_factory=list)
    status: Literal["pending", "running", "succeeded", "degraded", "failed"] = "pending"


class PubMedState(OrchestratorBaseModel):
    """PubMed 工具輸入輸出狀態。"""

    latest_query: PubMedQuery | None = None
    query_history: list[PubMedQueryLog] = Field(default_factory=list)
    results: list[PubMedDocument] = Field(default_factory=list)
    empty_retry_count: int = 0


class QdrantState(OrchestratorBaseModel):
    """Qdrant Wrapper 相關的狀態資訊。"""

    collection_ready: bool = False
    upsert_metrics: list[BatchTelemetry] = Field(default_factory=list)
    search_results: list[QdrantSearchRecord] = Field(default_factory=list)
    health: Literal["healthy", "degraded", "unavailable"] = "healthy"


class RagState(OrchestratorBaseModel):
    """RAG 合成節點的暫存資料。"""

    context_bundle: list[ContextChunk] = Field(default_factory=list)
    synthesis_notes: list[str] = Field(default_factory=list)
    answer_draft: str | None = None


class CriticState(OrchestratorBaseModel):
    """醫療審查節點的結果。"""

    findings: list[CriticFeedback] = Field(default_factory=list)
    trust_score: float = 1.0
    revision_required: bool = False


class TelemetryState(OrchestratorBaseModel):
    """跨節點共享的遙測資訊。"""

    tool_invocations: list[ToolCallMetric] = Field(default_factory=list)
    active_tasks: dict[str, TaskStatus] = Field(default_factory=dict)
    error_flags: list[ErrorSignal] = Field(default_factory=list)


class FallbackState(OrchestratorBaseModel):
    """降級流程紀錄。"""

    events: list[FallbackEvent] = Field(default_factory=list)
    terminal_reason: str | None = None


class UIState(OrchestratorBaseModel):
    """UI streaming 與輸出控管。"""

    stream_anchor: str = "root"
    partial_updates: list[StreamUpdate] = Field(default_factory=list)


class LangGraphState(OrchestratorBaseModel):
    """LangGraph 狀態機核心資料結構。"""

    user_query: UserQueryState = Field(default_factory=UserQueryState)
    planning: PlanningState = Field(default_factory=PlanningState)
    pubmed: PubMedState = Field(default_factory=PubMedState)
    qdrant: QdrantState = Field(default_factory=QdrantState)
    rag: RagState = Field(default_factory=RagState)
    critic: CriticState = Field(default_factory=CriticState)
    telemetry: TelemetryState = Field(default_factory=TelemetryState)
    fallback: FallbackState = Field(default_factory=FallbackState)
    ui: UIState = Field(default_factory=UIState)
    extensions: dict[str, Any] = Field(default_factory=dict)
    status: Literal[
        "idle", "running", "succeeded", "failed", "degraded", "cancelled"
    ] = "idle"
    current_node: str | None = None
    retry_counters: dict[str, int] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    def touch(self) -> None:
        """更新最後修改時間，供節點寫入後呼叫。"""

        self.updated_at = datetime.utcnow()


__all__ = [
    "BatchTelemetry",
    "ContextChunk",
    "CriticFeedback",
    "CriticState",
    "ErrorSignal",
    "FallbackEvent",
    "FallbackState",
    "LangGraphState",
    "PlanStep",
    "PlanningState",
    "PubMedDocument",
    "PubMedQueryLog",
    "PubMedState",
    "QdrantSearchRecord",
    "QdrantState",
    "RagState",
    "StreamUpdate",
    "TaskStatus",
    "TelemetryState",
    "ToolCallMetric",
    "UIState",
    "UserQueryState",
    "VectorHit",
]
