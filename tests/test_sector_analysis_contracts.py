# -*- coding: utf-8 -*-
"""板块分析模块 icontract 契约测试 (Layer 2 三层防御)。

注意：契约测试必须开 ICONTRACT_SLOW=true 跑（CI 已在 type-safety.yml 配置）：
- 违反契约时 icontract.ViolationError 应被抛出
- 满足契约时正常返回
- 离线运行，不依赖网络

契约来源：src/services/sector_analysis_service.py 中的 @require / @ensure 装饰器
- _prosperity_pillar（当日行情支柱）：4 维 None 或 [0, 100]；rank_of ∈ [1, total_boards]；
  budget 必须是未到期的 ReportBudget
- compose_report（v2 编排）：code 6 位数字；返回 v2 载荷契约键；
  assessment.status 与 identity.subject_status 一致；载荷不携带旧综合评分字段
"""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd
import pytest
from icontract import ViolationError

from src.services import sector_analysis_service as svc
from src.services.sector_analysis_service import BudgetExhausted, ReportBudget


def _budget(seconds: float = 30.0) -> ReportBudget:
    return ReportBudget(seconds)


# ========================================================================
# _prosperity_pillar 契约（当日行情支柱；mock 网络层后离线验证）
# ========================================================================

def _em_table(rows: int = 86) -> pd.DataFrame:
    df = pd.DataFrame({
        "板块名称": [f"板块{i}" for i in range(rows)],
        "板块代码": [f"BK{i:04d}" for i in range(rows)],
        "涨跌幅": [-(i + 1) * 0.1 for i in range(rows)],
        "换手率": [3.0] * rows,
        "成交额": [1e9] * rows,
        "总市值": [None] * rows,
        "上涨家数": [10] * rows,
        "下跌家数": [20] * rows,
        "领涨股票": ["x"] * rows,
        "领涨股票-涨跌幅": [None] * rows,
    })
    df.loc[36, "板块名称"] = "半导体"
    df.loc[36, "板块代码"] = "BK0036"
    df.loc[36, "涨跌幅"] = -3.7  # 第 37 高
    return df


class TestProsperityPillarContracts:
    """_prosperity_pillar 当日行情合约（rank_of ∈ [1, total_boards] + 4 维 [0, 100]）。"""

    def _run(self, monkeypatch: pytest.MonkeyPatch, table: pd.DataFrame,
             name: str = "半导体", board_code: str | None = None) -> Dict[str, Any]:
        import akshare as ak

        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: table)
        return svc._prosperity_pillar(name, budget=_budget(), board_code=board_code)

    def test_rank_within_range_for_normal_output(self, monkeypatch):
        """正常计算路径的 rank_of 恒在 [1, total_boards]（分位数学保证）。

        命名修正（第二轮审查）：原名 test_rank_out_of_range_raises_contract
        实际只验证正常输出，并未触发契约违反——rank = total - below 数学上
        不可能越界，icontract ensure 是防御层。真正的越界反例由 Pydantic
        IntradayPillar.validate_rank 覆盖（见 TestIntradayRankSchemaDefense）。
        """
        import akshare as ak
        import pandas as pd

        n = 5
        df = pd.DataFrame({
            "板块名称": ["A", "半导体", "C", "D", "E"],
            "板块代码": [f"BK{i:04d}" for i in range(n)],
            "涨跌幅": [1.0, 2.0, 3.0, 4.0, 5.0],
            "换手率": [None] * n,
            "成交额": [None] * n,
            "上涨家数": [None] * n,
            "下跌家数": [None] * n,
        })
        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: df)
        out = svc._prosperity_pillar("半导体", budget=_budget())
        assert out["sector_found"] is True
        assert out["rank_of"] is not None
        assert 1 <= out["rank_of"] <= out["total_boards"]

    def test_rank_within_total_boards(self, monkeypatch):
        result = self._run(monkeypatch, _em_table())
        assert result["sector_found"] is True
        assert result["total_boards"] == 86
        assert result["rank_of"] is not None
        assert 1 <= result["rank_of"] <= result["total_boards"]

    def test_dims_none_or_in_range(self, monkeypatch):
        result = self._run(monkeypatch, _em_table())
        for dim in ("momentum", "activity", "amount_activity", "breadth"):
            assert result[dim] is None or 0.0 <= result[dim] <= 100.0

    def test_expired_budget_raises_contract(self, monkeypatch):
        """到期 budget 违反 require 契约。"""
        import time

        import akshare as ak

        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: _em_table())
        budget = ReportBudget(0.05)
        time.sleep(0.1)
        with pytest.raises((ViolationError, BudgetExhausted)):
            svc._prosperity_pillar("半导体", budget=budget)

    def test_non_string_name_raises_contract(self, monkeypatch):
        import akshare as ak

        monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: _em_table())
        with pytest.raises(ViolationError):
            svc._prosperity_pillar(123, budget=_budget())  # type: ignore[arg-type]


