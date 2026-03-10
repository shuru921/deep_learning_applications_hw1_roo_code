"""共用測試 fixture。"""

from __future__ import annotations

import pytest

from tests.test_orchestrator import StubPubMedSuccess, StubQdrantSuccess


@pytest.fixture()
def stub_pubmed_success() -> StubPubMedSuccess:
    """建立新的 PubMed 成功 stub。"""

    return StubPubMedSuccess()


@pytest.fixture()
def stub_qdrant_success() -> StubQdrantSuccess:
    """建立新的 Qdrant 成功 stub。"""

    return StubQdrantSuccess()


__all__ = ["stub_pubmed_success", "stub_qdrant_success"]
