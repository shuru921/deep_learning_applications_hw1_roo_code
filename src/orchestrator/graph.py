"""LangGraph Orchestrator 實作，負責建立多代理狀態機流程。

此模組提供 `build_medical_research_graph` 函式，依據
[`plans/phase4_langgraph_plan.md`](plans/phase4_langgraph_plan.md:1-75) 的藍圖，
結合 [`schemas.py`](src/orchestrator/schemas.py:1-329) 所定義的狀態結構，
完成節點函式、條件邊、錯誤處理與降級策略。缺少 `langgraph` 套件時會
回傳可替代的執行器，以避免執行階段崩潰。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Literal, Mapping, MutableMapping, Sequence

try:  # pragma: no cover - langgraph 並非測試必備依賴
    from langgraph.graph import END, StateGraph
    from langgraph.graph.state import CompiledStateGraph as CompiledGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover - 於缺少 langgraph 時提供降級執行器
    StateGraph = None  # type: ignore[assignment]
    END = "__end__"
    LANGGRAPH_AVAILABLE = False

    class CompiledGraph:  # type: ignore[too-few-public-methods]
        def __init__(self, message: str) -> None:
            self._message = message

        def invoke(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError(self._message)

        async def ainvoke(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError(self._message)

        def get_graph(self) -> Mapping[str, Any]:
            return {"error": self._message}


try:  # pragma: no cover - 支援封包外部執行
    from .schemas import (
        BatchTelemetry,
        ContextChunk,
        CriticFeedback,
        ErrorSignal,
        FallbackEvent,
        LangGraphState,
        PlanStep,
        PubMedDocument,
        PubMedQueryLog,
        QdrantSearchRecord,
        StreamUpdate,
        TaskStatus,
        ToolCallMetric,
        VectorHit,
    )
except ImportError:  # pragma: no cover - 絕對匯入後備
    from src.orchestrator.schemas import (  # type: ignore[no-redef]
        BatchTelemetry,
        ContextChunk,
        CriticFeedback,
        ErrorSignal,
        FallbackEvent,
        LangGraphState,
        PlanStep,
        PubMedDocument,
        PubMedQueryLog,
        QdrantSearchRecord,
        StreamUpdate,
        TaskStatus,
        ToolCallMetric,
        VectorHit,
    )

try:
    from ..errors import (  # pragma: no cover
        PubMedEmptyResult,
        PubMedError,
        QdrantError,
        QdrantTimeoutError,
    )
except ImportError:  # pragma: no cover
    from src.errors import (  # type: ignore[no-redef]
        PubMedEmptyResult,
        PubMedError,
        QdrantError,
        QdrantTimeoutError,
    )

try:
    from ..clients.pubmed_wrapper import (  # pragma: no cover
        PubMedArticle,
        PubMedBatch,
        PubMedQuery,
        PubMedSearchResult,
        PubMedSummary,
        PubMedWrapper,
    )
except ImportError:  # pragma: no cover
    from src.clients.pubmed_wrapper import (  # type: ignore[no-redef]
        PubMedArticle,
        PubMedBatch,
        PubMedQuery,
        PubMedSearchResult,
        PubMedSummary,
        PubMedWrapper,
    )

try:
    from ..clients.qdrant_wrapper import (  # pragma: no cover
        QdrantQuery,
        QdrantQueryResult,
        QdrantRecord,
        QdrantUpsertResult,
        QdrantWrapper,
    )
except ImportError:  # pragma: no cover
    from src.clients.qdrant_wrapper import (  # type: ignore[no-redef]
        QdrantQuery,
        QdrantQueryResult,
        QdrantRecord,
        QdrantUpsertResult,
        QdrantWrapper,
    )

try:
    from ..settings import PubMedSettings, QdrantSettings  # pragma: no cover
except ImportError:  # pragma: no cover
    from src.settings import PubMedSettings, QdrantSettings  # type: ignore[no-redef]


LOGGER = logging.getLogger("orchestrator.langgraph")


@dataclass(slots=True)
class OrchestratorDependencies:
    """封裝節點執行所需的外部依賴。"""

    pubmed: PubMedWrapper | None = None
    qdrant: QdrantWrapper | None = None
    vectorizer: Callable[[str], Sequence[float]] | None = None
    logger: logging.Logger | None = None


@dataclass(slots=True)
class OrchestratorConfig:
    """Orchestrator 參數設定。"""

    pubmed_settings: PubMedSettings = field(default_factory=PubMedSettings)
    qdrant_settings: QdrantSettings = field(default_factory=QdrantSettings)
    pubmed_empty_threshold: int = 2
    critic_revision_limit: int = 2
    max_pubmed_ids: int = 20
    max_context_chunks: int = 8
    min_context_for_confidence: int = 2


@dataclass(slots=True)
class NodeContext:
    """節點執行環境資訊。"""

    dependencies: OrchestratorDependencies
    config: OrchestratorConfig

    def logger(self) -> logging.Logger:
        return self.dependencies.logger or LOGGER


@dataclass(slots=True)
class ToolInvocationResult:
    """包裝工具呼叫的結果與遙測資訊。"""

    metric: ToolCallMetric
    task_status: TaskStatus
    result: Any | None = None
    exception: Exception | None = None


StateInput = LangGraphState | Mapping[str, Any]


def _ensure_state(state: StateInput) -> LangGraphState:
    if isinstance(state, LangGraphState):
        return state
    if hasattr(LangGraphState, "model_validate"):
        return LangGraphState.model_validate(state)  # type: ignore[attr-defined]
    return LangGraphState.parse_obj(state)  # type: ignore[call-arg]


def _export_state(state: LangGraphState) -> dict[str, Any]:
    if hasattr(state, "model_dump"):
        return state.model_dump(mode="python")  # type: ignore[call-arg]
    return state.dict()  # type: ignore[call-arg]


def _deep_copy_state(state: LangGraphState) -> LangGraphState:
    if hasattr(state, "model_copy"):
        return state.model_copy(deep=True)  # type: ignore[call-arg]
    return state.copy(deep=True)  # type: ignore[call-arg]


def _run_sync(awaitable: Awaitable[LangGraphState]) -> LangGraphState:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        return loop.run_until_complete(awaitable)  # type: ignore[misc]

    return asyncio.run(awaitable)


def _hash_vector(text: str, size: int) -> list[float]:
    """若無外部向量器，使用 deterministic hash 產生向量。"""

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    if size <= 0:
        return []
    floats = [byte / 255.0 for byte in digest]
    vector: list[float] = []
    idx = 0
    while len(vector) < size:
        vector.append(floats[idx % len(floats)])
        idx += 1
    return vector


def _start_tool_metric(tool: str, action: str) -> ToolInvocationResult:
    now = datetime.utcnow()
    metric = ToolCallMetric(tool=tool, action=action, status="running", started_at=now)
    task_id = f"{tool}:{action}:{math.floor(now.timestamp() * 1000)}"
    task_status = TaskStatus(task_id=task_id, status="running", started_at=now)
    return ToolInvocationResult(metric=metric, task_status=task_status)


def _finalize_tool_metric(
    invocation: ToolInvocationResult,
    status: Literal[
        "success",
        "warning",
        "error",
        "timeout",
        "cancelled",
    ],
) -> None:
    finished = datetime.utcnow()
    invocation.metric.status = status
    invocation.metric.ended_at = finished
    invocation.metric.latency_ms = (
        finished - invocation.metric.started_at
    ).total_seconds() * 1000
    invocation.task_status.finished_at = finished
    status_mapping: dict[str, str] = {
        "success": "completed",
        "warning": "degraded",
        "error": "failed",
        "timeout": "timeout",
        "cancelled": "cancelled",
    }
    invocation.task_status.status = status_mapping.get(status, status)


def _record_tool_telemetry(
    state: LangGraphState,
    invocation: ToolInvocationResult,
) -> None:
    state.telemetry.tool_invocations.append(invocation.metric)
    state.telemetry.active_tasks[invocation.task_status.task_id] = (
        invocation.task_status
    )


def _append_error_flag(
    state: LangGraphState,
    *,
    source: str,
    code: str,
    message: str,
    severity: Literal["info", "warning", "error", "critical"] = "error",
    data: Mapping[str, Any] | None = None,
) -> None:
    state.telemetry.error_flags.append(
        ErrorSignal(
            source=source,
            code=code,
            message=message,
            severity=severity,
            data=dict(data or {}),
        )
    )


async def _call_tool(
    state: LangGraphState,
    *,
    tool: str,
    action: str,
    call: Callable[[], Awaitable[Any]],
) -> ToolInvocationResult:
    invocation = _start_tool_metric(tool, action)
    try:
        result = await call()
    except Exception as exc:  # pragma: no cover - 具體錯誤由節點判斷
        invocation.exception = exc
        invocation.metric.error = str(exc)
        _finalize_tool_metric(invocation, "error")
    else:
        invocation.result = result
        _finalize_tool_metric(invocation, "success")
    finally:
        _record_tool_telemetry(state, invocation)
    return invocation


def _activate_node(state: LangGraphState, node_name: str) -> None:
    print(f"--- [DEBUG] Node Executing: {node_name} ---")
    state.current_node = node_name
    state.status = "running"
    state.ui.partial_updates = []


def _normalize_terms(prompt: str) -> list[str]:
    parts = re.split(
        r"[\n,;]| and | or | 而且 | 並且 | 以及 ", prompt, flags=re.IGNORECASE
    )
    terms = [segment.strip() for segment in parts if segment and segment.strip()]
    if not terms and prompt.strip():
        terms = [prompt.strip()]
    return [term.lower() for term in terms]


async def planner_node(state_in: StateInput, ctx: NodeContext) -> LangGraphState:
    state = _ensure_state(state_in)
    _activate_node(state, "planner")
    logger = ctx.logger()

    state.planning.iteration += 1
    state.planning.status = "running"

    if not state.user_query.normalized_terms:
        state.user_query.normalized_terms = _normalize_terms(
            state.user_query.raw_prompt
        )

    if not state.planning.plan_steps:
        steps: list[PlanStep] = []
        for idx, term in enumerate(state.user_query.normalized_terms or ["general"]):
            owner: Literal[
                "planner",
                "researcher",
                "librarian",
                "critic",
                "fallback",
                "responder",
                "system",
            ] = (
                "researcher" if idx == 0 else "librarian"
            )
            steps.append(
                PlanStep(
                    step_id=f"plan-{state.planning.iteration}-{idx+1}",
                    objective=f"蒐集與 {term} 相關的臨床研究與摘要",
                    owner=owner,
                )
            )
        steps.append(
            PlanStep(
                step_id=f"plan-{state.planning.iteration}-critique",
                objective="彙整結果並進行臨床審查",
                owner="critic",
            )
        )
        state.planning.plan_steps = steps
    state.ui.partial_updates.append(
        StreamUpdate(segment="planner", content=f"已制定研究計畫，預計執行 {len(state.planning.plan_steps)} 個步驟。")
    )

    query_terms = state.user_query.normalized_terms or [state.user_query.raw_prompt]
    search_term = " ".join(query_terms).strip() or state.user_query.raw_prompt

    state.pubmed.latest_query = PubMedQuery(
        term=search_term,
        retmax=min(
            ctx.config.max_pubmed_ids, ctx.config.pubmed_settings.rate_requests * 4
        ),
        sort="relevance",
    )
    state.pubmed.empty_retry_count = (
        0 if state.planning.iteration == 1 else state.pubmed.empty_retry_count
    )

    state.pubmed.query_history.append(
        PubMedQueryLog(
            query=state.pubmed.latest_query,
            status="pending",
        )
    )

    if state.retry_counters.get("planner", 0) != state.planning.iteration:
        state.retry_counters["planner"] = state.planning.iteration

    logger.info(
        "Planner node executed",
        extra={
            "iteration": state.planning.iteration,
            "search_term": search_term,
            "retry_count": state.pubmed.empty_retry_count
        }
    )
    state.touch()
    return state


async def pubmed_search_node(state_in: StateInput, ctx: NodeContext) -> LangGraphState:
    state = _ensure_state(state_in)
    _activate_node(state, "pubmed_search")
    logger = ctx.logger()

    if ctx.dependencies.pubmed is None:
        message = "PubMedWrapper 未配置，無法執行檢索"
        _append_error_flag(
            state,
            source="pubmed.search",
            code="pubmed-not-configured",
            message=message,
            severity="critical",
        )
        state.fallback.events.append(
            FallbackEvent(trigger="pubmed", action="abort", reason=message)
        )
        state.fallback.terminal_reason = "pubmed-unavailable"
        state.status = "degraded"
        # 增加重試計數以觸發 fallback 分支，防止無限迴圈
        state.pubmed.empty_retry_count = 999
        state.touch()
        return state

    latest_query = state.pubmed.latest_query
    if latest_query is None:
        latest_query = PubMedQuery(
            term=state.user_query.raw_prompt or "medical research"
        )
        state.pubmed.latest_query = latest_query
        state.pubmed.query_history.append(
            PubMedQueryLog(query=latest_query, status="pending")
        )

    state.ui.partial_updates.append(
        StreamUpdate(segment="pubmed_search", content=f"正在搜尋 PubMed 文獻：{latest_query.term}...")
    )

    invocation = await _call_tool(
        state,
        tool="pubmed",
        action="search",
        call=lambda: ctx.dependencies.pubmed.search(latest_query),
    )

    search_result: PubMedSearchResult | None = None
    if invocation.exception is not None:
        if isinstance(invocation.exception, PubMedEmptyResult):
            state.pubmed.empty_retry_count += 1
            state.pubmed.query_history[-1].status = "empty"
            state.pubmed.query_history[-1].result_count = 0
            _append_error_flag(
                state,
                source="pubmed.search",
                code="pubmed-empty-result",
                message=str(invocation.exception),
                severity="warning",
                data={"retry": state.pubmed.empty_retry_count},
            )
            logger.warning(
                "pubmed search empty",
                extra={
                    "retry": state.pubmed.empty_retry_count,
                    "term": latest_query.term,
                },
            )
            state.touch()
            return state

        if isinstance(invocation.exception, PubMedError):
            state.pubmed.query_history[-1].status = "failed"
            _append_error_flag(
                state,
                source="pubmed.search",
                code="pubmed-error",
                message=str(invocation.exception),
                severity="error",
            )
            state.fallback.events.append(
                FallbackEvent(
                    trigger="pubmed",
                    action="degrade",
                    reason="pubmed-search-error",
                    metadata={"message": str(invocation.exception)},
                )
            )
            state.status = "degraded"
            state.touch()
            return state

        raise invocation.exception

    search_result = invocation.result  # type: ignore[assignment]
    if not isinstance(search_result, PubMedSearchResult):  # pragma: no cover - 防禦
        _append_error_flag(
            state,
            source="pubmed.search",
            code="pubmed-invalid-result",
            message="PubMed search 回傳型別不正確",
        )
        state.pubmed.query_history[-1].status = "failed"
        state.fallback.events.append(
            FallbackEvent(
                trigger="pubmed",
                action="degrade",
                reason="invalid-search-result",
            )
        )
        state.status = "degraded"
        state.touch()
        return state

    state.pubmed.query_history[-1].status = "succeeded"
    state.pubmed.query_history[-1].result_count = search_result.count

    max_ids = min(len(search_result.ids), ctx.config.max_pubmed_ids)
    top_ids = tuple(search_result.ids[:max_ids])

    try:
        TaskGroup = asyncio.TaskGroup  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - Python 3.10 兼容
        details_result, summary_result = await asyncio.gather(
            _call_tool(
                state,
                tool="pubmed",
                action="fetch_details",
                call=lambda: ctx.dependencies.pubmed.fetch_details(top_ids),
            ),
            _call_tool(
                state,
                tool="pubmed",
                action="fetch_summaries",
                call=lambda: ctx.dependencies.pubmed.fetch_summaries(top_ids),
            ),
        )
    else:
        async with TaskGroup() as tg:  # type: ignore[misc]
            details_task = tg.create_task(
                _call_tool(
                    state,
                    tool="pubmed",
                    action="fetch_details",
                    call=lambda: ctx.dependencies.pubmed.fetch_details(top_ids),
                )
            )
            summary_task = tg.create_task(
                _call_tool(
                    state,
                    tool="pubmed",
                    action="fetch_summaries",
                    call=lambda: ctx.dependencies.pubmed.fetch_summaries(top_ids),
                )
            )
        details_result = details_task.result()
        summary_result = summary_task.result()

    if details_result.exception is not None:
        _append_error_flag(
            state,
            source="pubmed.fetch_details",
            code="pubmed-fetch-error",
            message=str(details_result.exception),
        )
        state.fallback.events.append(
            FallbackEvent(
                trigger="pubmed",
                action="degrade",
                reason="fetch-details-error",
            )
        )
        state.status = "degraded"
        state.touch()
        return state

    details: PubMedBatch = details_result.result  # type: ignore[assignment]
    summaries: Sequence[PubMedSummary] = (
        summary_result.result if summary_result.result else tuple()
    )
    summary_map = {summary.pmid: summary for summary in summaries}

    documents: list[PubMedDocument] = []
    for article in details.articles:
        documents.append(
            _article_to_document(
                article,
                summary_map.get(article.pmid),
                search_result.metrics,
            )
        )

    state.pubmed.results = documents
    state.pubmed.empty_retry_count = 0
    state.ui.partial_updates.append(
        StreamUpdate(segment="pubmed_search", content=f"文獻檢索完成，共取得 {len(documents)} 篇內容。")
    )

    if details.warnings:
        _append_error_flag(
            state,
            source="pubmed.fetch_details",
            code="pubmed-warnings",
            message=";".join(details.warnings),
            severity="info",
        )

    logger.debug(
        "pubmed search completed",
        extra={"results": len(documents), "term": latest_query.term},
    )

    state.touch()
    return state


def _article_to_document(
    article: PubMedArticle,
    summary: PubMedSummary | None,
    metrics: Mapping[str, Any],
) -> PubMedDocument:
    authors = list(summary.authors) if summary else []
    keywords: list[str] = []
    if summary and summary.raw:
        keywords = [kw for kw in summary.raw.get("keywords", []) if isinstance(kw, str)]
    return PubMedDocument(
        pmid=article.pmid,
        title=article.title,
        abstract=article.abstract,
        journal=article.journal,
        published_at=article.published,
        authors=authors,
        keywords=keywords,
        metadata={
            "fetch_metrics": dict(metrics),
            "raw": article.raw,
        },
    )


async def result_normalizer_node(
    state_in: StateInput,
    ctx: NodeContext,
) -> LangGraphState:
    state = _ensure_state(state_in)
    _activate_node(state, "result_normalizer")
    state.ui.partial_updates.append(
        StreamUpdate(segment="normalizer", content="正在進行資料正規化與向量預處理...")
    )
    logger = ctx.logger()

    if not state.pubmed.results:
        # 如果沒有內容可以處理，直接返回，確保分支邏輯能正確轉向 retry 或 fallback
        state.touch()
        return state

    vector_size = ctx.config.qdrant_settings.vector_size
    vectorizer = ctx.dependencies.vectorizer
    chunks: list[ContextChunk] = []
    qdrant_records: list[QdrantRecord] = []
    search_vector: list[float] | None = None

    for idx, doc in enumerate(state.pubmed.results[: ctx.config.max_context_chunks]):
        content = "\n".join(filter(None, [doc.title, doc.abstract or ""])).strip()
        
        # Qdrant 要求 64 位元整數或 UUID
        # 我們使用 uuid5 (namespace DNS) 與 PMID 結合產生穩定的 UUID
        chunk_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"pmid-{doc.pmid}-{idx}")
        chunk_id = str(chunk_uuid)
        
        chunk = ContextChunk(
            chunk_id=chunk_id,
            content=content,
            source=doc.pmid,
            score=doc.score,
            metadata={
                "journal": doc.journal,
                "published_at": doc.published_at,
                "authors": doc.authors,
            },
        )
        chunks.append(chunk)

        text_for_vector = content or doc.title or doc.abstract or doc.pmid
        vector: Sequence[float]
        if vectorizer:
            try:
                vector = vectorizer(text_for_vector)
            except Exception as exc:  # pragma: no cover - 外部向量器錯誤
                vector = _hash_vector(text_for_vector, vector_size)
                _append_error_flag(
                    state,
                    source="normalizer.vectorizer",
                    code="vectorizer-error",
                    message=str(exc),
                    severity="warning",
                )
        else:
            vector = _hash_vector(text_for_vector, vector_size)

        if len(vector) != vector_size:
            vector = list(vector)[:vector_size]
            if len(vector) < vector_size:
                vector.extend([0.0] * (vector_size - len(vector)))

        qdrant_records.append(
            QdrantRecord(
                id=chunk_id,
                vector=list(vector),
                payload={
                    "pmid": doc.pmid,
                    "title": doc.title,
                    "abstract": doc.abstract,
                    "journal": doc.journal,
                    "published_at": doc.published_at,
                },
            )
        )

        if search_vector is None:
            search_vector = list(vector)

    state.rag.context_bundle = chunks
    state.extensions["qdrant_records"] = qdrant_records
    state.extensions["search_vector"] = search_vector

    logger.debug(
        "normalized pubmed results",
        extra={"chunks": len(chunks), "vector_size": vector_size},
    )
    state.touch()
    return state


StateApplier = Callable[[LangGraphState], None]


async def qdrant_upsert_node(
    state_in: StateInput,
    ctx: NodeContext,
) -> StateApplier:
    state = _ensure_state(state_in)
    qdrant = ctx.dependencies.qdrant
    records: Sequence[QdrantRecord] = state.extensions.get("qdrant_records", [])
    new_metrics: list[BatchTelemetry] = []
    new_error_flags: list[ErrorSignal] = []
    new_fallback_events: list[FallbackEvent] = []
    telemetry_updates: list[ToolInvocationResult] = []
    new_health: str | None = None

    if not records:
        return lambda _: None

    if qdrant is None:
        new_error_flags.append(
            ErrorSignal(
                source="qdrant.upsert",
                code="qdrant-not-configured",
                message="QdrantWrapper 未配置",
                severity="critical",
            )
        )
        new_fallback_events.append(
            FallbackEvent(trigger="qdrant", action="abort", reason="qdrant-unavailable")
        )
        new_health = "unavailable"

        def apply(target: LangGraphState) -> None:
            target.telemetry.error_flags.extend(new_error_flags)
            target.fallback.events.extend(new_fallback_events)
            target.qdrant.health = new_health or target.qdrant.health

        return apply

    invocation = await _call_tool(
        state,
        tool="qdrant",
        action="upsert",
        call=lambda: qdrant.upsert(records),
    )
    telemetry_updates.append(invocation)

    if invocation.exception is not None:
        error = invocation.exception
        if isinstance(error, QdrantError):
            new_error_flags.append(
                ErrorSignal(
                    source="qdrant.upsert",
                    code="qdrant-error",
                    message=str(error),
                    severity="error",
                    data=error.to_payload(),
                )
            )
            new_fallback_events.append(
                FallbackEvent(
                    trigger="qdrant",
                    action="degrade",
                    reason="qdrant-upsert-error",
                    metadata=error.to_payload(),
                )
            )
            new_health = "degraded"
            if isinstance(error, QdrantTimeoutError):
                new_health = "unavailable"
        else:
            new_error_flags.append(
                ErrorSignal(
                    source="qdrant.upsert",
                    code="unknown-error",
                    message=str(error),
                )
            )
            new_health = "degraded"

        def apply_error(target: LangGraphState) -> None:
            target.telemetry.tool_invocations.extend(
                [i.metric for i in telemetry_updates]
            )
            for update in telemetry_updates:
                target.telemetry.active_tasks[update.task_status.task_id] = (
                    update.task_status
                )
            target.telemetry.error_flags.extend(new_error_flags)
            target.fallback.events.extend(new_fallback_events)
            if new_health:
                target.qdrant.health = new_health

        return apply_error

    result: QdrantUpsertResult = invocation.result  # type: ignore[assignment]
    warnings = [failure.get("detail", {}) for failure in result.failed]
    batch = BatchTelemetry(
        batch_size=len(records),
        processed=result.processed,
        latency_ms=(result.metrics.get("latency") or 0) * 1000,
        retry_count=result.metrics.get("retry_count", 0),
        warnings=[str(item) for item in warnings if item],
        detail=dict(result.metrics),
    )
    new_metrics.append(batch)

    if result.failed:
        new_error_flags.append(
            ErrorSignal(
                source="qdrant.upsert",
                code="qdrant-partial-failure",
                message="部分資料 upsert 失敗",
                severity="warning",
                data={"failed": result.failed},
            )
        )
        new_health = "degraded"

    def apply_success(target: LangGraphState) -> None:
        target.qdrant.upsert_metrics.extend(new_metrics)
        target.telemetry.tool_invocations.extend([i.metric for i in telemetry_updates])
        for update in telemetry_updates:
            target.telemetry.active_tasks[update.task_status.task_id] = (
                update.task_status
            )
        target.telemetry.error_flags.extend(new_error_flags)
        if new_health:
            target.qdrant.health = new_health

    return apply_success


async def qdrant_search_node(
    state_in: StateInput,
    ctx: NodeContext,
) -> StateApplier:
    state = _ensure_state(state_in)
    qdrant = ctx.dependencies.qdrant
    telemetry_updates: list[ToolInvocationResult] = []
    new_error_flags: list[ErrorSignal] = []
    new_search_records: list[QdrantSearchRecord] = []
    new_context: list[ContextChunk] = []
    search_vector: Sequence[float] | None = state.extensions.get("search_vector")

    if qdrant is None:
        new_error_flags.append(
            ErrorSignal(
                source="qdrant.search",
                code="qdrant-not-configured",
                message="QdrantWrapper 未配置",
                severity="critical",
            )
        )

        def apply_missing(target: LangGraphState) -> None:
            target.telemetry.error_flags.extend(new_error_flags)
            # 標記 Qdrant 狀態為不可用，避免在批判環節無限重試
            target.qdrant.health = "unavailable"

        return apply_missing

    if not search_vector:
        new_error_flags.append(
            ErrorSignal(
                source="qdrant.search",
                code="missing-search-vector",
                message="查無查詢向量，跳過 Qdrant 查詢",
                severity="warning",
            )
        )

        def apply_no_vector(target: LangGraphState) -> None:
            target.telemetry.error_flags.extend(new_error_flags)

        return apply_no_vector

    request = QdrantQuery(
        vector=list(search_vector), limit=ctx.config.max_context_chunks
    )
    invocation = await _call_tool(
        state,
        tool="qdrant",
        action="query",
        call=lambda: qdrant.query(request),
    )
    telemetry_updates.append(invocation)

    if invocation.exception is not None:
        error = invocation.exception
        if isinstance(error, QdrantError):
            new_error_flags.append(
                ErrorSignal(
                    source="qdrant.search",
                    code="qdrant-error",
                    message=str(error),
                    severity="error",
                    data=error.to_payload(),
                )
            )
        else:
            new_error_flags.append(
                ErrorSignal(
                    source="qdrant.search",
                    code="unknown-error",
                    message=str(error),
                )
            )

        def apply_error(target: LangGraphState) -> None:
            target.telemetry.tool_invocations.extend(
                [i.metric for i in telemetry_updates]
            )
            for update in telemetry_updates:
                target.telemetry.active_tasks[update.task_status.task_id] = (
                    update.task_status
                )
            target.telemetry.error_flags.extend(new_error_flags)

        return apply_error

    result: QdrantQueryResult = invocation.result  # type: ignore[assignment]
    hits: list[VectorHit] = []
    for point in result.points:
        payload: MutableMapping[str, Any] = {}
        if hasattr(point, "payload") and isinstance(point.payload, Mapping):
            payload = dict(point.payload)
        hits.append(
            VectorHit(
                point_id=str(getattr(point, "id", "")),
                score=float(getattr(point, "score", 0.0)),
                payload=payload,
                source="qdrant",
            )
        )
        if payload.get("pmid") and payload.get("abstract"):
            chunk_id = f"rag-{payload['pmid']}"
            new_context.append(
                ContextChunk(
                    chunk_id=chunk_id,
                    content="\n".join(
                        filter(None, [payload.get("title"), payload.get("abstract")])
                    ),
                    source=payload.get("pmid"),
                    score=float(getattr(point, "score", 0.0)),
                    metadata={
                        "journal": payload.get("journal"),
                        "published_at": payload.get("published_at"),
                    },
                )
            )

    search_record = QdrantSearchRecord(
        query_vector=list(search_vector),
        hits=hits,
        latency_ms=(result.metrics.get("latency", 0.0) or 0.0) * 1000,
        degraded=bool(result.metrics.get("warnings")),
        notes=[str(w) for w in result.metrics.get("warnings", [])],
    )
    new_search_records.append(search_record)

    def apply_success(target: LangGraphState) -> None:
        target.qdrant.search_results.extend(new_search_records)
        if new_context:
            target.rag.context_bundle.extend(new_context)
        target.telemetry.tool_invocations.extend([i.metric for i in telemetry_updates])
        for update in telemetry_updates:
            target.telemetry.active_tasks[update.task_status.task_id] = (
                update.task_status
            )
        target.telemetry.error_flags.extend(new_error_flags)

    return apply_success


async def qdrant_parallel_node(
    state_in: StateInput,
    ctx: NodeContext,
) -> LangGraphState:
    state = _ensure_state(state_in)
    _activate_node(state, "qdrant_parallel")
    upsert_copy = _deep_copy_state(state)
    search_copy = _deep_copy_state(state)

    try:
        TaskGroup = asyncio.TaskGroup  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - Python 3.10 兼容

        async def _run_upsert() -> StateApplier:
            return await qdrant_upsert_node(upsert_copy, ctx)

        async def _run_search() -> StateApplier:
            return await qdrant_search_node(search_copy, ctx)

        upsert_applier, search_applier = await asyncio.gather(
            _run_upsert(), _run_search()
        )
    else:
        async with TaskGroup() as tg:  # type: ignore[misc]
            upsert_task = tg.create_task(qdrant_upsert_node(upsert_copy, ctx))
            search_task = tg.create_task(qdrant_search_node(search_copy, ctx))
        upsert_applier = upsert_task.result()
        search_applier = search_task.result()

    for applier in (upsert_applier, search_applier):
        applier(state)

    state.touch()
    return state


async def rag_synthesizer_node(
    state_in: StateInput,
    ctx: NodeContext,
) -> LangGraphState:
    state = _ensure_state(state_in)
    _activate_node(state, "rag_synthesizer")

    context = state.rag.context_bundle[: ctx.config.max_context_chunks]
    if not context:
        _append_error_flag(
            state,
            source="rag.synth",
            code="missing-context",
            message="缺少上下文，暫無法產生回答",
            severity="warning",
        )
        state.touch()
        return state

    summary_segments = []
    citations: list[str] = []
    for idx, chunk in enumerate(context, start=1):
        summary_segments.append(f"({idx}) {chunk.content[:400]}")
        if chunk.source:
            citations.append(chunk.source)

    answer = (
        "以下為依據 PubMed 與 Qdrant 檢索結果所整理的重點：\n"
        + "\n".join(summary_segments)
        + "\n\n請注意：此回答僅供學術參考，非醫療建議。"
    )
    state.rag.answer_draft = answer
    state.rag.synthesis_notes = (
        [f"引用文獻：{', '.join(citations)}"] if citations else []
    )

    state.ui.partial_updates.append(
        StreamUpdate(segment="rag_draft", content=answer, channel="text")
    )

    state.touch()
    return state


async def medical_critic_node(
    state_in: StateInput,
    ctx: NodeContext,
) -> LangGraphState:
    state = _ensure_state(state_in)
    _activate_node(state, "medical_critic")

    answer = state.rag.answer_draft or ""
    context_count = len(state.rag.context_bundle)
    feedbacks: list[CriticFeedback] = []
    trust_score = 0.6
    revision_required = False
    critic_next = "final"
    issue_code = ""

    if context_count >= ctx.config.min_context_for_confidence and len(answer) > 100:
        trust_score = 0.85
        revision_required = False
    else:
        revision_required = True
        issue_code = "critic-data-gap" if context_count == 0 else "critic-weak-context"
        feedbacks.append(
            CriticFeedback(
                issue="缺乏足夠的佐證片段" if context_count == 0 else "摘要篇幅不足",
                severity="major",
                suggestion="請增加更多來源或補充臨床背景。",
                requires_revision=True,
                source_nodes=["rag_synthesizer"],
            )
        )
        critic_next = "result_normalizer" if context_count == 0 else "rag_synthesizer"

    if not state.pubmed.results:
        revision_required = True
        issue_code = "critic-plan-gap"
        critic_next = "planner"
        feedbacks.append(
            CriticFeedback(
                issue="檢索結果不足以支持結論",
                severity="critical",
                suggestion="重新規劃檢索策略，納入更多關鍵字。",
                requires_revision=True,
                source_nodes=["pubmed_search"],
            )
        )

    state.critic.findings = feedbacks
    state.critic.trust_score = trust_score
    state.critic.revision_required = revision_required
    state.extensions["critic_next"] = critic_next

    if revision_required:
        state.retry_counters["critic"] = state.retry_counters.get("critic", 0) + 1
        _append_error_flag(
            state,
            source="medical_critic",
            code=issue_code or "critic-revision",
            message="醫療審查要求修正",
            severity="warning",
            data={"next": critic_next},
        )
    else:
        state.retry_counters.pop("critic", None)

    state.touch()
    return state


async def fallback_recovery_node(
    state_in: StateInput, ctx: NodeContext
) -> LangGraphState:
    state = _ensure_state(state_in)
    _activate_node(state, "fallback")

    if state.fallback.terminal_reason:
        state.touch()
        return state

    reason = None
    if state.pubmed.empty_retry_count > ctx.config.pubmed_empty_threshold:
        reason = "pubmed-no-result"
    elif state.qdrant.health != "healthy":
        reason = "qdrant-degraded"
    elif state.retry_counters.get("critic", 0) > ctx.config.critic_revision_limit:
        reason = "critic-failed-validation"

    if reason:
        state.fallback.events.append(
            FallbackEvent(trigger="orchestrator", action="finalize", reason=reason)
        )
        state.fallback.terminal_reason = reason
        _append_error_flag(
            state,
            source="fallback",
            code=reason,
            message=f"啟動降級終止流程: {reason}",
            severity="warning",
        )

    state.touch()
    return state


async def final_responder_node(
    state_in: StateInput, ctx: NodeContext
) -> LangGraphState:
    state = _ensure_state(state_in)
    _activate_node(state, "final_responder")

    if state.fallback.terminal_reason:
        status = "degraded"
        message = (
            "目前研究工作無法順利完成，原因："
            f"{state.fallback.terminal_reason}. 建議稍後再試或採用替代資訊來源。"
        )
    else:
        status = "succeeded"
        message = state.rag.answer_draft or "尚無可用的回答。"

    state.ui.partial_updates.append(
        StreamUpdate(segment="final", content=message, final=True)
    )
    state.telemetry.tool_invocations.append(
        ToolCallMetric(tool="orchestrator", action="respond", status="success")
    )
    state.touch()
    state.status = status
    state.touch()
    return state


def _pubmed_branch(state: StateInput, ctx: NodeContext) -> Literal[
    "continue",
    "retry",
    "fallback",
]:
    current = _ensure_state(state)
    logger = ctx.logger()
    
    if current.pubmed.results:
        logger.info("PubMed Branch: continue (results found)")
        return "continue"
        
    retry_count = current.pubmed.empty_retry_count
    
    # 核心邏輯修改：如果沒有結果且重試次數少於 3 次，則回 planner (retry)
    if retry_count < 3:
        logger.info(f"PubMed Branch: retry (count: {retry_count}, iteration: {current.planning.iteration})")
        return "retry"
    
    # 超過 3 次強制進入降級 (fallback)
    logger.warning(f"PubMed Branch: fallback (retry limit 3 reached, current: {retry_count})")
    return "fallback"


def _critic_branch(state: StateInput, ctx: NodeContext) -> Literal[
    "approve",
    "normalize",
    "plan",
    "fallback",
    "revise",
]:
    current = _ensure_state(state)
    if not current.critic.revision_required:
        return "approve"
    retry_count = current.retry_counters.get("critic", 0)
    if retry_count > ctx.config.critic_revision_limit:
        return "fallback"
    next_node = current.extensions.get("critic_next", "revise")
    if next_node == "planner":
        return "plan"
    if next_node == "result_normalizer":
        return "normalize"
    if next_node == "rag_synthesizer":
        return "revise"
    return "revise"


async def _sequential_executor(
    ctx: NodeContext,
    state: LangGraphState,
) -> LangGraphState:
    """在缺少 langgraph 時的手動流程執行器。"""

    state = await planner_node(state, ctx)

    while True:
        state = await pubmed_search_node(state, ctx)
        branch = _pubmed_branch(state, ctx)
        if branch == "continue":
            break
        if branch == "fallback":
            state = await fallback_recovery_node(state, ctx)
            return await final_responder_node(state, ctx)
        state = await planner_node(state, ctx)

    state = await result_normalizer_node(state, ctx)
    state = await qdrant_parallel_node(state, ctx)
    while True:
        state = await rag_synthesizer_node(state, ctx)
        state = await medical_critic_node(state, ctx)
        critic_branch = _critic_branch(state, ctx)
        if critic_branch == "approve":
            break
        if critic_branch == "plan":
            state = await planner_node(state, ctx)
            continue
        if critic_branch == "normalize":
            state = await result_normalizer_node(state, ctx)
            state = await qdrant_parallel_node(state, ctx)
            continue
        if critic_branch == "revise":
            continue
        state = await fallback_recovery_node(state, ctx)
        return await final_responder_node(state, ctx)

    state = await fallback_recovery_node(state, ctx)
    state = await final_responder_node(state, ctx)
    return state


class FallbackCompiledGraph(CompiledGraph):  # type: ignore[misc]
    """缺少 langgraph 套件時的兼容執行器。"""

    def __init__(self, ctx: NodeContext) -> None:
        self._ctx = ctx

    def invoke(self, state: Mapping[str, Any]) -> dict[str, Any]:
        state_obj = _ensure_state(state)
        result = _run_sync(_sequential_executor(self._ctx, state_obj))
        return _export_state(result)

    async def ainvoke(self, state: Mapping[str, Any]) -> dict[str, Any]:
        state_obj = _ensure_state(state)
        result = await _sequential_executor(self._ctx, state_obj)
        return _export_state(result)

    def get_graph(self) -> Mapping[str, Any]:
        return {"mode": "fallback"}


def build_medical_research_graph(
    *,
    dependencies: OrchestratorDependencies | None = None,
    config: OrchestratorConfig | None = None,
) -> CompiledGraph:
    """建立醫學研究工作流程的 LangGraph 圖實例。"""

    deps = dependencies or OrchestratorDependencies()
    cfg = config or OrchestratorConfig()
    ctx = NodeContext(dependencies=deps, config=cfg)

    if not LANGGRAPH_AVAILABLE or StateGraph is None:
        return FallbackCompiledGraph(ctx)

    async def _planner_wrapper(state: Mapping[str, Any]) -> dict[str, Any]:
        result = await planner_node(state, ctx)
        return _export_state(result)

    async def _pubmed_wrapper(state: Mapping[str, Any]) -> dict[str, Any]:
        result = await pubmed_search_node(state, ctx)
        return _export_state(result)

    async def _normalizer_wrapper(state: Mapping[str, Any]) -> dict[str, Any]:
        result = await result_normalizer_node(state, ctx)
        return _export_state(result)

    async def _qdrant_parallel_wrapper(state: Mapping[str, Any]) -> dict[str, Any]:
        result = await qdrant_parallel_node(state, ctx)
        return _export_state(result)

    async def _rag_wrapper(state: Mapping[str, Any]) -> dict[str, Any]:
        result = await rag_synthesizer_node(state, ctx)
        return _export_state(result)

    async def _critic_wrapper(state: Mapping[str, Any]) -> dict[str, Any]:
        result = await medical_critic_node(state, ctx)
        return _export_state(result)

    async def _fallback_wrapper(state: Mapping[str, Any]) -> dict[str, Any]:
        result = await fallback_recovery_node(state, ctx)
        return _export_state(result)

    async def _final_wrapper(state: Mapping[str, Any]) -> dict[str, Any]:
        result = await final_responder_node(state, ctx)
        return _export_state(result)

    graph = StateGraph(LangGraphState)
    graph.add_node("planner", _planner_wrapper)
    graph.add_node("pubmed_search", _pubmed_wrapper)
    graph.add_node("result_normalizer", _normalizer_wrapper)
    graph.add_node("qdrant_parallel", _qdrant_parallel_wrapper)
    graph.add_node("rag_synthesizer", _rag_wrapper)
    graph.add_node("medical_critic", _critic_wrapper)
    graph.add_node("fallback", _fallback_wrapper)
    graph.add_node("final_responder", _final_wrapper)

    graph.set_entry_point("planner")
    graph.add_edge("planner", "pubmed_search")

    def pubmed_branch_wrapper(state: Mapping[str, Any]) -> str:
        return _pubmed_branch(state, ctx)

    graph.add_conditional_edges(
        "pubmed_search",
        pubmed_branch_wrapper,
        {
            "continue": "result_normalizer",
            "retry": "planner",
            "fallback": "fallback",
        },
    )

    graph.add_edge("result_normalizer", "qdrant_parallel")
    graph.add_edge("qdrant_parallel", "rag_synthesizer")
    graph.add_edge("fallback", "final_responder")

    def critic_branch_wrapper(state: Mapping[str, Any]) -> str:
        return _critic_branch(state, ctx)

    graph.add_conditional_edges(
        "medical_critic",
        critic_branch_wrapper,
        {
            "approve": "fallback",
            "normalize": "result_normalizer",
            "plan": "planner",
            "revise": "rag_synthesizer",
            "fallback": "fallback",
        },
    )

    graph.add_edge("rag_synthesizer", "medical_critic")
    graph.add_edge("final_responder", END)

    compiled = graph.compile()
    return compiled


__all__ = [
    "OrchestratorDependencies",
    "OrchestratorConfig",
    "build_medical_research_graph",
    "planner_node",
    "pubmed_search_node",
    "result_normalizer_node",
    "qdrant_upsert_node",
    "qdrant_search_node",
    "qdrant_parallel_node",
    "rag_synthesizer_node",
    "medical_critic_node",
    "fallback_recovery_node",
    "final_responder_node",
]
