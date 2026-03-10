"""前端用小工具。"""

from __future__ import annotations

from typing import Any

from ..orchestrator.schemas import LangGraphState


def pydantic_to_dict(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="python")  # type: ignore[call-arg]
    if hasattr(model, "dict"):
        return model.dict()  # type: ignore[call-arg]
    return dict(model)


def ensure_correlation_id(state: LangGraphState, correlation_id: str) -> str:
    """將 correlation ID 寫入遙測狀態，若既有則沿用。"""

    if not correlation_id:
        raise ValueError("correlation_id 不可為空字串")

    existing = getattr(state.telemetry, "correlation_id", None)
    if existing:
        return existing

    state.telemetry.correlation_id = correlation_id
    return correlation_id


__all__ = ["pydantic_to_dict", "ensure_correlation_id"]
