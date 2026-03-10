"""FastAPI 伺服器組態與入口。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

try:  # pragma: no cover - langgraph 為選用依賴
    from langgraph.graph.state import CompiledStateGraph as CompiledGraph

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover
    CompiledGraph = None  # type: ignore[assignment]
    LANGGRAPH_AVAILABLE = False

from ..errors import ToolingError
from ..settings import AppSettings
from .deps import OrchestratorGraphManager, create_default_graph_factory
from .routes import include_routes


LOGGER = logging.getLogger("app.server")


def _resolve_template_dirs() -> Path:
    templates_dir = Path(__file__).parent / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    return templates_dir


def _resolve_static_dir() -> Path:
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    return static_dir


def create_app() -> FastAPI:
    """FastAPI 應用程式工廠。"""

    settings = AppSettings.from_env()
    app = FastAPI(title="Medical Research Orchestrator", version="0.1.0")

    templates = Jinja2Templates(directory=str(_resolve_template_dirs()))
    static_dir = _resolve_static_dir()
    app.mount(
        settings.static_base_path, StaticFiles(directory=str(static_dir)), name="static"
    )

    if settings.enable_cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.state.app_settings = settings
    app.state.templates = templates
    if LANGGRAPH_AVAILABLE:
        app.state.graph_manager = OrchestratorGraphManager(
            create_default_graph_factory()
        )
    else:

        def _fallback_factory() -> "CompiledGraph":
            raise RuntimeError(
                "langgraph 套件未安裝，請安裝 `langgraph` 或參考 README 說明"
            )

        app.state.graph_manager = OrchestratorGraphManager(_fallback_factory)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _: Any, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "message": exc.errors(),
            },
        )

    @app.exception_handler(ToolingError)
    async def _tooling_error_handler(_: Any, exc: ToolingError) -> JSONResponse:
        payload = exc.to_payload()
        status_code = payload.pop("status_code", 502)
        fallback = payload.pop("fallback", exc.fallback_hint)
        return JSONResponse(
            status_code=status_code,
            content={
                "error": payload.get("error_code", exc.error_code),
                "message": payload.get("message", exc.message),
                "detail": payload.get("detail"),
                "fallback": fallback,
            },
        )

    @app.exception_handler(RuntimeError)
    async def _runtime_error_handler(_: Any, exc: RuntimeError) -> JSONResponse:
        LOGGER.exception("runtime error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": "runtime_error",
                "message": str(exc),
            },
        )

    include_routes(app, settings=settings)
    return app


__all__ = ["create_app"]
