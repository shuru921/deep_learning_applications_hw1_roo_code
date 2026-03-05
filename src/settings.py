"""工具封裝相關設定。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class PubMedSettings:
    api_key: str | None = None
    tool_name: str = "mars-pubmed-wrapper"
    email: str | None = None
    rate_requests: int = 3
    rate_period: float = 1.0
    rate_timeout: float | None = 2.0


@dataclass(slots=True)
class QdrantSettings:
    host: str = "localhost"
    port: int = 6333
    grpc_port: int | None = None
    use_ssl: bool = False
    collection_name: str = "medical_articles"
    vector_size: int = 1536
    distance: str = "COSINE"
    consistency: str = "medium"
    timeout: float = 10.0


__all__ = ["PubMedSettings", "QdrantSettings"]
