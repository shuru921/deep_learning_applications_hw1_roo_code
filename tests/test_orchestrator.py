"""LangGraph Orchestrator 測試：涵蓋成功與降級流程。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from src.orchestrator.graph import (
    OrchestratorConfig,
    OrchestratorDependencies,
    build_medical_research_graph,
)
from src.orchestrator.schemas import LangGraphState
from src.settings import PubMedSettings, QdrantSettings

from src.clients.pubmed_wrapper import (
    PubMedArticle,
    PubMedBatch,
    PubMedSearchResult,
    PubMedSummary,
)
from src.clients.qdrant_wrapper import QdrantQueryResult, QdrantUpsertResult


def _dump_state(state: LangGraphState) -> Mapping[str, Any]:
    if hasattr(state, "model_dump"):
        return state.model_dump(mode="python")  # type: ignore[call-arg]
    return state.dict()  # type: ignore[call-arg]


def _load_state(payload: Mapping[str, Any]) -> LangGraphState:
    if hasattr(LangGraphState, "model_validate"):
        return LangGraphState.model_validate(payload)  # type: ignore[attr-defined]
    return LangGraphState.parse_obj(payload)  # type: ignore[call-arg]


@pytest.fixture()
def fallback_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.orchestrator.graph.LANGGRAPH_AVAILABLE", False, raising=False
    )
    monkeypatch.setattr("src.orchestrator.graph.StateGraph", None, raising=False)


@dataclass(slots=True)
class StubPubMedSuccess:
    search_called: int = 0

    async def search(self, query: Any) -> PubMedSearchResult:
        self.search_called += 1
        return PubMedSearchResult(
            ids=("12345",),
            query_key="q1",
            webenv="env1",
            count=1,
            metrics={"request_id": "search-1", "retry_count": 0},
        )

    async def fetch_details(self, ids: Any) -> PubMedBatch:
        article = PubMedArticle(
            pmid="12345",
            title="免疫療法臨床試驗",
            abstract="研究顯示免疫療法具顯著存活率提升。",
            journal="Clinical Trials Journal",
            published="2024-01-15",
            raw={"pmid": "12345"},
        )
        return PubMedBatch(
            articles=(article,),
            raw_xml=b"<PubmedArticleSet />",
            metrics={"request_id": "details-1", "retry_count": 0},
        )

    async def fetch_summaries(self, ids: Any) -> tuple[PubMedSummary, ...]:
        summary = PubMedSummary(
            pmid="12345",
            title="免疫療法臨床試驗",
            authors=("Dr. Roo", "Dr. Lang"),
            source="Clinical Trials Journal",
            pubdate="2024",
            raw={"title": "免疫療法臨床試驗"},
            metrics={"request_id": "summary-1", "retry_count": 0},
            warnings=(),
        )
        return (summary,)


@dataclass(slots=True)
class StubQdrantSuccess:
    upsert_called: int = 0
    query_called: int = 0

    async def upsert(self, records: Any) -> QdrantUpsertResult:
        self.upsert_called += 1
        return QdrantUpsertResult(
            processed=len(records),
            failed=(),
            metrics={"latency": 0.001, "retry_count": 0, "warnings": ()},
        )

    async def query(self, request: Any) -> QdrantQueryResult:
        self.query_called += 1
        point = SimpleNamespace(
            id="vec-1",
            score=0.93,
            payload={
                "pmid": "12345",
                "title": "免疫療法臨床試驗",
                "abstract": "研究顯示免疫療法具顯著存活率提升。",
                "journal": "Clinical Trials Journal",
                "published_at": "2024-01-15",
            },
        )
        return QdrantQueryResult(
            points=(point,),
            metrics={"latency": 0.002, "warnings": ()},
            warnings=(),
        )


def _run_graph(
    dependencies: OrchestratorDependencies, config: OrchestratorConfig
) -> LangGraphState:
    graph = build_medical_research_graph(dependencies=dependencies, config=config)
    initial_state = LangGraphState()
    initial_state.user_query.raw_prompt = "請提供免疫療法相關研究摘要"
    result_payload = graph.invoke(_dump_state(initial_state))
    return _load_state(result_payload)


def _collect_metrics(state: LangGraphState) -> dict[tuple[str, str], str]:
    metrics: dict[tuple[str, str], str] = {}
    for metric in state.telemetry.tool_invocations:
        metrics[(metric.tool, metric.action)] = metric.status
    return metrics


def test_orchestrator_success_pipeline(fallback_executor: None) -> None:
    dependencies = OrchestratorDependencies(
        pubmed=StubPubMedSuccess(),
        qdrant=StubQdrantSuccess(),
    )
    config = OrchestratorConfig(
        pubmed_settings=PubMedSettings(rate_requests=2),
        qdrant_settings=QdrantSettings(vector_size=8),
        max_pubmed_ids=5,
        max_context_chunks=3,
    )

    state = _run_graph(dependencies, config)

    assert state.status == "succeeded"
    assert state.pubmed.results, "應成功聚合 PubMed 結果"
    assert state.rag.context_bundle, "應建立 RAG 上下文"
    assert state.rag.answer_draft is not None and "免疫療法" in state.rag.answer_draft

    rag_updates = [
        update for update in state.ui.partial_updates if update.segment == "rag_draft"
    ]
    assert rag_updates, "應推送 RAG 草稿至 UI"

    final_update = state.ui.partial_updates[-1]
    assert final_update.final is True
    assert final_update.segment == "final"
    assert "以下為依據" in final_update.content

    metrics = _collect_metrics(state)
    assert metrics[("pubmed", "search")] == "success"
    assert metrics[("pubmed", "fetch_details")] == "success"
    assert metrics[("pubmed", "fetch_summaries")] == "success"
    assert metrics[("qdrant", "upsert")] == "success"
    assert metrics[("qdrant", "query")] == "success"
    assert metrics[("orchestrator", "respond")] == "success"

    assert not state.fallback.terminal_reason
    assert state.critic.revision_required is False
    assert all(flag.severity != "error" for flag in state.telemetry.error_flags)


def test_orchestrator_qdrant_degradation_triggers_fallback(
    fallback_executor: None,
) -> None:
    pubmed_stub = StubPubMedSuccess()
    dependencies = OrchestratorDependencies(pubmed=pubmed_stub, qdrant=None)
    config = OrchestratorConfig(
        pubmed_settings=PubMedSettings(rate_requests=1),
        qdrant_settings=QdrantSettings(vector_size=8),
        max_pubmed_ids=5,
        max_context_chunks=2,
    )

    state = _run_graph(dependencies, config)

    assert state.status == "degraded"
    assert state.fallback.terminal_reason == "qdrant-degraded"
    assert state.fallback.events, "應記錄降級事件"

    qdrant_flags = [
        flag for flag in state.telemetry.error_flags if flag.source.startswith("qdrant")
    ]
    assert qdrant_flags, "應紀錄 Qdrant 相關錯誤旗標"
    assert any(flag.code == "qdrant-not-configured" for flag in qdrant_flags)

    metrics = _collect_metrics(state)
    assert metrics[("pubmed", "search")] == "success"
    assert metrics[("pubmed", "fetch_details")] == "success"
    assert metrics[("pubmed", "fetch_summaries")] == "success"
    assert metrics[("orchestrator", "respond")] == "success"

    final_update = state.ui.partial_updates[-1]
    assert final_update.final is True
    assert "無法順利完成" in final_update.content

    assert state.qdrant.health == "unavailable"
