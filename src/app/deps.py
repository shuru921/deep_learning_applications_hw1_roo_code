"""FastAPI 依賴與 Orchestrator 物件管理。"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Callable
import uuid

from fastapi import Request

from ..orchestrator.graph import (
    OrchestratorConfig,
    OrchestratorDependencies,
    build_medical_research_graph,
)
from ..settings import AppSettings


if TYPE_CHECKING:  # pragma: no cover - 僅供型別檢查
    from langgraph.graph.state import CompiledStateGraph as CompiledGraph


CompiledGraphFactory = Callable[[], "CompiledGraph"]


class OrchestratorGraphManager:
    """負責延遲初始化並快取 LangGraph compiled graph。"""

    def __init__(
        self,
        factory: CompiledGraphFactory,
    ) -> None:
        self._factory = factory
        self._graph: "CompiledGraph" | None = None
        self._lock = asyncio.Lock()

    async def get_graph(self) -> "CompiledGraph":
        """取得 compiled graph，若未初始化則建立。"""

        if self._graph is not None:
            return self._graph
        async with self._lock:
            if self._graph is None:
                self._graph = self._factory()
        return self._graph


async def get_graph_manager(request: Request) -> OrchestratorGraphManager:
    """作為 FastAPI 依賴，回傳既有的 graph manager。"""

    return request.app.state.graph_manager


async def get_app_settings(request: Request) -> AppSettings:
    """回傳快取於應用程式狀態的 AppSettings。"""

    return request.app.state.app_settings


async def get_correlation_id(request: Request) -> str:
    """取得或生成本次請求所使用的 correlation ID。"""

    existing: str | None = getattr(request.state, "correlation_id", None)
    if existing:
        return existing

    header_id = request.headers.get("X-Correlation-ID")
    correlation_id = header_id.strip() if header_id else str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    return correlation_id


def create_default_graph_factory() -> CompiledGraphFactory:
    """建立預設的 compiled graph 工廠函式。"""

    dependencies = OrchestratorDependencies()
    config = OrchestratorConfig()
    return partial(
        build_medical_research_graph,
        dependencies=dependencies,
        config=config,
    )


__all__ = [
    "OrchestratorGraphManager",
    "create_default_graph_factory",
    "get_graph_manager",
    "get_app_settings",
    "get_correlation_id",
]
