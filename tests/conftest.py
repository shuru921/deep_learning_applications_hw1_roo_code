"""共用測試 fixture。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Callable

import pytest
from fastapi import FastAPI, Request

from src.app.deps import get_app_settings, get_correlation_id
from src.settings import AppSettings
from tests.test_orchestrator import StubPubMedSuccess, StubQdrantSuccess


@pytest.fixture()
def stub_pubmed_success() -> StubPubMedSuccess:
    """建立新的 PubMed 成功 stub。"""

    return StubPubMedSuccess()


@pytest.fixture()
def stub_qdrant_success() -> StubQdrantSuccess:
    """建立新的 Qdrant 成功 stub。"""

    return StubQdrantSuccess()


@pytest.fixture()
def app_dependency_overrides() -> (
    Callable[..., Iterator[dict[Any, Callable[..., Any]]]]
):
    """提供容易覆寫 FastAPI 依賴的 context manager。"""

    @contextmanager
    def _apply(
        app: FastAPI,
        *,
        settings: AppSettings | None = None,
        correlation_id: str | None = None,
    ) -> Iterator[dict[Any, Callable[..., Any]]]:
        overrides: dict[Any, Callable[..., Any]] = {}

        if settings is not None:

            async def _override_settings() -> AppSettings:
                return settings

            overrides[get_app_settings] = _override_settings

        if correlation_id is not None:

            async def _override_correlation_id(request: Request) -> str:
                request.state.correlation_id = correlation_id
                return correlation_id

            overrides[get_correlation_id] = _override_correlation_id

        for dependency, override in overrides.items():
            app.dependency_overrides[dependency] = override

        try:
            yield overrides
        finally:
            for dependency in overrides:
                app.dependency_overrides.pop(dependency, None)

    return _apply


__all__ = [
    "stub_pubmed_success",
    "stub_qdrant_success",
    "app_dependency_overrides",
]
