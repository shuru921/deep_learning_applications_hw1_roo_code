"""Qdrant 非同步封裝模組。"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence, TypeVar

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


_T = TypeVar("_T")


def _dig(obj: Any, *attrs: str) -> Any:
    current = obj
    for attr in attrs:
        if current is None:
            return None
        if isinstance(current, Mapping):
            current = current.get(attr)
        else:
            current = getattr(current, attr, None)
    return current


@dataclass(slots=True)
class CollectionConfig:
    name: str
    vector_size: int
    distance: rest_models.Distance = rest_models.Distance.COSINE
    shard_number: int | None = None
    replication_factor: int | None = None
    write_consistency_factor: int | None = None
    on_disk_payload: bool | None = None


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
        max_retries: int = 3,
        retry_backoff: tuple[float, float] = (0.5, 2.0),
        max_backoff: float | None = 5.0,
        jitter: tuple[float, float] = (0.5, 1.5),
        logger: Any | None = None,
    ) -> None:
        if max_batch_size <= 0:
            raise ValueError("max_batch_size 必須大於 0")
        if timeout <= 0:
            raise ValueError("timeout 必須大於 0")
        if consistency not in {"weak", "medium", "strong"}:
            raise ValueError("consistency 必須為 weak/medium/strong")
        if max_retries < 0:
            raise ValueError("max_retries 必須大於等於 0")
        if retry_backoff[0] <= 0 or retry_backoff[1] < 1:
            raise ValueError("retry_backoff 需為 (base_delay>0, multiplier>=1)")
        if max_backoff is not None and max_backoff <= 0:
            raise ValueError("max_backoff 若提供需大於 0")
        if jitter[0] <= 0 or jitter[0] > jitter[1]:
            raise ValueError("jitter 需滿足 0 < min <= max")

        self._client = client
        self._collection = collection
        self._vector_size = vector_size
        self._distance = rest_models.Distance(distance)
        self._payload_schema = payload_schema or {}
        self._max_batch_size = max_batch_size
        self._timeout = timeout
        self._consistency = rest_models.WriteConsistency(consistency)
        self._logger = logger or self._default_logger()
        self._max_retries = max_retries
        self._base_backoff = retry_backoff[0]
        self._backoff_multiplier = retry_backoff[1]
        self._max_backoff = max_backoff
        self._jitter_range = jitter

    async def ensure_collection(self, config: CollectionConfig | None = None) -> None:
        target = config or CollectionConfig(
            name=self._collection, vector_size=self._vector_size
        )

        async def _describe() -> Any:
            return await self._client.get_collection(target.name)

        metrics: dict[str, Any] = {"operation": "describe"}
        try:
            existing = await self._run_with_retry(
                _describe,
                operation="describe",
                payload={"collection": target.name},
                metrics=metrics,
            )
            self._validate_collection(existing, target)
        except QdrantClientException as exc:
            if getattr(exc, "status_code", None) == 404:
                metrics_retry: dict[str, Any] = {"operation": "create_collection"}
                await self._run_with_retry(
                    lambda: self._create_collection(target),
                    operation="create_collection",
                    payload={
                        "collection": target.name,
                        "vector_size": target.vector_size,
                    },
                    metrics=metrics_retry,
                )
                self._log_event(
                    "qdrant.ensure_collection",
                    {
                        "collection": target.name,
                        "action": "created",
                        **_normalize_metrics(metrics_retry),
                    },
                )
            else:
                raise self._map_exception(
                    exc, "describe", collection=target.name
                ) from exc
        else:
            self._log_event(
                "qdrant.ensure_collection",
                {
                    "collection": target.name,
                    "action": "validated",
                    **_normalize_metrics(metrics),
                },
            )

    async def upsert(self, records: Sequence[QdrantRecord]) -> QdrantUpsertResult:
        if not records:
            return QdrantUpsertResult(processed=0)

        total = 0
        failures: list[Mapping[str, Any]] = []
        start = time.perf_counter()
        total_retry = 0
        retry_events: list[Any] = []
        backoff_schedule: list[Any] = []

        for chunk in self._chunk_records(records):
            payload = [
                rest_models.PointStruct(
                    id=record.id,
                    vector=self._validate_vector(record.vector),
                    payload=self._validate_payload(record.payload),
                )
                for record in chunk
            ]
            chunk_metrics: dict[str, Any] = {
                "operation": "upsert",
                "batch_size": len(chunk),
            }
            try:
                await self._run_with_retry(
                    lambda: self._client.upsert(
                        collection_name=self._collection,
                        wait=True,
                        points=payload,
                        write_consistency=self._consistency,
                    ),
                    operation="upsert",
                    payload={
                        "collection": self._collection,
                        "batch_size": len(chunk),
                    },
                    metrics=chunk_metrics,
                )
                total += len(chunk)
            except asyncio.TimeoutError as exc:
                raise QdrantTimeoutError(
                    "Qdrant upsert 逾時",
                    collection=self._collection,
                    detail={"processed": total},
                    cause=exc,
                ) from exc
            except QdrantError as error:
                failures.extend(
                    {
                        "id": record.id,
                        "error": type(error).__name__,
                        "detail": error.to_payload(),
                    }
                    for record in chunk
                )
            finally:
                total_retry += chunk_metrics.get("retry_count", 0)
                retry_events.extend(chunk_metrics.get("retry_events", ()))
                backoff_schedule.extend(chunk_metrics.get("backoff_schedule", ()))

        took = time.perf_counter() - start
        metrics = {
            "latency": took,
            "failed": len(failures),
            "retry_count": total_retry,
            "retry_events": tuple(retry_events),
            "backoff_schedule": tuple(backoff_schedule),
        }
        self._log_event(
            "qdrant.upsert",
            {
                "collection": self._collection,
                "processed": total,
                "failed": len(failures),
                "took_ms": took * 1000,
                "retry_count": total_retry,
            },
        )
        return QdrantUpsertResult(
            processed=total,
            failed=tuple(failures),
            metrics=metrics,
        )

    async def query(self, request: QdrantQuery) -> QdrantQueryResult:
        start = time.perf_counter()
        metrics: dict[str, Any] = {
            "operation": "query",
            "limit": request.limit,
        }
        try:
            result = await self._run_with_retry(
                lambda: self._client.search(
                    collection_name=self._collection,
                    query_vector=request.vector,
                    query_filter=request.filter,
                    limit=request.limit,
                    with_payload=request.with_payload,
                    with_vectors=request.with_vectors,
                    score_threshold=request.score_threshold,
                ),
                operation="query",
                payload={
                    "collection": self._collection,
                    "limit": request.limit,
                },
                metrics=metrics,
            )
        except asyncio.TimeoutError as exc:
            raise QdrantTimeoutError(
                "Qdrant 查詢逾時",
                collection=self._collection,
                cause=exc,
            ) from exc
        except QdrantError as exc:
            raise exc

        took = time.perf_counter() - start
        normalized_metrics = _normalize_metrics(metrics)
        result_metrics = {"latency": took, **normalized_metrics}
        result_metrics.setdefault("warnings", tuple())
        self._log_event(
            "qdrant.query",
            {
                "collection": self._collection,
                "returned": len(result),
                "took_ms": took * 1000,
                "retry_count": normalized_metrics.get("retry_count", 0),
            },
        )
        return QdrantQueryResult(
            points=tuple(result),
            metrics=result_metrics,
        )

    async def delete(self, point_ids: Sequence[str]) -> QdrantDeleteResult:
        if not point_ids:
            return QdrantDeleteResult(deleted=0)

        start = time.perf_counter()
        metrics: dict[str, Any] = {
            "operation": "delete",
            "count": len(point_ids),
        }
        try:
            response = await self._run_with_retry(
                lambda: self._client.delete(
                    collection_name=self._collection,
                    points_selector=rest_models.PointIdsList(points=list(point_ids)),
                ),
                operation="delete",
                payload={
                    "collection": self._collection,
                    "count": len(point_ids),
                },
                metrics=metrics,
            )
        except asyncio.TimeoutError as exc:
            raise QdrantTimeoutError(
                "Qdrant 刪除逾時",
                collection=self._collection,
                cause=exc,
            ) from exc
        except QdrantError as exc:
            raise exc

        took = time.perf_counter() - start
        deleted = getattr(getattr(response, "result", None), "count", len(point_ids))
        result_metrics = {"latency": took, **_normalize_metrics(metrics)}
        result_metrics.setdefault("retry_count", 0)
        return QdrantDeleteResult(deleted=deleted, metrics=result_metrics)

    async def healthcheck(self) -> QdrantHealthStatus:
        start = time.perf_counter()
        metrics: dict[str, Any] = {"operation": "healthcheck"}
        try:
            info = await self._run_with_retry(
                lambda: self._client.get_cluster_info(),
                operation="healthcheck",
                payload={"collection": self._collection},
                metrics=metrics,
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
                    "retry_count": metrics.get("retry_count", 0),
                },
            )
            return QdrantHealthStatus(
                status=status,
                detail=detail,
                metrics={"latency": took, **_normalize_metrics(metrics)},
            )
        except asyncio.TimeoutError as exc:
            raise QdrantTimeoutError(
                "Qdrant 健康檢查逾時",
                collection=self._collection,
                cause=exc,
            ) from exc
        except QdrantError as exc:
            raise exc

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

    async def _run_with_retry(
        self,
        func: Callable[[], Awaitable[_T]],
        *,
        operation: str,
        payload: Mapping[str, Any],
        metrics: dict[str, Any] | None = None,
    ) -> _T:
        last_exc: Exception | None = None
        metrics = metrics if metrics is not None else {}
        retry_events: list[Any] = []
        backoff_schedule: list[Any] = []
        for attempt in range(1, self._max_retries + 2):
            try:
                result = await asyncio.wait_for(func(), timeout=self._timeout)
                metrics["retry_events"] = tuple(retry_events)
                metrics["backoff_schedule"] = tuple(backoff_schedule)
                metrics["retry_count"] = len(retry_events)
                return result
            except asyncio.TimeoutError:
                raise
            except QdrantClientException as exc:
                last_exc = exc
                mapped = self._map_exception(exc, operation)
                retryable = (
                    isinstance(mapped, QdrantConnectivityError)
                    or (
                        isinstance(mapped, QdrantSchemaError)
                        and getattr(exc, "status_code", None) == 404
                    )
                    or (
                        isinstance(mapped, QdrantError)
                        and getattr(exc, "status_code", None) in {408, 425}
                    )
                )
                retry_events.append(
                    {
                        "attempt": attempt,
                        "status": getattr(exc, "status_code", None),
                        "retryable": retryable,
                        "error": type(mapped).__name__,
                    }
                )
                if not retryable or attempt > self._max_retries:
                    raise mapped
            except Exception as exc:  # pragma: no cover - 非預期錯誤
                last_exc = exc
                retry_events.append(
                    {
                        "attempt": attempt,
                        "error": type(exc).__name__,
                        "retryable": False,
                    }
                )
                if attempt > self._max_retries:
                    raise QdrantError(
                        "Qdrant 操作失敗",
                        operation=operation,
                        detail=dict(payload),
                        cause=exc,
                    )

            delay = self._compute_backoff(attempt)
            backoff_schedule.append({"attempt": attempt, "delay": delay})
            await asyncio.sleep(delay)

        metrics["retry_events"] = tuple(retry_events)
        metrics["backoff_schedule"] = tuple(backoff_schedule)
        metrics["retry_count"] = len(retry_events)
        assert last_exc is not None
        raise QdrantError(
            "Qdrant 操作失敗",
            operation=operation,
            detail=dict(payload),
            cause=last_exc,
        )

    def _compute_backoff(self, attempt: int) -> float:
        delay = self._base_backoff * (self._backoff_multiplier ** (attempt - 1))
        if self._max_backoff is not None:
            delay = min(delay, self._max_backoff)
        jitter_min, jitter_max = self._jitter_range
        return max(delay * random.uniform(jitter_min, jitter_max), 0.0)

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

    def _validate_collection(self, existing: Any, expected: CollectionConfig) -> None:
        vectors_config = _dig(existing, "config", "params", "vectors")
        actual_vector_size = getattr(vectors_config, "size", None)
        actual_distance = getattr(vectors_config, "distance", None)
        if actual_vector_size != expected.vector_size:
            raise QdrantSchemaError(
                "向量維度不匹配",
                collection=expected.name,
                detail={"expected": expected.vector_size, "actual": actual_vector_size},
            )
        if (
            actual_distance
            and str(actual_distance).lower() != str(expected.distance).lower()
        ):
            raise QdrantSchemaError(
                "距離量測不匹配",
                collection=expected.name,
                detail={
                    "expected": str(expected.distance).lower(),
                    "actual": str(actual_distance).lower(),
                },
            )
        expected_replication = expected.replication_factor
        actual_replication = _dig(existing, "config", "params", "replication_factor")
        if (
            expected_replication is not None
            and actual_replication is not None
            and actual_replication != expected_replication
        ):
            raise QdrantSchemaError(
                "replication_factor 不匹配",
                collection=expected.name,
                detail={
                    "expected": expected_replication,
                    "actual": actual_replication,
                },
            )
        expected_shards = expected.shard_number
        actual_shards = _dig(existing, "config", "params", "shard_number")
        if (
            expected_shards is not None
            and actual_shards is not None
            and actual_shards != expected_shards
        ):
            raise QdrantSchemaError(
                "shard_number 不匹配",
                collection=expected.name,
                detail={"expected": expected_shards, "actual": actual_shards},
            )
        expected_on_disk = expected.on_disk_payload
        actual_on_disk = _dig(existing, "config", "params", "on_disk_payload")
        if (
            expected_on_disk is not None
            and actual_on_disk is not None
            and bool(actual_on_disk) != bool(expected_on_disk)
        ):
            raise QdrantSchemaError(
                "on_disk_payload 不匹配",
                collection=expected.name,
                detail={
                    "expected": bool(expected_on_disk),
                    "actual": bool(actual_on_disk),
                },
            )

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


def _normalize_metrics(metrics: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, list):
            normalized[key] = tuple(value)
        else:
            normalized[key] = value
    return normalized
