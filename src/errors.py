"""共用錯誤層，提供工具封裝專用例外類別。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, MutableMapping


@dataclass(slots=True)
class ToolingError(Exception):
    """封裝層錯誤基底類別，提供標準化結構供上層捕捉。"""

    message: str
    detail: Mapping[str, Any] | None = None
    cause: Exception | None = None
    error_code: str = "tooling_error"
    fallback_hint: str | None = None
    status_code: int = 500

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_payload(self) -> MutableMapping[str, Any]:
        payload: MutableMapping[str, Any] = {"message": self.message}
        if self.detail:
            payload["detail"] = dict(self.detail)
        if self.cause:
            payload["cause"] = {
                "type": type(self.cause).__name__,
                "message": str(self.cause),
            }
        if self.error_code:
            payload["error_code"] = self.error_code
        if self.fallback_hint:
            payload["fallback"] = self.fallback_hint
        payload["status_code"] = self.status_code
        return payload


@dataclass(slots=True)
class RateLimitError(ToolingError):
    """Rate limit 行為相關錯誤。"""

    waited: float | None = None
    limit: int | None = None
    per_seconds: float | None = None


@dataclass(slots=True)
class RateLimitTimeoutError(RateLimitError):
    """Rate limiter 等待超時。"""


@dataclass(slots=True)
class PubMedError(ToolingError):
    request_id: str | None = None
    status_code: int | None = None
    error_code: str = "pubmed_error"


@dataclass(slots=True)
class PubMedRateLimitError(PubMedError):
    pass


@dataclass(slots=True)
class PubMedHTTPError(PubMedError):
    pass


@dataclass(slots=True)
class PubMedParseError(PubMedError):
    pass


@dataclass(slots=True)
class PubMedEmptyResult(PubMedError):
    pass


@dataclass(slots=True)
class QdrantError(ToolingError):
    operation: str | None = None
    collection: str | None = None
    error_code: str = "qdrant_error"


@dataclass(slots=True)
class QdrantConnectivityError(QdrantError):
    pass


@dataclass(slots=True)
class QdrantSchemaError(QdrantError):
    pass


@dataclass(slots=True)
class QdrantConsistencyError(QdrantError):
    pass


@dataclass(slots=True)
class QdrantTimeoutError(QdrantError):
    pass
