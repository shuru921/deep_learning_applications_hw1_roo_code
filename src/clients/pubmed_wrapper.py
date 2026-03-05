"""PubMed API 非同步封裝。"""

from __future__ import annotations

import asyncio
import json
import random
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

import httpx

try:
    import structlog
except ModuleNotFoundError:  # pragma: no cover - 在未安裝 structlog 的環境降級
    structlog = None  # type: ignore[assignment]

from ..errors import (
    PubMedEmptyResult,
    PubMedError,
    PubMedHTTPError,
    PubMedParseError,
    PubMedRateLimitError,
    RateLimitTimeoutError,
)
from ..utils.rate_limit import AsyncRateLimiter


BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


@dataclass(slots=True)
class PubMedQuery:
    term: str
    retmax: int = 20
    retstart: int = 0
    sort: str | None = None
    datetype: str | None = None
    mindate: str | None = None
    maxdate: str | None = None
    field: str | None = None
    use_history: bool = True

    def to_params(self) -> MutableMapping[str, str]:
        params: MutableMapping[str, str] = {
            "db": "pubmed",
            "term": self.term,
            "retmax": str(self.retmax),
            "retstart": str(self.retstart),
            "retmode": "json",
        }
        if self.sort:
            params["sort"] = self.sort
        if self.datetype:
            params["datetype"] = self.datetype
        if self.mindate:
            params["mindate"] = self.mindate
        if self.maxdate:
            params["maxdate"] = self.maxdate
        if self.field:
            params["field"] = self.field
        if self.use_history:
            params["retmode"] = "json"
            params["usehistory"] = "y"
        return params


@dataclass(slots=True)
class PubMedArticle:
    pmid: str
    title: str | None
    abstract: str | None
    journal: str | None
    published: str | None
    raw: Mapping[str, Any] | None = None


@dataclass(slots=True)
class PubMedSearchResult:
    ids: Sequence[str]
    query_key: str | None
    webenv: str | None
    count: int
    warnings: Sequence[str] = field(default_factory=tuple)
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PubMedBatch:
    articles: Sequence[PubMedArticle]
    raw_xml: bytes
    warnings: Sequence[str] = field(default_factory=tuple)
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PubMedSummary:
    pmid: str
    title: str | None
    authors: Sequence[str]
    source: str | None
    pubdate: str | None
    raw: Mapping[str, Any] | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    warnings: Sequence[str] = field(default_factory=tuple)


