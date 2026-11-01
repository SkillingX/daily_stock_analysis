# -*- coding: utf-8 -*-
"""issue #71：iFinD 主源当前数据迁移的离线回归。

供应商样本只有 root 于 2026-11-01 对 688183 采集的六份真实探针回执
（tests/fixtures/ifind_*_688183_*.json，已脱敏：财务/估值/带日期批次/无日期
批次/行业/资金）；全部断言经生产解析/资格/消费层，不预构建合格字典、不 mock
解析与资格层。P1 已获用户批准（2026-11-01）：四比率按 iFinD 公布值参与单源
财务分析——本文件锁定该采纳的诚实标注契约：field_meta 保留 adjustment_unknown
披露标签、selection_reason=ifind_published_single_source、rule_eligible=True
（批准的规则使用）、质量仍由核验证据承载（不伪装 verified）、交叉核验证据仍
not_comparable、不与其他来源混算。

F（storage schema-only）与 G（资金采纳证据）组件及专属测试由独立 worker
拥有（tests/test_fundamental_snapshot_legacy_init.py、
tests/test_capital_adoption_evidence_contract.py），不在本文件。
"""
from __future__ import annotations

import json
import time
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict
from zoneinfo import ZoneInfo

import pytest

from tests.test_choice_integration import choice_transport
from tests.test_model_concurrency import provider  # noqa: F401  （假 LLM 服务器：隔离模型）
from data_provider.base import DataFetcherManager
from data_provider.capital_flow_provider import get_main_inflow_cumulative
from data_provider.choice_adapter import ChoiceAdapter
from data_provider.cross_source_validator import AnchorReading
from data_provider.ifind_fundamental_adapter import (
    IfindFetcher,
    _BALANCE_FIELD_KEYWORDS,
    _ifind_data,
    _parse_ifind_response,
    _parse_industry,
)
from src.config import Config
from src.core.research_run import ResearchRun
from src.deep_research_dims.context import industry_hint_from_research

FIXTURES = Path(__file__).parent / "fixtures"

FINANCIAL_ENV = json.loads((FIXTURES / "ifind_financials_688183_probe.json").read_text())
VALUATION_ENV = json.loads((FIXTURES / "ifind_current_info_688183_probe.json").read_text())
DATED_INFO_ENV = json.loads((FIXTURES / "ifind_current_info_688183_dated.json").read_text())
UNDATED_INFO_ENV = json.loads((FIXTURES / "ifind_current_info_688183_undated.json").read_text())
INDUSTRY_ENV = json.loads((FIXTURES / "ifind_industry_688183_probe.json").read_text())
FLOW_ENV = json.loads((FIXTURES / "ifind_flow_688183_probe.json").read_text())

_FLOW_NOW = datetime(2026, 11, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def probe_transport(choice_transport, monkeypatch):
    """choice 策略 + iFinD 主源；iFinD 按 tool 路由真实回执，Choice 全部 10001009。"""
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    payloads = {
        "get_stock_financials": FINANCIAL_ENV,
        "get_stock_info": DATED_INFO_ENV,
        "get_stock_performance": FLOW_ENV,
    }
    from datetime import date as _date
    from src.core import trading_calendar as _tc
    _sessions = [date(2026, 10, 30), date(2026, 10, 29), date(2026, 10, 28), date(2026, 10, 27),
                 date(2026, 10, 26), date(2026, 10, 23), date(2026, 10, 22), date(2026, 10, 21),
                 date(2026, 10, 20), date(2026, 10, 19), date(2026, 10, 16)]
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: _sessions[:max(0, min(count, len(_sessions)))])
    calls: Dict[str, list] = {"ifind": [], "choice": []}

    async def query(self, tool, question, *, deadline=None):
        calls["ifind"].append((tool, question))
        return deepcopy(payloads[tool])

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )

    def native(method, args, options, deadline):
        calls["choice"].append((method, args[1] if len(args) > 1 else args[0]))
        return {
            "error": "authentication_failed", "native_code": 10001009,
            "error_text": "login count up to limit", "stage": "login",
            "codes": [], "indicators": [], "dates": [], "data": {},
        }

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    return calls


# ---------------------------------------------------------------- iFinD 解析层

def test_financial_probe_parses_six_fields_with_honest_ratio_gaps():
    raw = json.dumps({"code": 1, "data": FINANCIAL_ENV["data"]}, ensure_ascii=False)
    for field in ("revenue", "net_profit", "roe", "gross_margin", "revenue_yoy", "net_profit_yoy"):
        reading = _parse_ifind_response(
            raw, _keywords(field), field, None, code="688183", a_share_contract=True,
        )
        if field in {"revenue", "net_profit"}:
            assert reading is not None and reading.adjustment == "restated"
        else:
            # 四比率：供应商未回显调整口径 → adjustment=None（真实回执事实）
            assert reading is not None and reading.adjustment is None


def _keywords(field: str) -> list:
    from data_provider.ifind_fundamental_adapter import _IFIND_ANCHOR_QUERIES
    return _IFIND_ANCHOR_QUERIES[field][2]


def test_balance_probe_parses_same_period_consolidated_restated():
    raw = json.dumps({"code": 1, "data": FINANCIAL_ENV["data"]}, ensure_ascii=False)
    for field, expected in (("assets", 18450820000.0), ("liabilities", 8612290000.0)):
        reading = _parse_ifind_response(
            raw, _BALANCE_FIELD_KEYWORDS[field], field, None, code="688183", a_share_contract=True,
        )
        assert reading is not None and reading.value == expected
        assert reading.period == "2026-09-30"
        assert reading.unit == "currency_base" and reading.currency == "CNY"
        assert reading.statement_scope == "consolidated" and reading.adjustment == "restated"
        assert reading.caliber == "balance_sheet_total"


def test_balance_mixed_currency_is_rejected_not_relabelled():
    from data_provider.base import _qualified_ifind_balance
    from data_provider.cross_source_validator import AnchorReading
    good = AnchorReading("ifind", 18450820000.0, period="2026-09-30", unit="currency_base",
                         currency="CNY", statement_scope="consolidated", adjustment="restated")
    usd = AnchorReading("ifind", 8612290000.0, period="2026-09-30", unit="currency_base",
                        currency="USD", statement_scope="consolidated", adjustment="restated")
    assert _qualified_ifind_balance({"assets": usd, "liabilities": good}, "2026-09-30") is None
    assembled = _qualified_ifind_balance({"assets": good, "liabilities": good}, "2026-09-30")
    assert assembled is not None and assembled["currency"] == "CNY"


def test_industry_conflicting_rows_rejected_not_first_row_wins():
    extra = "|688183.SH|生益电子|机械设备|S28|机械设备|X|Y|\n"
    env = deepcopy(INDUSTRY_ENV)
    env["data"]["answer"] = INDUSTRY_ENV["data"]["answer"] + "\n" + extra
    industry = _parse_industry(_ifind_data(env), "688183")
    assert industry["name"] is None
    assert "duplicate_industry_conflict" in industry["errors"]


def test_industry_missing_level_or_date_is_disclosed():
    env = deepcopy(INDUSTRY_ENV)
    env["data"]["indicators_params"] = {
        key: {k: v for k, v in meta.items() if k not in {"行业级别", "截止日期"}}
        for key, meta in INDUSTRY_ENV["data"]["indicators_params"].items()
    }
    industry = _parse_industry(_ifind_data(env), "688183")
    assert industry["name"] == "电子"
    assert industry["level"] is None and industry["as_of"] is None
    assert "industry_metadata_missing" in industry["errors"]


def test_industry_probe_keeps_confirmed_metadata_only():
    data = _ifind_data(INDUSTRY_ENV)
    industry = _parse_industry(data, "688183")
    assert industry == {
        "source": "ifind", "system": "sw", "classification_version": None,
        "level": "一级行业", "code": "S27", "name": "电子",
        "as_of": "2026-10-30", "errors": [],
    }
    # 估值载荷无申万列：不消费所属同花顺行业，行业如实缺席
    missing = _parse_industry(_ifind_data(VALUATION_ENV), "688183")
    assert missing["name"] is None and "no_sw_industry" in missing["errors"]


def test_undated_combined_reply_is_not_qualified_and_reason_is_kept(monkeypatch):
    """「最新」字面日期回执：候选不得合格，行业元数据缺失原因保留。"""
    from data_provider.cross_source_validator import reading_input_reasons
    from datetime import date as _date
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 10, 30), date(2026, 10, 29)][:max(0, min(count, 2))])
    async def query(self, tool, question, *, deadline=None):
        return deepcopy(UNDATED_INFO_ENV)
    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )
    fetcher = IfindFetcher("https://fixture.invalid/mcp", "test-only")
    info = fetcher.current_info_snapshot("688183", deadline=None)
    assert info["requested_date"] == "2026-10-30"  # 请求日期来自严格日历
    from data_provider.cross_source_validator import normalize_report_period, reading_input_reasons
    for field, reading in info["valuation"].items():
        # 供应商只回显字面「最新」：不可证明的观测时间在源读数上持久标记
        # time_unknown，所有消费层经同一资格门得到一致拒绝
        assert normalize_report_period(reading.observed_at)[0] is None, field
        assert "time_unknown" in reading_input_reasons(reading, field), field
    industry = info["industry"]
    assert industry["name"] == "电子" and industry["as_of"] is None
    assert "industry_metadata_missing" in industry["errors"]


@pytest.mark.parametrize("mutated,expected_reason", [
    ("20200103", "stale"),      # 明显过期：不得当当前主值
    ("20990102", "time_in_future"),  # 未来日期：不得当当前主值
    ("20261029", None),          # T-1：窗口内合格
])
def test_valuation_observation_window_current_contract(monkeypatch, mutated, expected_reason):
    """root 时间窗反例：估值实际观测日期须落在严格日历 T/T-1 会话窗口内。

    日历时钟冻结于夹具采集时点（2026-11-01），与相邻 undated/PB 用例一致，
    保证回归确定性；不削弱生产新鲜度语义。
    """
    from datetime import date as _date
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 10, 30), date(2026, 10, 29)][:max(0, min(count, 2))])
    from data_provider.cross_source_validator import reading_input_reasons
    env = deepcopy(DATED_INFO_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace("|20261030|", f"|{mutated}|")

    async def query(self, tool, question, *, deadline=None):
        return deepcopy(env)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    fetcher = IfindFetcher("https://fixture.invalid/mcp", "test-only")
    info = fetcher.current_info_snapshot("688183", deadline=None)
    assert info["requested_date"] == "2026-10-30"
    for field, reading in info["valuation"].items():
        if expected_reason is None:
            assert reading.normalization_error is None, field
            assert not reading_input_reasons(reading, field), field
        else:
            assert expected_reason in reading_input_reasons(reading, field), field


def test_valuation_entirely_missing_dates_marked_time_unknown(monkeypatch):
    """root 重放反例：行日期与参数日期全部缺席时，四个估值读数都不得恢复资格。"""
    from data_provider.cross_source_validator import reading_input_reasons
    env = deepcopy(DATED_INFO_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace("|20261030|", "||")
    env["data"]["indicators_params"] = {
        key: {k: v for k, v in meta.items() if k not in {"交易日期", "截止日期"}}
        for key, meta in DATED_INFO_ENV["data"]["indicators_params"].items()
    }

    from datetime import date as _date
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 10, 30), date(2026, 10, 29)][:max(0, min(count, 2))])
    async def query(self, tool, question, *, deadline=None):
        return deepcopy(env)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    fetcher = IfindFetcher("https://fixture.invalid/mcp", "test-only")
    info = fetcher.current_info_snapshot("688183", deadline=None)
    assert len(info["valuation"]) >= 3
    for field, reading in info["valuation"].items():
        assert reading.observed_at is None, field
        assert "time_unknown" in reading_input_reasons(reading, field), field
    assert "industry_metadata_missing" in info["industry"]["errors"]


