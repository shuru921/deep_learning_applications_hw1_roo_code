"""最小封裝連通性檢驗腳本。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import httpx

try:  # pragma: no cover - 測試環境可能缺少依賴
    from qdrant_client.async_qdrant_client import AsyncQdrantClient
except ModuleNotFoundError:  # pragma: no cover
    AsyncQdrantClient = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.clients.pubmed_wrapper import PubMedQuery, PubMedWrapper
from src.clients.qdrant_wrapper import CollectionConfig, QdrantWrapper
from src.settings import PubMedSettings, QdrantSettings
from src.utils import AsyncRateLimiter, RateLimitConfig


@asynccontextmanager
async def create_pubmed_wrapper() -> Any:
    settings = PubMedSettings(
        api_key=os.getenv("PUBMED_API_KEY"),
        tool_name=os.getenv("PUBMED_TOOL_NAME", "mars-test-suite"),
        email=os.getenv("PUBMED_EMAIL"),
        rate_requests=int(os.getenv("PUBMED_RATE_REQUESTS", "3")),
        rate_period=float(os.getenv("PUBMED_RATE_PERIOD", "1.0")),
        rate_timeout=float(os.getenv("PUBMED_RATE_TIMEOUT", "2.0")),
    )
    client = httpx.AsyncClient(timeout=10.0)
    rate_limiter = AsyncRateLimiter(
        RateLimitConfig(
            requests=settings.rate_requests,
            per_seconds=settings.rate_period,
            timeout=settings.rate_timeout,
        )
    )
    wrapper = PubMedWrapper(
        client,
        rate_limiter,
        api_key=settings.api_key,
        tool_name=settings.tool_name,
        email=settings.email,
    )
    try:
        await wrapper.warm_up()
        yield wrapper
    finally:
        await client.aclose()


@asynccontextmanager
async def create_qdrant_wrapper() -> Any:
    settings = QdrantSettings(
        host=os.getenv("QDRANT_HOST", "localhost"),
        port=int(os.getenv("QDRANT_PORT", "6333")),
        grpc_port=int(os.getenv("QDRANT_GRPC_PORT", "0")) or None,
        use_ssl=os.getenv("QDRANT_USE_SSL", "false").lower() == "true",
        collection_name=os.getenv("QDRANT_COLLECTION", "mars-test"),
        vector_size=int(os.getenv("QDRANT_VECTOR_SIZE", "8")),
        distance=os.getenv("QDRANT_DISTANCE", "COSINE"),
        consistency=os.getenv("QDRANT_CONSISTENCY", "medium"),
    )
    if AsyncQdrantClient is None:
        raise RuntimeError("qdrant-client 未安裝，無法進行連通性檢查")

    client = AsyncQdrantClient(
        host=settings.host,
        port=settings.port,
        grpc_port=settings.grpc_port,
        https=settings.use_ssl,
    )

    wrapper = QdrantWrapper(
        client,
        collection=settings.collection_name,
        vector_size=settings.vector_size,
        distance=settings.distance,
        max_batch_size=64,
        timeout=settings.timeout,
        consistency=settings.consistency,
    )

    try:
        await wrapper.ensure_collection(
            CollectionConfig(
                name=settings.collection_name,
                vector_size=settings.vector_size,
                distance=wrapper._distance,  # type: ignore[attr-defined]
            )
        )
        yield wrapper
    finally:
        with suppress(Exception):  # pragma: no cover - 測試保護
            close_async = getattr(client, "close_async", None)
            if close_async is not None:
                await close_async()


async def check_pubmed(wrapper: PubMedWrapper) -> dict[str, Any]:
    query = PubMedQuery(term="cancer", retmax=1)
    try:
        result = await wrapper.search(query)
        return {
            "ok": True,
            "count": result.count,
            "sample_ids": list(result.ids)[:5],
        }
    except Exception as exc:  # pragma: no cover - 測試失敗時捕捉
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}


async def check_qdrant(wrapper: QdrantWrapper) -> dict[str, Any]:
    try:
        health = await wrapper.healthcheck()
        return {
            "ok": True,
            "status": health.status,
            "detail": health.detail,
        }
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": type(exc).__name__, "detail": str(exc)}


async def main() -> None:
    results: dict[str, Any] = {}
    async with create_pubmed_wrapper() as pubmed:
        results["pubmed"] = await check_pubmed(pubmed)

    async with create_qdrant_wrapper() as qdrant:
        results["qdrant"] = await check_qdrant(qdrant)

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        pass
