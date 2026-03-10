"""工具封裝相關設定。"""

from __future__ import annotations

from dataclasses import dataclass
import os


def _split_env_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return tuple()
    return tuple(item.strip() for item in value.split(",") if item.strip())


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


@dataclass(slots=True)
class AppSettings:
    """Phase 5 FastAPI 應用程式設定。"""

    ui_base_path: str = "/ui"
    static_base_path: str = "/static"
    api_base_path: str = "/api"
    api_timeout_seconds: float = 120.0
    allowed_origins: tuple[str, ...] = (
        "http://localhost:3000",
        "http://localhost:8000",
    )
    enable_cors: bool = True

    @classmethod
    def from_env(cls) -> "AppSettings":
        base_defaults = cls()
        ui_base_path = os.getenv("APP_UI_BASE_PATH", base_defaults.ui_base_path)
        static_base_path = os.getenv(
            "APP_STATIC_BASE_PATH", base_defaults.static_base_path
        )
        api_base_path = os.getenv("APP_API_BASE_PATH", base_defaults.api_base_path)
        timeout_raw = os.getenv("APP_API_TIMEOUT_SECONDS")
        try:
            timeout = float(timeout_raw) if timeout_raw else cls.api_timeout_seconds
        except ValueError:
            timeout = cls.api_timeout_seconds
        allowed_origins_env = _split_env_list(os.getenv("APP_ALLOWED_ORIGINS"))
        allowed_origins = (
            allowed_origins_env
            if allowed_origins_env
            else base_defaults.allowed_origins
        )
        enable_cors_raw = os.getenv("APP_ENABLE_CORS")
        enable_cors = (
            enable_cors_raw.lower() in {"1", "true", "yes"}
            if enable_cors_raw
            else cls.enable_cors
        )
        return cls(
            ui_base_path=ui_base_path,
            static_base_path=static_base_path,
            api_base_path=api_base_path,
            api_timeout_seconds=timeout,
            allowed_origins=allowed_origins,
            enable_cors=enable_cors,
        )


__all__ = ["PubMedSettings", "QdrantSettings", "AppSettings"]
