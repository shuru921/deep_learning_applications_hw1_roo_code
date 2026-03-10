"""應用程式路由與 streaming 端點。"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Iterable

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..orchestrator.schemas import LangGraphState
from .deps import OrchestratorGraphManager, get_app_settings, get_graph_manager


NDJSON_MEDIA_TYPE = "application/x-ndjson"


class ResearchQuery(BaseModel):
    """前端 research 查詢 payload。"""

    query: str = Field(..., min_length=1, max_length=4096)
    max_articles: int | None = Field(default=None, ge=1, le=100)


def _serialize_state(state: LangGraphState) -> dict[str, Any]:
    if hasattr(state, "model_dump"):
        return state.model_dump(mode="python")  # type: ignore[call-arg]
    return state.dict()  # type: ignore[call-arg]


def _deserialize_state(payload: dict[str, Any]) -> LangGraphState:
    if hasattr(LangGraphState, "model_validate"):
        return LangGraphState.model_validate(payload)  # type: ignore[attr-defined]
    return LangGraphState.parse_obj(payload)  # type: ignore[call-arg]


def _encode_ndjson(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")


async def _graph_state_stream(
    manager: OrchestratorGraphManager,
    state_payload: dict[str, Any],
) -> AsyncIterator[LangGraphState]:
    graph = await manager.get_graph()

    if hasattr(graph, "astream"):
        async for snapshot in graph.astream(state_payload):  # type: ignore[attr-defined]
            if not isinstance(snapshot, dict):
                continue
            yield _deserialize_state(snapshot)
        return

    result = await graph.ainvoke(state_payload)
    yield _deserialize_state(result)


async def _stream_updates(
    request: Request,
    manager: OrchestratorGraphManager,
    payload: ResearchQuery,
) -> AsyncIterator[bytes]:
    yield _encode_ndjson({"event": "start", "query": payload.query})

    initial_state = LangGraphState()
    initial_state.user_query.raw_prompt = payload.query
    if payload.max_articles is not None:
        constraints = dict(getattr(initial_state.user_query, "constraints", {}))
        constraints["max_articles"] = payload.max_articles
        initial_state.user_query.constraints = constraints  # type: ignore[assignment]

    try:
        async for state in _graph_state_stream(
            manager,
            _serialize_state(initial_state),
        ):
            for update in state.ui.partial_updates:
                if await request.is_disconnected():
                    return
                yield _encode_ndjson(
                    {
                        "event": "update",
                        "segment": update.segment,
                        "final": update.final,
                        "channel": update.channel,
                        "content": update.content,
                        "created_at": update.created_at.isoformat(),
                    }
                )

        summary_state = state
        telemetry_payload = (
            summary_state.telemetry.model_dump(mode="python")
            if hasattr(summary_state.telemetry, "model_dump")
            else summary_state.telemetry.dict()  # type: ignore[call-arg]
        )
        fallback_payload: Iterable[dict[str, Any]] = []
        if summary_state.fallback.events:
            fallback_payload = [
                (
                    event.model_dump(mode="python")
                    if hasattr(event, "model_dump")
                    else event.dict()
                )
                for event in summary_state.fallback.events
            ]

        yield _encode_ndjson(
            {
                "event": "summary",
                "status": summary_state.status,
                "fallback": list(fallback_payload),
                "telemetry": telemetry_payload,
                "terminal_reason": summary_state.fallback.terminal_reason,
            }
        )

        yield _encode_ndjson({"event": "complete", "status": summary_state.status})

    except RuntimeError as exc:
        yield _encode_ndjson(
            {
                "event": "error",
                "error": "graph_unavailable",
                "message": str(exc),
            }
        )
    except Exception as exc:  # pragma: no cover - 外部依賴錯誤
        yield _encode_ndjson(
            {
                "event": "error",
                "error": "execution_failed",
                "message": str(exc),
            }
        )


router = APIRouter()


def include_routes(app: FastAPI, *, settings: Any | None = None) -> None:
    """將此模組的路由掛載至 FastAPI 應用。"""

    app.include_router(router, prefix=settings.api_base_path if settings else "")


@router.get("/ui", response_class=HTMLResponse)
async def render_ui(
    request: Request,
    settings=Depends(get_app_settings),
):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "ui_post_endpoint": f"{settings.ui_base_path}/query",
            "static_base": settings.static_base_path,
        },
    )


@router.post("/api/research")
async def api_research(
    request: Request,
    payload: ResearchQuery,
    manager: OrchestratorGraphManager = Depends(get_graph_manager),
):
    stream = _stream_updates(request, manager, payload)
    return StreamingResponse(stream, media_type=NDJSON_MEDIA_TYPE)


@router.post("/ui/query")
async def ui_query(
    request: Request,
    query: str = Form(...),
    max_articles: int | None = Form(default=None),
    manager: OrchestratorGraphManager = Depends(get_graph_manager),
):
    payload = ResearchQuery(query=query, max_articles=max_articles)
    stream = _stream_updates(request, manager, payload)
    return StreamingResponse(stream, media_type=NDJSON_MEDIA_TYPE)


__all__ = ["include_routes", "router"]