def test_undated_ifind_candidates_do_not_overwrite_qualified_choice(choice_transport, monkeypatch):
    """iFinD 候选不合格（日期不可证明）时不得覆盖健康 Choice 主值。"""
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    payloads = {
        "get_stock_financials": FINANCIAL_ENV,
        "get_stock_info": UNDATED_INFO_ENV,
        "get_stock_performance": FLOW_ENV,
    }

    async def query(self, tool, question, *, deadline=None):
        return deepcopy(payloads[tool])

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )

    def native(method, args, options, deadline):
        indicators = args[1] if len(args) > 1 else ""
        if method == "tradedates":
            return {"data": ["2026-10-30"]}
        if indicators == "NAME,LATESTREPORTNEW":
            return {"codes": ["688183.SH"], "indicators": ["NAME", "LATESTREPORTNEW"],
                    "dates": [], "data": {"688183.SH": ["生益电子", "2026/09/30"]}}
        if indicators == "PETTM,PB,MV":
            return {"codes": ["688183.SH"], "indicators": ["PETTM", "PB", "MV"],
                    "dates": [], "data": {"688183.SH": [11.0, 2.5, 56200860000.0]}}
        if indicators == "SUMASSETNEW,SUMLIABNEW,ACTUALISSUINGDATE":
            return {"codes": ["688183.SH"], "indicators": ["SUMASSETNEW", "SUMLIABNEW", "ACTUALISSUINGDATE"],
                    "dates": [], "data": {"688183.SH": [25563100000.0, 12800000000.0, "2026/10/31"]}}
        if indicators.startswith("RATINGTARGET"):
            return {"codes": ["688183.SH"], "indicators": indicators.split(","),
                    "dates": [], "data": {"688183.SH": [None, None, None]}}
        return {"codes": [args[0]], "indicators": indicators.split(","),
                "dates": [], "data": {args[0]: [None for _ in indicators.split(",")]}}

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    valuation = result["valuation"]["data"]
    # 健康 Choice 主值原样保留，未被未合格的 iFinD 候选覆盖
    assert valuation["pe_ratio"] == 11.0 and valuation["pb_ratio"] == 2.5
    assert valuation["total_mv"] == 56200860000.0
    assert result["valuation"]["field_meta"]["pe_ratio"]["source"] == "choice"
    # 行业不合格原因保留在诊断，不静默丢弃
    assert any("ifind:industry:industry_metadata_missing" in item for item in result["research"]["errors"])


def test_pb_latest_requires_basis_date_evidence_p2_gate(monkeypatch):
    from datetime import date as _date
    from src.core import trading_calendar as _tc
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [date(2026, 10, 30), date(2026, 10, 29)][:max(0, min(count, 2))])
    async def query(self, tool, question, *, deadline=None):
        return deepcopy(DATED_INFO_ENV)
    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    fetcher = IfindFetcher("https://fixture.invalid/mcp", "test-only")
    info = fetcher.current_info_snapshot("688183", deadline=None)
    assert info["valuation"]["pb_ratio"].caliber == "MRQ"
    assert info["valuation"]["pe_ratio"].caliber == "TTM"
    negative = deepcopy(DATED_INFO_ENV)
    negative["data"]["indicators_params"].pop("市净率(PB,最新)")
    raw = json.dumps({"code": 1, "data": negative["data"]}, ensure_ascii=False)
    reading = _parse_ifind_response(
        raw, _keywords("pb_ratio"), "pb_ratio", None, code="688183", a_share_contract=True,
    )
    assert reading is not None and reading.caliber is None
    from data_provider.cross_source_validator import reading_input_reasons
    assert "caliber_unknown" in reading_input_reasons(reading, "pb_ratio")


# ------------------------------------------------------------- 资金窗口（D）

def _flow_config(monkeypatch):
    config = Config(data_source_profile="choice", deep_research_cross_validate=False)
    config.enable_ifind_mcp = True
    config.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    config.ifind_mcp_token = "test-only"
    config.financial_primary_source = "ifind"
    monkeypatch.setattr(Config, "_instance", config)
    async def query(self, tool, question, *, deadline=None):
        return deepcopy(FLOW_ENV)
    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )
    # 测试永不出站：Choice 原生层一律按有界失败处理，禁止真实 SDK/网络
    def offline_native(method, args, options, deadline):
        return {
            "error": "authentication_failed", "native_code": 10001009,
            "error_text": "login count up to limit", "stage": "login",
            "codes": [], "indicators": [], "dates": [], "data": {},
        }
    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(offline_native))
    return config


def test_ifind_flow_windows_match_root_oracle(monkeypatch):
    _flow_config(monkeypatch)
    result = get_main_inflow_cumulative("688183", current_time=_FLOW_NOW)
    assert result["source"] == "ifind"
    assert result["as_of_date"] == "2026-10-30"
    assert result["main_net_inflow"] == pytest.approx(-114551026.71)
    assert result["inflow_5d"] == pytest.approx(-420930098.61)
    assert result["inflow_10d"] == pytest.approx(-732350631.35)
    dates = [row["date"] for row in result["daily_series"]]
    assert len(dates) == 11 and "2026-10-30" in dates
    # 周末空行（10/17、10/18、10/24、10/25）不成为数据行
    assert not {"2026-10-17", "2026-10-18", "2026-10-24", "2026-10-25"} & set(dates)
    by_date = {row["date"]: row["value"] for row in result["daily_series"]}
    assert by_date["2026-10-21"] == pytest.approx(28139236.0)  # 2813.9236万 只换算一次
    assert by_date["2026-10-16"] == pytest.approx(16690420.0)
    assert result["status"] == "ok"
    assert result["field_meta"]["main_inflow"]["source"] == "ifind"
    assert result["field_meta"]["main_inflow"]["rule_eligible"] is True


def _mutated_flow_env(**mutations: Dict[int, str]) -> Dict[str, Any]:
    env = deepcopy(FLOW_ENV)
    lines = env["data"]["answer"].splitlines()
    for index, line in enumerate(lines):
        for date_text, replacement in mutations.items():
            if date_text in line:
                cells = line.split("|")
                cells[-2] = replacement
                lines[index] = "|".join(cells)
    env["data"]["answer"] = "\n".join(lines)
    return env


def _flow_with(env, monkeypatch):
    async def query(self, tool, question, *, deadline=None):
        return deepcopy(env)
    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )


def test_ifind_flow_gap_is_disclosed_not_filled(monkeypatch):
    _flow_config(monkeypatch)
    _flow_with(_mutated_flow_env(**{"20261022": "\t"}), monkeypatch)
    result = get_main_inflow_cumulative("688183", current_time=_FLOW_NOW)
    # 10/22 属近10交易日窗口：缺数即不完整，1/5日窗口不受影响
    assert result["inflow_10d"] is None
    assert result["inflow_5d"] == pytest.approx(-420930098.61)
    assert result["coverage"]["10"]["missing_dates"] == ["2026-10-22"]
    assert "ifind:window_10_incomplete" in result["errors"]


def test_ifind_flow_t1_pending_day_is_disclosed(monkeypatch):
    _flow_config(monkeypatch)
    _flow_with(_mutated_flow_env(**{"20261030": "\t"}), monkeypatch)
    result = get_main_inflow_cumulative("688183", current_time=_FLOW_NOW)
    assert result["as_of_date"] == "2026-10-29"
    assert result["main_net_inflow"] == pytest.approx(-96340438.54)
    assert result["coverage"]["pending_day"] == {"expected_date": "2026-10-30", "observed": False}
    assert "资金窗口尚未完整" in (result.get("note") or "")


def test_flow_missing_security_column_rejected(monkeypatch):
    """root 反例：无证券代码列的逐日表不能证明行归属，整体拒绝。"""
    _flow_config(monkeypatch)
    env = deepcopy(FLOW_ENV)
    lines = env["data"]["answer"].splitlines()
    env["data"]["answer"] = "\n".join(
        line for line in lines if "证券代码" not in line
    )
    _flow_with(env, monkeypatch)
    result = get_main_inflow_cumulative("688183", current_time=_FLOW_NOW)
    assert result["status"] == "failed"
    assert any("ifind:invalid_response" in item for item in result["errors"])
    assert result["main_net_inflow"] is None


@pytest.mark.parametrize("rolling_label", ["近5日主力净流入额", "近20日主力净流入额"])
def test_flow_rolling_caliber_rejected_as_daily_series(monkeypatch, rolling_label):
    """root 反例：近N日累计口径（正向身份不匹配）不得当逐日序列进入窗口合计。"""
    _flow_config(monkeypatch)
    env = deepcopy(FLOW_ENV)
    env["data"]["answer"] = env["data"]["answer"].replace("主力净流入额", rolling_label)
    env["data"]["indicators_params"] = {key.replace("主力净流入额", rolling_label): meta
                                        for key, meta in FLOW_ENV["data"]["indicators_params"].items()}
    _flow_with(env, monkeypatch)
    result = get_main_inflow_cumulative("688183", current_time=_FLOW_NOW)
    assert result["status"] == "failed"
    assert any("ifind:invalid_response" in item for item in result["errors"])
    assert result["inflow_5d"] is None


def test_ifind_flow_unavailable_calendar_never_falls_back_to_natural_days(monkeypatch):
    _flow_config(monkeypatch)
    from src.core import trading_calendar
    monkeypatch.setattr(trading_calendar, "recent_completed_trading_sessions", lambda *a, **k: None)
    result = get_main_inflow_cumulative("688183", current_time=_FLOW_NOW)
    assert result["status"] == "failed"
    assert "ifind:calendar_unavailable" in result["errors"]
    assert result["main_net_inflow"] is None and result["inflow_10d"] is None
    assert result["daily_series"] == []


def test_current_info_timeout_reserves_bounded_flow_opportunity(choice_transport, monkeypatch):
    """必要 iFinD 批次超时不吞掉资金窗口的预算机会（root 40s 配置边界）。"""
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    choice_transport.ifind_mcp_timeout_seconds = 2
    payloads = {
        "get_stock_financials": FINANCIAL_ENV,
        "get_stock_info": DATED_INFO_ENV,
        "get_stock_performance": FLOW_ENV,
    }
    import asyncio

    async def query(self, tool, question, *, deadline=None):
        if tool == "get_stock_info":
            await asyncio.sleep(8)  # 超过 2s MCP 上限 → 超时被有界取消
        return deepcopy(payloads[tool])

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only", timeout_seconds=2)),
    )

    def native(method, args, options, deadline):
        return {
            "error": "authentication_failed", "native_code": 10001009,
            "error_text": "login count up to limit", "stage": "login",
            "codes": [], "indicators": [], "dates": [], "data": {},
        }

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    # 超时被如实记录，且不重置全局预算
    assert any("ifind:current_info:timeout" in item for item in result["errors"])
    # 先行的财务批次不受影响，估值缺口如实披露
    assert result["growth"]["data"]["revenue"] == 9547590000.0
    assert result["valuation"]["data"].get("pe_ratio") is None
    # 资金窗口仍获得有界机会并完成（source=ifind，窗口完整性按真实日历披露）
    assert result["capital_flow"]["data"]["stock_flow"]["source"] == "ifind"


# ------------------------------------------------------- 同报告 10001009（E）

class _FakePipe:
    """真实 _native_request 锁层下的假管道：放行前不可读，close 的 None 不计出站。"""

    def __init__(self, reply, release, first_dispatched):
        self._reply = reply
        self._release = release
        self._first_dispatched = first_dispatched
        self.sent: list[Any] = []  # 含 close() 的 None
        self._served = 0

    def send(self, payload):
        self.sent.append(payload)
        if payload is not None:
            self._first_dispatched.set()

    def poll(self, timeout=None):
        import time as _time
        deadline = None if timeout is None else _time.monotonic() + timeout
        while True:
            if self._release.is_set() and self._served < len([p for p in self.sent if p is not None]):
                return True
            if deadline is not None and _time.monotonic() >= deadline:
                return False
            _time.sleep(0.005)

    def recv(self):
        self._served += 1
        return self._reply

    def close(self):
        pass


class _FakeProcess:
    """假原生进程：永不 spawn，任何 close/join 路径无副作用。"""

    def is_alive(self):
        return True

    def join(self, timeout=None):
        pass

    def terminate(self):
        pass

    def kill(self):
        pass

    def close(self):
        pass


