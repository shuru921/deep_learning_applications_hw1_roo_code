"""PubMed API 非同步封裝。"""

from __future__ import annotations

import asyncio
import json
import time
import xml.etree.ElementTree as ET
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
        logger: Any | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries 必須大於等於 0")
        if retry_backoff[0] <= 0 or retry_backoff[1] < 1:
            raise ValueError("retry_backoff 需為 (base_delay>0, multiplier>=1)")

        self._client = async_client
        self._rate_limiter = rate_limiter
        self._api_key = api_key
        self._tool_name = tool_name
        self._email = email
        self._max_retries = max_retries
        self._base_backoff = retry_backoff[0]
        self._backoff_multiplier = retry_backoff[1]
        self._logger = logger or self._default_logger()

    async def warm_up(self) -> None:
        """預熱連線與 rate limiter。"""

        try:
            async with self._rate_limiter.throttle():
                await asyncio.sleep(0)
        except TimeoutError as exc:  # pragma: no cover - 表現為配置錯誤
            raise PubMedRateLimitError("Rate limiter 預熱失敗", cause=exc) from exc

    async def search(self, query: PubMedQuery) -> PubMedSearchResult:
        params = query.to_params()
        response, metrics = await self._request(endpoint="esearch.fcgi", params=params)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:  # pragma: no cover - httpx 已保證 json()
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
        response, metrics = await self._request("efetch.fcgi", params=params)
        raw_bytes = response.content
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
        response, metrics = await self._request("esummary.fcgi", params=params)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:  # pragma: no cover
            raise PubMedParseError("無法解析 PubMed 摘要 JSON", cause=exc)

        result_map = payload.get("result", {})
        summaries: list[PubMedSummary] = []
        for pid in ids:
            entry = result_map.get(pid)
            if not entry:
                continue
            authors = [a.get("name") for a in entry.get("authors", []) if a.get("name")]
            summaries.append(
                PubMedSummary(
                    pmid=pid,
                    title=entry.get("title"),
                    authors=tuple(authors),
                    source=entry.get("source"),
                    pubdate=entry.get("pubdate"),
                    raw=entry,
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
    ) -> tuple[httpx.Response, Mapping[str, Any]]:
        url = f"{BASE_URL}{endpoint}"
        merged_params = {**params}
        if self._api_key:
            merged_params["api_key"] = self._api_key
        if self._tool_name:
            merged_params["tool"] = self._tool_name
        if self._email:
            merged_params["email"] = self._email

        last_exc: Exception | None = None
        total_wait = 0.0
        for attempt in range(1, self._max_retries + 2):
            try:
                async with self._rate_limiter.throttle() as waited:
                    total_wait += waited
                    start = time.perf_counter()
                    response = await self._client.get(url, params=merged_params)
                    latency = time.perf_counter() - start
                response.raise_for_status()
                return response, {
                    "latency": latency,
                    "retry_count": attempt - 1,
                    "rate_limit_wait": total_wait,
                }
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    last_exc = exc
                    if attempt > self._max_retries:
                        raise PubMedRateLimitError(
                            "PubMed API 達到 Rate Limit",
                            detail={
                                "endpoint": endpoint,
                                "status": exc.response.status_code,
                            },
                            cause=exc,
                        ) from exc
                else:
                    raise PubMedHTTPError(
                        "PubMed HTTP 狀態錯誤",
                        detail={
                            "endpoint": endpoint,
                            "status": exc.response.status_code,
                        },
                        cause=exc,
                    ) from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt > self._max_retries:
                    raise PubMedHTTPError(
                        "PubMed HTTP 請求失敗",
                        detail={"endpoint": endpoint},
                        cause=exc,
                    ) from exc

            await asyncio.sleep(self._compute_backoff(attempt))

        assert last_exc is not None
        raise PubMedError("PubMed 請求失敗", cause=last_exc)

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
        return self._base_backoff * (self._backoff_multiplier ** (attempt - 1))

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
