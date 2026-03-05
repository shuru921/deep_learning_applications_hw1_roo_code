"""封裝層行為測試：涵蓋 rate limit 與重試機制。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.clients.pubmed_wrapper import PubMedQuery, PubMedWrapper
from src.clients import qdrant_wrapper as qw
from src.clients.qdrant_wrapper import CollectionConfig, QdrantWrapper
from src.errors import (
    PubMedRateLimitError,
    QdrantConnectivityError,
    QdrantSchemaError,
    RateLimitTimeoutError,
)


class DummyAsyncClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses

    async def get(self, url: str, params: Any = None) -> httpx.Response:
        if not self._responses:
            raise AssertionError("No more responses configured")
        return self._responses.pop(0)


class DummyLimiter:
    def __init__(
        self, waits: list[float] | None = None, *, raise_timeout: bool = False
    ):
        self._waits = waits or [0.0]
        self._raise_timeout = raise_timeout
        self._index = 0

    @asynccontextmanager
    async def throttle(self):
        if self._raise_timeout:
            raise RateLimitTimeoutError("timeout", waited=1.0)
        waited = self._waits[self._index] if self._index < len(self._waits) else 0.0
        self._index += 1
        yield waited


def _make_json_response(
    payload: dict[str, Any], *, request_id: str = "req-1"
) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.test"),
        content=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "x-request-id": request_id},
    )


def _make_xml_response(content: str, *, request_id: str = "req-xml") -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.test"),
        content=content.encode("utf-8"),
        headers={"content-type": "application/xml", "x-request-id": request_id},
    )


@pytest.mark.asyncio
async def test_pubmed_search_metrics_capture() -> None:
    payload = {
        "esearchresult": {
            "idlist": ["1"],
            "count": "1",
        }
    }
    client = DummyAsyncClient([_make_json_response(payload)])
    limiter = DummyLimiter([0.25])
    wrapper = PubMedWrapper(
        client,
        limiter,  # type: ignore[arg-type]
        tool_name="unit-test",
        email="unit@test",
    )

    result = await wrapper.search(PubMedQuery(term="cancer"))

    assert result.metrics["retry_count"] == 0
    assert result.metrics["rate_limit_wait"] == pytest.approx(0.25)
    assert result.metrics["request_id"] == "req-1"


@pytest.mark.asyncio
async def test_pubmed_rate_limit_timeout_translated() -> None:
    client = DummyAsyncClient([])
    limiter = DummyLimiter(raise_timeout=True)
    wrapper = PubMedWrapper(client, limiter)  # type: ignore[arg-type]

    with pytest.raises(PubMedRateLimitError):
        await wrapper.search(PubMedQuery(term="timeout"))


@pytest.mark.asyncio
async def test_pubmed_fetch_details_handles_xml() -> None:
    xml_content = """
    <PubmedArticleSet>
        <PubmedArticle>
            <MedlineCitation>
                <PMID>123</PMID>
                <Article>
                    <ArticleTitle>Sample</ArticleTitle>
                    <Abstract><AbstractText>Body</AbstractText></Abstract>
                    <Journal><Title>Journal</Title></Journal>
                    <Journal><JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>
                </Article>
            </MedlineCitation>
        </PubmedArticle>
    </PubmedArticleSet>
    """
    client = DummyAsyncClient(
        [
            _make_xml_response(xml_content),
            _make_json_response({"result": {"123": {"title": "S", "authors": []}}}),
        ]
    )
    limiter = DummyLimiter([0.0, 0.0])
    wrapper = PubMedWrapper(client, limiter)  # type: ignore[arg-type]

    batch = await wrapper.fetch_details(["123"])
    assert batch.metrics["retry_count"] == 0
    summaries = await wrapper.fetch_summaries(["123"])
    assert summaries[0].metrics["retry_count"] == 0


class DummyQdrantClient:
    def __init__(self, *, fail_then_succeed: bool = False, always_fail: bool = False):
        self.fail_then_succeed = fail_then_succeed
        self.always_fail = always_fail
        self.search_calls = 0
        self.delete_calls = 0
        self.get_collection_calls = 0

    async def get_collection(self, name: str) -> Any:
        self.get_collection_calls += 1
        if self.fail_then_succeed and self.get_collection_calls == 1:
            raise qw.QdrantClientException("missing", status_code=404)
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=SimpleNamespace(size=128, distance="cosine"),
                    replication_factor=2,
                    shard_number=3,
                    on_disk_payload=True,
                )
            )
        )

    async def create_collection(self, **_: Any) -> None:
        return None

    async def search(self, **_: Any) -> Any:
        self.search_calls += 1
        if self.always_fail or (self.fail_then_succeed and self.search_calls == 1):
            raise qw.QdrantClientException("down", status_code=503)
        return (SimpleNamespace(id="p1", score=0.99),)

    async def delete(self, **_: Any) -> Any:
        self.delete_calls += 1
        if self.always_fail:
            raise qw.QdrantClientException("down", status_code=503)
        return SimpleNamespace(result=SimpleNamespace(count=1))

    async def get_cluster_info(self) -> Any:
        if self.always_fail:
            raise qw.QdrantClientException("down", status_code=503)
        return SimpleNamespace(
            status=SimpleNamespace(value="green"),
            peers=[SimpleNamespace(id="node-1")],
            leader="node-1",
        )


@pytest.mark.asyncio
async def test_qdrant_retry_and_metrics_capture() -> None:
    client = DummyQdrantClient(fail_then_succeed=True)
    wrapper = QdrantWrapper(
        client,
        collection="test",
        vector_size=128,
        distance="COSINE",
        max_retries=2,
        retry_backoff=(0.01, 1.0),
        max_backoff=0.02,
    )

    await wrapper.ensure_collection()
    result = await wrapper.query(qw.QdrantQuery(vector=[0.0] * 128))

    assert result.metrics["retry_count"] == 1
    assert wrapper._client.search_calls == 2


@pytest.mark.asyncio
async def test_qdrant_connectivity_error_after_retries() -> None:
    client = DummyQdrantClient(always_fail=True)
    wrapper = QdrantWrapper(
        client,
        collection="fail",
        vector_size=128,
        distance="COSINE",
        max_retries=1,
        retry_backoff=(0.01, 1.0),
        max_backoff=0.02,
    )

    with pytest.raises(QdrantConnectivityError):
        await wrapper.query(qw.QdrantQuery(vector=[0.0] * 128))


@pytest.mark.asyncio
async def test_qdrant_schema_validation() -> None:
    class SchemaClient(DummyQdrantClient):
        async def get_collection(self, name: str) -> Any:  # type: ignore[override]
            return SimpleNamespace(
                config=SimpleNamespace(
                    params=SimpleNamespace(
                        vectors=SimpleNamespace(size=64, distance="dot"),
                        replication_factor=1,
                        shard_number=1,
                        on_disk_payload=False,
                    )
                )
            )

    wrapper = QdrantWrapper(
        SchemaClient(),
        collection="schema",
        vector_size=128,
        distance="COSINE",
    )

    with pytest.raises(QdrantSchemaError):
        await wrapper.ensure_collection(
            CollectionConfig(
                name="schema",
                vector_size=128,
                replication_factor=2,
                shard_number=2,
                on_disk_payload=True,
            )
        )