def test_waiting_lock_request_does_not_relogin_after_10001009(monkeypatch):
    """真实 _native_request 锁层竞争：持锁请求拿到 10001009 时在锁内标记，
    已在等锁的同报告请求不得再登录（root 反例边界）。

    本测试不使用 choice_transport（其 fixture 会替换 _native_request），
    自行装配纯传输隔离；spawn 边界 fail-closed——任何真实 spawn 直接断言失败
    （此前坏状态曾触发一次真实 sdk.start/10001020，绝不允许复现）。
    """
    import threading
    from contextvars import copy_context
    from data_provider import choice_adapter as ca_module
    from data_provider.cross_source_validator import SourceReadError

    config = Config(data_source_profile="choice", deep_research_cross_validate=False)
    config.enable_ifind_mcp = True
    config.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    config.ifind_mcp_token = "test-only"
    config.financial_primary_source = "ifind"
    monkeypatch.setattr(Config, "_instance", config)

    class _NoSpawn:
        def __getattr__(self, name):
            raise AssertionError("fail-closed: test reached real native spawn boundary")

    monkeypatch.setattr(ca_module, "multiprocessing", _NoSpawn())

    class _ObservedLock:
        """包装真实 Lock：记录获取尝试，第二个等待者到达即置位事件。"""

        def __init__(self):
            self._real = threading.Lock()
            self.attempts = 0
            self.contender_at_lock = threading.Event()

        def acquire(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts >= 2:
                self.contender_at_lock.set()
            return self._real.acquire(*args, **kwargs)

        def release(self):
            self._real.release()

    observed = _ObservedLock()
    monkeypatch.setattr(ChoiceAdapter, "_lock", observed, raising=False)
    release = threading.Event()
    first_dispatched = threading.Event()
    pipe = _FakePipe({
        "error": "authentication_failed", "native_code": 10001009,
        "error_text": "login count up to limit", "stage": "login",
        "codes": [], "indicators": [], "dates": [], "data": {},
    }, release, first_dispatched)
    monkeypatch.setattr(ChoiceAdapter, "_connection", pipe, raising=False)
    monkeypatch.setattr(ChoiceAdapter, "_process", _FakeProcess(), raising=False)
    outcomes: dict[str, Exception | None] = {}

    def call(tag: str, indicators: str) -> None:
        try:
            ChoiceAdapter().css("688183", indicators, "EndDate=2026-10-30", deadline=time.monotonic() + 30)
            outcomes[tag] = None
        except Exception as exc:  # noqa: BLE001
            outcomes[tag] = exc

    first = second = None
    try:
        with ResearchRun(120).bind():
            ctx1, ctx2 = copy_context(), copy_context()
            first = threading.Thread(target=lambda: ctx1.run(call, "first", "NAME,LATESTREPORTNEW"))
            first.start()
            assert first_dispatched.wait(5), "首个请求必须有界内到达原生出站"
            second = threading.Thread(target=lambda: ctx2.run(call, "second", "RATINGTARGETPRICE"))
            second.start()
            assert observed.contender_at_lock.wait(5), "竞争者必须真实到达锁获取尝试"
            release.set()
            first.join(10)
            second.join(10)
    finally:
        release.set()
        if first is not None:
            first.join(5)
        if second is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive(), "两个线程必须已退出，不得拖住后续门禁"
    assert len([item for item in pipe.sent if item is not None]) == 1, pipe.sent
    assert isinstance(outcomes["first"], SourceReadError)
    assert isinstance(outcomes["second"], SourceReadError)
    assert outcomes["second"].reason_code == "authentication_failed"


def test_same_report_login_limit_stops_repeated_choice_logins(probe_transport):
    calls = probe_transport
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    # 只有首个 header 查询触达 Choice 原生层；其后 targets/company 等均被同报告止损拦下
    assert len(calls["choice"]) == 1, calls["choice"]
    assert calls["choice"][0][0] == "css"
    errors = result["errors"]
    # header 经 _record_failure 以 source:reason 形态记录；可选查询按 scope 记录
    assert any("choice:authentication_failed" == item for item in errors)
    assert any("choice:targets:authentication_failed" in item for item in errors)
    assert any("choice:company:authentication_failed" in item for item in errors)


def test_next_report_retries_choice_normally_after_login_limit(probe_transport, monkeypatch):
    calls = probe_transport
    attempted: list[str] = []

    def counting_native(method, args, options, deadline):
        attempted.append(method)
        return {
            "error": "authentication_failed", "native_code": 10001009,
            "error_text": "login count up to limit", "stage": "login",
            "codes": [], "indicators": [], "dates": [], "data": {},
        }

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(counting_native))
    with ResearchRun(60).bind():
        DataFetcherManager().get_fundamental_context("688183", budget_seconds=30)
    first_report_attempts = len(attempted)
    assert first_report_attempts >= 1
    with ResearchRun(60).bind():
        DataFetcherManager().get_fundamental_context("688183", budget_seconds=30)
    # 下一份报告是新 ResearchRun：header 照常重新尝试登录
    assert len(attempted) > first_report_attempts


# ------------------------------------------------------- 编排级（预算与兜底）

def test_necessary_current_fields_complete_before_optional_choice(probe_transport, monkeypatch):
    calls = probe_transport
    choice_started: list[float] = []
    original_native = ChoiceAdapter._native_request

    def slow_native(method, args, options, deadline):
        choice_started.append(time.monotonic())
        time.sleep(2)
        return {
            "error": "authentication_failed", "native_code": 10001009,
            "error_text": "login count up to limit", "stage": "login",
            "codes": [], "indicators": [], "dates": [], "data": {},
        }

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(slow_native))
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    assert calls["ifind"], "必要 iFinD 批次必须真实发生"
    assert choice_started, "对照的 Choice 出站必须真实发生"
    # 必要当前资料先于可选 Choice 出站完成
    assert calls["ifind"][0][0] == "get_stock_financials"
    assert {tool for tool, _ in calls["ifind"][:3]} == {
        "get_stock_financials", "get_stock_info", "get_stock_performance",
    }
    growth = result["growth"]["data"]
    assert result["valuation"]["data"]["pe_ratio"] == 24.7573
    assert result["research"]["industry_ifind"]["name"] == "电子"
    assert result["capital_flow"]["data"]["stock_flow"]["source"] == "ifind"
    assert result["capital_flow"]["source_chain"][0]["provider"] == "ifind"


def test_prefer_ifind_full_current_migration_with_choice_down(probe_transport, monkeypatch):
    calls = probe_transport
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    growth = result["growth"]["data"]
    # P1 已获用户批准：四比率按 iFinD 公布值单源采用（真实探针值），仅缺调整口径
    for field, expected in (("roe", 29.34), ("gross_margin", 35.2306),
                            ("revenue_yoy", 39.8106), ("net_profit_yoy", 71.4933)):
        assert growth[field] == expected, field
        meta = result["growth"]["field_meta"][field]
        assert meta["selection_reason"] == "ifind_published_single_source"
        # P1 契约：规则可用（用户批准的单源规则使用）与独立核验分离；
        # input_reasons 保留 adjustment_unknown 作为披露标签
        assert meta["rule_eligible"] is True
        assert meta["quality"]["status"] != "verified"
        assert "adjustment_unknown" in meta["input_reasons"]
    anchors = (result.get("cross_validation") or {}).get("anchors") or {}
    if anchors.get("roe"):
        # 跨源核验不因单源采用而放宽：质量不得伪装 verified
        assert anchors["roe"]["quality"]["status"] != "verified"
    balance = growth["balance_sheet"]
    assert balance["source"] == "ifind" and balance["period"] == "2026-09-30"
    assert balance["assets"] == 18450820000.0 and balance["liabilities"] == 8612290000.0
    assert balance["statement_scope"] == "consolidated" and balance["adjustment"] == "restated"
    valuation = result["valuation"]["data"]
    assert valuation["pe_ratio"] == 24.7573 and valuation["pb_ratio"] == 5.7123
    assert valuation["total_mv"] == 56200860000.0
    assert valuation.get("trade_date") == "2026-10-30"
    assert result["as_of"] == "2026-10-30"
    research = result["research"]
    assert research["industry_ifind"]["name"] == "电子"
    assert research["industry_ifind"]["classification_version"] is None
    assert research["industry_ifind"]["code"] is None, "回执未回显行业代码列，不得补造"
    assert research["industry_ifind"]["as_of"] == "2026-10-30"
    assert "industry" not in research, "行业已由 iFinD 覆盖时不得再出 Choice 行业查询"
    # iFinD products 失败被隔离记录，不拖垮财务/估值/行业
    assert any("ifind:products:" in item for item in result["errors"])
    assert result["growth"]["data"]["revenue"] is not None
    from src.agent.tools.data_tools import _compact_fundamental_context
    compact = _compact_fundamental_context(result)
    assert (compact.get("research") or {}).get("industry_ifind", {}).get("name") == "电子"
    from src.deep_research_dims.context import industry_hint_from_research
    hint = industry_hint_from_research(compact.get("research") or {}, {})
    assert hint == "电子"


def test_industry_hint_prefers_ifind_then_choice_then_boards():
    ifind_ok = {"source": "ifind", "name": "电子", "code": "S27", "errors": []}
    ifind_bad = {"source": "ifind", "name": None, "errors": ["no_sw_industry"]}
    assert industry_hint_from_research({"industry_ifind": ifind_ok, "industry": {"HISSW2021": "通信"}}, {}) == "电子"
    assert industry_hint_from_research({"industry_ifind": ifind_bad, "industry": {"HISSW2021": "通信"}}, {}) == "通信"
    assert industry_hint_from_research({"industry_ifind": ifind_bad}, {"industry": "元器件"}) == "元器件"
    assert industry_hint_from_research({}, {}) == ""


def test_choice_policy_primary_keeps_choice_capital_flow(choice_transport, monkeypatch):
    """主源 choice 保持既有资金路径：NETINFLOW 单源语义不变。"""
    choice_transport.financial_primary_source = "choice"
    dates = ["2026-10-30", "2026-10-29", "2026-10-28"]

    def native(method, args, options, deadline):
        if method == "tradedates":
            return {"data": list(reversed(dates))}
        assert method == "csd"
        values = [-100.0, -200.0, -300.0]
        return {"codes": [args[0]], "indicators": ["NETINFLOW"], "dates": list(reversed(dates)),
                "data": {args[0]: [values]}}

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    from data_provider.capital_flow_provider import _choice_main_inflow_cumulative
    result = _choice_main_inflow_cumulative("300260", 12, deadline=None)
    assert result["source"] == "choice"
    assert result["as_of_date"] == "2026-10-30"
    assert result["main_net_inflow"] == pytest.approx(-300.0)


# ----------------------------------------------------------------- 缓存（策略）

def test_cache_identity_distinguishes_financial_primary_source(choice_transport):
    """主源切换隔离由 data_source_contract（-primary-ifind-v1）承载；
    基础缓存键不再重复策略位，legacy 键形状保持不变（root 缓存审查）。"""
    choice_transport.financial_primary_source = "choice"
    contract_choice = choice_transport.data_source_contract
    choice_transport.financial_primary_source = "ifind"
    contract_ifind = choice_transport.data_source_contract
    assert contract_choice != contract_ifind
    assert "primary-ifind" in contract_ifind and "primary-ifind" not in contract_choice
    manager = DataFetcherManager()
    legacy_key = manager._get_fundamental_cache_key("688183", 30)
    assert legacy_key == "688183|facts=6|budget=300|sf=1", "legacy/choice 键形状保持不变"


def test_dim_cache_mapping_version_scoped_to_ifind_policy(choice_transport, monkeypatch, tmp_path):
    """mapping 版本失效仅适用 ifind 主源（root 矩阵）：legacy+choice 旧 sector
    命中不变；ifind 下迁移前旧 sector miss；ifind 新版本写入后命中。"""
    from src.deep_research_dims import dim_cache
    import json as _json
    monkeypatch.setattr(dim_cache, "_CACHE_DIR", str(tmp_path / "cache"))
    old_payload = {"dim": "sector", "status": "ok", "narrative": "旧行业提示"}

    def _strip_mapping_version():
        path = dim_cache._cache_path("688183", "sector")
        record = _json.loads(Path(path).read_text())
        record.pop("mapping_version", None)
        Path(path).write_text(_json.dumps(record, ensure_ascii=False), encoding="utf-8")

    # legacy + choice 主源：旧 sector 缓存照常命中（键形状与命中不变）
    for policy in ("choice", None):
        if policy:
            choice_transport.financial_primary_source = policy
        dim_cache.save_cached_dim("688183", "sector", old_payload)
        _strip_mapping_version()
        assert dim_cache.load_cached_dim("688183", "sector") == old_payload, policy
    # ifind 主源：迁移前旧 sector（无 mapping_version）miss 重算
    choice_transport.enable_ifind_mcp = True
    choice_transport.financial_primary_source = "ifind"
    dim_cache.save_cached_dim("688183", "sector", old_payload)
    _strip_mapping_version()
    assert dim_cache.load_cached_dim("688183", "sector") is None
    # ifind 新写入带当前版本后命中
    dim_cache.save_cached_dim("688183", "sector", old_payload)
    assert dim_cache.load_cached_dim("688183", "sector") == old_payload


