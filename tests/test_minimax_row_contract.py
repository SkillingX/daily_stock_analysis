# -*- coding: utf-8 -*-
"""MiniMax raw 行类型契约（issue #83 source-contract convergence）。

真实反例（root standards/spec 复现）：raw rows=[good, snippet-object, good]
在 ``snippet[:500]`` 切片处对 dict 抛 TypeError，被 ``_do_search`` 外层广
捕获变成**整批空失败响应**——两条有效新闻全部丢失，sector 准入永远收不到
行。root 明确批准的最小共享适配器改动：在切片/域名/日期处理**前**逐行
校验实际类型，畸形行本地拒绝并计数（``SearchResponse.malformed_rows``），
有效行全部保留。

本文件走**真实 MiniMax 解析代码**（MiniMaxSearchProvider._do_search 全路径，
仅隔离 HTTP 终端 ``_post_with_retry``）；不是用 fake SearchResponse 绕过
解析层的测试。默认字段/复制路径（filter/limit/rank）保字段一并覆盖。
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import src.search_service as search_service_module
from src.search_service import (
    MiniMaxSearchProvider,
    SearchResponse,
    SearchResult,
)


class _SyntheticHTTPResponse:
    """合成 HTTP 200 JSON 响应（终端隔离；解析代码全真实）。"""

    def __init__(self, body: Dict[str, Any]):
        self.status_code = 200
        self._body = body
        self.headers: Dict[str, str] = {"content-type": "application/json"}

    def json(self) -> Dict[str, Any]:
        return self._body


def _run_minimax(monkeypatch: pytest.MonkeyPatch, organic: List[Any], *,
                 query: str = "白酒行业 政策 新闻") -> SearchResponse:
    """真实 MiniMax _do_search + 合成 200 终端响应（零外部 TCP）。"""
    provider = MiniMaxSearchProvider(["synthetic-key"])

    def _fake_post(url, headers=None, json=None, timeout=15):  # noqa: ANN001
        return _SyntheticHTTPResponse(
            {"base_resp": {"status_code": 0}, "organic": organic},
        )

    monkeypatch.setattr(search_service_module, "_post_with_retry", _fake_post)
    return provider._do_search(query, "synthetic-key", 10)


def _good(title: str, link: str, *, snippet: str = "白酒行业新规落地",
          date: Any = "2026-11-06") -> Dict[str, Any]:
    return {"title": title, "snippet": snippet, "link": link, "date": date}


class TestMiniMaxRowTypeContract:
    """行类型契约：切片前校验，逐行拒绝计数，有效行保留。"""

    def test_malformed_snippet_between_good_rows_kept(self, monkeypatch):
        # root 反例（P2）：rows=[good, snippet-object, good] 曾整批丢失
        # （切片 TypeError → 广捕获 → 空失败响应）；契约下两条有效行保留、
        # 畸形行计数 1、success=True（部分畸形不等于供应商失败）
        resp = _run_minimax(monkeypatch, [
            _good("白酒行业新政策A", "https://example.com/a"),
            {"title": "白酒行业动态B", "snippet": {"s": 1},
             "link": "https://example.com/b", "date": "2026-11-06"},
            _good("白酒行业新政策C", "https://example.com/c"),
        ])
        assert resp.success is True
        assert [r.title for r in resp.results] == [
            "白酒行业新政策A", "白酒行业新政策C",
        ]
        assert resp.malformed_rows == 1

    def test_title_object_rejected_row_kept_neighbors(self, monkeypatch):
        # 真实 MiniMax JSON title=dict（spec 反例）曾是 str() 字符串化假
        # 标题；契约下整行拒绝，不 str(dict) 伪合法
        resp = _run_minimax(monkeypatch, [
            _good("白酒行业新政策A", "https://example.com/a"),
            {"title": {"n": "假标题"}, "snippet": "白酒行业动态",
             "link": "https://example.com/b", "date": "2026-11-06"},
            _good("白酒行业新政策C", "https://example.com/c"),
        ])
        assert resp.success is True
        assert len(resp.results) == 2
        assert resp.malformed_rows == 1

    def test_link_nonstring_and_missing_rejected(self, monkeypatch):
        # link=int / link 缺键 / title 空白：三类畸形行逐行拒绝；夹一条
        # 有效行证明部分畸形仍是成功响应（all-invalid 数据失败另有专测）
        resp = _run_minimax(monkeypatch, [
            {"title": "白酒A", "link": 12345, "snippet": "s",
             "date": "2026-11-06"},
            _good("白酒行业新政策", "https://example.com/ok"),
            {"title": "白酒B", "snippet": "s", "date": "2026-11-06"},
            {"title": "   ", "link": "https://example.com/c", "snippet": "s",
             "date": "2026-11-06"},
        ])
        assert resp.success is True
        assert [r.url for r in resp.results] == ["https://example.com/ok"]
        assert resp.malformed_rows == 3

    def test_non_dict_row_rejected(self, monkeypatch):
        resp = _run_minimax(monkeypatch, [
            "not-a-row",
            _good("白酒行业新政策", "https://example.com/a"),
        ])
        assert resp.success is True
        assert len(resp.results) == 1
        assert resp.malformed_rows == 1

    def test_date_object_rejected_snippet_list_rejected(self, monkeypatch):
        # 可选字段出现即必须 str：date=list / snippet=bytes 都是畸形行
        resp = _run_minimax(monkeypatch, [
            {"title": "白酒A", "snippet": "s",
             "link": "https://example.com/a", "date": ["2026", "11"]},
            {"title": "白酒B", "snippet": b"raw",
             "link": "https://example.com/b", "date": "2026-11-06"},
            _good("白酒行业新政策C", "https://example.com/c"),
        ])
        assert resp.success is True
        assert len(resp.results) == 1
        assert resp.malformed_rows == 2

    def test_missing_optional_snippet_and_date_allowed(self, monkeypatch):
        # 合法缺失可选字段：snippet/date 缺键不是畸形（缺失 ≠ 类型非法），
        # 行保留（snippet 空串、published_date None）
        resp = _run_minimax(monkeypatch, [
            {"title": "白酒行业新政策", "link": "https://example.com/a"},
        ])
        assert resp.success is True
        assert len(resp.results) == 1
        assert resp.results[0].snippet == ""
        assert resp.results[0].published_date is None
        assert resp.malformed_rows == 0

    def test_all_invalid_rows_explicit_data_failure_not_no_news(
            self, monkeypatch):
        # organic 有行且全部畸形 → 显式数据失败（success=False + sanitized
        # 类别 + 计数），不冒充「无新闻」（空结果 success=True 是无新闻语义）
        resp = _run_minimax(monkeypatch, [
            {"title": {"n": 1}, "link": "https://example.com/a"},
            {"title": "白酒", "snippet": {"s": 1},
             "link": "https://example.com/b", "date": "2026-11-06"},
        ])
        assert resp.success is False
        assert resp.results == []
        assert resp.malformed_rows == 2
        assert "畸形" in (resp.error_message or "")

    def test_genuinely_empty_organic_remains_legitimate_empty(
            self, monkeypatch):
        resp = _run_minimax(monkeypatch, [])
        assert resp.success is True
        assert resp.results == []
        assert resp.malformed_rows == 0

    def test_normal_rows_behavior_unchanged(self, monkeypatch):
        # 正常行解析行为不变：标题/摘要切片/域名提取/日期透传
        resp = _run_minimax(monkeypatch, [
            _good("白酒行业新政策", "https://news.example.com/baijiu/1",
                  snippet="摘要" * 300, date="2026-11-06"),
        ])
        assert resp.success is True
        assert resp.malformed_rows == 0
        item = resp.results[0]
        assert item.title == "白酒行业新政策"
        assert len(item.snippet) == 500  # 既有 500 字符切片保留
        assert item.url == "https://news.example.com/baijiu/1"
        assert item.published_date == "2026-11-06"

    def test_offwindow_good_row_still_time_filtered_not_malformed(
            self, monkeypatch):
        # 时间过滤语义不变：类型有效但窗口外的行被既有 _is_within_days
        # 过滤（不是畸形）；畸形计数只针对类型
        resp = _run_minimax(monkeypatch, [
            _good("白酒行业旧闻", "https://example.com/old",
                  date="2020-01-01"),
        ])
        assert resp.success is True
        assert resp.results == []
        assert resp.malformed_rows == 0


class TestSearchResponseMetadataPreservation:
    """追加默认 0 字段被既有复制/过滤/限行路径保留。

    这些是实例方法但不依赖实例状态（类属性 + classmethod 归一化）；
    ``object.__new__`` 构造裸实例避免拉起完整 SearchService 配置。
    """

    @staticmethod
    def _service() -> Any:
        from src.search_service import SearchService

        return object.__new__(SearchService)

    @staticmethod
    def _response(malformed: int) -> SearchResponse:
        return SearchResponse(
            query="白酒行业", results=[SearchResult(
                title="白酒行业新政策", snippet="s",
                url="https://example.com/a", source="example.com",
                published_date="2026-11-06",
            )],
            provider="MiniMax", success=True, malformed_rows=malformed,
        )

    def test_default_zero_on_plain_construction(self):
        resp = SearchResponse(query="q", results=[], provider="p")
        assert resp.malformed_rows == 0

    def test_filter_news_response_preserves_count(self):
        resp = self._response(2)
        out = self._service()._filter_news_response(
            resp, search_days=7, max_results=5, log_scope="t",
        )
        assert out.malformed_rows == 2

    def test_normalize_and_limit_preserves_count(self):
        resp = self._response(3)
        out = self._service()._normalize_and_limit_response(resp, max_results=5)
        assert out.malformed_rows == 3

    def test_limit_search_response_preserves_count(self):
        from src.search_service import SearchService

        resp = self._response(4)
        out = SearchService._limit_search_response(resp, max_results=5)
        assert out.malformed_rows == 4

    def test_rank_and_context_filter_preserve_count(self):
        from src.search_service import SearchService

        resp = self._response(5)
        ranked = SearchService._rank_news_response(
            resp, stock_code="600519", stock_name="贵州茅台",
            prefer_chinese=True, max_results=5, log_scope="t",
        )
        assert ranked.malformed_rows == 5
        ctx = SearchService._filter_ranked_news_for_context(
            ranked, log_scope="t",
        )
        assert ctx.malformed_rows == 5
