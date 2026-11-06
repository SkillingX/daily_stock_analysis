# -*- coding: utf-8 -*-
"""行业催化统一准入（issue #83 主契约单元测试）。

一条准入路径：source 验证（公共 HTTP(S) 结构契约）→ 行业相关性 → 日期获得
（provider / 文章元数据两分支）→ **同一个**事件资格/去重登记。覆盖 root
反例与日期证据合同（真实 newspaper parser，终端 HTTP 不发起）：

- 正常 provider 日期 / 正常文章元数据日期（raw basis 升级）
- URL-only 不作日期证据（保持背景）
- URL/meta 冲突 → 保守背景
- 旧事件当年/旧年两条日期路径均降级（2026 年 1 月旧月、2020 旧年、
  今天发布的「2020年与2019年销量对比」历史背景）
- 「对比/修订/废止/取代」等旧标题豁免词表已删除：不再绕过日期主体判定
- 探查显式错误分类：传输/超限/解析数据失败降背景，程序错误传播
- 非 HTTP(S)/私网 literal IP/userinfo 来源本地拒绝计数
- 跨域同源转载（新华社通稿）两条日期路径都是一个事件
- 错行业 / 个股活动 / 未来日期 / 显式供应商失败 ≠ 无新闻 / sanitized 403
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest

from src.search_service import SearchResult
from src.services.sector_analysis_service import ReportBudget
from src.services import sector_research
from src.services import sector_analysis_service as svc


def _today() -> date:
    return datetime.now().date()


def _budget(seconds: float = 30.0) -> ReportBudget:
    return ReportBudget(seconds)


class _FakeProvider:
    """受控合成搜索 provider（终端响应隔离；search() 返回给定结果）。"""

    name = "SyntheticSearch"

    def __init__(self, rows: List[Dict[str, Any]], *, success: bool = True,
                 error_message: Optional[str] = None):
        self._rows = rows
        self._success = success
        self._error_message = error_message
        self.calls: List[str] = []

    @property
    def is_available(self) -> bool:
        return True

    def search(self, query: str, max_results: int = 5, days: int = 7):
        self.calls.append(query)
        results = [SearchResult(**row) for row in self._rows]
        return type("R", (), {
            "query": query, "results": results, "provider": self.name,
            "success": self._success, "error_message": self._error_message,
            "search_time": 0.01,
        })()


def _install_provider(monkeypatch: pytest.MonkeyPatch, provider: _FakeProvider) -> None:
    from src.search_service import get_search_service

    svc_instance = get_search_service()
    monkeypatch.setattr(svc_instance, "_providers", [provider])


def _result(title: str, url: str, *, snippet: str = "", source: str = "",
            published: Optional[str] = None) -> Dict[str, Any]:
    return {"title": title, "snippet": snippet, "url": url,
            "source": source, "published_date": published}


# ---------------------------------------------------------------------------
# 文章日期证据（真实 newspaper parser；HTML 注入，无网络）
# ---------------------------------------------------------------------------

_META_HTML = """<!DOCTYPE html>
<html><head>
<meta property="article:published_time" content="{ts}">
<title>白酒行业新政策</title>
</head><body><p>正文</p></body></html>"""

_PLAIN_HTML = """<!DOCTYPE html>
<html><head><title>白酒行业新闻</title></head>
<body><p>白酒行业某企业动态，员工运动会顺利举办。</p></body></html>"""


class TestArticleDateEvidence:
    """真实 newspaper 提取：URL 启发式禁用 + raw basis 可保存。"""

    def test_metadata_date_with_raw_basis(self):
        url = "https://news.example.com/baijiu/12345.html"  # URL 无日期
        evidence = sector_research.extract_article_date_evidence(
            _META_HTML.format(ts="2026-11-05T09:00:00"), url,
        )
        assert evidence.metadata_date == date(2026, 11, 5)
        assert evidence.raw_basis is not None
        assert "article:published_time" in evidence.raw_basis
        assert evidence.url_declared_date is None

    def test_url_only_stays_unqualified(self):
        # URL 含日期 + HTML 无任何 meta 日期 → url_only，不作日期证据
        url = "https://news.example.com/2026/11/06/baijiu.html"
        evidence = sector_research.extract_article_date_evidence(_PLAIN_HTML, url)
        assert evidence.metadata_date is None
        assert evidence.url_declared_date == date(2026, 11, 6)
        qualified, reason = sector_research.qualifies_article_declared_date(
            evidence, _today() - timedelta(days=2), _today(),
        )
        assert qualified is None
        assert reason == "url_only"

    def test_url_meta_conflict_rejected(self):
        # URL 日期 ≠ 元数据日期 → provenance 冲突，保守拒绝
        url = "https://news.example.com/2026/11/06/baijiu.html"
        evidence = sector_research.extract_article_date_evidence(
            _META_HTML.format(ts="2026-11-05T09:00:00"), url,
        )
        assert evidence.metadata_date == date(2026, 11, 5)
        assert evidence.url_declared_date == date(2026, 11, 6)
        qualified, reason = sector_research.qualifies_article_declared_date(
            evidence, _today() - timedelta(days=2), _today(),
        )
        assert qualified is None
        assert reason == "url_meta_date_conflict"

    def test_metadata_without_raw_basis_rejected(self):
        # 元数据日期无法定位原始 meta 标签（构造未匹配形态）→ 保守拒绝
        url = "https://news.example.com/a.html"
        html = _META_HTML.format(ts="2026-11-05T09:00:00").replace(
            'property="article:published_time"', 'data-custom="unknown-field"')
        evidence = sector_research.extract_article_date_evidence(html, url)
        # data-custom 不在标准出版元数据集合 → metadata_date None
        assert evidence.metadata_date is None

    def test_offwindow_metadata_date_rejected(self):
        url = "https://news.example.com/a.html"
        evidence = sector_research.extract_article_date_evidence(
            _META_HTML.format(ts="2026-01-05T09:00:00"), url,
        )
        qualified, reason = sector_research.qualifies_article_declared_date(
            evidence, _today() - timedelta(days=2), _today(),
        )
        assert qualified is None
        assert reason == "article_date_offwindow"

    def test_real_article_download_path_not_used_here(self):
        # 本文件只测提取逻辑（HTML 已下载）；探查网络路径由 API 全链路测试覆盖
        assert callable(svc._probe_article_date_evidence)

    @staticmethod
    def _fake_stream_response(html: str, *, history: Optional[list] = None,
                              url: str = "https://news.example.com/a.html",
                              chunk_size: int = 0):
        """合成 stream 响应（终端 HTTP 隔离；真实 requests.Session 层被打桩）。"""
        payload = html.encode("utf-8", errors="replace")
        hops = list(history or [])

        class _FakeResp:
            status_code = 200
            text = html
            headers = {"content-type": "text/html; charset=utf-8"}
            encoding = "utf-8"
            apparent_encoding = "utf-8"
            history = hops

            @property
            def url(self) -> str:
                return url

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=64 * 1024):  # noqa: ARG001
                if chunk_size:
                    for i in range(0, len(payload), chunk_size):
                        yield payload[i:i + chunk_size]
                else:
                    yield payload

            def close(self):
                self.closed = True

            def __enter__(self):
                return self

            def __exit__(self, *exc_info):  # noqa: ANN002
                self.close()
                return False

        return _FakeResp()

    def test_real_probe_bounded_stream_with_terminal_http_isolated(
            self, monkeypatch):
        # contract 3：有界 stream 读取（requests.Session 终端隔离），元数据
        # 提取走本模块逻辑；publish_date/Article.download 全文路径不消费
        html = _META_HTML.format(ts="2026-11-05T09:00:00")
        fake_resp = self._fake_stream_response(
            html, url="https://news.example.com/baijiu/12345.html")
        seen_kwargs: Dict[str, Any] = {}

        def _fake_get(self, method, url, **kwargs):  # noqa: ANN001
            seen_kwargs.update(kwargs)
            return fake_resp

        monkeypatch.setattr("requests.Session.request", _fake_get)
        budget = _budget()
        evidence, failure, final_url = svc._probe_article_date_evidence(
            "https://news.example.com/baijiu/12345.html", budget,
        )
        assert failure is None
        assert evidence.metadata_date == date(2026, 11, 5)
        assert evidence.raw_basis is not None
        assert "article:published_time" in evidence.raw_basis
        assert seen_kwargs.get("stream") is True  # 有界读取路径

    def test_probe_transport_failure_is_explicit_background(self, monkeypatch):
        # 决策 4：已识别传输错误 → 显式类别降级（背景保留），不是「无日期」
        import requests as _requests

        def _failing_get(self, method, url, **kwargs):  # noqa: ANN001
            raise _requests.ConnectionError("synthetic network down")

        monkeypatch.setattr("requests.Session.request", _failing_get)
        budget = _budget()
        evidence, failure, final_url = svc._probe_article_date_evidence(
            "https://news.example.com/down.html", budget,
        )
        assert failure == "transport"
        assert evidence.metadata_date is None

    def test_probe_oversize_stops_reading_and_reports_category(
            self, monkeypatch):
        # 决策 3：响应超过字节上限 → 显式 article_oversize（读取期停止）
        big = "x" * (sector_research.CATALYST_ARTICLE_MAX_BYTES + 1024)
        fake_resp = self._fake_stream_response(big, chunk_size=64 * 1024)
        read_total = {"n": 0}

        original_iter = fake_resp.iter_content

        def _counting_iter(chunk_size=64 * 1024):  # noqa: ANN001
            for chunk in original_iter(chunk_size):
                read_total["n"] += len(chunk)
                yield chunk

        monkeypatch.setattr(fake_resp, "iter_content", _counting_iter)

        def _fake_get(self, method, url, **kwargs):  # noqa: ANN001
            return fake_resp

        monkeypatch.setattr("requests.Session.request", _fake_get)
        evidence, failure, final_url = svc._probe_article_date_evidence(
            "https://news.example.com/big.html", _budget(30.0),
        )
        assert failure == "article_oversize"
        # 实际读取在到达上限后停止（不消费完整响应）
        assert read_total["n"] <= sector_research.CATALYST_ARTICLE_MAX_BYTES + 64 * 1024

    def test_parser_none_contract_raises_explicit_data_error(self):
        # 决策 4：newspaper ``Parser.fromstring`` 的显式 None 契约（0.2.8
        # invalid string 分支）→ ArticleParseFailure（预期解析数据错误类别）
        with pytest.raises(sector_research.ArticleParseFailure):
            sector_research.extract_article_date_evidence(
                "\udcff\udcfe invalid encoding sequence",
                "https://news.example.com/junk.html",
            )

    def test_probe_parse_failure_is_explicit_background(self, monkeypatch):
        # 决策 4：probe 捕获显式 ArticleParseFailure → article_parse_failed
        # 背景降级（只捕获该显式类别，不做宽泛包装）
        fake_resp = self._fake_stream_response(
            "<html><head><title>x</title></head></html>",
            url="https://news.example.com/junk.html")

        def _fake_get(self, method, url, **kwargs):  # noqa: ANN001
            return fake_resp

        def _raise_parse_failure(html: str, url_arg: str):  # noqa: ANN001
            raise sector_research.ArticleParseFailure(
                "newspaper parser rejected the bounded html")

        monkeypatch.setattr("requests.Session.request", _fake_get)
        monkeypatch.setattr(
            sector_research, "extract_article_date_evidence",
            _raise_parse_failure,
        )
        evidence, failure, final_url = svc._probe_article_date_evidence(
            "https://news.example.com/junk.html", _budget(),
        )
        assert failure == "article_parse_failed"
        assert evidence.metadata_date is None

    def test_probe_program_error_propagates_not_hidden(self, monkeypatch):
        # 决策 4：extractor 程序错误（TypeError）原样传播，不伪装「无日期」
        fake_resp = self._fake_stream_response(_META_HTML.format(
            ts="2026-11-05T09:00:00"))

        def _fake_get(self, method, url, **kwargs):  # noqa: ANN001
            return fake_resp

        monkeypatch.setattr("requests.Session.request", _fake_get)

        def _boom(html: str, url: str):  # noqa: ANN001
            raise TypeError("synthetic extractor programming error")

        monkeypatch.setattr(
            sector_research, "extract_article_date_evidence", _boom)
        with pytest.raises(TypeError):
            svc._probe_article_date_evidence(
                "https://news.example.com/prog.html", _budget(),
            )

    def test_probe_redirect_escape_rejected_before_connecting_target(
            self, monkeypatch):
        # 决策 3：redirect 手动逐跳、先校验后连接——私网目标**从未被连接**；
        # 自动跟随会让 requests 先连出目标再检查（逃逸已发生）
        attempts: List[str] = []

        class _RedirectResp:
            status_code = 302
            headers = {"Location": "http://10.0.0.1/next"}
            url = "https://news.example.com/hop.html"

            def close(self):
                pass

            def iter_content(self, chunk_size=64 * 1024):  # noqa: ANN001
                raise AssertionError("redirect response body must not be read")

        def _fake_get(self, method, url, **kwargs):  # noqa: ANN001
            attempts.append(url)
            assert kwargs.get("allow_redirects") is False
            if url == "https://news.example.com/hop.html":
                return _RedirectResp()
            raise AssertionError(f"redirect target must not be connected: {url}")

        monkeypatch.setattr("requests.Session.request", _fake_get)
        evidence, failure, final_url = svc._probe_article_date_evidence(
            "https://news.example.com/hop.html", _budget(),
        )
        assert failure == "redirect_escaped:source_non_public_host"
        assert evidence.metadata_date is None
        assert attempts == ["https://news.example.com/hop.html"]

    def test_probe_public_redirect_followed_and_date_extracted(
            self, monkeypatch):
        # 合法公共跳转正常跟随：第二跳取到可保存 meta 日期
        html = _META_HTML.format(ts="2026-11-05T09:00:00")
        first = self._fake_stream_response(
            "", url="https://news.example.com/hop2.html")
        first.status_code = 302
        first.headers = {"Location": "https://news.example.com/final.html"}
        second = self._fake_stream_response(
            html, url="https://news.example.com/final.html")
        calls: List[str] = []

        def _fake_get(self, method, url, **kwargs):  # noqa: ANN001
            calls.append(url)
            return first if url.endswith("hop2.html") else second

        monkeypatch.setattr("requests.Session.request", _fake_get)
        evidence, failure, final_url = svc._probe_article_date_evidence(
            "https://news.example.com/hop2.html", _budget(),
        )
        assert failure is None
        assert calls == ["https://news.example.com/hop2.html",
                         "https://news.example.com/final.html"]
        assert evidence.metadata_date == date(2026, 11, 5)
        assert evidence.raw_basis is not None

    def test_probe_invalid_source_url_rejected_before_outbound(self, monkeypatch):
        # 决策 3：出站前同一 URL 结构校验（注册前已验，重入防御）
        def _unexpected_get(self, method, url, **kwargs):  # noqa: ANN001
            raise AssertionError("outbound must not happen for invalid source")

        monkeypatch.setattr("requests.Session.request", _unexpected_get)
        evidence, failure, final_url = svc._probe_article_date_evidence(
            "ftp://127.0.0.1/private.html", _budget(),
        )
        assert failure.startswith("source_invalid:")
        assert evidence.metadata_date is None


# ---------------------------------------------------------------------------
# 统一准入路径（fake provider；准入/资格/去重真实）
# ---------------------------------------------------------------------------

def _run_admission(monkeypatch: pytest.MonkeyPatch,
                   rows: List[Dict[str, Any]], *,
                   board_name: str = "白酒Ⅱ",
                   stock_name: str = "贵州茅台",
                   success: bool = True,
                   error_message: Optional[str] = None,
                   probe_results: Optional[Dict[str, Any]] = None,
                   ) -> Dict[str, Any]:
    provider = _FakeProvider(rows, success=success, error_message=error_message)
    _install_provider(monkeypatch, provider)
    if probe_results is not None:
        # 探查终端隔离：URL → HTML 合成（签名 (evidence, failure, final_url)，
        # 合成场景无重定向：final_url == url）；提取逻辑真实
        def _fake_probe(url: str, budget: ReportBudget
                        ) -> Tuple[Any, Optional[str], Optional[str]]:
            html = probe_results.get(url, "")
            return (sector_research.extract_article_date_evidence(html, url),
                    None, url)

        monkeypatch.setattr(svc, "_probe_article_date_evidence", _fake_probe)
    return svc._catalysts_pillar(
        board_name, budget=_budget(), subject_confirmed=True, stock_name=stock_name,
    )


class TestOneAdmissionPath:
    """统一准入：provider-dated 与 article-dated 共享同一事件资格。"""

    def test_normal_provider_date_becomes_catalyst(self, monkeypatch):
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", "https://example.com/news/1",
                    snippet="新规落地", published=today),
        ])
        assert len(out["items"]) == 1
        item = out["items"][0]
        assert item["date_basis"] == "provider_published"
        assert item["published_date"] == today

    def test_normal_article_metadata_date_becomes_catalyst_with_basis(
            self, monkeypatch):
        url = "https://example.com/news/meta-only.html"  # URL 无日期
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", url, snippet="新规落地"),
        ], probe_results={url: _META_HTML.format(
            ts=datetime.now().date().isoformat() + "T09:00:00")})
        assert len(out["items"]) == 1
        item = out["items"][0]
        assert item["date_basis"] == "article_metadata"
        assert item["date_basis_detail"] is not None
        assert "article:published_time" in item["date_basis_detail"]

    def test_url_only_stays_background_not_upgraded(self, monkeypatch):
        # spec FAIL1 反例：URL 含日期 + HTML 无 meta → 保持背景（不升级）
        url = "https://example.com/2026/11/06/baijiu.html"
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", url, snippet="新规落地"),
        ], probe_results={url: _PLAIN_HTML})
        assert out["items"] == []
        assert len(out["background_items"]) == 1
        assert "URL" in (out["background_items"][0]["downgrade_reason"] or "")

    def test_conflicting_url_meta_dates_stay_background(self, monkeypatch):
        url = "https://example.com/2026/11/06/baijiu.html"
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", url, snippet="新规落地"),
        ], probe_results={url: _META_HTML.format(
            ts=(datetime.now().date() - timedelta(days=1)).isoformat() + "T09:00:00")})
        assert out["items"] == []
        reasons = " ".join(
            b["downgrade_reason"] or "" for b in out["background_items"])
        assert "冲突" in reasons

    def test_probe_budget_exhaustion_is_local_not_fatal(self, monkeypatch):
        # contract 4 反例：探查预算耗尽 → 局部终止探查（不传播整报超时），
        # provider-dated 已取得催化保留
        url = "https://example.com/news/no-date.html"
        today = _today().isoformat()
        rows = [
            _result("白酒行业新政策发布A", "https://example.com/news/a.html",
                    published=today),
            _result("白酒行业旧闻待探查", url, snippet="无日期条目"),
        ]

        def _exhausted_probe(url_arg: str, budget: ReportBudget):
            raise svc.BudgetExhausted("取数预算已耗尽")

        provider = _FakeProvider(rows)
        _install_provider(monkeypatch, provider)
        monkeypatch.setattr(svc, "_probe_article_date_evidence", _exhausted_probe)
        out = svc._catalysts_pillar(
            "白酒Ⅱ", budget=_budget(), subject_confirmed=True, stock_name="贵州茅台",
        )
        assert len(out["items"]) == 1  # provider-dated 催化保留
        assert any("探查因取数预算不足提前结束" in g for g in out["gaps"])

    def test_retrospective_old_month_downgraded_on_provider_path(self, monkeypatch):
        # spec FAIL2 反例 A：2026 年 1 月回顾（pub 日期=今天，在窗口内；
        # 事件主题时点早于窗口起始月）→ 降级背景
        today = datetime.now().date().isoformat()
        out = _run_admission(monkeypatch, [
            _result("2026年1月白酒政策回顾", "https://example.com/news/old1",
                    published=today),
        ])
        assert out["items"] == []
        assert len(out["background_items"]) == 1
        assert "历史回顾" in (out["background_items"][0]["downgrade_reason"] or "")

    def test_retrospective_old_month_downgraded_on_article_path(self, monkeypatch):
        # spec FAIL2 反例 B：同回顾 + 今日文章元数据日期 → 升级后重入资格降级
        url = "https://example.com/news/retro.html"
        out = _run_admission(monkeypatch, [
            _result("2026年1月白酒政策回顾", url, snippet="回顾年初政策"),
        ], probe_results={url: _META_HTML.format(
            ts=datetime.now().date().isoformat() + "T09:00:00")})
        assert out["items"] == []
        assert len(out["background_items"]) == 1
        assert "历史回顾" in (out["background_items"][0]["downgrade_reason"] or "")

    def test_retrospective_old_year_stays_background_when_published_today(
            self, monkeypatch):
        # 2020 旧年回顾 + 今日文章元数据日期 → 仍背景
        url = "https://example.com/news/retro-2020.html"
        out = _run_admission(monkeypatch, [
            _result("回顾2020年白酒板块牛市", url, snippet="行情回顾"),
        ], probe_results={url: _META_HTML.format(
            ts=datetime.now().date().isoformat() + "T09:00:00")})
        assert out["items"] == []

    def test_historical_comparison_published_today_is_background_both_paths(
            self, monkeypatch):
        # root 反例（converged 轮）：「白酒行业2020年与2019年销量对比」
        # 今天发布（provider 日期与文章元数据日期两路径）都是历史背景，
        # 不是已证实近期催化——「对比」豁免词表已删除
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒行业2020年与2019年销量对比",
                    "https://example.com/news/cmp", published=today),
        ])
        assert out["items"] == []
        assert len(out["background_items"]) == 1
        assert "历史回顾" in (out["background_items"][0]["downgrade_reason"] or "")
        url = "https://example.com/news/cmp-meta.html"
        out = _run_admission(monkeypatch, [
            _result("白酒行业2020年与2019年销量对比", url, snippet="对比报道"),
        ], probe_results={url: _META_HTML.format(
            ts=datetime.now().date().isoformat() + "T09:00:00")})
        assert out["items"] == []
        assert len(out["background_items"]) == 1

    def test_current_policy_with_old_reference_still_catalyst(self, monkeypatch):
        # 新政策（标题声明最晚年份=当前）参照对象含旧年份 → 不降级：
        # 豁免词表删除不误伤当前事件（声明年份主体判定）
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("2026年白酒新规出台 与2018年版对比", "https://example.com/news/new",
                    published=today),
        ])
        assert len(out["items"]) == 1

    def test_declared_day_earlier_than_window_downgraded_both_paths(
            self, monkeypatch):
        # root 边界反例（边界终态）：标题声明完整日 11-01 早于窗口起始
        # （today-2），发布/文章元数据日期都是今天（窗口内）→ 两条日期
        # 路径都降级背景；声明日不降精度为月（旧实现月=窗口月 → 放行）
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒行业2026年11月1日政策回顾",
                    "https://example.com/news/old-day", published=today),
        ])
        assert out["items"] == []
        assert len(out["background_items"]) == 1
        assert "历史回顾" in (out["background_items"][0]["downgrade_reason"] or "")
        # article-metadata 路径：同标题 + 今日元数据确证 → 升级重入资格降级
        url = "https://example.com/news/old-day-meta.html"
        out = _run_admission(monkeypatch, [
            _result("白酒行业2026年11月1日政策回顾", url, snippet="回顾政策"),
        ], probe_results={url: _META_HTML.format(
            ts=_today().isoformat() + "T09:00:00")})
        assert out["items"] == []
        assert len(out["background_items"]) == 1
        reasons = out["background_items"][0]["downgrade_reason"] or ""
        assert "历史回顾" in reasons

    def test_declared_day_within_window_stays_catalyst_both_paths(
            self, monkeypatch):
        # 正控（边界终态）：窗口内/同日完整日期声明 → 不降级，两路保持催化
        today = _today()
        within = (today - timedelta(days=1)).isoformat()
        out = _run_admission(monkeypatch, [
            _result(f"白酒行业{within.replace('-', '年')}年政策落地发布会",
                    "https://example.com/news/in-window",
                    published=today.isoformat()),
        ])
        assert len(out["items"]) == 1
        url = "https://example.com/news/same-day-meta.html"
        same_day = today.isoformat().replace("-", "年")
        # 同日标题（yyyy 年 mm 月 dd 日）+ 今日文章元数据 → cat-1 升级成功
        y, m, d = today.year, today.month, today.day
        title = f"白酒行业{y}年{m}月{d}日新政落地"
        out = _run_admission(monkeypatch, [
            _result(title, url, snippet="新政发布"),
        ], probe_results={url: _META_HTML.format(
            ts=today.isoformat() + "T09:00:00")})
        assert len(out["items"]) == 1
        assert out["items"][0]["date_basis"] == "article_metadata"

    def test_error_status_probe_failure_keeps_background_safe_reason(
            self, monkeypatch):
        # issue #83 边界终态：探查终端错误状态（article_http_error:*）→
        # 背景保留 + 安全原因（不 500、零日期升级、不冒充真「无新闻」）；
        # provider-dated 催化不受影响。真实 socket 状态检查见
        # test_sector_article_fetch_bounds（200/302/403/404/500/304 全真实）。
        today = _today().isoformat()
        url = "https://example.com/news/http-error.html"
        rows = [
            _result("白酒行业新政策发布A", "https://example.com/news/ok.html",
                    published=today),
            _result("白酒行业旧闻待探查", url, snippet="无日期条目"),
        ]
        provider = _FakeProvider(rows)
        _install_provider(monkeypatch, provider)
        monkeypatch.setattr(
            svc, "_probe_article_date_evidence",
            lambda u, b: (
                sector_research.ArticleDateEvidence(None, None, None),
                "article_http_error:403",
                None,
            ),
        )
        out = svc._catalysts_pillar(
            "白酒Ⅱ", budget=_budget(), subject_confirmed=True,
            stock_name="贵州茅台",
        )
        assert len(out["items"]) == 1  # provider-dated 催化保留
        bg = [b for b in out["background_items"] if b["url"] == url]
        assert len(bg) == 1
        reason = bg[0]["downgrade_reason"] or ""
        assert "文章探查未确证" in reason and "article_http_error" in reason

    def test_nonpublic_and_nonhttp_sources_rejected_locally(self, monkeypatch):
        # root 反例（converged 轮）：ftp/私网 literal 来源在准入入口本地拒绝
        # 计数（不进已证实/背景，不发起探查出站）
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策A", "ftp://news.example.com/a",
                    published=today),
            _result("白酒行业新政策B", "http://127.0.0.1/private.html",
                    published=today),
            _result("白酒行业新政策C", "https://user:pw@news.example.net/c",
                    published=today),
        ])
        assert out["items"] == []
        assert out["background_items"] == []
        assert out["dropped_invalid_source"] == 3

    def test_future_provider_date_excluded(self, monkeypatch):
        future = (_today() + timedelta(days=2)).isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", "https://example.com/news/f",
                    published=future),
        ])
        assert out["items"] == []
        assert out["dropped_offwindow"] == 1

    def test_wrong_industry_dropped(self, monkeypatch):
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("芯片行业重大突破", "https://example.com/news/chip",
                    published=today),
        ])
        assert out["items"] == []
        assert out["dropped_offtopic"] == 1

    def test_stock_only_event_dropped(self, monkeypatch):
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒企业贵州茅台员工运动会圆满举办", "https://example.com/news/sport",
                    published=today),
        ])
        assert out["items"] == []
        assert out["dropped_offtopic"] == 1

    def test_undated_wrong_industry_stays_background_and_offtopic(self, monkeypatch):
        # 错行业 + 无日期：既不进背景也不探查（相关性先于日期分支）
        out = _run_admission(monkeypatch, [
            _result("芯片行业某动态", "https://example.com/news/chip2"),
        ], probe_results={"https://example.com/news/chip2": _PLAIN_HTML})
        assert out["background_items"] == []
        assert out["dropped_offtopic"] == 1


class TestProvenanceRepairGaps:
    """provenance repair 缺口回归（准入层）：终态 URL 管辖日期证据/域名/去重、
    IPv6 解析 hostname、候选文本类型契约（title/snippet 不被 str() 伪合法）。"""

    def _install_probe(self, monkeypatch: pytest.MonkeyPatch,
                       by_url: Dict[str, Tuple[Any, Optional[str], Optional[str]]],
                       ) -> None:
        def _fake_probe(url: str, budget: ReportBudget
                        ) -> Tuple[Any, Optional[str], Optional[str]]:
            return by_url[url]

        monkeypatch.setattr(svc, "_probe_article_date_evidence", _fake_probe)

    @staticmethod
    def _meta_evidence(declared_url: str, meta_date: date) -> Any:
        html = _META_HTML.format(ts=meta_date.isoformat() + "T09:00:00")
        return sector_research.extract_article_date_evidence(html, declared_url)

    def test_redirect_terminal_conflict_stays_background(self, monkeypatch):
        # 根因反例（#2）：original.html→302→/2020/11/06/final.html（终态
        # 自带 2020 声明）→200 meta=今天。终态 URL 声明参与**同一冲突判定**
        # → provenance 冲突保守背景（不得借原始无日期 URL 绕过注册 cat-1）
        original = "https://publisher.example.com/original.html"
        terminal = "https://publisher.example.com/2020/11/06/final.html"
        today = _today()
        ev = self._meta_evidence(terminal, today)
        self._install_probe(monkeypatch, {original: (ev, None, terminal)})
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", original, snippet="新规落地"),
        ])
        assert out["items"] == []
        reasons = " ".join(
            b["downgrade_reason"] or "" for b in out["background_items"])
        assert "冲突" in reasons

    def test_redirect_terminal_consistent_upgrades_with_final_url(
            self, monkeypatch):
        # 一致日期重定向正控：终态 URL 声明 == meta 日期 → 照常升级 cat-1；
        # url 保留原引用身份，final_url 如实记录实际文章地址，source_domain
        # 取终态 host（实际文章域名）
        original = "https://alias.example.com/redirect.html"
        today = _today()
        terminal = (
            f"https://publisher.example.com/dated/{today.isoformat()}"
            "/story.html"
        )
        ev = self._meta_evidence(terminal, today)
        self._install_probe(monkeypatch, {original: (ev, None, terminal)})
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", original, snippet="新规落地"),
        ])
        assert len(out["items"]) == 1
        item = out["items"][0]
        assert item["date_basis"] == "article_metadata"
        assert item["url"] == original
        assert item["final_url"] == terminal
        assert item["source_domain"] == "publisher.example.com"

    def test_same_article_redirect_aliases_not_double_confirmation(
            self, monkeypatch):
        # 根因反例（#2 dedup）：同一实际文章的两个**跨域**重定向 alias
        # （短链域与正文域不同，背景登记互不判重）先后探查，probe 终态
        # 相同 → 按实际文章去重：一个 cat + 一个按实际文章重复计数，
        # 不是两条独立确认
        alias_a = "https://news.example.com/original.html"
        alias_b = "https://s.example.net/original.html"
        today = _today()
        terminal = (
            f"https://publisher.example.com/dated/{today.isoformat()}"
            "/story.html"
        )
        ev = self._meta_evidence(terminal, today)
        self._install_probe(monkeypatch, {
            alias_a: (ev, None, terminal), alias_b: (ev, None, terminal),
        })
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", alias_a, snippet="新规落地"),
            _result("白酒行业新政策发布", alias_b, snippet="新规落地"),
        ])
        assert len(out["items"]) == 1
        assert out["items"][0]["url"] == alias_a
        assert out["items"][0]["final_url"] == terminal
        assert out["dropped_duplicate_url"] == 1

    def test_direct_terminal_url_qualifies_identically(self, monkeypatch):
        # direct/redirect 同资格对照：直接请求同一终态 URL（无重定向）
        # 的资格判定与经重定向到达完全一致（一致日期同样升级）
        terminal = (
            f"https://publisher.example.com/dated/{_today().isoformat()}"
            "/story.html"
        )
        today = _today()
        ev = self._meta_evidence(terminal, today)
        self._install_probe(monkeypatch, {terminal: (ev, None, terminal)})
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", terminal, snippet="新规落地"),
        ])
        assert len(out["items"]) == 1
        item = out["items"][0]
        assert item["date_basis"] == "article_metadata"
        assert item["url"] == terminal
        assert item["final_url"] == terminal

    def test_distinct_ipv6_hosts_same_title_not_merged(self, monkeypatch):
        # 根因反例（#3）：两个不同公网 IPv6 literal host、同题、无转载标记
        # → 按解析后的完整 hostname 判定不同源，各自独立注册（修复前按
        # '[2606' 伪同域 false-merge 成 dropped_syndicated）
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布",
                    "https://[2606:4700:4700::1111]/a", published=today),
            _result("白酒行业新政策发布",
                    "https://[2606:4700:4700::1001]/b", published=today),
        ])
        assert len(out["items"]) == 2
        assert out["items"][0]["source_domain"] == "2606:4700:4700::1111"
        assert out["items"][1]["source_domain"] == "2606:4700:4700::1001"
        assert out["dropped_syndicated"] == 0

    def test_provider_dated_item_has_no_final_url(self, monkeypatch):
        # provider 口径不做文章探查：final_url=None（可选默认字段的缺省语义）
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", "https://example.com/news/1",
                    snippet="新规落地", published=today),
        ])
        assert len(out["items"]) == 1
        assert out["items"][0]["final_url"] is None

    def test_malformed_title_type_rejected_valid_rows_kept(self, monkeypatch):
        # 根因反例（#4）：真实 MiniMax JSON title=dict 曾被 str() 字符串化
        # 成假标题登记 cat-1 → 单一准入入口类型契约本地拒绝计数；剩余有效
        # 行照常准入；请求不 500
        today = _today().isoformat()
        rows = [
            {"title": {"n": "假标题"}, "snippet": "白酒行业动态",
             "url": "https://example.com/bad", "source": "s",
             "published_date": today},
            _result("白酒行业新政策发布", "https://example.com/good",
                    published=today),
        ]
        out = _run_admission(monkeypatch, rows)
        assert out["dropped_invalid_field"] == 1
        assert len(out["items"]) == 1
        assert out["items"][0]["url"] == "https://example.com/good"

    def test_malformed_snippet_and_nonstring_title_rejected_counted(
            self, monkeypatch):
        # snippet 可选：出现即必须是 str（dict/bytes/数字不得伪合法成摘要）；
        # title 数字/空白同样畸形行拒绝——全部本地计数，不 str() 补救
        today = _today().isoformat()
        rows = [
            {"title": "白酒行业动态A", "snippet": {"s": 1},
             "url": "https://example.com/s1", "source": "s",
             "published_date": today},
            {"title": "白酒行业动态B", "snippet": b"raw-bytes",
             "url": "https://example.com/s2", "source": "s",
             "published_date": today},
            {"title": 123, "snippet": "白酒行业动态C",
             "url": "https://example.com/s3", "source": "s",
             "published_date": today},
            {"title": "   ", "snippet": "白酒行业动态D",
             "url": "https://example.com/s4", "source": "s",
             "published_date": today},
        ]
        out = _run_admission(monkeypatch, rows)
        assert out["dropped_invalid_field"] == 4
        assert out["items"] == []

    def test_missing_optional_snippet_still_admitted(self, monkeypatch):
        # 缺失可选 snippet 不是错误：title/url 有效的行照常准入（snippet
        # 为 None 缺字段语义；缺失可选文本不产生 500）
        today = _today().isoformat()
        rows = [{"title": "白酒行业新政策发布", "snippet": None,
                 "url": "https://example.com/no-snippet", "source": "s",
                 "published_date": today}]
        out = _run_admission(monkeypatch, rows)
        assert len(out["items"]) == 1
        assert out["items"][0]["snippet"] is None
        assert out["dropped_invalid_field"] == 0


class TestSourceContractConvergence:
    """source-contract convergence 缺口回归（准入层）：完整来源身份（长域名
    不截断/超限本地拒绝）、背景终态 provenance（冲突/别名/安全失败背景保留
    实际文章地址与域名）、去重身份与展示 provenance 一致。"""

    def _install_probe(self, monkeypatch: pytest.MonkeyPatch,
                       by_url: Dict[str, Tuple[Any, Optional[str], Optional[str]]],
                       ) -> None:
        def _fake_probe(url: str, budget: ReportBudget
                        ) -> Tuple[Any, Optional[str], Optional[str]]:
            return by_url[url]

        monkeypatch.setattr(svc, "_probe_article_date_evidence", _fake_probe)

    @staticmethod
    def _meta_evidence(declared_url: str, meta_date: date) -> Any:
        html = _META_HTML.format(ts=meta_date.isoformat() + "T09:00:00")
        return sector_research.extract_article_date_evidence(html, declared_url)

    # ---- 缺口 1：完整来源身份 ----

    def test_long_distinct_hostnames_not_collapsed_as_syndicated(
            self, monkeypatch):
        # root standards 反例：两个合法 131 字符 hostname（各 label ≤63、
        # 共享前 120 字符、仅 one/two 标签不同）曾被 host[:120] 截断成
        # 同一伪域名 → false dropped_syndicated=1、存档域名是不存在的截断串；
        # 完整身份下两源独立注册，source_domain 为完整真实 hostname
        prefix = "a" * 60 + "." + "b" * 58  # 119 字符共享前缀
        host_one = f"{prefix}.one.example"
        host_two = f"{prefix}.two.example"
        assert len(host_one) == 131 and len(host_two) == 131
        assert host_one[:120] == host_two[:120] and host_one != host_two
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", f"https://{host_one}/a", published=today),
            _result("白酒行业新政策发布", f"https://{host_two}/b", published=today),
        ])
        assert len(out["items"]) == 2
        assert {i["source_domain"] for i in out["items"]} == {host_one, host_two}
        assert out["dropped_syndicated"] == 0

    def test_overlong_host_rejected_locally_not_truncated(self, monkeypatch):
        # 共享 URL 准入边界：hostname > 253（DNS 全名上限）本地拒绝计数，
        # 绝不截断后当身份，也不等最终严格校验炸 500
        overlong = ".".join(["d" * 60, "e" * 60, "f" * 60, "g" * 60,
                             "example.com"])
        assert len(overlong) == 255
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", f"https://{overlong}/a",
                    published=today),
        ])
        assert out["items"] == []
        assert out["dropped_invalid_source"] == 1

    def test_boundary_253_hostname_accepted_full_identity(self, monkeypatch):
        # 边界正控：恰好 253 字符的合法 hostname（各 label ≤63）完整接受
        # （schema 上限一致）
        host = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 49,
                         "example.com"])
        assert len(host) == 253
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", f"https://{host}/a", published=today),
        ])
        assert len(out["items"]) == 1
        assert out["items"][0]["source_domain"] == host

    # ---- 缺口 2：背景终态 provenance ----

    def test_conflict_background_retains_terminal_provenance(self, monkeypatch):
        # root 反例：redirect 冲突正确降背景，但背景只剩 original 域名，
        # 无法得知哪个实际 URL 冲突；契约：终态已知即保留 final_url +
        # 实际 hostname（source_domain=终态 host），url 保留原引用
        original = "https://news.example.com/current.html"
        terminal = "https://publisher.example.net/2020/11/06/final.html"
        ev = self._meta_evidence(terminal, _today())
        self._install_probe(monkeypatch, {original: (ev, None, terminal)})
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", original, snippet="新规落地"),
        ])
        assert out["items"] == []
        assert len(out["background_items"]) == 1
        bg = out["background_items"][0]
        assert bg["url"] == original
        assert bg["final_url"] == terminal
        assert bg["source_domain"] == "publisher.example.net"
        assert "冲突" in (bg["downgrade_reason"] or "")

    def test_duplicate_alias_background_retains_terminal_provenance(
            self, monkeypatch):
        # 同文 alias：alias_a 升级为 cat（终态键），alias_b 重键撞到同一
        # 实际文章 → 按重复计数；重复背景保留终态地址与实际域名
        alias_a = "https://news.example.com/original.html"
        alias_b = "https://s.example.net/original.html"
        today = _today()
        terminal = (
            f"https://publisher.example.com/dated/{today.isoformat()}"
            "/story.html"
        )
        ev = self._meta_evidence(terminal, today)
        self._install_probe(monkeypatch, {
            alias_a: (ev, None, terminal), alias_b: (ev, None, terminal),
        })
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", alias_a, snippet="新规落地"),
            _result("白酒行业新政策发布", alias_b, snippet="新规落地"),
        ])
        assert len(out["items"]) == 1
        assert out["items"][0]["url"] == alias_a
        assert out["dropped_duplicate_url"] == 1
        bg = [b for b in out["background_items"] if b["url"] == alias_b]
        assert len(bg) == 1
        assert bg[0]["final_url"] == terminal
        assert bg[0]["source_domain"] == "publisher.example.com"

    def test_http_error_background_keeps_known_terminal(self, monkeypatch):
        # 到达终态的安全失败（非 2xx）：背景保留安全原因 + 终态地址/域名
        # （去重身份与展示 provenance 一致——重键到实际文章）
        original = "https://news.example.com/gone.html"
        terminal = "https://publisher.example.net/moved/final.html"
        self._install_probe(monkeypatch, {
            original: (sector_research.ArticleDateEvidence(None, None, None),
                       "article_http_error:403", terminal),
        })
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", original, snippet="新规落地"),
        ])
        assert out["items"] == []
        bg = out["background_items"][0]
        assert bg["final_url"] == terminal
        assert bg["source_domain"] == "publisher.example.net"
        assert "article_http_error" in (bg["downgrade_reason"] or "")

    def test_transport_failure_background_has_no_terminal(self, monkeypatch):
        # 终态未到达（传输失败）：final_url=None（不猜终态）、原注册键/域名
        # 保持；安全原因照常
        original = "https://news.example.com/down.html"
        self._install_probe(monkeypatch, {
            original: (sector_research.ArticleDateEvidence(None, None, None),
                       "transport", None),
        })
        out = _run_admission(monkeypatch, [
            _result("白酒行业新政策发布", original, snippet="新规落地"),
        ])
        assert out["items"] == []
        bg = out["background_items"][0]
        assert bg["final_url"] is None
        assert bg["source_domain"] == "news.example.com"
        assert "传输失败" in (bg["downgrade_reason"] or "")

    def test_provider_dated_backgrounds_have_final_url_none(self, monkeypatch):
        # provider 口径背景（历史回顾）不走探查：final_url=None（默认值
        # 显式携带，与公开载荷/详情读回一致）
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("2026年1月白酒政策回顾", "https://example.com/old",
                    published=today),
        ])
        assert out["items"] == []
        assert out["background_items"][0]["final_url"] is None


class TestSyndicationMerge:
    """跨域同源转载 = 同一事件（两条日期路径）。"""

    _TITLE = "白酒行业消费新趋势报告出台"

    def test_provider_dated_cross_domain_syndication_merged(self, monkeypatch):
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result(self._TITLE, "https://xinhua.example.com/a",
                    snippet="来源：新华社", source="xinhua.example.com",
                    published=today),
            _result(self._TITLE, "https://finance.example.net/b",
                    snippet="转载自新华社报道", source="finance.example.net",
                    published=today),
        ])
        assert len(out["items"]) == 1
        assert out["items"][0]["same_event_urls"] == ["https://finance.example.net/b"]
        assert out["dropped_syndicated"] == 1

    def test_article_dated_cross_domain_syndication_merged(self, monkeypatch):
        url_a = "https://xinhua.example.com/c"
        url_b = "https://finance.example.net/d"
        probe = {
            url_a: _META_HTML.format(
                ts=datetime.now().date().isoformat() + "T08:00:00"),
            url_b: _META_HTML.format(
                ts=datetime.now().date().isoformat() + "T09:00:00"),
        }
        out = _run_admission(monkeypatch, [
            _result(self._TITLE, url_a, snippet="来源：新华社"),
            _result(self._TITLE, url_b, snippet="转载自新华社报道"),
        ], probe_results=probe)
        assert len(out["items"]) == 1
        assert out["dropped_syndicated"] == 1

    def test_cross_domain_same_title_without_mark_not_merged(self, monkeypatch):
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result(self._TITLE, "https://a.example.com/1", published=today),
            _result(self._TITLE, "https://b.example.com/2", published=today),
        ])
        # 无转载标记：保守不合并；display cap 内第二条独立保留
        assert out["dropped_syndicated"] == 0


class TestProviderFailureSemantics:
    """显式失败响应 ≠「无新闻」；诊断 sanitize。"""

    def test_failed_search_response_not_silent_no_news(self, monkeypatch):
        out = _run_admission(monkeypatch, [], success=False,
                             error_message="HTTP 403")
        assert out["items"] == []
        assert any("返回失败" in g for g in out["gaps"])
        assert any("不冒充" in g or "失败" in g for g in out["gaps"])

    def test_synthetic_bearer_leak_sanitized(self, monkeypatch):
        # contract 5 反例：403 + synthetic Authorization/Bearer 诊断
        leak = 'HTTP 403: auth failed for "Authorization: Bearer sk-abc123secret"'
        out = _run_admission(monkeypatch, [], success=False, error_message=leak)
        joined = " ".join(out["gaps"])
        assert "sk-abc123secret" not in joined
        assert "Bearer" not in joined or "redacted" in joined

    def test_all_results_dropped_no_fake_news(self, monkeypatch):
        # 真实无窗口内新闻（全部被过滤）：如实「无已证实」，不补造
        today = _today().isoformat()
        out = _run_admission(monkeypatch, [
            _result("白酒企业贵州茅台员工运动会", "https://example.com/s",
                    published=today),
        ])
        assert out["items"] == []
        assert any("无已证实近期催化" in g for g in out["gaps"])

    def test_invalid_source_local_rejection(self, monkeypatch):
        # 空 URL 条目在准入循环入口直接跳过（不计 invalid）；超长 URL 本地拒绝
        out = _run_admission(monkeypatch, [
            _result("白酒行业新闻2", "https://" + "x" * 600,
                    published=_today().isoformat()),
        ])
        assert out["items"] == []
        assert out["dropped_invalid_source"] == 1


class TestEligibilityAndSharedFilters:
    """资格前置与共享过滤不变。"""

    def test_unconfirmed_identity_skips_search(self, monkeypatch):
        provider = _FakeProvider([_result("白酒新政策", "https://a.com/1",
                                          published=_today().isoformat())])
        _install_provider(monkeypatch, provider)
        out = svc._catalysts_pillar(
            "白酒Ⅱ", budget=_budget(), subject_confirmed=False, stock_name="贵州茅台",
        )
        assert out["eligible"] is False
        assert provider.calls == []  # 未出站

    def test_shared_news_window_used(self, monkeypatch):
        from src.search_service import get_search_service

        provider = _FakeProvider([])
        _install_provider(monkeypatch, provider)
        svc._catalysts_pillar(
            "白酒Ⅱ", budget=_budget(), subject_confirmed=True, stock_name="贵州茅台",
        )
        search_svc = get_search_service()
        out = svc._catalysts_pillar(
            "白酒Ⅱ", budget=_budget(), subject_confirmed=True, stock_name="贵州茅台",
        )
        # 窗口来自共享配置（news_window_days），不另设假设多窗口
        assert out["window_days"] == int(search_svc.news_window_days)
        assert out["window_end"] == _today().isoformat()

    def test_search_budget_exhausted_returns_gap_not_raise(self, monkeypatch):
        provider = _FakeProvider([_result("白酒新政策", "https://a.com/1",
                                          published=_today().isoformat())])
        _install_provider(monkeypatch, provider)
        # 预算已在 fetch 边界过期 → 直接缺口返回，不向上传播
        budget = ReportBudget(1.0, finish_margin=0.9)
        import time as _time
        _time.sleep(0.15)  # 取数预算（0.1s）过期
        out = svc._catalysts_pillar(
            "白酒Ⅱ", budget=budget, subject_confirmed=True, stock_name="贵州茅台",
        )
        assert out["items"] == []
        assert any("取数预算不足" in g for g in out["gaps"])