def test_dim_cache_path_distinguishes_financial_primary_source(choice_transport, monkeypatch):
    from src.deep_research_dims import dim_cache
    choice_transport.financial_primary_source = "choice"
    path_choice = dim_cache._cache_path("688183", "capital")
    choice_transport.financial_primary_source = "ifind"
    path_ifind = dim_cache._cache_path("688183", "capital")
    assert path_choice != path_ifind
    # 不消费基本面主源的维度不受影响
    path_intel_choice = dim_cache._cache_path("688183", "intel")
    choice_transport.financial_primary_source = "ifind"
    assert dim_cache._cache_path("688183", "intel") == path_intel_choice



# ------------------------------------------------- 受控纵向（raw→服务→API→下载）

def test_vertical_raw_probes_flow_through_service_save_api_download(
    probe_transport, provider, tmp_path, monkeypatch
):
    """受控纵向（issue #71）：真实探针回执 → 生产解析/资格 → manager → 真实共享
    上下文装配 → 深研服务保存 → API 详情/下载一致 → 历史可读 → 离线 PDF。

    仅隔离供应商 transport（probe_transport 的 iFinD 路由 + Choice 10001009）
    与 LLM（provider 假服务器）；不 mock 解析/资格/消费层。P1 已批准：四比率按
    iFinD 公布值单源采用并显式标注未知口径/未独立核验；sector 溯源随维度留存。
    """
    import time as _time
    from pathlib import Path as _Path
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.config import get_config
    from src.storage import DatabaseManager
    from src.search_service import SearchService, SearchResult, SearchResponse
    from src.services import deep_research_service as service_module
    from api.v1.endpoints.deep_research import router
    from src.agent.deep_research import orchestrator

    probe_transport  # 传输隔离就位（iFinD 路由真实回执；Choice 全部 10001009）
    cfg = get_config()
    cfg.deep_research_timeout_seconds = 30
    cfg.deep_research_engine = "dual_track"
    cfg.litellm_model = "openai/fixture"
    cfg.agent_litellm_model = "openai/fixture"
    cfg.llm_model_list = [{"model_name": "openai/fixture", "litellm_params": {"model": "openai/fixture", "api_base": provider[0], "api_key": "fixture-key"}}]
    import pandas as _pd
    # transport 隔离（行情/日线为本次迁移之外的外部供应商 transport）
    monkeypatch.setattr(DataFetcherManager, "get_realtime_quote",
                        lambda self, code, **kwargs: {"price": 24.75, "name": "生益电子"})
    monkeypatch.setattr("src.services.history_loader.load_history_df",
                        lambda code, days=60: (_pd.DataFrame(), ""))
    monkeypatch.setattr(service_module, "_dual_track_adapter", None)
    monkeypatch.setattr(service_module, "_DEEP_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    database = DatabaseManager(f"sqlite:///{tmp_path / 'reports.sqlite'}")
    monkeypatch.setattr(service_module, "get_db", lambda: database)
    from src.deep_research_dims.context import build_shared_context as real_build_shared_context
    context = real_build_shared_context("688183", "生益电子")
    # 真实装配事实断言（进入服务前即可见）
    fundamental_ctx = context.fundamental
    assert fundamental_ctx["industry_hint"] == "电子"
    assert fundamental_ctx["market_cap"] == 56200860000.0
    assert fundamental_ctx["pe_ttm"] == 24.7573 and fundamental_ctx["pb"] == 5.7123
    assert fundamental_ctx["balance_sheet"]["source"] == "ifind"
    assert fundamental_ctx["balance_sheet"]["period"] == "2026-09-30"
    # P1 已批准：四比率为单源公布值（同实际财期、其余资格满足）
    assert fundamental_ctx["roe"] == 29.34 and fundamental_ctx["gross_margin"] == 35.2306
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim as _build_f1
    _f1 = _build_f1(context)
    _safety = _f1.financial_safety
    assert _safety["source"] == "ifind" and _safety["leverage"] is not None, "raw probe 经实际 F1 必须产出 leverage"
    assert pytest.approx(86.1229e8 / 184.5082e8 * 100, abs=0.05) == _safety["leverage"]
    monkeypatch.setattr(orchestrator, "build_shared_context", lambda *args, **kwargs: context)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(SearchService, "is_available", property(lambda self: True))
    monkeypatch.setattr(SearchService, "search_comprehensive_intel", lambda self, **kwargs: {
        "business": SearchResponse(query="fixture", provider="fixture", results=[SearchResult(title="公司公告样本", snippet="已取得的业务资料样本", url="https://example.com/disclosure", source="公司公告", published_date="2026-10-01")])
    })
    try:
        service = service_module.deep_research_service
        result = service.generate_report("688183", "生益电子", dims=["business", "fundamental", "capital", "sector"], force_refresh=True)
        assert result["report_id"]
        assert _time.monotonic() is not None
        fundamental = result["dimensions"]["fundamental"]
        payload = json.dumps(fundamental, ensure_ascii=False, default=str)
        # P1 契约：六项规则可用（用户批准的单源规则使用）→ 计数/状态一致 ok；
        # 未知口径/未独立核验标注必须可见，质量不得伪装 verified
        assert fundamental["status"] == "ok", "全部用户批准输入齐备且真实合格时应如实 ok"
        assert "ifind_published_single_source" in payload
        assert any("调整口径未知" in item or "公布值" in item for item in (fundamental.get("data_quality") or {}).get("warnings", []))
        # financial_safety 说明经既有 note 键进入保存载荷，内部键名不泄漏
        safety_payload = json.dumps(fundamental.get("financial_safety") or {}, ensure_ascii=False)
        assert "披露日期未提供" in safety_payload and "disclosure_note" not in safety_payload
        for field in ("roe", "gross_margin", "revenue_yoy", "net_profit_yoy"):
            quality = (fundamental["data_quality"]["fields"][field]["quality"])
            assert quality["status"] != "verified", field
        assert '"source": "ifind"' in payload or '"source":"ifind"' in payload
        assert "电子" in result["markdown"]
        # 审查修复（issue #71 渲染）：财务质量资格表（7 列）的 PE/PB 行必须显示
        # 实际估值观测时点（2026-10-30），不得再显示「期间未知」；财务行保留
        # 实际财期。render.py 的指标快照窄表不在本修复范围（零 diff）。
        md_lines = result["markdown"].splitlines()
        def _quality_rows(prefix: str) -> list:
            return [line for line in md_lines if line.startswith(prefix) and line.count("|") >= 8]
        pe_rows, pb_rows = _quality_rows("| PE |"), _quality_rows("| PB |")
        assert pe_rows and pb_rows, "财务质量资格表必须包含 PE/PB 行"
        for row in (*pe_rows, *pb_rows):
            assert "2026-10-30（观察时点）" in row, f"PE/PB 行必须显示实际观察时点：{row}"
        roe_rows = _quality_rows("| ROE |")
        assert roe_rows and all("2026-09-30" in row for row in roe_rows), "财务字段行保留实际财期，不复制观察时点"
        # financial_safety 经既有 note 键渲染为「说明」，内部键不得泄漏到报告
        assert "disclosure_note" not in result["markdown"]
        assert "披露日期未提供" in result["markdown"]
        # 行业来源窄缝行与免责声明之间保留一个空段落（不被空白控制拼接）
        assert "不构成投资建议。*\n\n- 行业来源：" in result["markdown"]
        # 行业溯源（issue #71）：来源/级别/截止日期经维度事实进入保存/API/历史链，
        # 并经既有分节模板窄缝确定性进入报告正文
        # 溯源经既有分节模板窄缝（sector.retained_facts 直读）确定性进入正文
        assert "行业来源：ifind" in result["markdown"]
        assert "正式申万行业" in result["markdown"] and "2026-10-30" in result["markdown"]
        sector_dim = result["dimensions"]["sector"]
        sector_payload = json.dumps(sector_dim, ensure_ascii=False, default=str)
        assert '"tool": "industry_ifind"' in sector_payload or 'industry_ifind' in sector_payload
        assert "分类版本未回显" in sector_payload and "2026-10-30" in sector_payload
        # 溯源在维度载荷确定性留存（API/历史经 dims_json 交付），并经既有分节
        # 模板的窄缝（sector retained fact）确定性进入报告正文/PDF
        assert "行业来源：iFinD" in sector_payload
        assert "ifind" in payload
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            detail = client.get(f"/reports/{result['report_id']}")
            assert detail.status_code == 200
            assert detail.json()["data"]["markdown"] == result["markdown"]
            download = client.get(f"/reports/{result['report_id']}/markdown")
            assert download.status_code == 200
            assert download.text == result["markdown"]
        # 历史存储可读且与 API 返回一致
        record = database.get_deep_research_report(result["report_id"])
        assert record is not None
        # 迁移事实进保存载荷：资金窗口 ifind 单源（真实 oracle）、采纳证据字段在位
        dims_payload = json.dumps(result["dimensions"], ensure_ascii=False, default=str)
        assert '"source": "ifind"' in dims_payload or '"source":"ifind"' in dims_payload
        capital_dim = result["dimensions"]["capital"]
        assert "-114551026.71" in dims_payload.replace(", ", ","), "1日窗口金额必须随维度保存"
        assert capital_dim.get("as_of_date") == "2026-10-30"
        # 真实 SSE：一次 generate/stream 到 done 终态（非仅旧机制套件）
        sse_app = FastAPI()
        sse_app.include_router(router)
        with TestClient(sse_app) as sse_client:
            sse_response = sse_client.post(
                "/generate/stream",
                json={"stock_code": "688183", "stock_name": "生益电子",
                      "dims": ["business", "fundamental", "capital", "sector"], "force_refresh": True},
            )
            assert sse_response.status_code == 200
            assert '"type": "done"' in sse_response.text, "SSE 必须到达真实 done 终态帧"
            assert "ifind" in sse_response.text, "done 帧载荷必须携带本次迁移事实"
        # 离线 PDF：保存的 Markdown 经真实渲染器产出含迁移事实文本的 PDF
        from src.md2pdf import markdown_to_pdf_file
        pdf_path = tmp_path / "report.pdf"
        assert markdown_to_pdf_file(result["markdown"], str(pdf_path))
        assert pdf_path.stat().st_size > 1000
        pypdf = pytest.importorskip("pypdf")
        reader = pypdf.PdfReader(str(pdf_path))
        pdf_text = "".join(page.extract_text() or "" for page in reader.pages)
        assert "电子" in pdf_text, "行业事实必须可从 PDF 文本提取"
        assert "资产负债" in pdf_text or "负债" in pdf_text, "资产负债事实必须进入 PDF"
        pdf_flat = pdf_text.replace("\n", "")
        assert "行业来源：ifind" in pdf_flat, "行业溯源窄缝必须进入 PDF"
        assert "正式申万行业" in pdf_flat, "行业溯源表格必须进入 PDF"
        assert "资产负债" in pdf_text or "负债" in pdf_text, "资产负债事实必须进入 PDF"
        assert "2026-10-30（观察时点）" in pdf_flat, "PE/PB 观察时点必须进入 PDF"
        assert "disclosure_note" not in pdf_flat, "内部键名不得泄漏到 PDF"
    finally:
        database._engine.dispose()


# ------------------------------------------------- 跨源/剪刀差反例（reader 收敛）

def test_p1_unknown_adjustment_readings_never_verify_through_validator():
    """P1 读数经真实 validator 不得核验通过/宣称一致（root 剪刀差反例，作用域收敛：
    仅本票带 adjustment_unknown 标注的四比率读数；不改全局 None/None 语义）。"""
    from types import SimpleNamespace
    from data_provider.cross_source_validator import CrossSourceValidator
    left = AnchorReading("choice", 39.8106, period="2026-09-30", period_basis="YTD",
                         unit="percentage_point", caliber="operating_revenue_yoy",
                         statement_scope="consolidated", adjustment="restated")
    right = AnchorReading("ifind", 39.8106, period="2026-09-30", period_basis="YTD",
                          unit="percentage_point", caliber="operating_revenue_yoy",
                          statement_scope="consolidated", adjustment=None,
                          normalization_error="adjustment_unknown")
    validator = CrossSourceValidator([
        SimpleNamespace(name="choice", read=lambda *a, **k: left),
        SimpleNamespace(name="ifind", read=lambda *a, **k: right),
    ])
    try:
        result = validator.verify("688183", "revenue_yoy", primary_reading=right).to_compact()
    finally:
        validator.close()
    assert result["quality"]["status"] != "verified"
    assert result["agreed"] is False
    relation = next((r for r in result["readings"] if r["source"] == "ifind"), None)
    assert relation is not None and "adjustment_unknown" in relation["reason_codes"]


def test_scissors_requires_same_source_and_caliber_pair():
    """混口径两腿（iFinD 未知口径 + Choice 重述）不得计算剪刀差；同 iFinD 公布值对可算。"""
    from src.deep_research_dims.context import SharedContext
    from src.deep_research_dims.fundamental_dim import build_fundamental_dim
    from datetime import date as _date

    def _ctx(roe_meta):
        ctx = SharedContext("688183", "生益电子", _date(2026, 11, 1).isoformat())
        ctx.fundamental = {
            "revenue": 9547590000.0, "net_profit": 1911600000.0, "roe": 29.34,
            "gross_margin": 35.2306, "revenue_growth": 39.8106, "net_profit_yoy": 71.4933,
            "pe_ttm": 24.7573, "pb": 5.7123, "market_cap": 56200860000.0,
            "industry_hint": "电子", "balance_sheet": {}, "research": {},
            "field_meta": roe_meta,
        }
        return ctx

    base_meta = {
        "revenue": {"source": "ifind", "value": 9547590000.0, "period": "2026-09-30", "period_basis": "YTD", "unit": "currency_base", "currency": "CNY", "statement_scope": "consolidated", "adjustment": "restated", "caliber": "operating_revenue", "quality": {"status": "verified", "reason_codes": []}},
        "net_profit": {"source": "ifind", "value": 1911600000.0, "period": "2026-09-30", "period_basis": "YTD", "unit": "currency_base", "currency": "CNY", "statement_scope": "consolidated", "adjustment": "restated", "caliber": "parent_net_profit", "quality": {"status": "verified", "reason_codes": []}},
        "roe": {"source": "ifind", "value": 29.34, "period": "2026-09-30", "period_basis": "YTD", "unit": "percentage_point", "statement_scope": "consolidated", "adjustment": None, "caliber": "weighted_roe", "selection_reason": "ifind_published_single_source", "input_reasons": ["adjustment_unknown"], "rule_eligible": False, "quality": {"status": "single_source", "reason_codes": ["single_family"]}},
        "gross_margin": dict(base_meta_roe := {"source": "ifind", "value": 35.2306, "period": "2026-09-30", "period_basis": "YTD", "unit": "percentage_point", "statement_scope": "consolidated", "adjustment": None, "caliber": "gross_margin", "selection_reason": "ifind_published_single_source", "input_reasons": ["adjustment_unknown"], "rule_eligible": False, "quality": {"status": "single_source", "reason_codes": ["single_family"]}}),
        "revenue_yoy": {"source": "ifind", "value": 39.8106, "period": "2026-09-30", "period_basis": "YTD", "unit": "percentage_point", "statement_scope": "consolidated", "adjustment": None, "caliber": "operating_revenue_yoy", "selection_reason": "ifind_published_single_source", "input_reasons": ["adjustment_unknown"], "rule_eligible": False, "quality": {"status": "single_source", "reason_codes": ["single_family"]}},
        "net_profit_yoy": {"source": "ifind", "value": 71.4933, "period": "2026-09-30", "period_basis": "YTD", "unit": "percentage_point", "statement_scope": "consolidated", "adjustment": None, "caliber": "parent_net_profit_yoy", "selection_reason": "ifind_published_single_source", "input_reasons": ["adjustment_unknown"], "rule_eligible": False, "quality": {"status": "single_source", "reason_codes": ["single_family"]}},
    }
    # 同 iFinD 同批公布值对：剪刀差可计算
    dim = build_fundamental_dim(_ctx(base_meta))
    assert dim.growth_quality["scissors"] == pytest.approx(31.68, abs=0.02)
    # 混口径：营收同比保持 iFinD 未知口径（P1 参与），净利同比换 Choice 重述 → 剪刀差必须为 None
    mixed = deepcopy(base_meta)
    mixed["net_profit_yoy"] = {"source": "choice", "value": 71.4933, "period": "2026-09-30", "period_basis": "YTD", "unit": "percentage_point", "statement_scope": "consolidated", "adjustment": "restated", "caliber": "parent_net_profit_yoy", "selection_reason": "primary", "input_reasons": [], "rule_eligible": True, "quality": {"status": "verified", "reason_codes": []}}
    dim_mixed = build_fundamental_dim(_ctx(mixed))
    assert dim_mixed.growth_quality["scissors"] is None
    # 非 P1 跨源 restated 同期对：旧行为保留（无 P1 参与，仅 period/basis 条件）
    legacy = deepcopy(base_meta)
    legacy["revenue_yoy"] = {"source": "choice", "value": 39.8106, "period": "2026-09-30", "period_basis": "YTD", "unit": "percentage_point", "statement_scope": "consolidated", "adjustment": "restated", "caliber": "operating_revenue_yoy", "selection_reason": "primary", "input_reasons": [], "rule_eligible": True, "quality": {"status": "verified", "reason_codes": []}}
    legacy["net_profit_yoy"] = {"source": "ifind", "value": 71.4933, "period": "2026-09-30", "period_basis": "YTD", "unit": "percentage_point", "statement_scope": "consolidated", "adjustment": "restated", "caliber": "parent_net_profit_yoy", "selection_reason": "primary", "input_reasons": [], "rule_eligible": True, "quality": {"status": "verified", "reason_codes": []}}
    dim_legacy = build_fundamental_dim(_ctx(legacy))
    assert dim_legacy.growth_quality["scissors"] == pytest.approx(31.68, abs=0.02), "非 P1 旧对剪刀差行为不受本次收敛影响"


# ------------------------------------- P1 同期选择/省查一致（root 精确反例）

def _p1_reading(period="2026-09-30", value=29.34, field_caliber="weighted_roe"):
    return AnchorReading("ifind", value, period=period, period_basis="YTD", unit="percentage_point",
                         caliber=field_caliber, statement_scope="consolidated", adjustment=None,
                         normalization_error="adjustment_unknown")


def test_select_prefers_approved_ifind_current4_over_healthy_same_period_choice():
    """root 反例：同财期健康 Choice 不得替换已批准的 iFinD current4（主源优先）。"""
    from data_provider.cross_source_validator import select_current_financials
    ifind = {
        "revenue": AnchorReading("ifind", 9547590000.0, period="2026-09-30", period_basis="YTD",
                                 unit="currency_base", currency="CNY", caliber="operating_revenue",
                                 statement_scope="consolidated", adjustment="restated"),
        "roe": _p1_reading(),
    }
    choice = {
        "revenue": AnchorReading("choice", 9547590000.0, period="2026-09-30", period_basis="YTD",
                                 unit="currency_base", currency="CNY", caliber="operating_revenue",
                                 statement_scope="consolidated", adjustment="restated"),
        "roe": AnchorReading("choice", 29.0, period="2026-09-30", period_basis="YTD",
                             unit="percentage_point", caliber="weighted_roe",
                             statement_scope="consolidated", adjustment="restated"),
    }
    period, selected = select_current_financials(ifind, choice)
    assert period == "2026-09-30"
    assert selected["roe"].source == "ifind" and selected["roe"].value == 29.34
    assert selected["revenue"].source == "ifind"


def test_older_period_p1_candidate_does_not_block_newer_choice_or_win():
    """旧期 P1 候选不授旧期保护：不阻止更新期合格 Choice 前移，也不得成为旧期主值。"""
    from data_provider.cross_source_validator import select_current_financials
    ifind = {
        "roe": _p1_reading(period="2026-06-30"),
    }
    choice = {
        "roe": AnchorReading("choice", 28.0, period="2026-09-30", period_basis="YTD",
                             unit="percentage_point", caliber="weighted_roe",
                             statement_scope="consolidated", adjustment="restated"),
    }
    period, selected = select_current_financials(ifind, choice)
    assert period == "2026-09-30"
    assert selected["roe"].source == "choice" and selected["roe"].value == 28.0


def test_mixed_period_p1_ratio_not_omitted_from_choice_growth_query(choice_transport, monkeypatch):
    """root 阻断反例（manager/transport 级，健康 Choice）：混合财期批次（9月金额
    + 6月比率）中旧期 P1 比率不得省查——Choice 增长查询必须仍含 ROEWA，且不含
    已被合格省查的金额指标；省查与选择保证一致，无人为缺口。"""
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    env = deepcopy(FINANCIAL_ENV)
    lines = env["data"]["answer"].splitlines()
    header_cells = lines[0].strip().strip("|").split("|")
    row_cells = lines[2].strip().strip("|").split("|")
    sep = lines[1]
    date_idx = next(i for i, c in enumerate(header_cells) if c.strip() == "日期")
    keep_money = {0, 1, date_idx}
    money_idx = [i for i, c in enumerate(header_cells)
                 if i in keep_money or ("（单位：元）" in c and "单季" not in c)]
    ratio_idx = [i for i in range(len(header_cells)) if i not in money_idx and i != date_idx]
    ratio_idx_full = [0, 1, date_idx] + ratio_idx

    def pick(cells, indexes):
        return "|" + "|".join(cells[i] for i in indexes) + "|"

    env["data"]["answer"] = (pick(header_cells, money_idx) + "\n" + sep + "\n" + pick(row_cells, money_idx)
                             + "\n\n" + pick(header_cells, ratio_idx_full) + "\n" + sep + "\n"
                             + pick(row_cells, ratio_idx_full).replace("|20260930|", "|20260630|") + "\n")
    payloads = {
        "get_stock_financials": env,
        "get_stock_info": DATED_INFO_ENV,
        "get_stock_performance": FLOW_ENV,
    }
    calls: Dict[str, list] = {"ifind": [], "choice": []}

    async def query(self, tool, question, *, deadline=None):
        calls["ifind"].append((tool, question))
        return deepcopy(payloads[tool])

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )

    def native(method, args, options, deadline):
        indicators = args[1] if len(args) > 1 else ""
        calls["choice"].append(indicators)
        if method == "tradedates":
            return {"data": ["2026-10-30"]}
        if indicators == "NAME,LATESTREPORTNEW":
            return {"codes": ["688183.SH"], "indicators": ["NAME", "LATESTREPORTNEW"],
                    "dates": [], "data": {"688183.SH": ["生益电子", "2026/09/30"]}}
        if indicators == "SUMASSETNEW,SUMLIABNEW,ACTUALISSUINGDATE":
            return {"codes": ["688183.SH"], "indicators": ["SUMASSETNEW", "SUMLIABNEW", "ACTUALISSUINGDATE"],
                    "dates": [], "data": {"688183.SH": [18450820000.0, 8612290000.0, "2026/10/20"]}}
        if indicators.startswith("RATINGTARGET"):
            return {"codes": ["688183.SH"], "indicators": indicators.split(","),
                    "dates": [], "data": {"688183.SH": [None, None, None]}}
        if "ROEWA" in indicators:
            values = {"ROEWA": 4.0, "GPMARGIN": 23.4, "YOYOR": 9.6, "YOYPNI": -7.5,
                      "ACTUALISSUINGDATE": "2026/10/20"}
            return {"codes": ["688183.SH"], "indicators": indicators.split(","),
                    "dates": [], "data": {"688183.SH": [values.get(k) for k in indicators.split(",")]}}
        return {"codes": [args[0]], "indicators": indicators.split(","),
                "dates": [], "data": {args[0]: [None for _ in indicators.split(",")]}}

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    growth_calls = [indicators for indicators in calls["choice"] if "ROEWA" in indicators]
    assert growth_calls, "旧期 P1 比率不得省查：增长查询必须仍含 ROEWA"
    assert all("OPERATEREVENEW" not in indicators for indicators in growth_calls), \
        "9 月合格金额项应按既有省查语义省去"
    growth = result["growth"]["data"]
    # Choice 当期 ROE 到达，6 月 P1 候选不被选为当期主值（证据留存于核验锚点）
    assert growth["roe"] == 4.0
    assert result["growth"]["field_meta"]["roe"]["source"] == "choice"
    assert growth.get("report_date") == "2026-09-30"