class PubMedWrapper:
    """PubMed E-utilities 封裝，提供防禦性非同步操作。"""

    def __init__(
        self,
        async_client: httpx.AsyncClient,
        rate_limiter: AsyncRateLimiter,
        *,
        api_key: str | None = None,
        tool_name: str | None = None,
        email: str | None = None,
        max_retries: int = 3,
        retry_backoff: tuple[float, float] = (0.5, 2.0),
        max_backoff: float | None = 10.0,
        jitter: tuple[float, float] = (0.5, 1.5),
        logger: Any | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries 必須大於等於 0")
        if retry_backoff[0] <= 0 or retry_backoff[1] < 1:
            raise ValueError("retry_backoff 需為 (base_delay>0, multiplier>=1)")
        if max_backoff is not None and max_backoff <= 0:
            raise ValueError("max_backoff 若提供需大於 0")
        if jitter[0] <= 0 or jitter[0] > jitter[1]:
            raise ValueError("jitter 需滿足 0 < min <= max")

        self._client = async_client
        self._rate_limiter = rate_limiter
        self._api_key = api_key
        self._tool_name = tool_name
        self._email = email
        self._max_retries = max_retries
        self._base_backoff = retry_backoff[0]
        self._backoff_multiplier = retry_backoff[1]
        self._max_backoff = max_backoff
        self._jitter = jitter
        self._logger = logger or self._default_logger()

    async def warm_up(self) -> None:
        """預熱連線與 rate limiter。"""

        try:
            async with self._rate_limiter.throttle():
                await asyncio.sleep(0)
        except RateLimitTimeoutError as exc:  # pragma: no cover - 表現為配置錯誤
            raise PubMedRateLimitError("Rate limiter 預熱失敗", cause=exc) from exc

    async def search(self, query: PubMedQuery) -> PubMedSearchResult:
        params = query.to_params()
        response, metrics = await self._request(
            endpoint="esearch.fcgi", params=params, expected="json"
        )
        raw_body = self._handle_response(
            response,
            expected="json",
            request_id=metrics.get("request_id"),
        )
        decoder = response.encoding or "utf-8"
        try:
            payload = json.loads(raw_body.decode(decoder))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PubMedParseError("無法解析 PubMed 搜尋回應", cause=exc)

        search_info = payload.get("esearchresult", {})
        id_list = search_info.get("idlist", [])
        if not id_list:
            raise PubMedEmptyResult("搜尋結果為空", detail={"term": query.term})

        result = PubMedSearchResult(
            ids=id_list,
            query_key=search_info.get("querykey"),
            webenv=search_info.get("webenv"),
            count=int(search_info.get("count", 0)),
            warnings=(
                tuple(payload.get("warnings", {}).values())
                if isinstance(payload.get("warnings"), Mapping)
                else tuple()
            ),
            metrics=metrics,
        )

        self._log_event(
            "pubmed.search",
            {
                "term": query.term,
                "ids": id_list[:5],
                "count": result.count,
                **metrics,
            },
        )
        return result

    async def fetch_details(
        self,
        ids: Sequence[str],
        *,
        rettype: str = "abstract",
        retmode: str = "xml",
    ) -> PubMedBatch:
        if not ids:
            raise ValueError("ids 不可為空")

        params = {
            "db": "pubmed",
            "id": ",".join(ids),
            "rettype": rettype,
            "retmode": retmode,
        }
        response, metrics = await self._request(
            "efetch.fcgi", params=params, expected="xml"
        )
        raw_bytes = self._handle_response(
            response,
            expected="xml",
            request_id=metrics.get("request_id"),
        )
        try:
            articles = tuple(self._parse_xml(raw_bytes))
        except PubMedParseError:
            raise
        except Exception as exc:  # pragma: no cover - 未知解析錯誤
            raise PubMedParseError("解析 PubMed XML 失敗", cause=exc)

        batch = PubMedBatch(articles=articles, raw_xml=raw_bytes, metrics=metrics)
        self._log_event(
            "pubmed.fetch_details",
            {
                "requested": len(ids),
                "parsed": len(articles),
                **metrics,
            },
        )
        return batch

    async def fetch_summaries(self, ids: Sequence[str]) -> Sequence[PubMedSummary]:
        if not ids:
            raise ValueError("ids 不可為空")

        params = {
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "json",
        }
        response, metrics = await self._request(
            "esummary.fcgi", params=params, expected="json"
        )
        raw_body = self._handle_response(
            response,
            expected="json",
            request_id=metrics.get("request_id"),
        )
        decoder = response.encoding or "utf-8"
        try:
            payload = json.loads(raw_body.decode(decoder))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # pragma: no cover
            raise PubMedParseError("無法解析 PubMed 摘要 JSON", cause=exc)

        result_map = payload.get("result", {})
        summaries: list[PubMedSummary] = []
        for pid in ids:
            entry = result_map.get(pid)
            if not entry:
                continue
            authors = [a.get("name") for a in entry.get("authors", []) if a.get("name")]
            warnings: list[str] = []
            if not entry.get("title"):
                warnings.append("missing_title")
            summaries.append(
                PubMedSummary(
                    pmid=pid,
                    title=entry.get("title"),
                    authors=tuple(authors),
                    source=entry.get("source"),
                    pubdate=entry.get("pubdate"),
                    raw=entry,
                    metrics=metrics,
                    warnings=tuple(warnings),
                )
            )

        self._log_event(
            "pubmed.fetch_summaries",
            {
                "requested": len(ids),
                "returned": len(summaries),
                **metrics,
            },
        )
        return tuple(summaries)

    async def _request(
        self,
        endpoint: str,
        params: Mapping[str, Any],
        expected: str | None = None,
    ) -> tuple[httpx.Response, Mapping[str, Any]]:
        url = f"{BASE_URL}{endpoint}"
        merged_params = self._build_params(endpoint, params)

        metrics: dict[str, Any] = {
            "retry_count": 0,
            "rate_limit_wait": 0.0,
            "waited": 0.0,
            "request_id": None,
            "expected": expected,
        }

        for attempt in range(1, self._max_retries + 2):
            try:
                async with self._throttle() as waited:
                    metrics["waited"] = waited
                    metrics["rate_limit_wait"] += waited
                    start = time.perf_counter()
                    response = await self._client.get(url, params=merged_params)
                    metrics["latency"] = time.perf_counter() - start
                    metrics["status_code"] = response.status_code
                    metrics["request_id"] = response.headers.get("x-request-id")
                    response.raise_for_status()
                metrics["retry_count"] = attempt - 1
                metrics["retry_events"] = tuple(metrics.get("retry_events", []))
                metrics["backoff_schedule"] = tuple(metrics.get("backoff_schedule", []))
                return response, metrics
            except RateLimitTimeoutError as exc:
                raise PubMedRateLimitError(
                    "Rate limiter acquire timeout",
                    detail={"endpoint": endpoint, "waited": exc.waited},
                    cause=exc,
                ) from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                metrics.setdefault("retry_events", []).append(
                    {
                        "attempt": attempt,
                        "status": status,
                        "retryable": status == 429 or 500 <= status < 600,
                    }
                )
                if status == 429:
                    if attempt > self._max_retries:
                        raise PubMedRateLimitError(
                            "PubMed API 達到 Rate Limit",
                            detail={
                                "endpoint": endpoint,
                                "status": status,
                                "request_id": metrics.get("request_id"),
                            },
                            cause=exc,
                        ) from exc
                elif 500 <= status < 600:
                    if attempt > self._max_retries:
                        raise PubMedHTTPError(
                            "PubMed 服務暫時失效",
                            detail={
                                "endpoint": endpoint,
                                "status": status,
                                "request_id": metrics.get("request_id"),
                            },
                            cause=exc,
                        ) from exc
                else:
                    raise PubMedHTTPError(
                        "PubMed HTTP 狀態錯誤",
                        detail={
                            "endpoint": endpoint,
                            "status": status,
                            "request_id": metrics.get("request_id"),
                        },
                        cause=exc,
                    ) from exc
            except httpx.HTTPError as exc:
                metrics.setdefault("retry_events", []).append(
                    {"attempt": attempt, "error": type(exc).__name__}
                )
                if attempt > self._max_retries:
                    raise PubMedHTTPError(
                        "PubMed HTTP 請求失敗",
                        detail={
                            "endpoint": endpoint,
                            "request_id": metrics.get("request_id"),
                        },
                        cause=exc,
                    ) from exc

            metrics["retry_count"] = attempt
            backoff = self._compute_backoff(attempt)
            metrics.setdefault("backoff_schedule", []).append(
                {"attempt": attempt, "delay": backoff}
            )
            await asyncio.sleep(backoff)

        raise PubMedError(
            "PubMed 請求失敗",
            detail={"endpoint": endpoint, "retry_count": metrics["retry_count"]},
        )

    def _build_params(
        self, endpoint: str, payload: Mapping[str, Any]
    ) -> MutableMapping[str, Any]:
        params = {**payload}
        if self._api_key:
            params.setdefault("api_key", self._api_key)
        if self._tool_name:
            params.setdefault("tool", self._tool_name)
        if self._email:
            params.setdefault("email", self._email)
        return params

    @asynccontextmanager
    async def _throttle(self) -> Any:
        async with self._rate_limiter.throttle() as waited:
            yield waited

    def _handle_response(
        self,
        response: httpx.Response,
        *,
        expected: str | None = None,
        request_id: str | None = None,
    ) -> bytes:
        content_type = response.headers.get("content-type", "").lower()
        if expected == "json" and "json" not in content_type:
            raise PubMedHTTPError(
                "PubMed 回應 Content-Type 異常",
                detail={
                    "expected": "json",
                    "actual": content_type,
                    "request_id": request_id,
                },
                status_code=response.status_code,
            )
        if expected == "xml" and "xml" not in content_type:
            raise PubMedHTTPError(
                "PubMed 回應 Content-Type 異常",
                detail={
                    "expected": "xml",
                    "actual": content_type,
                    "request_id": request_id,
                },
                status_code=response.status_code,
            )
        return response.content

    def _parse_xml(self, raw: bytes) -> Iterable[PubMedArticle]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise PubMedParseError("XML 解析失敗", cause=exc)

        for article_node in root.findall(".//PubmedArticle"):
            pmid_elem = article_node.find(".//PMID")
            pmid = pmid_elem.text if pmid_elem is not None else None
            if not pmid:
                continue

            title_elem = article_node.find(".//ArticleTitle")
            abstract_elems = article_node.findall(".//AbstractText")
            abstract_text = "\n".join(
                filter(None, (elem.text for elem in abstract_elems))
            )
            journal_elem = article_node.find(".//Journal/Title")
            date_elem = article_node.find(".//Article/Journal/JournalIssue/PubDate")
            published = self._extract_pub_date(date_elem)

            yield PubMedArticle(
                pmid=pmid,
                title=title_elem.text if title_elem is not None else None,
                abstract=abstract_text or None,
                journal=journal_elem.text if journal_elem is not None else None,
                published=published,
                raw=self._element_to_mapping(article_node),
            )

    def _extract_pub_date(self, element: ET.Element | None) -> str | None:
        if element is None:
            return None
        parts = [
            (element.find("Year"), ""),
            (element.find("Month"), ""),
            (element.find("Day"), ""),
        ]
        values = [node.text for node, _ in parts if node is not None and node.text]
        return "-".join(values) if values else None

    def _element_to_mapping(self, element: ET.Element) -> Mapping[str, Any]:
        def recurse(node: ET.Element) -> Any:
            children = list(node)
            if not children:
                return node.text
            grouped: MutableMapping[str, Any] = {}
            for child in children:
                grouped.setdefault(child.tag, []).append(recurse(child))
            return {
                tag: value if len(value) > 1 else value[0]
                for tag, value in grouped.items()
            }

        return {element.tag: recurse(element)}

    def _compute_backoff(self, attempt: int) -> float:
        delay = self._base_backoff * (self._backoff_multiplier ** (attempt - 1))
        if self._max_backoff is not None:
            delay = min(delay, self._max_backoff)
        jitter_min, jitter_max = self._jitter
        jitter_scale = random.uniform(jitter_min, jitter_max)
        return max(delay * jitter_scale, 0.0)

    def _log_event(self, event: str, payload: Mapping[str, Any]) -> None:
        if hasattr(self._logger, "bind"):
            self._logger.bind(tool="pubmed").info(event, **payload)
        else:  # pragma: no cover - 降級至標準 logger
            self._logger.info("%s %s", event, payload)

    def _default_logger(self) -> Any:
        if structlog is not None:
            return structlog.get_logger("pubmed.wrapper")
        import logging

        logger = logging.getLogger("pubmed.wrapper")
        if not logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        return logger


__all__ = [
    "PubMedWrapper",
    "PubMedQuery",
    "PubMedSearchResult",
    "PubMedBatch",
    "PubMedArticle",
    "PubMedSummary",
]
