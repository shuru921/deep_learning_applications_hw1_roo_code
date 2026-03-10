#!/usr/bin/env bash
#
# Phase 5.4 CI 驗證腳本。
#
# 使用方式：
#   bash scripts/run_ci_checks.sh
#
# 功能：
#   1. 建立虛擬環境（若不存在）並安裝專案依賴。
#   2. 執行 linters（目前以 ruff/pytest 為例，如未設定可依 TODO 調整）。
#   3. 執行 pytest 全套測試（含 E2E 測試）。
#   4. 驗證 FastAPI 模板載入。
#   5. Telemetry/Correlation ID 煙霧測試。

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
VENV_PATH="${PROJECT_ROOT}/.venv"

cd "${PROJECT_ROOT}"

python --version >/dev/null 2>&1 || {
  echo "[ERROR] Python 未安裝或不在 PATH" >&2
  exit 1
}

if [ ! -d "${VENV_PATH}" ]; then
  python -m venv "${VENV_PATH}"
fi

# shellcheck source=/dev/null
source "${VENV_PATH}/bin/activate"

python -m pip install --upgrade pip

if [ -f "requirements.txt" ]; then
  python -m pip install -r requirements.txt
elif [ -f "pyproject.toml" ]; then
  python -m pip install -e .
else
  echo "[WARN] 未找到 requirements.txt 或 pyproject.toml，使用預設依賴" >&2
  python -m pip install fastapi uvicorn[standard] httpx pytest ruff python-multipart jinja2
fi

python -m pip install pytest-asyncio pytest-anyio >/dev/null 2>&1 || {
  echo "[WARN] 安裝 pytest 非同步外掛失敗，請檢查環境" >&2
}

echo "[INFO] Running linters..."
if command -v ruff >/dev/null 2>&1; then
  ruff check src tests || {
    echo "[WARN] ruff 檢查失敗，請修正 linter 問題" >&2
    exit 1
  }
else
  echo "[TODO] 尚未設定 ruff，請依專案風格導入適當 linter" >&2
fi

echo "[INFO] Running pytest..."
pytest

echo "[INFO] FastAPI 模板載入煙霧測試..."
python - <<'PYCODE'
from fastapi import Request

from src.app import create_app


app = create_app()
request = Request(scope={"type": "http", "headers": []})
template_response = app.state.templates.TemplateResponse(
    "index.html",
    {"request": request, "ui_post_endpoint": "/ui/query", "static_base": "/static"},
)

assert template_response.status_code == 200
print("[OK] FastAPI 模板載入成功")
PYCODE

echo "[INFO] Telemetry correlation ID 煙霧測試..."
python - <<'PYCODE'
import asyncio

from fastapi import Request

from src.app.deps import get_correlation_id


scope = {
    "type": "http",
    "headers": [(b"x-correlation-id", b"test-corr-id")],
    "method": "GET",
    "path": "/",
    "raw_path": b"/",
    "query_string": b"",
    "client": ("testclient", 5000),
    "server": ("testserver", 80),
    "scheme": "http",
    "http_version": "1.1",
}
async def _receive() -> dict[str, object]:
    return {"type": "http.request", "body": b"", "more_body": False}


request = Request(scope=scope, receive=_receive)

correlation_id = asyncio.run(get_correlation_id(request))
assert correlation_id == "test-corr-id"
print("[OK] correlation ID smoke test 通過")
PYCODE

echo "[INFO] CI checks 完成"