def _mixed_period_financial_env(money_period, ratio_period):
    """两表混合财期批次：金额列 money_period、比率列 ratio_period。"""
    env = deepcopy(FINANCIAL_ENV)
    lines = env["data"]["answer"].splitlines()
    header_cells = lines[0].strip().strip("|").split("|")
    row_cells = lines[2].strip().strip("|").split("|")
    sep = lines[1]
    date_idx = next(i for i, c in enumerate(header_cells) if c.strip() == "日期")
    keep_money = {0, 1, date_idx}
    money_idx = [i for i, c in enumerate(header_cells)
                 if i in keep_money or ("（单位：元）" in c and "单季" not in c)]
    ratio_idx = [i for i in range(len(header_cells)) if i not in money_idx and i != date_idx]
    ratio_idx_full = [0, 1, date_idx] + ratio_idx

    def pick(cells, indexes):
        return "|" + "|".join(cells[i] for i in indexes) + "|"

    env["data"]["answer"] = (pick(header_cells, money_idx) + "\n" + sep + "\n"
                             + pick(row_cells, money_idx).replace("|20260930|", f"|{money_period}|")
                             + "\n\n" + pick(header_cells, ratio_idx_full) + "\n" + sep + "\n"
                             + pick(row_cells, ratio_idx_full).replace("|20260930|", f"|{ratio_period}|") + "\n")
    return env