class TestIntradayRankSchemaDefense:
    """rank 越界防御的真实反例：载荷层 Pydantic model_validator 拒绝越界。

    正常计算路径（rank = total - below）数学上不可能越界，icontract ensure
    是防御层；schema 的 model_validator 是越界到达载荷时的实际守门。
    """

    def test_rank_above_total_boards_rejected(self):
        from pydantic import ValidationError

        from src.schemas.sector_analysis import IntradayPillar

        with pytest.raises(ValidationError, match="board population"):
            IntradayPillar(rank_of=999, total_boards=86)

    def test_rank_within_total_boards_accepted(self):
        from src.schemas.sector_analysis import IntradayPillar

        pillar = IntradayPillar(rank_of=86, total_boards=86)
        assert pillar.rank_of == 86


# ========================================================================
# compose_report 契约（v2 编排）
# ========================================================================

class TestComposeReportContracts:
    """compose_report v2 编排合约。"""

    @pytest.fixture(autouse=True)
    def _mock_pillars(self, monkeypatch):
        monkeypatch.setattr(
            svc, "_identify_sector",
            lambda code, *, budget: {
                "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
                "board_name": "白酒Ⅱ", "board_code": "BK1277",
                "subject_status": "confirmed",
                "membership": {"verified": True, "member_count": 20},
                "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
                "classification_system": "东财行业板块",
                "classification_level": None, "classification_version": None,
                "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
                "sector_name": "白酒Ⅱ", "gaps": [], "_board_table": None,
            },
        )
        monkeypatch.setattr(
            svc, "_policy_pillar",
            lambda text: {"lean": "supportive", "label": "支持", "gaps": []},
        )
        monkeypatch.setattr(
            svc, "_base_rate_pillar",
            lambda text: {
                "base_rate": 0.5, "basis": "test", "hit_keyword": "白酒",
                "is_default": False, "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": 0.5, "all_rates": [],
            },
        )
        monkeypatch.setattr(
            svc, "_prosperity_pillar",
            lambda name, *, budget, board_code=None, board_table=None: {
                "sector_found": True,
                "momentum": 80.0, "activity": 60.0, "amount_activity": 40.0, "breadth": 100.0,
                "rank_of": 1, "total_boards": 86,
                "row": {}, "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_concepts_pillar",
            lambda code, *, budget: {"items": [], "source": "ifind",
                                     "total_returned": 0, "truncated": False,
                                     "gaps": []},
        )
        # issue #82：趋势/同业支柱与申万链为 compose 直接依赖，一并替身
        monkeypatch.setattr(
            svc, "_sector_trend_pillar",
            lambda code, name, *, budget, subject_confirmed=False, current_time=None: {
                "eligible": bool(subject_confirmed),
                "window_completed_sessions": 20, "interval_count": 19,
                "start_date": "2026-10-09" if subject_confirmed else None,
                "end_date": "2026-11-04" if subject_confirmed else None,
                "expected_dates": ["2026-10-09", "2026-10-12"] if subject_confirmed else [],
                "period_return_pct": 5.1234 if subject_confirmed else None,
                "benchmark": ({
                    "name": "沪深300", "symbol": "sh000300", "source": "东财指数日线",
                    "start_date": "2026-10-09", "end_date": "2026-11-04",
                    "qualified_sessions": 20, "period_return_pct": 3.8889, "gaps": [],
                } if subject_confirmed else None),
                "relative_return_pp": 1.2345 if subject_confirmed else None,
                "source": "东财行业板块日线" if subject_confirmed else None,
                "observed_at": "2026-11-05T14:00:02", "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_peers_pillar",
            lambda code, cons, *, budget, snapshot_observed_at=None,
            subject_confirmed=False: {
                "eligible": bool(subject_confirmed),
                "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）",
                "items": ([
                    {"code": code, "name": "测试股票", "amount": 3.1e9,
                     "change_pct": 1.65, "is_target": True},
                ] if subject_confirmed else []),
                "target_in_amount_top5": True if subject_confirmed else None,
                "target_change_pct_rank": 1 if subject_confirmed else None,
                "sample_size": 1 if subject_confirmed else 0,
                "snapshot_observed_at": "2026-11-05T14:00:01" if subject_confirmed else None,
                "source": "东财行业板块成分（身份验证同一快照）" if subject_confirmed else None,
                "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_resolve_sw_chain",
            lambda code, text, *, budget: {
                "l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                "l3_code": "851251.SI", "gaps": []},
        )

    def test_returns_v2_payload_contract_keys(self):
        result = svc.compose_report("600519", "测试股票", budget=_budget())
        required = {"payload_version", "stock_code", "stock_name", "generated_at",
                    "identity", "background", "concepts", "assessment", "gaps"}
        assert required.issubset(result.keys())
        assert result["payload_version"] == 2

    def test_status_agrees_with_subject(self):
        result = svc.compose_report("600519", "测试股票", budget=_budget())
        assert result["assessment"]["status"] == "unscored"
        assert result["identity"]["subject_status"] == "confirmed"

    def test_retired_composite_fields_stay_none(self):
        """评分退役字段保留路径但显式 None（追加兼容 + 不恢复旧结论）。"""
        result = svc.compose_report("600519", "测试股票", budget=_budget())
        for retired in ("sector_score", "band", "beta_meaning", "weights",
                        "prosperity_score"):
            assert result[retired] is None
        # 非评分事实路径投影可读
        assert result["identification"]["sector_name"] == "白酒Ⅱ"
        assert result["policy"]["score"] is None
        assert result["prosperity"]["capital"] is None

    def test_invalid_code_raises_contract(self):
        with pytest.raises(ViolationError):
            svc.compose_report("12345", "测试", budget=_budget())  # 5 位

    def test_non_digit_code_raises_contract(self):
        with pytest.raises(ViolationError):
            svc.compose_report("ABC123", "测试", budget=_budget())  # 含字母

    def test_too_long_code_raises_contract(self):
        with pytest.raises(ViolationError):
            svc.compose_report("1234567", "测试", budget=_budget())  # 7 位

    def test_inconsistent_status_raises_contract(self, monkeypatch):
        """身份 confirmed 但 assessment 被改为 insufficient_data：跨字段不变式守门。"""
        import src.services.sector_analysis_service as mod

        orig = mod._compose_status
        monkeypatch.setattr(mod, "_compose_status", lambda ident: ("insufficient_data", ["x"]))
        try:
            with pytest.raises(ViolationError):
                svc.compose_report("600519", "测试股票", budget=_budget())
        finally:
            monkeypatch.setattr(mod, "_compose_status", orig)


# ========================================================================
# issue #82 新契约：区间收益（Decimal）+ 趋势/同业资格一致性
# ========================================================================


class TestPeriodReturnContracts:
    """_period_return_pct：入参 >=2 个正有限 Decimal，出参有限 Decimal。"""

    def test_valid_input_returns_finite_decimal(self):
        from decimal import Decimal

        out = svc._period_return_pct([Decimal("100"), Decimal("110")])
        assert isinstance(out, Decimal) and out.is_finite()
        assert out == Decimal("10.0")

    def test_single_close_raises(self):
        from decimal import Decimal

        with pytest.raises(ViolationError):
            svc._period_return_pct([Decimal("100")])

    def test_zero_or_negative_close_raises(self):
        from decimal import Decimal

        with pytest.raises(ViolationError):
            svc._period_return_pct([Decimal("100"), Decimal("0")])
        with pytest.raises(ViolationError):
            svc._period_return_pct([Decimal("100"), Decimal("-1")])


class TestTrendPeersEligibilityContracts:
    """compose_report：trend/peers eligible 必须严格等于 confirmed 资格。"""

    @pytest.fixture(autouse=True)
    def _zero_tcp_guard(self, monkeypatch: pytest.MonkeyPatch):
        """零出站 TCP 守卫：任何 connect 尝试立即失败并计数，用例结束要求 0 次。

        不满足于「出站异常被服务层吞成 partial success」：只要发生连接
        尝试，计数即 > 0，用例以失败告终。即使进程 config 启用 iFinD，
        本套件也已替身非目标支柱（_prosperity_pillar/_concepts_pillar），
        不会读取真实凭据或发起任何供应商请求。
        """
        import socket

        attempts: list[str] = []

        def _guarded_connect(sock: socket.socket, address: Any) -> None:
            attempts.append(repr(address))
            raise AssertionError(f"unexpected outbound TCP attempt: {address!r}")

        monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
        yield
        assert not attempts, (
            f"expected ZERO outbound TCP attempts, got {len(attempts)}: {attempts}"
        )

    def _run(self, monkeypatch: pytest.MonkeyPatch, *, subject: str,
             trend_eligible: bool, peers_eligible: bool):
        monkeypatch.setattr(
            svc, "_identify_sector",
            lambda code, *, budget: {
                "industry_em": "白酒Ⅱ", "industry_cninfo": "", "business_scope": "",
                "board_name": "白酒Ⅱ", "board_code": "BK1277",
                "subject_status": subject,
                "membership": {"verified": subject == "confirmed",
                               "member_count": 20},
                "observed_at": "2026-11-05T14:00:00", "vendor_as_of": None,
                "classification_system": "东财行业板块",
                "classification_level": None, "classification_version": None,
                "sw_chain": {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
                "sector_name": "白酒Ⅱ", "gaps": [], "_board_table": None,
            },
        )
        monkeypatch.setattr(
            svc, "_sector_trend_pillar",
            lambda code, name, *, budget, subject_confirmed=False, current_time=None: {
                "eligible": trend_eligible, "window_completed_sessions": 20,
                "interval_count": 19, "start_date": None, "end_date": None,
                "expected_dates": [], "period_return_pct": None, "benchmark": None,
                "relative_return_pp": None, "source": None,
                "observed_at": "2026-11-05T14:00:02", "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_peers_pillar",
            lambda code, cons, *, budget, snapshot_observed_at=None,
            subject_confirmed=False: {
                "eligible": peers_eligible,
                "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）",
                "items": [], "target_in_amount_top5": None,
                "target_change_pct_rank": None, "sample_size": 0,
                "snapshot_observed_at": None, "source": None, "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_resolve_sw_chain",
            lambda code, text, *, budget: {
                "l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
        )
        # 非目标支柱替身（本套件只测 compose_report 资格一致性 @ensure）：
        # compose 拿到 _board_table=None 会触发 _prosperity_pillar 真实出站
        # （AkShare 板块表），_concepts_pillar 在 config 启用 iFinD 时出站
        # （MCP 供应商）。二者是本套件唯一的真实供应商边界，替身后完全离线；
        # policy/base_rate 是纯本地计算，不属于供应商边界，保持真实路径。
        monkeypatch.setattr(
            svc, "_prosperity_pillar",
            lambda name, *, budget, board_code=None, board_table=None: {
                "sector_found": True,
                "momentum": 80.0, "activity": 60.0, "amount_activity": 40.0,
                "breadth": 100.0, "rank_of": 1, "total_boards": 86,
                "row": {}, "gaps": [],
            },
        )
        monkeypatch.setattr(
            svc, "_concepts_pillar",
            lambda code, *, budget: {"items": [], "source": "ifind",
                                     "total_returned": 0, "truncated": False,
                                     "gaps": []},
        )
        return svc.compose_report("600519", "测试股票", budget=_budget())

    def test_confirmed_with_matching_eligibility_passes(self, monkeypatch):
        result = self._run(monkeypatch, subject="confirmed",
                           trend_eligible=True, peers_eligible=True)
        assert result["sector_trend"]["eligible"] is True
        assert result["sector_peers"]["eligible"] is True

    def test_unconfirmed_with_matching_eligibility_passes(self, monkeypatch):
        result = self._run(monkeypatch, subject="unverified",
                           trend_eligible=False, peers_eligible=False)
        assert result["sector_trend"]["eligible"] is False

    def test_confirmed_but_trend_not_eligible_raises(self, monkeypatch):
        """confirmed 身份但趋势块未取得资格（实现错位）：契约拒绝。"""
        with pytest.raises(ViolationError):
            self._run(monkeypatch, subject="confirmed",
                      trend_eligible=False, peers_eligible=True)

    def test_unconfirmed_but_peers_eligible_raises(self, monkeypatch):
        """unverified 身份但同业块给了资格（无资格查询）：契约拒绝。"""
        with pytest.raises(ViolationError):
            self._run(monkeypatch, subject="unverified",
                      trend_eligible=False, peers_eligible=True)
