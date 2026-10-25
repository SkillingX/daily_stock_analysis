# -*- coding: utf-8 -*-
"""供应链 SemiAnalysis 检索工具（search_semianalysis）单元测试。

镜像 tests/test_policy_minesweeper_tools.py 的「公司公告检索」段：
- 鸭子类型 fake（_FakeResult/_FakeSearchResponse/_FakeProvider/_FakeSearchService）
- DI 搜索服务（monkeypatch _get_search_service），无真实网络
- 覆盖 query 构造 / provider 选择 / handler 全路径 / 工具元数据 / prompt 规则
"""

from __future__ import annotations

from typing import Any, List

import pytest

import src.agent.tools.supply_chain_tools as sct
from src.agent.tools.supply_chain_tools import (
    ALL_SUPPLY_CHAIN_TOOLS,
    _build_semianalysis_query,
    _handle_search_semianalysis,
    _pick_search_provider,
    _SEARCH_DAYS,
    search_semianalysis_tool,
)
from src.agent.supply_chain_executor import _SUPPLY_CHAIN_SYSTEM_PROMPT_TEMPLATE


# ============================================================
# 鸭子类型 fake
# ============================================================

class _FakeResult:
    """鸭子类型 SearchResult（title/snippet/url/source/published_date）。"""

    def __init__(self, title: str, url: str, source: str = "", snippet: str = "", published_date: str = ""):
        self.title = title
        self.url = url
        self.source = source
        self.snippet = snippet
        self.published_date = published_date


class _FakeSearchResponse:
    """鸭子类型 SearchResponse。"""

    def __init__(self, results: List[Any], success: bool = True, error_message: str = "", provider: str = "Tavily"):
        self.results = results
        self.success = success
        self.error_message = error_message
        self.provider = provider


class _FakeProvider:
    """鸭子类型 search provider：记录原生 .search() 调用，可控 is_available/结果/抛错。"""

    def __init__(self, *, available: bool = True, response: Any = None, raise_exc: Any = None, name: str = "Tavily"):
        self.is_available = available
        self.name = name
        self._response = response
        self._raise = raise_exc
        self.calls: List[dict] = []

    def search(self, query: str, max_results: int = 5, days: int = 7):
        self.calls.append({"query": query, "max_results": max_results, "days": days})
        if self._raise:
            raise self._raise
        return self._response


class _FakeSearchService:
    """鸭子类型 SearchService：仅持有 _providers 列表。"""

    def __init__(self, providers: List[Any]):
        self._providers = providers


def _patch_service(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider):
    """把 _get_search_service 替换为含单个 provider 的 fake service。"""
    svc = _FakeSearchService([provider])
    monkeypatch.setattr(sct, "_get_search_service", lambda: svc)
    return provider


# ============================================================
# _build_semianalysis_query
# ============================================================

class TestBuildSemianalysisQuery:
    def test_prefixes_site_operator_and_keywords(self):
        q = _build_semianalysis_query("HBM3E supply")
        assert q == "site:semianalysis.com HBM3E supply"

    def test_strips_whitespace(self):
        assert _build_semianalysis_query("  CoWoS capacity  ") == "site:semianalysis.com CoWoS capacity"

    def test_empty_keywords_yields_site_only(self):
        assert _build_semianalysis_query("   ") == "site:semianalysis.com"

    def test_none_keywords_yields_site_only(self):
        assert _build_semianalysis_query(None) == "site:semianalysis.com"


# ============================================================
# _pick_search_provider
# ============================================================

class TestPickSearchProvider:
    def test_none_service_returns_none(self):
        assert _pick_search_provider(None) is None

    def test_no_available_provider_returns_none(self):
        svc = _FakeSearchService([_FakeProvider(available=False)])
        assert _pick_search_provider(svc) is None

    def test_returns_first_available_provider(self):
        available = _FakeProvider(available=True)
        svc = _FakeSearchService([_FakeProvider(available=False), available])
        assert _pick_search_provider(svc) is available

    def test_ignores_empty_providers_list(self):
        assert _pick_search_provider(_FakeSearchService([])) is None


# ============================================================
# _handle_search_semianalysis
# ============================================================