def _healthy_choice_transport(payloads, calls, monkeypatch):
    async def query(self, tool, question, *, deadline=None):
        calls["ifind"].append((tool, question))
        return deepcopy(payloads[tool])

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )

    def native(method, args, options, deadline):
        indicators = args[1] if len(args) > 1 else ""
        calls["choice"].append(indicators)
        if method == "tradedates":
            return {"data": ["2026-10-30"]}
        if indicators == "NAME,LATESTREPORTNEW":
            return {"codes": ["688183.SH"], "indicators": ["NAME", "LATESTREPORTNEW"],
                    "dates": [], "data": {"688183.SH": ["生益电子", "2026/09/30"]}}
        if indicators == "SUMASSETNEW,SUMLIABNEW,ACTUALISSUINGDATE":
            return {"codes": ["688183.SH"], "indicators": ["SUMASSETNEW", "SUMLIABNEW", "ACTUALISSUINGDATE"],
                    "dates": [], "data": {"688183.SH": [18450820000.0, 8612290000.0, "2026/10/20"]}}
        if indicators.startswith("RATINGTARGET"):
            return {"codes": ["688183.SH"], "indicators": indicators.split(","),
                    "dates": [], "data": {"688183.SH": [None, None, None]}}
        if "ROEWA" in indicators:
            values = {"ROEWA": 4.0, "GPMARGIN": 23.4, "YOYOR": 9.6, "YOYPNI": -7.5,
                      "ACTUALISSUINGDATE": "2026/10/20"}
            return {"codes": ["688183.SH"], "indicators": indicators.split(","),
                    "dates": [], "data": {"688183.SH": [values.get(k) for k in indicators.split(",")]}}
        return {"codes": [args[0]], "indicators": indicators.split(","),
                "dates": [], "data": {args[0]: [None for _ in indicators.split(",")]}}

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    return calls


def test_omission_matrix_jun_strict_money_sep_p1_ratio_omitted(choice_transport, monkeypatch):
    """(c) 6月严格金额 + 9月 P1 比率：P1 在 iFinD 当前财期且存在严格锚 → 省查比率，
    增长查询不含任何比率指标（金额项按严格语义同样省去）。"""
    calls: Dict[str, list] = {"ifind": [], "choice": []}
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    payloads = {
        "get_stock_financials": _mixed_period_financial_env("20260630", "20260930"),
        "get_stock_info": DATED_INFO_ENV,
        "get_stock_performance": FLOW_ENV,
    }
    _healthy_choice_transport(payloads, calls, monkeypatch)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    growth_calls = [i for i in calls["choice"] if "ROEWA" in i]
    assert not growth_calls, f"9月 P1 比率在严格锚存在时应省查：{calls['choice']}"
    assert result["growth"]["data"]["roe"] == 29.34
    assert result["growth"]["field_meta"]["roe"]["selection_reason"] == "ifind_published_single_source"


def test_omission_matrix_p1_only_jun_keeps_choice_query_with_newer_sep(choice_transport, monkeypatch):
    """(d) P1-only 6月批次 + 更新期 Choice 9月：无严格锚 → 保守保留比率查询，
    Choice 9月值被选择，不出现省查后丢弃。"""
    calls: Dict[str, list] = {"ifind": [], "choice": []}
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    payloads = {
        "get_stock_financials": _mixed_period_financial_env("20260930", "20260630"),
        "get_stock_info": DATED_INFO_ENV,
        "get_stock_performance": FLOW_ENV,
    }
    _healthy_choice_transport(payloads, calls, monkeypatch)
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    growth_calls = [i for i in calls["choice"] if "ROEWA" in i]
    assert growth_calls, "无严格锚时旧期 P1 必须保守保留 Choice 比率查询"
    assert result["growth"]["data"]["roe"] == 4.0
    assert result["growth"]["field_meta"]["roe"]["source"] == "choice"


def test_selector_anchor_includes_latest_p1_period_for_advance():
    """前移基线锚包含最新有效 P1 财期：旧严格金额不阻止更新期 iFinD P1 同期选择。"""
    from data_provider.cross_source_validator import select_current_financials
    ifind = {
        "revenue": AnchorReading("ifind", 1000.0, period="2026-06-30", period_basis="YTD",
                                 unit="currency_base", currency="CNY", caliber="operating_revenue",
                                 statement_scope="consolidated", adjustment="restated"),
        "roe": _p1_reading(period="2026-09-30"),
    }
    choice = {
        "revenue": AnchorReading("choice", 1100.0, period="2026-09-30", period_basis="YTD",
                                 unit="currency_base", currency="CNY", caliber="operating_revenue",
                                 statement_scope="consolidated", adjustment="restated"),
    }
    period, selected = select_current_financials(ifind, choice)
    assert period == "2026-09-30"
    assert selected["roe"].source == "ifind" and selected["roe"].value == 29.34


# ----------------------------------------------- 审查修复回归（取消保留/出处/标签）

def test_cancel_during_optional_work_keeps_early_ifind_flow_windows(probe_transport, monkeypatch):
    """资金早批完成后辅助工作取消：三窗口/来源/截至日不得被空部分块替换，
    也不得为同一需求重复出站（研究取消即停止取数/建模）。"""
    from src.core.research_run import ResearchStopped

    run = ResearchRun(120)

    def stopping_dragon(self, code, **kwargs):
        run.stop("cancelled")
        raise ResearchStopped(run.reason_text)

    monkeypatch.setattr(DataFetcherManager, "get_dragon_tiger_context", stopping_dragon)
    with run.bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    stock_flow = result["capital_flow"]["data"]["stock_flow"]
    assert stock_flow["source"] == "ifind"
    assert stock_flow["main_net_inflow"] == pytest.approx(-114551026.71)
    assert stock_flow["as_of_date"] == "2026-10-30"
    assert all((stock_flow.get("coverage") or {}).get(str(n), {}).get("complete") for n in (1, 5, 10))
    flow_calls = [tool for tool, _ in probe_transport["ifind"] if tool == "get_stock_performance"]
    assert flow_calls == ["get_stock_performance"], "取消后不得重新出站补取资金"


def test_cancel_at_choice_snapshot_keeps_early_flow_in_shared_context(probe_transport, monkeypatch):
    """更早退出路径（Choice 快照处取消，早于 auxiliary try）：早批资金块已即时
    登记进共享上下文，ResearchStopped 传播后 ctx 仍持有完整三窗口资料。"""
    from src.core.research_run import ResearchStopped
    from src.deep_research_dims.context import SharedContext

    run = ResearchRun(120)
    run.context = SharedContext("688183", "生益电子", "2026-11-01T10:00:00")

    def stopped_snapshot(self, *args, **kwargs):
        run.stop("cancelled")
        raise ResearchStopped(run.reason_text)

    monkeypatch.setattr(ChoiceAdapter, "snapshot", stopped_snapshot)
    with run.bind(), pytest.raises(ResearchStopped):
        DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    block = run.context.fundamental.get("capital_flow")
    assert isinstance(block, dict), "早批资金块必须在共享上下文预登记"
    stock_flow = block["data"]["stock_flow"]
    assert stock_flow["source"] == "ifind"
    assert stock_flow["main_net_inflow"] == pytest.approx(-114551026.71)
    assert stock_flow["inflow_5d"] is not None and stock_flow["inflow_10d"] is not None
    assert stock_flow["as_of_date"] == "2026-10-30"


def test_interrupted_report_retains_stage_zero_capital_facts(probe_transport):
    """取消早于资金研究员：阶段 0 早批资金事实随受控中断结果/保存载荷留存，
    全程无新增取数或模型调用（dim 为降级/取消口径，不伪造评分）。"""
    from src.agent.deep_research.orchestrator import _interrupted_result
    from src.deep_research_dims.context import SharedContext

    run = ResearchRun(120)
    with run.bind():
        block = DataFetcherManager().get_capital_flow_context("688183", budget_seconds=30, include_sector_flows=False)
    run.context = SharedContext("688183", "生益电子", "2026-11-01T10:00:00", fundamental={"capital_flow": block})
    run.stop("cancelled")
    result = _interrupted_result(run, "688183", "生益电子", {"capital"}, None)
    dim = result.dims["capital"]
    assert dim.status == "degraded" and dim.score is None
    labels = {fact.label for fact in dim.retained_facts}
    assert {"单日主力净流入", "5交易日主力净流入", "10交易日主力净流入"} <= labels
    by_label = {fact.label: fact for fact in dim.retained_facts}
    assert by_label["单日主力净流入"].value == "-114551026.71"
    assert by_label["单日主力净流入"].source == "ifind"
    assert "-114551026.71" in result.markdown and "ifind" in result.markdown
    saved = json.dumps(result.dims_payload["capital"], ensure_ascii=False)
    assert "-114551026.71" in saved and "ifind" in saved, "保存/API 载荷同链路留存"


