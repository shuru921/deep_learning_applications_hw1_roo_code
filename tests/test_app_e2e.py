"""FastAPI 端到端測試，涵蓋成功與降級場景。"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
from httpx import ASGITransport
import pytest
from fastapi import FastAPI, Request

from src.app import create_app
from src.app.deps import (
    OrchestratorGraphManager,
    get_correlation_id,
    get_graph_manager,
)
from src.app.routes import NDJSON_MEDIA_TYPE
from src.errors import QdrantError
from src.orchestrator.graph import (
    OrchestratorDependencies,
    build_medical_research_graph,
)


async def _collect_ndjson_stream(response: httpx.Response) -> list[dict[str, Any]]:
    """收集 NDJSON streaming 回應。"""

    events: list[dict[str, Any]] = []
    async for line in response.aiter_lines():
        if not line:
            continue
        data = json.loads(line)
        if data.get("event") == "error":
            print(f"\n--- Stream Error Details ---")
            print(f"Error: {data.get('error')}, Message: {data.get('message')}")
        events.append(data)
    return events


class StubQdrantUnavailable:
    """模擬 Qdrant 錯誤的 stub。"""

    async def upsert(self, records: Any) -> Any:
        raise QdrantError(message="qdrant outage", operation="upsert")

    async def query(self, request: Any) -> Any:
        raise QdrantError(message="qdrant outage", operation="query")


async def _build_graph_manager(
    dependencies: OrchestratorDependencies,
) -> OrchestratorGraphManager:
    graph = build_medical_research_graph(dependencies=dependencies)

    class _ImmediateManager(OrchestratorGraphManager):
        def __init__(self) -> None:
            super().__init__(lambda: graph)

        async def get_graph(self) -> Any:  # type: ignore[override]
            return graph

    return _ImmediateManager()


async def create_test_app(
    *,
    pubmed: Any,
    qdrant: Any,
    correlation_id: str = "test-correlation",
) -> FastAPI:
    dependencies = OrchestratorDependencies(pubmed=pubmed, qdrant=qdrant)
    manager = await _build_graph_manager(dependencies)
    app = create_app()

    async def _override_graph_manager() -> OrchestratorGraphManager:
        return manager

    async def _override_correlation_id(request: Request) -> str:
        request.state.correlation_id = correlation_id
        return correlation_id

    app.dependency_overrides[get_graph_manager] = _override_graph_manager
    app.dependency_overrides[get_correlation_id] = _override_correlation_id
    return app


@pytest.fixture()
async def success_app(
    stub_pubmed_success,
    stub_qdrant_success,
) -> AsyncIterator[FastAPI]:
    app = await create_test_app(
        pubmed=stub_pubmed_success,
        qdrant=stub_qdrant_success,
    )
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
async def async_client(success_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """建立 httpx AsyncClient。"""

    transport = ASGITransport(app=success_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


@pytest.mark.anyio
async def test_api_research_success_stream(
    caplog: pytest.LogCaptureFixture,
    async_client: httpx.AsyncClient,
) -> None:
    caplog.set_level(logging.INFO, logger="app.stream")
    response = await async_client.post(
        "/api/research",
        json={"query": "免疫療法 臨床摘要"},
        headers={
            "Accept": NDJSON_MEDIA_TYPE,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    correlation_id = response.headers.get("X-Correlation-ID")
    assert correlation_id
    events = await _collect_ndjson_stream(response)
    event_sequence = [event["event"] for event in events]
    assert event_sequence[0] == "start"
    assert event_sequence[-1] == "complete"
    assert "update" in event_sequence
    summary_event = next(event for event in events if event["event"] == "summary")
    updates = [event for event in events if event["event"] == "update"]

    correlation_ids = {event["correlation_id"] for event in events}
    assert len(correlation_ids) == 1
    correlation_id = correlation_ids.pop()
    assert correlation_id == response.headers["X-Correlation-ID"]

    assert any(update["segment"] == "rag_draft" for update in updates)
    assert any("免疫" in update["content"] for update in updates)
    assert any(update["segment"] == "final" for update in updates)

    telemetry = summary_event["telemetry"]
    assert summary_event["status"] == "succeeded"
    assert summary_event["fallback"] == []
    assert summary_event["terminal_reason"] is None
    assert telemetry["correlation_id"] == correlation_id
    assert all(flag["severity"] != "error" for flag in telemetry["error_flags"])
    assert all(flag["severity"] != "critical" for flag in telemetry["error_flags"])

    start_logs = [
        record for record in caplog.records if getattr(record, "event", "") == "start"
    ]
    complete_logs = [
        record
        for record in caplog.records
        if getattr(record, "event", "") == "complete"
    ]
    assert start_logs and start_logs[0].correlation_id == correlation_id
    assert complete_logs and complete_logs[0].correlation_id == correlation_id


@pytest.mark.anyio
async def test_ui_query_qdrant_degraded(
    caplog: pytest.LogCaptureFixture,
    stub_pubmed_success,
) -> None:
    qdrant_stub = StubQdrantUnavailable()
    app = await create_test_app(
        pubmed=stub_pubmed_success,
        qdrant=qdrant_stub,
        correlation_id="degraded-correlation",
    )
    caplog.set_level(logging.INFO, logger="app.stream")
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/ui/query",
            data={"query": "免疫療法 臨床摘要"},
            headers={
                "Accept": NDJSON_MEDIA_TYPE,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert response.status_code == 200
        events = await _collect_ndjson_stream(response)
        event_sequence = [event["event"] for event in events]
        assert event_sequence[0] == "start"
        assert event_sequence[-1] == "complete"
        summary_event = next(event for event in events if event["event"] == "summary")
        updates = [event for event in events if event["event"] == "update"]

        correlation_ids = {event["correlation_id"] for event in events}
        assert correlation_ids == {"degraded-correlation"}
        assert response.headers["X-Correlation-ID"] == "degraded-correlation"

        assert summary_event["status"] == "degraded"
        assert summary_event["fallback"]
        assert summary_event["terminal_reason"] == "qdrant-degraded"

        telemetry = summary_event["telemetry"]
        qdrant_flags = [
            flag for flag in telemetry["error_flags"] if "qdrant" in flag["source"]
        ]
        assert qdrant_flags, "應記錄 Qdrant 降級錯誤旗標"
        assert any(flag["severity"] in {"error", "critical"} for flag in qdrant_flags)

        final_update = next(
            update for update in updates if update["segment"] == "final"
        )
        assert "無法順利完成" in final_update["content"]

        fallback_events = summary_event["fallback"]
        assert any(event["trigger"] == "qdrant" for event in fallback_events)

        complete_logs = [
            record
            for record in caplog.records
            if getattr(record, "event", "") == "complete"
        ]
        assert complete_logs and complete_logs[0].status == "degraded"
