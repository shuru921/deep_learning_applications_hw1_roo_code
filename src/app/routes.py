"""應用程式路由與 streaming 端點。"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, AsyncIterator, Iterable, Mapping

from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from ..orchestrator.schemas import LangGraphState
from .deps import (
    OrchestratorGraphManager,
    get_app_settings,
    get_correlation_id,
    get_graph_manager,
)
from .utils import ensure_correlation_id


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
    return (json.dumps(_to_json_compatible(data), ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _to_json_compatible(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, BaseModel):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")  # type: ignore[attr-defined]
        return value.dict()
    if isinstance(value, Mapping):
        return {key: _to_json_compatible(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_compatible(item) for item in value]
    return value


async def _graph_state_stream(
    manager: OrchestratorGraphManager,
    state_payload: dict[str, Any],
) -> AsyncIterator[LangGraphState]:
    graph = await manager.get_graph()

    if hasattr(graph, "astream"):
        async for snapshot in graph.astream(state_payload, config={"recursion_limit": 30}):  # type: ignore[attr-defined]
            if isinstance(snapshot, dict):
                # LangGraph astream (updates mode) returns {node_name: state_update}
                # We iterate over values to get the actual state dicts
                for value in snapshot.values():
                    if isinstance(value, dict):
                        try:
                            yield _deserialize_state(value)
                        except Exception:
                            continue
            elif isinstance(snapshot, (list, tuple)):
                # Handle potential list of states
                for item in snapshot:
                    if isinstance(item, dict):
                        try:
                            yield _deserialize_state(item)
                        except Exception:
                            continue
        return

    result = await graph.ainvoke(state_payload)
    yield _deserialize_state(result)


async def _stream_updates(
    request: Request,
    manager: OrchestratorGraphManager,
    payload: ResearchQuery,
    *,
    correlation_id: str,
    scenario: str,
    logger: logging.Logger,
) -> AsyncIterator[bytes]:
    logger.info(
        "stream start",
        extra={
            "correlation_id": correlation_id,
            "event": "start",
            "scenario": scenario,
        },
    )
    yield _encode_ndjson(
        {
            "event": "start",
            "query": payload.query,
            "correlation_id": correlation_id,
        }
    )

    initial_state = LangGraphState()
    initial_state.user_query.raw_prompt = payload.query
    if payload.max_articles is not None:
        constraints = dict(getattr(initial_state.user_query, "constraints", {}))
        constraints["max_articles"] = payload.max_articles
        initial_state.user_query.constraints = constraints  # type: ignore[assignment]
    ensure_correlation_id(initial_state, correlation_id)

    try:
        async for state in _graph_state_stream(
            manager,
            _serialize_state(initial_state),
        ):
            ensure_correlation_id(state, correlation_id)
            logger.info(
                "stream update",
                extra={
                    "correlation_id": correlation_id,
                    "event": "update",
                    "scenario": scenario,
                    "status": state.status,
                },
            )
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
                        "correlation_id": correlation_id,
                    }
                )

        summary_state = state
        ensure_correlation_id(summary_state, correlation_id)
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
                "correlation_id": correlation_id,
            }
        )

        logger.info(
            "stream summary",
            extra={
                "correlation_id": correlation_id,
                "event": "summary",
                "scenario": scenario,
                "status": summary_state.status,
                "error_flags": [
                    flag.code for flag in summary_state.telemetry.error_flags
                ],
                "fallback_events": len(summary_state.fallback.events),
            },
        )
        logger.info(
            "stream complete",
            extra={
                "correlation_id": correlation_id,
                "event": "complete",
                "scenario": scenario,
                "status": summary_state.status,
            },
        )
        yield _encode_ndjson(
            {
                "event": "complete",
                "status": summary_state.status,
                "correlation_id": correlation_id,
            }
        )

    except RuntimeError as exc:
        yield _encode_ndjson(
            {
                "event": "error",
                "error": "graph_unavailable",
                "message": str(exc),
                "correlation_id": correlation_id,
            }
        )
    except Exception as exc:  # pragma: no cover - 外部依賴錯誤
        if hasattr(exc, "exceptions"):
            for idx, sub_exc in enumerate(getattr(exc, "exceptions", [])):
                print(
                    f"Sub-exception {idx}: {type(sub_exc).__name__} - {sub_exc}",
                )
        print(f"Main Error: {type(exc).__name__} - {exc}")
        yield _encode_ndjson(
            {
                "event": "error",
                "error": "execution_failed",
                "message": str(exc),
                "correlation_id": correlation_id,
            }
        )


api_router = APIRouter()
ui_router = APIRouter()


def include_routes(app: FastAPI, *, settings: Any | None = None) -> None:
    """將此模組的路由掛載至 FastAPI 應用。"""

    api_prefix = settings.api_base_path if settings else ""
    ui_prefix = settings.ui_base_path if settings else ""
    app.include_router(api_router, prefix=api_prefix)
    app.include_router(ui_router, prefix=ui_prefix)

    @app.get("/.well-known/appspecific/com.chrome.devtools.json", include_in_schema=False)
    async def chrome_devtools():
        return JSONResponse(content={})


@ui_router.get("", response_class=HTMLResponse)
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


@ui_router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return HTMLResponse("")





@api_router.post("/research")
async def api_research(
    request: Request,
    payload: ResearchQuery,
    manager: OrchestratorGraphManager = Depends(get_graph_manager),
    correlation_id: str = Depends(get_correlation_id),
):
    logger = logging.getLogger("app.stream")
    stream = _stream_updates(
        request,
        manager,
        payload,
        correlation_id=correlation_id,
        scenario="api",
        logger=logger,
    )
    headers = {"X-Correlation-ID": correlation_id}
    return StreamingResponse(stream, media_type=NDJSON_MEDIA_TYPE, headers=headers)


@ui_router.post("/query")
async def ui_query(
    request: Request,
    query: str = Form(...),
    max_articles: int | None = Form(default=None),
    manager: OrchestratorGraphManager = Depends(get_graph_manager),
    correlation_id: str = Depends(get_correlation_id),
):
    payload = ResearchQuery(query=query, max_articles=max_articles)
    logger = logging.getLogger("app.stream")
    stream = _stream_updates(
        request,
        manager,
        payload,
        correlation_id=correlation_id,
        scenario="ui",
        logger=logger,
    )
    headers = {"X-Correlation-ID": correlation_id}
    return StreamingResponse(stream, media_type=NDJSON_MEDIA_TYPE, headers=headers)


__all__ = ["include_routes", "api_router", "ui_router"]