@pytest.mark.parametrize("with_existing_capital_dim", [False, True])
def test_interrupted_capital_retains_qualified_market_caps_without_flow(
        probe_transport, monkeypatch, with_existing_capital_dim):
    """无资金块的纯中断用例：已取得的合格总市值/流通市值（真实 iFinD 探针经生产
    资格层落进 field_meta）经既有 get_stock_info 投影随中断资金维度留存——缺资金
    块不阻断；已有降级资金维度时本地追加/去重；全程零新增出站/取数，不改冻结
    的 ctx/run.dimensions，不伪造评分。"""
    from decimal import Decimal
    from src.agent.deep_research import explore_agents
    from src.agent.deep_research.orchestrator import _interrupted_result
    from src.core.research_run import ResearchStopped
    from src.deep_research_dims.context import SharedContext
    from src.schemas.deep_research_dims import CapitalDim

    run = ResearchRun(120)
    run.context = SharedContext("688183", "生益电子", "2026-11-01T10:00:00")

    def stopping_native(method, args, options, deadline):
        run.stop("cancelled")
        raise ResearchStopped(run.reason_text)

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(stopping_native))
    with run.bind():
        raw = DataFetcherManager().get_fundamental_context(
            "688183", budget_seconds=60, include_sector_flows=False)
    from src.agent.tools.data_tools import _compact_fundamental_context
    compact = _compact_fundamental_context(raw)
    # 与 _safe_fundamental 同形的共享上下文：资金块 + valuation/growth 的
    # field_meta 提升（中断资金投影消费的正是这两个键）。
    run.context.fundamental = {
        "capital_flow": compact.get("capital_flow") or {},
        "field_meta": {
            **((compact.get("valuation") or {}).get("field_meta") or {}),
            **((compact.get("growth") or {}).get("field_meta") or {}),
        },
    }
    caps = run.context.fundamental["field_meta"]
    assert Decimal(str(caps["circ_mv"]["value"])) == Decimal("54719840000"), "真实探针必须先取得合格市值"
    run.context.fundamental.pop("capital_flow", None)  # 纯用例：资金块缺失不得阻断市值留存
    if with_existing_capital_dim:
        run.dimensions["capital"] = CapitalDim(status="degraded", degraded_reason="资金尚未形成解读")

    calls: list[bool] = []

    def no_new_work(*args, **kwargs):
        calls.append(True)
        raise AssertionError("停止后的中断组装只能投影已取得事实")

    monkeypatch.setattr(explore_agents, "_market_cap_materials", no_new_work)
    monkeypatch.setattr(DataFetcherManager, "get_fundamental_context", no_new_work)
    monkeypatch.setattr(DataFetcherManager, "get_capital_flow_context", no_new_work)
    frozen = deepcopy(run.context.fundamental)
    result = _interrupted_result(run, "688183", "生益电子", {"capital"}, None)
    capital = result.dims_payload["capital"]
    facts = {item["label"]: item for item in capital["retained_facts"]}
    assert capital["status"] == "degraded" and capital["flow_score"] is None
    for label, expected in (("总市值", "56200860000"), ("流通市值", "54719840000")):
        assert label in facts, facts
        fact = facts[label]
        assert Decimal(fact["value"]) == Decimal(expected) and fact["source"] == "ifind", fact
        assert fact["period"].replace("-", "") == "20261030" and "CNY" in fact["unit"], fact
    assert "流通市值" in result.markdown
    assert not calls
    assert run.context.fundamental == frozen and not run.facts
    if with_existing_capital_dim:
        assert not run.dimensions["capital"].retained_facts, "不得改动冻结的 run 维度实例"


def test_cancel_during_optional_products_keeps_completed_current_facts(probe_transport, monkeypatch):
    """可选主营批次取消（真实 products seam）：取消保留契约覆盖全部可选查询
    退出——聚合照常落位已完成估值/财务/行业/资金，停止后不再发起任何新出站
    （资金不重复取），停止原因由 run 自身状态真实承载。"""
    from src.core.research_run import ResearchStopped

    run = ResearchRun(120)

    def stopping_products(self, code, period=None, *, deadline=None):
        run.stop("cancelled")
        raise ResearchStopped(run.reason_text)

    monkeypatch.setattr(IfindFetcher, "product_segments", stopping_products)
    with run.bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert result["valuation"]["data"]["pe_ratio"] == pytest.approx(24.7573)
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34)
    industry = result["research"]["industry_ifind"]
    assert industry["name"] == "电子" and industry["as_of"] == "2026-10-30" and not industry.get("errors")
    stock_flow = result["capital_flow"]["data"]["stock_flow"]
    assert stock_flow["source"] == "ifind" and stock_flow["as_of_date"] == "2026-10-30"
    assert all((stock_flow.get("coverage") or {}).get(str(n), {}).get("complete") for n in (1, 5, 10))
    flow_calls = [tool for tool, _ in probe_transport["ifind"] if tool == "get_stock_performance"]
    assert flow_calls == ["get_stock_performance"], "停止后不得重新出站补取资金"


def test_native_choice_stop_keeps_completed_current_facts(probe_transport, monkeypatch):
    """真实 Choice 原生传输停止（_native_request seam）：Choice 快照内部截停后
    聚合仍完整落位 iFinD 估值/财务/行业与早批资金，共享上下文同步持有资金块。"""
    from src.core.research_run import ResearchStopped
    from src.deep_research_dims.context import SharedContext

    run = ResearchRun(120)
    run.context = SharedContext("688183", "生益电子", "2026-11-01T10:00:00")

    def stopping_native(method, args, options, deadline):
        run.stop("cancelled")
        raise ResearchStopped(run.reason_text)

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(stopping_native))
    with run.bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    assert result["valuation"]["data"]["pe_ratio"] == pytest.approx(24.7573)
    assert result["growth"]["data"]["roe"] == pytest.approx(29.34)
    industry = result["research"]["industry_ifind"]
    assert industry["name"] == "电子" and industry["as_of"] == "2026-10-30"
    stock_flow = result["capital_flow"]["data"]["stock_flow"]
    assert stock_flow["source"] == "ifind" and stock_flow["main_net_inflow"] == pytest.approx(-114551026.71)
    registered = run.context.fundamental.get("capital_flow") or {}
    assert (registered.get("data") or {}).get("stock_flow", {}).get("source") == "ifind"
    flow_calls = [tool for tool, _ in probe_transport["ifind"] if tool == "get_stock_performance"]
    assert flow_calls == ["get_stock_performance"], "停止后不得重新出站补取资金"


def test_interrupted_report_keeps_official_industry_in_selected_sector(probe_transport, monkeypatch):
    """真实原生停止后的中断报告：所选 sector 维度降级且按既有纯构建器保留官方
    iFinD 行业（真实来源/级别/实际截止日期），保存载荷与 Markdown 同链路留存，
    不编造行业或评分。"""
    from src.agent.deep_research.orchestrator import _interrupted_result
    from src.core.research_run import ResearchStopped
    from src.deep_research_dims.context import SharedContext

    run = ResearchRun(120)
    run.context = SharedContext("688183", "生益电子", "2026-11-01T10:00:00")

    def stopping_native(method, args, options, deadline):
        run.stop("cancelled")
        raise ResearchStopped(run.reason_text)

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(stopping_native))
    with run.bind():
        fundamental = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60, include_sector_flows=False)
    from src.agent.tools.data_tools import _compact_fundamental_context
    compact = _compact_fundamental_context(fundamental)
    # 与 _safe_fundamental 同源的共享上下文字段：行业提示与 research 全部来自
    # 实际聚合结果，不手工编造。
    run.context.fundamental = {
        "industry_hint": industry_hint_from_research(compact.get("research") or {}, (compact.get("boards") or {}).get("data") or {}),
        "research": compact.get("research") or {},
        "capital_flow": compact.get("capital_flow") or {},
    }
    assert run.stop_reason, "原生停止必须已真实发生"
    result = _interrupted_result(run, "688183", "生益电子", {"sector"}, None)
    sector = result.dims["sector"]
    assert sector.status == "degraded" and sector.sector_hint == "电子"
    industry = [fact for fact in sector.retained_facts if fact.tool == "industry_ifind"]
    assert industry and industry[0].period == "2026-10-30" and industry[0].source == "ifind"
    saved = json.dumps(result.dims_payload["sector"], ensure_ascii=False)
    assert "industry_ifind" in saved and "2026-10-30" in saved, "保存载荷同链路留存"
    assert "电子" in result.markdown


@pytest.mark.parametrize("stop_at", ("choice_snapshot", "optional_products"))
def test_cancelled_service_keeps_selected_dims_facts(probe_transport, provider, tmp_path, monkeypatch, stop_at):
    """受控服务取消（真实可选 seam）：fundamental+sector+capital 三所选维度保持
    降级/取消口径且已取得事实经保存/API/Markdown 同链路留存；模型零调用，
    本地供应商资金只出站一次，持久化报告与下载内容一致。"""
    from decimal import Decimal
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.config import get_config
    from src.core.research_run import current_research, ResearchStopped
    from src.storage import DatabaseManager
    from src.services import deep_research_service as module
    from api.v1.endpoints.deep_research import router

    cfg = get_config()
    cfg.deep_research_engine = "dual_track"
    cfg.deep_research_timeout_seconds = 30
    cfg.litellm_model = cfg.agent_litellm_model = "openai/fixture"
    cfg.llm_model_list = [{"model_name": "openai/fixture", "litellm_params": {
        "model": "openai/fixture", "api_base": provider[0], "api_key": "fixture-key"}}]
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(DataFetcherManager, "get_realtime_quote", lambda *a, **k: None)
    monkeypatch.setattr("src.services.history_loader.load_history_df", lambda *a, **k: (__import__("pandas").DataFrame(), ""))
    monkeypatch.setattr(module, "_dual_track_adapter", None)
    monkeypatch.setattr(module, "_DEEP_RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(DatabaseManager, "_instance", None)
    database = DatabaseManager(f"sqlite:///{tmp_path / 'cancelled.sqlite'}")
    monkeypatch.setattr(module, "get_db", lambda: database)

    seen = {}

    def stop_after_early_flow(*args, **kwargs):
        run = current_research.get()
        assert run is not None and run.context is not None
        flow = run.context.fundamental["capital_flow"]["data"]["stock_flow"]
        assert flow["source"] == "ifind" and flow["as_of_date"] == "2026-10-30"
        seen["run"] = run
        run.stop("cancelled")
        raise ResearchStopped(run.reason_text)

    if stop_at == "choice_snapshot":
        monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(stop_after_early_flow))
    else:
        monkeypatch.setattr(IfindFetcher, "product_segments", stop_after_early_flow)
    try:
        result = module.deep_research_service.generate_report(
            "688183", "生益电子", dims=["capital", "fundamental", "sector"], force_refresh=True)
        assert seen, "预期的停止 seam 必须真实执行"
        fundamental = result["dimensions"]["fundamental"]
        assert fundamental["status"] == "degraded" and fundamental["data_quality"]["rule_usable_count"] == 6
        sector = result["dimensions"]["sector"]
        industry = [fact for fact in sector["retained_facts"] if fact["tool"] == "industry_ifind"]
        assert sector["status"] == "degraded" and industry and industry[0]["period"] == "2026-10-30"
        capital = result["dimensions"]["capital"]
        assert capital["status"] == "degraded" and capital["score"] is None
        facts = {item["label"]: item for item in capital["retained_facts"]}
        assert {"单日主力净流入", "5交易日主力净流入", "10交易日主力净流入"} <= facts.keys()
        assert facts["单日主力净流入"]["value"] == "-114551026.71"
        assert facts["单日主力净流入"]["source"] == "ifind"
        # issue #71：已取得的合格市值随中断资金维度经保存/API/Markdown 同链路留存。
        for label, value in (("总市值", "56200860000"), ("流通市值", "54719840000")):
            assert label in facts, facts
            fact = facts[label]
            assert Decimal(fact["value"]) == Decimal(value) and fact["source"] == "ifind", fact
            assert fact["period"].replace("-", "") == "20261030" and "CNY" in fact["unit"], fact
        assert "流通市值" in result["markdown"]
        assert "-114551026.71" in result["markdown"]
        assert database.get_deep_research_report(result["report_id"]) is not None
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            detail = client.get(f"/reports/{result['report_id']}")
            assert detail.status_code == 200
            payload = detail.json()["data"]["dimensions"]
            assert payload["capital"] == capital and payload["fundamental"] == fundamental
            assert payload["sector"] == sector
            download = client.get(f"/reports/{result['report_id']}/markdown")
            assert download.status_code == 200 and download.text == result["markdown"]
        assert provider[1]["count"] == 0, "取消后不得发起任何模型调用"
        flow_calls = [tool for tool, _ in probe_transport["ifind"] if tool == "get_stock_performance"]
        assert flow_calls == ["get_stock_performance"], "本地供应商资金只出站一次"
    finally:
        database._engine.dispose()


