"""前端用小工具。"""

from __future__ import annotations

from typing import Any


def pydantic_to_dict(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="python")  # type: ignore[call-arg]
    if hasattr(model, "dict"):
        return model.dict()  # type: ignore[call-arg]
    return dict(model)


__all__ = ["pydantic_to_dict"]
