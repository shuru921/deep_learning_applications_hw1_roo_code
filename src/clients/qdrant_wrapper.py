"""Qdrant 非同步封裝模組。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable, Mapping, Sequence

try:  # pragma: no cover - 測試環境可能未安裝 qdrant-client
    from qdrant_client.async_qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as rest_models
    from qdrant_client.http.exceptions import QdrantClientException
except ModuleNotFoundError:  # pragma: no cover

    class AsyncQdrantClient:  # type: ignore[empty-body]
        ...

    class QdrantClientException(Exception):
        def __init__(self, message: str, status_code: int | None = None) -> None:
            super().__init__(message)
            self.status_code = status_code

    @dataclass(slots=True)
    class _VectorParams:
        size: int
        distance: str

    @dataclass(slots=True)
    class _PointStruct:
        id: str
        vector: Sequence[float]
        payload: Mapping[str, Any] | None = None

    @dataclass(slots=True)
    class _PointIdsList:
        points: Sequence[str]

    class _Distance(str):
        COSINE = "cosine"

        def __new__(cls, value: str) -> "_Distance":
            return str.__new__(cls, value.lower())

    rest_models = SimpleNamespace(  # type: ignore[assignment]
        Distance=_Distance,
        VectorParams=_VectorParams,
        PointStruct=_PointStruct,
        PointIdsList=_PointIdsList,
        Filter=object,
        PayloadSelector=object,
        WithVectors=object,
        ScoredPoint=object,
        WriteConsistency=lambda value: value,
    )

try:
    import structlog
except ModuleNotFoundError:  # pragma: no cover
    structlog = None  # type: ignore[assignment]

from ..errors import (
    QdrantConnectivityError,
    QdrantConsistencyError,
    QdrantError,
    QdrantSchemaError,
    QdrantTimeoutError,
)


@dataclass(slots=True)
class CollectionConfig:
    name: str
    vector_size: int
    distance: rest_models.Distance = rest_models.Distance.COSINE
    shard_number: int | None = None
    replication_factor: int | None = None
    write_consistency_factor: int | None = None
    on_disk_payload: bool = False


@dataclass(slots=True)
class QdrantRecord:
    id: str
    vector: Sequence[float]
    payload: Mapping[str, Any] | None = None


@dataclass(slots=True)
class QdrantUpsertResult:
    processed: int
    failed: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class QdrantQuery:
    vector: Sequence[float] | None = None
    filter: rest_models.Filter | None = None
    limit: int = 10
    with_payload: bool | rest_models.PayloadSelector = True
    with_vectors: bool | rest_models.WithVectors = False
    score_threshold: float | None = None


@dataclass(slots=True)
class QdrantQueryResult:
    points: Sequence[rest_models.ScoredPoint]
    metrics: Mapping[str, Any] = field(default_factory=dict)
    warnings: Sequence[str] = field(default_factory=tuple)


@dataclass(slots=True)
class QdrantDeleteResult:
    deleted: int
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class QdrantHealthStatus:
    status: str
    detail: Mapping[str, Any] | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)


class QdrantWrapper:
    """對 Qdrant 提供防禦性非同步操作。"""

    def __init__(
        self,
        client: AsyncQdrantClient,
        *,
        collection: str,
        vector_size: int,
        distance: str,
        payload_schema: Mapping[str, type] | None = None,
        max_batch_size: int = 128,
        timeout: float = 10.0,
        consistency: str = "medium",
        logger: Any | None = None,
    ) -> None:
        if max_batch_size <= 0:
            raise ValueError("max_batch_size 必須大於 0")
        if timeout <= 0:
            raise ValueError("timeout 必須大於 0")
        if consistency not in {"weak", "medium", "strong"}:
            raise ValueError("consistency 必須為 weak/medium/strong")

        self._client = client
        self._collection = collection
        self._vector_size = vector_size
        self._distance = rest_models.Distance(distance)
        self._payload_schema = payload_schema or {}
        self._max_batch_size = max_batch_size
        self._timeout = timeout
        self._consistency = rest_models.WriteConsistency(consistency)
        self._logger = logger or self._default_logger()

    async def ensure_collection(self, config: CollectionConfig | None = None) -> None:
        target = config or CollectionConfig(
            name=self._collection, vector_size=self._vector_size
        )
        try:
            existing = await self._client.get_collection(target.name)
            vectors_config = existing.config.params.vectors
            actual_vector_size = getattr(vectors_config, "size", None)
            if actual_vector_size != target.vector_size:
                raise QdrantSchemaError(
                    "向量維度不匹配",
                    detail={
                        "expected": target.vector_size,
                        "actual": actual_vector_size,
                    },
                    collection=target.name,
                )
        except QdrantClientException as exc:
            if getattr(exc, "status_code", None) == 404:
                await self._create_collection(target)
            else:
                raise self._map_exception(
                    exc, "describe", collection=target.name
                ) from exc

    async def upsert(self, records: Sequence[QdrantRecord]) -> QdrantUpsertResult:
        if not records:
            return QdrantUpsertResult(processed=0)

        total = 0
        failures: list[Mapping[str, Any]] = []
        start = time.perf_counter()

        for chunk in self._chunk_records(records):
            try:
                await asyncio.wait_for(
                    self._client.upsert(
                        collection_name=self._collection,
                        wait=True,
                        points=[
                            rest_models.PointStruct(
                                id=record.id,
                                vector=self._validate_vector(record.vector),
                                payload=self._validate_payload(record.payload),
                            )
                            for record in chunk
                        ],
                        write_consistency=self._consistency,
                    ),
                    timeout=self._timeout,
                )
                total += len(chunk)
            except asyncio.TimeoutError as exc:
                raise QdrantTimeoutError(
                    "Qdrant upsert 逾時",
                    collection=self._collection,
                    detail={"processed": total},
                    cause=exc,
                ) from exc
            except QdrantClientException as exc:
                error = self._map_exception(exc, "upsert")
                failures.extend(
                    {
                        "id": record.id,
                        "error": type(error).__name__,
                        "detail": error.to_payload(),
                    }
                    for record in chunk
                )

        took = time.perf_counter() - start
        self._log_event(
            "qdrant.upsert",
            {
                "collection": self._collection,
                "processed": total,
                "failed": len(failures),
                "took_ms": took * 1000,
            },
        )
        return QdrantUpsertResult(
            processed=total,
            failed=tuple(failures),
            metrics={"latency": took, "failed": len(failures)},
        )

    async def query(self, request: QdrantQuery) -> QdrantQueryResult:
        start = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                self._client.search(
                    collection_name=self._collection,
                    query_vector=request.vector,
                    query_filter=request.filter,
                    limit=request.limit,
                    with_payload=request.with_payload,
                    with_vectors=request.with_vectors,
                    score_threshold=request.score_threshold,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise QdrantTimeoutError(
                "Qdrant 查詢逾時",
                collection=self._collection,
                cause=exc,
            ) from exc
        except QdrantClientException as exc:
            raise self._map_exception(exc, "query") from exc

        took = time.perf_counter() - start
        self._log_event(
            "qdrant.query",
            {
                "collection": self._collection,
                "returned": len(result),
                "took_ms": took * 1000,
            },
        )
        return QdrantQueryResult(points=tuple(result), metrics={"latency": took})

    async def delete(self, point_ids: Sequence[str]) -> QdrantDeleteResult:
        if not point_ids:
            return QdrantDeleteResult(deleted=0)

        start = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self._client.delete(
                    collection_name=self._collection,
                    points_selector=rest_models.PointIdsList(points=list(point_ids)),
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise QdrantTimeoutError(
                "Qdrant 刪除逾時",
                collection=self._collection,
                cause=exc,
            ) from exc
        except QdrantClientException as exc:
            raise self._map_exception(exc, "delete") from exc

        took = time.perf_counter() - start
        deleted = getattr(getattr(response, "result", None), "count", len(point_ids))
        return QdrantDeleteResult(deleted=deleted, metrics={"latency": took})

    async def healthcheck(self) -> QdrantHealthStatus:
        start = time.perf_counter()
        try:
            info = await asyncio.wait_for(
                self._client.get_cluster_info(), timeout=self._timeout
            )
            took = time.perf_counter() - start
            status = (
                info.status.value if hasattr(info.status, "value") else str(info.status)
            )
            detail = {
                "peers": [peer.id for peer in getattr(info, "peers", [])],
                "leader": getattr(info, "leader", None),
            }
            self._log_event(
                "qdrant.healthcheck",
                {
                    "collection": self._collection,
                    "status": status,
                    "took_ms": took * 1000,
                },
            )
            return QdrantHealthStatus(
                status=status, detail=detail, metrics={"latency": took}
            )
        except asyncio.TimeoutError as exc:
            raise QdrantTimeoutError(
                "Qdrant 健康檢查逾時",
                collection=self._collection,
                cause=exc,
            ) from exc
        except QdrantClientException as exc:
            raise self._map_exception(exc, "healthcheck") from exc

    def _validate_vector(self, vector: Sequence[float]) -> Sequence[float]:
        if len(vector) != self._vector_size:
            raise QdrantSchemaError(
                "向量維度不符",
                collection=self._collection,
                detail={"expected": self._vector_size, "actual": len(vector)},
            )
        return vector

    def _validate_payload(
        self, payload: Mapping[str, Any] | None
    ) -> Mapping[str, Any] | None:
        if payload is None:
            return None
        for key, expected in self._payload_schema.items():
            if key not in payload:
                raise QdrantSchemaError(
                    "缺少 payload 欄位",
                    collection=self._collection,
                    detail={"field": key},
                )
            if not isinstance(payload[key], expected):
                raise QdrantSchemaError(
                    "payload 型別不符",
                    collection=self._collection,
                    detail={"field": key, "expected": expected.__name__},
                )
        return payload

    def _chunk_records(
        self, records: Sequence[QdrantRecord]
    ) -> Iterable[Sequence[QdrantRecord]]:
        for i in range(0, len(records), self._max_batch_size):
            yield records[i : i + self._max_batch_size]

    async def _create_collection(self, config: CollectionConfig) -> None:
        try:
            await self._client.create_collection(
                collection_name=config.name,
                vectors_config=rest_models.VectorParams(
                    size=config.vector_size,
                    distance=config.distance,
                ),
                on_disk_payload=config.on_disk_payload,
                shard_number=config.shard_number,
                replication_factor=config.replication_factor,
                write_consistency_factor=config.write_consistency_factor,
            )
            self._log_event(
                "qdrant.create_collection",
                {
                    "collection": config.name,
                    "vector_size": config.vector_size,
                    "distance": getattr(config.distance, "value", config.distance),
                },
            )
        except QdrantClientException as exc:
            raise self._map_exception(
                exc, "create_collection", collection=config.name
            ) from exc

    def _map_exception(
        self,
        exc: QdrantClientException,
        operation: str,
        *,
        collection: str | None = None,
    ) -> QdrantError:
        collection_name = collection or self._collection
        status = getattr(exc, "status_code", None)
        if status in {400, 401, 403, 404, 422}:
            return QdrantSchemaError(
                "Qdrant schema 驗證錯誤",
                collection=collection_name,
                detail={"status": status, "operation": operation},
                cause=exc,
            )
        if status in {409}:
            return QdrantConsistencyError(
                "Qdrant consistency 失敗",
                collection=collection_name,
                detail={"status": status, "operation": operation},
                cause=exc,
            )
        if status in {500, 502, 503, 504}:
            return QdrantConnectivityError(
                "Qdrant 服務不可用",
                collection=collection_name,
                detail={"status": status, "operation": operation},
                cause=exc,
            )
        return QdrantError(
            "Qdrant 操作失敗",
            collection=collection_name,
            detail={"status": status, "operation": operation},
            cause=exc,
        )

    def _log_event(self, event: str, payload: Mapping[str, Any]) -> None:
        if hasattr(self._logger, "bind"):
            self._logger.bind(tool="qdrant").info(event, **payload)
        else:  # pragma: no cover
            self._logger.info("%s %s", event, payload)

    def _default_logger(self) -> Any:
        if structlog is not None:
            return structlog.get_logger("qdrant.wrapper")
        import logging

        logger = logging.getLogger("qdrant.wrapper")
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger


__all__ = [
    "QdrantWrapper",
    "CollectionConfig",
    "QdrantRecord",
    "QdrantUpsertResult",
    "QdrantQuery",
    "QdrantQueryResult",
    "QdrantDeleteResult",
    "QdrantHealthStatus",
]