def test_dated_circ_mv_metadata_survives_manager_and_reaches_market_cap_anchor(probe_transport, monkeypatch):
    """带日期回执的流通市值（547.1984 亿）经 manager 投影保留来源/单位/时点，
    实际 get_stock_info 与市值锚点消费可用，无行情兜底出站。"""
    from src.agent.deep_research.explore_agents import _market_cap_materials
    from src.agent.tools.data_tools import _handle_get_stock_info
    from src.core.research_run import research_dimension
    from src.deep_research_dims.context import SharedContext

    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    record = result["valuation"]["field_meta"].get("circ_mv")
    assert record is not None, "流通市值字段记录必须随 manager 投影保留"
    assert record["value"] == 54719840000.0
    assert record["source"] == "ifind" and record["unit"] == "currency_base"
    assert record.get("rule_eligible") is True
    assert result["valuation"]["status"] == "ok", "可选 circ_mv 不改变三必填 status 口径"
    quotes: list = []
    monkeypatch.setattr(DataFetcherManager, "get_realtime_quote", lambda self, code, **k: quotes.append(code))
    tool_result = _handle_get_stock_info("688183")
    assert not quotes, "估值四项齐备时不得触发行情兜底"
    tool_meta = ((tool_result.get("fundamental_context") or {}).get("valuation") or {}).get("field_meta") or {}
    assert tool_meta.get("circ_mv", {}).get("value") == 54719840000.0
    assert tool_result.get("circ_mv") == 54719840000.0

    run = ResearchRun(120)
    ctx = SharedContext("688183", "生益电子", "2026-11-01T10:00:00")
    # 与 _safe_fundamental 相同的 field_meta 组装（估值+成长记录合并），不预构建资格
    ctx.fundamental = {"field_meta": {**result["valuation"]["field_meta"], **result["growth"]["field_meta"]}}
    run.context = ctx
    token = research_dimension.set("capital")
    try:
        with run.bind():
            material = _market_cap_materials()
        anchor = material["anchors"]["circ"]
        assert anchor["record"]["value"] == 54719840000.0
        assert anchor["record"]["source"] == "ifind"
        facts = {fact["label"]: fact for fact in run.facts["capital"]}
        assert facts["流通市值"]["value"] == "54719840000.0", "流通市值标量须随 get_stock_info 抽取留存"
    finally:
        research_dimension.reset(token)


def test_capital_tool_note_labels_actual_ifind_source(probe_transport):
    """iFinD 资金块的模型材料 note 必须按实际 source 标注，不得声称 Choice。"""
    from src.agent.tools.data_tools import _handle_get_capital_flow

    result = _handle_get_capital_flow("688183", include_sector_flows=False)
    assert result["source"] == "ifind"
    assert result["as_of_date"] == "2026-10-30"
    note = result.get("note") or ""
    assert "iFinD" in note, "note 必须按实际来源标注 iFinD"
    assert "Choice" not in note, "iFinD 窗口不得标注为 Choice"


def test_failed_flow_note_keeps_original_reason_and_fallback_source(choice_transport, monkeypatch):
    """iFinD 失败回退 Choice：失败 note 标实际回退来源，iFinD 原始失败原因保留。"""
    from data_provider.cross_source_validator import SourceReadError
    from src.agent.tools.data_tools import _handle_get_capital_flow

    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"

    async def failing_query(self, tool, question, *, deadline=None):
        raise SourceReadError("ifind", "timeout")

    monkeypatch.setattr(IfindFetcher, "_async_query", failing_query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )
    dates = ["2026-10-30", "2026-10-29", "2026-10-28"]

    def native(method, args, options, deadline):
        if method == "tradedates":
            return {"data": list(reversed(dates))}
        assert method == "csd"
        values = [-100.0, -200.0, -300.0]
        return {"codes": [args[0]], "indicators": ["NETINFLOW"], "dates": list(reversed(dates)),
                "data": {args[0]: [values]}}

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    result = _handle_get_capital_flow("300398", include_sector_flows=False)
    assert result["source"] == "choice", "回退块的来源标签必须是实际 Choice"
    note = result.get("note") or ""
    assert "Choice" in note
    assert any("timeout" in str(item) for item in result.get("errors", [])), "iFinD 原始失败原因必须随块保留"


def test_future_industry_cutoff_rejected_valuation_kept_choice_fallback(monkeypatch):
    """行业截止晚于严格日历请求截至日：拒绝采用并保留实际返回日期与原因；
    估值不受影响；hint/F2 不采用无效行业、不覆盖健康 Choice 补充。"""
    from datetime import date as _date
    from src.core import trading_calendar as _tc

    mutated = deepcopy(DATED_INFO_ENV)
    for meta in mutated["data"]["indicators_params"].values():
        if "截止日期" in meta:
            meta["截止日期"] = "20990102"
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: [_date(2026, 10, 30), _date(2026, 10, 29)][:max(0, min(count, 2))])

    async def query(self, tool, question, *, deadline=None):
        return deepcopy(mutated)

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )
    fetcher = IfindFetcher("https://fixture.invalid/mcp", "test-only")
    info = fetcher.current_info_snapshot("688183", deadline=None)
    industry = info["industry"]
    assert "industry_time_in_future" in industry["errors"]
    assert industry["as_of"] == "2099-01-02", "实际返回日期保留，不用请求日期回填"
    assert industry["name"] == "电子"
    assert any(reading.value is not None for reading in info["valuation"].values()), "估值批次不受行业拒绝影响"
    # hint：无效 iFinD 行业不采用，健康 Choice 补充保持生效
    hint = industry_hint_from_research(
        {"industry_ifind": industry, "industry": {"HISSW2021": "通信-Choice补充"}}, {})
    assert hint == "通信-Choice补充"
    # F2 消费端同一结论：errors 非空即不产出行业溯源事实
    from src.deep_research_dims.context import SharedContext
    from src.deep_research_dims.sector_dim import build_sector_dim

    ctx = SharedContext("688183", "生益电子", "2026-11-01T10:00:00")
    ctx.fundamental = {"industry_hint": hint, "research": {"industry_ifind": industry}}
    sector = build_sector_dim(ctx)
    assert not [fact for fact in sector.retained_facts if fact.tool == "industry_ifind"]


def test_manager_rejects_future_industry_and_keeps_choice_research_error(probe_transport, monkeypatch):
    """manager 装配层：未来日期行业不进 research，拒绝原因随 research errors 留存，
    其余当前资料照常装配。"""
    mutated = deepcopy(DATED_INFO_ENV)
    for meta in mutated["data"]["indicators_params"].values():
        if "截止日期" in meta:
            meta["截止日期"] = "20990102"

    async def query(self, tool, question, *, deadline=None):
        if tool == "get_stock_info":
            return deepcopy(mutated)
        return deepcopy(payloads_by_tool[tool])

    payloads_by_tool = {
        "get_stock_financials": FINANCIAL_ENV,
        "get_stock_performance": FLOW_ENV,
    }
    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    research = result["research"]
    assert "industry_ifind" not in research, "无效行业不得进入 research"
    assert any("ifind:industry:industry_time_in_future" in item for item in research.get("errors", []))
    assert result["valuation"]["data"]["total_mv"] == 56200860000.0


def test_all_rejected_valuations_keep_ifind_reasons_alongside_choice_failure(probe_transport, monkeypatch):
    """R10 根复现：全无日期估值 + Choice 登录失败——缺主值字段的拒绝证据不得
    只剩 Choice authentication_failed，真实 ifind 拒绝原因（time_unknown）与
    Choice 失败原因按源合并保留。"""
    payloads_by_tool = {"get_stock_financials": FINANCIAL_ENV, "get_stock_performance": FLOW_ENV}

    async def query(self, tool, question, *, deadline=None):
        if tool == "get_stock_info":
            return deepcopy(UNDATED_INFO_ENV)
        return deepcopy(payloads_by_tool[tool])

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    meta = result["valuation"]["field_meta"]
    for field in ("pe_ratio", "pb_ratio", "total_mv", "circ_mv"):
        record = meta.get(field) or {}
        errors = record.get("source_errors") or []
        ifind_reasons = {item.get("reason_code") for item in errors if item.get("source") == "ifind"}
        assert "time_unknown" in ifind_reasons, f"{field} 必须保留真实 ifind 拒绝原因：{record}"
        assert result["valuation"]["data"].get(field) is None, field
    for field in ("pe_ratio", "pb_ratio", "total_mv"):
        # Choice 快照覆盖的三必填字段：缺主值时既有 Choice 失败原因与 ifind 拒绝同存
        errors = (meta.get(field) or {}).get("source_errors") or []
        assert any(item.get("source") == "choice" for item in errors), \
            f"{field} 缺主值时既有 Choice 失败原因必须同存：{meta.get(field)}"


def test_healthy_choice_valuation_not_polluted_by_rejected_ifind(choice_transport, monkeypatch):
    """ifind 候选被拒 + 健康 Choice 估值兜底：主值/资格保持，不被 input_reasons
    污染（拒绝候选只留诊断不覆盖主值）。"""
    choice_transport.enable_ifind_mcp = True
    choice_transport.ifind_mcp_endpoint = "https://fixture.invalid/mcp"
    choice_transport.ifind_mcp_token = "test-only"
    choice_transport.financial_primary_source = "ifind"
    from datetime import date as _date
    from src.core import trading_calendar as _tc
    _sessions = [_date(2026, 10, 30), _date(2026, 10, 29)]
    monkeypatch.setattr(_tc, "recent_completed_trading_sessions",
                        lambda market, count, current_time=None, **_: _sessions[:max(0, min(count, len(_sessions)))])
    payloads = {"get_stock_financials": FINANCIAL_ENV, "get_stock_info": UNDATED_INFO_ENV, "get_stock_performance": FLOW_ENV}

    async def query(self, tool, question, *, deadline=None):
        return deepcopy(payloads[tool])

    monkeypatch.setattr(IfindFetcher, "_async_query", query, raising=False)
    monkeypatch.setattr(
        IfindFetcher, "get_instance",
        classmethod(lambda cls: IfindFetcher("https://fixture.invalid/mcp", "test-only")),
    )

    def native(method, args, options, deadline):
        indicators = args[1] if len(args) > 1 else ""
        if method == "tradedates":
            return {"data": ["2026-10-30"]}
        if indicators == "NAME,LATESTREPORTNEW":
            return {"codes": [args[0]], "indicators": ["NAME", "LATESTREPORTNEW"],
                    "dates": [], "data": {args[0]: ["生益电子", "2026/09/30"]}}
        values = {"PETTM": 20.0, "PB": 4.0, "MV": 50000000000.0}
        requested = indicators.split(",")
        # 其余请求返回空值占位（不触发 10001009 止损，Choice 估值批次保持健康）
        reply = [values.get(item) for item in requested]
        return {"codes": [args[0]], "indicators": requested, "dates": [],
                "data": {args[0]: reply}}

    monkeypatch.setattr(ChoiceAdapter, "_native_request", staticmethod(native))
    with ResearchRun(120).bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    assert result["valuation"]["data"]["pe_ratio"] == 20.0, "健康 Choice 主值保持"
    record = result["valuation"]["field_meta"]["pe_ratio"]
    assert record.get("source") == "choice" and not record.get("input_reasons"), \
        f"健康 Choice 主值不得被被拒 ifind 的 input_reasons 污染：{record}"
    ifind_errors = [item for item in record.get("source_errors") or [] if item.get("source") == "ifind"]
    assert "time_unknown" in {item.get("reason_code") for item in ifind_errors}, \
        f"健康主值存在时被拒 iFinD 拒绝证据仍须随记录保留：{record}"


def test_late_flow_after_stop_does_not_write_frozen_ctx(probe_transport, monkeypatch):
    """取消预登记沿既有 stop/finalized 语义：块返回后 run 才停止的晚到写入
    不得进冻结 ctx（本次调用内 result 保留不算 ctx 写入）。"""
    from src.deep_research_dims.context import SharedContext

    run = ResearchRun(120)
    run.context = SharedContext("688183", "生益电子", "2026-11-01T10:00:00")
    real_fetch = DataFetcherManager.get_capital_flow_context

    def stopping_flow(self, code, **kwargs):
        block = real_fetch(self, code, **kwargs)
        run.stop("cancelled")
        return block

    monkeypatch.setattr(DataFetcherManager, "get_capital_flow_context", stopping_flow)
    with run.bind():
        result = DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    assert "capital_flow" not in run.context.fundamental, "晚到资金块不得写进已停止的冻结 ctx"
    assert result["capital_flow"]["data"]["stock_flow"]["source"] == "ifind"


def test_flow_registration_respects_security_identity(probe_transport):
    """证券归属不串：ctx 属于另一只股票时早批块不得登记。"""
    from src.deep_research_dims.context import SharedContext

    run = ResearchRun(120)
    run.context = SharedContext("300398", "飞凯材料", "2026-11-01T10:00:00")
    with run.bind():
        DataFetcherManager().get_fundamental_context("688183", budget_seconds=60)
    assert "capital_flow" not in run.context.fundamental, "不匹配证券的 ctx 不得接收资金块"