class TestSearchSemianalysisHandler:
    def test_returns_error_when_no_available_provider(self, monkeypatch):
        _patch_service(monkeypatch, _FakeProvider(available=False))
        result = _handle_search_semianalysis("HBM3E supply")
        assert "error" in result and "不可用" in result["error"]
        assert result["keywords"] == "HBM3E supply"

    def test_unavailable_error_names_retained_plan_search_only(self, monkeypatch):
        # issue #47：不可用错误指向套餐搜索，不再指导配置退休的 SERPAPI 渠道
        _patch_service(monkeypatch, _FakeProvider(available=False))
        result = _handle_search_semianalysis("HBM")
        assert "SERPAPI" not in result["error"]
        assert "MiniMax" in result["error"]
        assert "智谱" in result["error"]

    def test_returns_error_when_service_none(self, monkeypatch):
        monkeypatch.setattr(sct, "_get_search_service", lambda: None)
        result = _handle_search_semianalysis("HBM3E supply")
        assert "error" in result and "不可用" in result["error"]

    def test_success_maps_results_and_passes_site_query(self, monkeypatch):
        response = _FakeSearchResponse(results=[
            _FakeResult("HBM3E deep dive", "https://www.semianalysis.com/p/hbm3e", "semianalysis.com", "摘要A", "2026-05-01"),
            _FakeResult("CoWoS capacity", "https://semianalysis.com/p/cowos", "semianalysis.com", "摘要B", "2026-04-01"),
        ])
        provider = _FakeProvider(available=True, response=response)
        _patch_service(monkeypatch, provider)

        result = _handle_search_semianalysis("HBM3E supply")

        assert "error" not in result
        assert result["count"] == 2
        assert result["results"][0]["url"] == "https://www.semianalysis.com/p/hbm3e"
        assert result["results"][0]["date"] == "2026-05-01"
        assert result["provider"] == "Tavily"
        assert "source_note" in result and "analysis" in result["source_note"]
        # 站点限定 query + 365 天窗口透传给 provider 原生 .search()
        assert provider.calls[0]["query"] == "site:semianalysis.com HBM3E supply"
        assert provider.calls[0]["days"] == _SEARCH_DAYS == 365
        assert provider.calls[0]["max_results"] == 5

    def test_max_results_param_passes_through(self, monkeypatch):
        response = _FakeSearchResponse(results=[_FakeResult("t", "https://semianalysis.com/a")])
        provider = _FakeProvider(available=True, response=response)
        _patch_service(monkeypatch, provider)
        _handle_search_semianalysis("CoWoS", max_results=8)
        assert provider.calls[0]["max_results"] == 8

    def test_results_capped_at_max_results(self, monkeypatch):
        response = _FakeSearchResponse(results=[_FakeResult(f"t{i}", f"https://semianalysis.com/{i}") for i in range(10)])
        provider = _FakeProvider(available=True, response=response)
        _patch_service(monkeypatch, provider)
        result = _handle_search_semianalysis("HBM", max_results=3)
        assert result["count"] == 3

    def test_returns_error_on_search_failure(self, monkeypatch):
        response = _FakeSearchResponse(results=[], success=False, error_message="quota exceeded")
        _patch_service(monkeypatch, _FakeProvider(available=True, response=response))
        result = _handle_search_semianalysis("HBM3E")
        assert "error" in result and "quota exceeded" in result["error"]
        assert result["query"] == "site:semianalysis.com HBM3E"

    def test_returns_error_on_search_exception(self, monkeypatch):
        _patch_service(monkeypatch, _FakeProvider(available=True, raise_exc=RuntimeError("boom")))
        result = _handle_search_semianalysis("HBM3E")
        assert "error" in result and "boom" in result["error"]

    def test_truncates_long_snippet(self, monkeypatch):
        long_text = "x" * 1000
        response = _FakeSearchResponse(results=[_FakeResult("t", "https://semianalysis.com/a", "semianalysis.com", long_text)])
        _patch_service(monkeypatch, _FakeProvider(available=True, response=response))
        result = _handle_search_semianalysis("HBM")
        assert result["results"][0]["snippet"] == "x" * 500


# ============================================================
# 工具元数据
# ============================================================

class TestSearchSemianalysisToolMetadata:
    def test_registered_in_all_supply_chain_tools(self):
        names = {t.name for t in ALL_SUPPLY_CHAIN_TOOLS}
        assert "search_semianalysis" in names

    def test_category_is_search(self):
        assert search_semianalysis_tool.category == "search"

    def test_required_keywords_param(self):
        param_names = {p.name: p for p in search_semianalysis_tool.parameters}
        assert "keywords" in param_names
        assert param_names["keywords"].required is True
        assert param_names["keywords"].type == "string"

    def test_max_results_optional_default(self):
        param_names = {p.name: p for p in search_semianalysis_tool.parameters}
        assert param_names["max_results"].required is False
        assert param_names["max_results"].default == 5

    def test_description_points_at_retained_plan_search(self):
        desc = search_semianalysis_tool.description
        assert "SERPAPI" not in desc
        assert "MiniMax" in desc and "智谱" in desc

    def test_description_mentions_semianalysis_and_use_case(self):
        desc = search_semianalysis_tool.description
        assert "semianalysis.com" in desc
        assert "半导体" in desc or "AI" in desc


# ============================================================
# prompt 规则（standing，仅半导体/AI 主题生效）
# ============================================================

class TestSemiAnalysisPromptRule:
    def test_prompt_template_contains_rule(self):
        assert "search_semianalysis" in _SUPPLY_CHAIN_SYSTEM_PROMPT_TEMPLATE
        assert "SemiAnalysis 检索规则" in _SUPPLY_CHAIN_SYSTEM_PROMPT_TEMPLATE

    def test_prompt_template_lists_tool_and_triggers(self):
        tpl = _SUPPLY_CHAIN_SYSTEM_PROMPT_TEMPLATE
        # 工具清单与触发词
        assert "search_semianalysis" in tpl
        assert "HBM" in tpl and "CoWoS" in tpl
        # 证据强度与付费墙边界
        assert "analysis" in tpl
        assert "付费墙" in tpl
        # 非半导体主题不必调用
        assert "不必调用" in tpl or "非半导体" in tpl
