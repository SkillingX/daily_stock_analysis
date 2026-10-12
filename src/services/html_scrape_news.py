# -*- coding: utf-8 -*-
"""
HTML scrape news provider (P3-1 fallback, 2026-07-17).

When Tavily / SearXNG / Brave all fail (most of the time in our
environment), this provider fetches company news from Eastmoney's
public announcement API:

  GET https://np-anotice-stock.eastmoney.com/api/security/ann?
      sr=-1&page_size=20&page_index=1&ann_type=A
      &client_source=web&stock_list={code}
      &f_node=0&s_node=0

Returns real company filings (业绩预增 / 董事会决议 / 股东大会 etc.) which
are then filtered to the past N days.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import List, Optional, Set

import requests

from src.search_service import BaseSearchProvider, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

_session: Optional[requests.Session] = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": "https://so.eastmoney.com/",
            }
        )
        _session = s
    return _session


_EASTMONEY_ANN_URL = (
    "https://np-anotice-stock.eastmoney.com/api/security/ann"
    "?sr=-1&page_size=20&page_index=1&ann_type=A"
    "&client_source=web&stock_list={code}&f_node=0&s_node=0"
)


def _fetch_eastmoney_announcements(code: str, days: int = 7) -> List[SearchResult]:
    """Fetch company announcements from Eastmoney and filter to past N days.

    Returns a list of SearchResult on success, empty list on failure.
    """
    results: List[SearchResult] = []
    s = _get_session()
    url = _EASTMONEY_ANN_URL.format(code=code)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = s.get(url, timeout=8)
        if r.status_code != 200:
            logger.debug(
                "[HTMLNews] eastmoney %s status=%d", code, r.status_code
            )
            return []
        data = r.json()
        items = (data.get("data") or {}).get("list") or []
        seen_titles: Set[str] = set()
        for item in items:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            nd = (item.get("notice_date") or "")[:10]
            if nd and nd < cutoff:
                continue
            if title in seen_titles:
                continue
            seen_titles.add(title)
            art_code = item.get("art_code") or ""
            detail_url = (
                f"https://np-cnotice-stock.eastmoney.com/api/content/ann?"
                f"art_code={art_code}&client_source=web&page_index=1"
            )
            col_code = item.get("columns_code") or ""
            if col_code:
                detail_url = (
                    f"https://data.eastmoney.com/notices/stock/{code}.html"
                )
            results.append(
                SearchResult(
                    title=title,
                    url=detail_url,
                    snippet=f"[{nd}] {title[:100]}" if nd else title[:100],
                    source="eastmoney_ann",
                )
            )
            if len(results) >= 5:
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("[HTMLNews] eastmoney %s failed: %s", code, exc)
    return results


class HTMLScrapeNewsProvider(BaseSearchProvider):
    """P3-1 (2026-07-17): Last-resort news provider that scrapes Eastmoney
    announcements when Tavily / SearXNG / Brave all return 0."""

    requires_api_key = False

    def __init__(self) -> None:
        super().__init__(api_keys=[], name="html_scrape")

    def _do_search(
        self,
        query: str,
        api_key: str = "",
        max_results: int = 5,
        days: int = 7,
    ) -> SearchResponse:
        code_match = re.search(r"\b(\d{6})\b", query)
        if not code_match:
            return SearchResponse(
                query=query,
                results=[],
                provider=self.name,
                success=False,
                error_message="no 6-digit stock code in query",
            )
        code = code_match.group(1)
        all_results: List[SearchResult] = []
        try:
            all_results.extend(
                _fetch_eastmoney_announcements(code, days=days)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[HTMLNews] %s scrape failed: %s", code, exc)
        seen_urls: Set[str] = set()
        deduped: List[SearchResult] = []
        for r in all_results:
            if r.url in seen_urls:
                continue
            seen_urls.add(r.url)
            deduped.append(r)
            if len(deduped) >= max_results:
                break
        return SearchResponse(
            query=query,
            results=deduped,
            provider=self.name,
            success=len(deduped) > 0,
            error_message=None if deduped else "no results from eastmoney",
        )
