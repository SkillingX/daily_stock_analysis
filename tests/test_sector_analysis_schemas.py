# -*- coding: utf-8 -*-
"""板块分析 Pydantic schema 测试 (Layer 3 三层防御) —— v2 载荷（issue #81）。

验证：
- 严格模式：拒绝隐式类型转换（str→int 等）
- frozen：禁止运行期赋值
- extra='forbid'：拒绝未声明字段（含旧综合评分字段反样）
- 数值范围：ge/le 守门
- 模式 pattern：stock_code 必须是 6 位数字
- 字面量 Literal：subject_status / payload status / lean / payload_version 必须是白名单

不验证：
- 业务不变式（status 与 subject_status 的一致性、载荷不含旧综合分）——
  由 icontract Layer 2 在 compose_report 上守门
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from pydantic import ValidationError

from src.schemas.sector_analysis import (
    AssessmentBlock,
    BackgroundBlock,
    BaseRateRow,
    BoardIdentity,
    ConceptsBlock,
    IntradayPillar,
    IntradayRow,
    MembershipInfo,
    PolicyPillar,
    SectorAnalysisDeleteResponse,
    SectorAnalysisDetailResponse,
    SectorAnalysisGenerateResponse,
    SectorAnalysisListResponse,
    SectorAnalysisReportItem,
    SectorReportPayload,
)


def _valid_identity() -> Dict[str, Any]:
    return {
        "industry_em": "白酒Ⅱ",
        "industry_cninfo": "酒、饮料和精制茶制造业",
        "business_scope": "白酒制造",
        "board_name": "白酒Ⅱ",
        "board_code": "BK1277",
        "subject_status": "confirmed",
        "membership": {"verified": True, "member_count": 20},
        "observed_at": "2026-11-05T14:00:00",
        "vendor_as_of": None,
        "classification_system": "东财行业板块",
        "classification_level": None,
        "classification_version": None,
        "sw_chain": {"l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                     "l3_code": "851251.SI", "gaps": []},
        "sector_name": "白酒Ⅱ",
        "gaps": [],
    }


def _valid_payload() -> Dict[str, Any]:
    """返回一个合法完整的 compose_report v2 载荷 dict。"""
    return {
        "payload_version": 2,
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "generated_at": "2026-11-05T14:00:00",
        "identity": _valid_identity(),
        "background": {
            "policy": {"lean": "neutral", "label": "中性", "gaps": []},
            "base_rate": {
                "base_rate": 0.55, "basis": "industry_base_rate:白酒",
                "hit_keyword": "白酒", "is_default": False,
                "table_version": "v1.0", "table_updated": "2026-09-29",
                "table_industry_count": 21, "percentile": 0.95, "all_rates": [],
            },
            "intraday": {
                "sector_found": True, "momentum": 72.9, "activity": 60.0,
                "amount_activity": 50.0, "breadth": 90.0,
                "rank_of": 135, "total_boards": 496,
                "row": {"board_name": "白酒Ⅱ", "board_code": "BK1277",
                        "change_pct": 1.65, "turnover_rate": 2.3, "amount": 3.1e9,
                        "up_count": 18, "down_count": 2},
                "gaps": [],
            },
        },
        "concepts": {
            "items": [{"name": "白酒概念", "source": "ifind", "as_of_nature": "latest"}],
            "source": "ifind", "total_returned": 1, "truncated": False, "gaps": [],
        },
        "sector_trend": {
            "eligible": True, "window_completed_sessions": 20, "interval_count": 19,
            "start_date": "2026-10-09", "end_date": "2026-11-04",
            "expected_dates": ["2026-10-09", "2026-10-12"],
            "period_return_pct": 5.1234,
            "benchmark": {"name": "沪深300", "symbol": "sh000300",
                          "source": "东财指数日线", "start_date": "2026-10-09",
                          "end_date": "2026-11-04", "qualified_sessions": 20,
                          "period_return_pct": 3.8889, "gaps": []},
            "relative_return_pp": 1.2345,
            "source": "东财行业板块日线", "observed_at": "2026-11-05T14:00:00",
            "gaps": [],
        },
        "sector_peers": {
            "eligible": True,
            "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）",
            "items": [
                {"code": "600519", "name": "贵州茅台", "amount": 3.1e9,
                 "change_pct": 1.65, "is_target": True},
                {"code": "000858", "name": "五粮液", "amount": 2.2e9,
                 "change_pct": 0.9, "is_target": False},
            ],
            "target_in_amount_top5": True, "target_change_pct_rank": 1,
            "sample_size": 2, "snapshot_observed_at": "2026-11-05T14:00:01",
            "source": "东财行业板块成分（身份验证同一快照）", "gaps": [],
        },
        "assessment": {"status": "unscored", "completeness": "complete",
                       "score": None, "reasons": []},
        # ---- v1 兼容投影（追加兼容：非评分事实路径可读 + 评分退役显式 None）----
        "identification": {
            "industry_em": "白酒Ⅱ", "industry_cninfo": "酒、饮料和精制茶制造业",
            "business_scope": "白酒制造", "sector_name": "白酒Ⅱ",
            "sw_chain": {"l1": "食品饮料", "l2": "白酒Ⅱ", "l3": "白酒Ⅲ",
                         "l3_code": "851251.SI", "gaps": []},
            "gaps": [],
        },
        "policy": {"lean": "neutral", "label": "中性", "score": None, "gaps": []},
        "base_rate": {
            "base_rate": 0.55, "basis": "industry_base_rate:白酒",
            "hit_keyword": "白酒", "is_default": False,
            "table_version": "v1.0", "table_updated": "2026-09-29",
            "table_industry_count": 21, "percentile": 0.95, "all_rates": [],
        },
        "prosperity": {
            "sector_found": True, "momentum": 72.9, "activity": 60.0,
            "capital": None, "breadth": 90.0,
            "rank_of": 135, "total_boards": 496,
            "row": {"board_name": "白酒Ⅱ", "board_code": "BK1277",
                    "change_pct": 1.65, "turnover_rate": 2.3, "amount": 3.1e9,
                    "up_count": 18, "down_count": 2, "volume": None,
                    "company_count": None},
            "gaps": [],
        },
        "sector_score": None, "band": None, "beta_meaning": None,
        "weights": None, "prosperity_score": None,
        "gaps": [],
    }


class TestSectorReportPayload:
    """v2 主载荷契约。"""

    def test_valid_payload(self):
        payload = SectorReportPayload.model_validate(_valid_payload())
        assert payload.payload_version == 2
        assert payload.identity.subject_status == "confirmed"
        assert payload.assessment.status == "unscored"
        assert payload.assessment.score is None
        assert payload.background.intraday.amount_activity == 50.0

    def test_retired_score_fields_reject_non_null(self):
        """评分退役字段是 None-only：任何非 None 值（旧总分/旧 band）拒绝。"""
        bad = _valid_payload()
        bad["sector_score"] = 61.5
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(bad)
        bad2 = _valid_payload()
        bad2["band"] = "中性"
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(bad2)

    def test_legacy_projection_fields_readable(self):
        """v1 兼容投影：非评分事实路径可读，评分字段显式 None（追加兼容）。"""
        payload = SectorReportPayload.model_validate(_valid_payload())
        assert payload.identification.industry_em == "白酒Ⅱ"
        assert payload.policy.lean == "neutral"
        assert payload.policy.score is None  # 政策档位分退役
        assert payload.base_rate.base_rate == 0.55
        assert payload.prosperity.momentum == 72.9
        assert payload.prosperity.capital is None  # 旧「资金」维度退役
        assert payload.sector_score is None
        assert payload.band is None
        assert payload.prosperity_score is None

    def test_invalid_payload_version_rejected(self):
        bad = _valid_payload()
        bad["payload_version"] = 1
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(bad)

    def test_invalid_status_rejected(self):
        bad = _valid_payload()
        bad["assessment"] = {"status": "partial", "score": None, "reasons": []}
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(bad)

    def test_invalid_subject_status_rejected(self):
        bad = _valid_payload()
        bad["identity"]["subject_status"] = "maybe"
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(bad)

    def test_strict_mode_rejects_string_number(self):
        bad = _valid_payload()
        bad["background"]["intraday"]["momentum"] = "72.9"  # str → float 应被 strict 拒绝
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(bad)

    def test_frozen_model(self):
        payload = SectorReportPayload.model_validate(_valid_payload())
        with pytest.raises(ValidationError):
            payload.stock_code = "000858"  # type: ignore[misc]

    def test_completeness_literal(self):
        bad = _valid_payload()
        bad["assessment"] = {"status": "unscored", "completeness": "unknown",
                             "score": None, "reasons": []}
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(bad)

    def test_concepts_total_returned_and_truncated(self):
        from src.schemas.sector_analysis import ConceptsBlock

        block = ConceptsBlock(items=[
            {"name": f"概念{i}", "source": "ifind", "as_of_nature": "latest"}
            for i in range(50)
        ], total_returned=51, truncated=True, gaps=["相关概念：展示上限截取"])
        assert block.total_returned == 51 and block.truncated is True
        with pytest.raises(ValidationError):
            ConceptsBlock(items=[
                {"name": f"概念{i}", "source": "ifind", "as_of_nature": "latest"}
                for i in range(51)
            ])

    def test_classification_metadata_unknown_is_valid(self):
        ident = _valid_identity()
        assert ident["classification_level"] is None
        model = BoardIdentity.model_validate(ident)
        assert model.classification_system == "东财行业板块"
        assert model.classification_level is None

    def test_score_field_must_be_none(self):
        """score 恒为 None（首版显式未评分），任何数值都拒绝。"""
        bad = _valid_payload()
        bad["assessment"]["score"] = 60.0
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(bad)

    def test_unknown_field_rejected(self):
        """extra=forbid：任意未声明字段拒绝（基线行为保留）。"""
        bad = _valid_payload()
        bad["some_unknown_field"] = "x"
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(bad)

    def test_stock_code_5_digits_or_letter_rejected(self):
        """stock_code 必须是 6 位数字（基线 TestPatterns 保留）。"""
        for bad_code in ("60051", "60051A"):
            bad = _valid_payload()
            bad["stock_code"] = bad_code
            with pytest.raises(ValidationError):
                SectorReportPayload.model_validate(bad)

    def test_gap_too_long_rejected(self):
        """缺口文本超长拒绝（基线行为保留）。"""
        bad = _valid_payload()
        bad["gaps"] = ["超长缺口" * 51]  # 204 个字符 > 200
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(bad)


class TestSubModels:
    def test_rank_validator(self):
        with pytest.raises(ValidationError):
            IntradayPillar.model_validate({
                "sector_found": True, "momentum": None, "activity": None,
                "amount_activity": None, "breadth": None,
                "rank_of": 500, "total_boards": 496, "row": {}, "gaps": [],
            })

    def test_membership_range(self):
        with pytest.raises(ValidationError):
            MembershipInfo.model_validate({"verified": True, "member_count": -1})

    def test_base_rate_row_range(self):
        with pytest.raises(ValidationError):
            BaseRateRow.model_validate({"keyword": "白酒", "base_rate": 1.5, "note": None})

    def test_policy_lean_literal(self):
        with pytest.raises(ValidationError):
            PolicyPillar.model_validate({"lean": "bearish", "label": "空", "gaps": []})

    def test_policy_lean_valid_values_accepted(self):
        """基线 TestLiterals 保留：合法 lean 三值全部接受。"""
        for lean in ("supportive", "neutral", "restrictive"):
            pillar = PolicyPillar.model_validate({"lean": lean, "label": "标签", "gaps": []})
            assert pillar.lean == lean

    def test_intraday_momentum_range_rejected(self):
        """基线 TestNumericRanges 保留：momentum 超出 [0, 100] 拒绝。"""
        with pytest.raises(ValidationError):
            IntradayPillar.model_validate({
                "sector_found": True, "momentum": 100.5, "activity": None,
                "amount_activity": None, "breadth": None,
                "row": {}, "gaps": [],
            })

    def test_rank_negative_rejected(self):
        """基线 TestNumericRanges 保留：rank_of 负数拒绝（ge=1）。"""
        with pytest.raises(ValidationError):
            IntradayPillar.model_validate({
                "sector_found": True, "momentum": None, "activity": None,
                "amount_activity": None, "breadth": None,
                "rank_of": -1, "total_boards": 86, "row": {}, "gaps": [],
            })

    def test_sw_chain_allows_none(self):
        """基线 TestNestedStructures 保留：申万链字段允许 None。"""
        from src.schemas.sector_analysis import SWChain

        chain = SWChain.model_validate({})
        assert chain.l1 is None and chain.l3_code is None

    def test_base_rate_row_required_fields(self):
        """基线 TestNestedStructures 保留：基率行 keyword/base_rate 必填。"""
        with pytest.raises(ValidationError):
            BaseRateRow.model_validate({"note": None})

    def test_concept_item_source_literal(self):
        with pytest.raises(ValidationError):
            ConceptsBlock.model_validate({
                "items": [{"name": "白酒概念", "source": "akshare", "as_of_nature": None}],
                "source": "ifind", "gaps": [],
            })

    def test_vendor_as_of_none_is_valid(self):
        ident = BoardIdentity.model_validate({**_valid_identity(), "vendor_as_of": None})
        assert ident.vendor_as_of is None

    def test_background_requires_all_three(self):
        with pytest.raises(ValidationError):
            BackgroundBlock.model_validate({"policy": {"lean": None, "label": "未知", "gaps": []}})

    def test_intraday_row_defaults(self):
        row = IntradayRow.model_validate({})
        assert row.board_name is None

    def test_synthesis_rejected_count_default_and_bounds(self):
        # 边界终态（issue #83）：rejected_count 追加兼容——默认 0（旧载荷
        # 无此键可读）；范围 0..SYNTH_CLAIMS_RAW_CAP；负数/非整数拒绝
        from src.schemas.sector_analysis import SectorSynthesisBlock

        block = SectorSynthesisBlock.model_validate({"eligible": True,
                                                     "status": "ok"})
        assert block.rejected_count == 0
        mixed = SectorSynthesisBlock.model_validate({
            "eligible": True, "status": "ok", "rejected_count": 1,
            "claims": [{"kind": "interpretation", "text": "政策构成催化",
                        "evidence_refs": ["cat-1"]}],
        })
        assert mixed.rejected_count == 1
        with pytest.raises(ValidationError):
            SectorSynthesisBlock.model_validate({"eligible": True,
                                                 "status": "ok",
                                                 "rejected_count": -1})
        with pytest.raises(ValidationError):
            SectorSynthesisBlock.model_validate({"eligible": True,
                                                 "status": "ok",
                                                 "rejected_count": 25})
        with pytest.raises(ValidationError):
            SectorSynthesisBlock.model_validate({"eligible": True,
                                                 "status": "ok",
                                                 "rejected_count": "1"})


class TestApiIoModels:
    def _valid_item(self) -> Dict[str, Any]:
        return {
            "id": "sa_202611051200", "stock_code": "600519", "stock_name": "贵州茅台",
            "created_at": "2026-11-05T12:00:00", "sector_score": None,
        }

    def test_list_item_without_new_fields(self):
        item = SectorAnalysisReportItem.model_validate(self._valid_item())
        assert item.sector_score is None

    def test_stock_code_pattern(self):
        bad = self._valid_item() | {"stock_code": "60051"}
        with pytest.raises(ValidationError):
            SectorAnalysisReportItem.model_validate(bad)

    def test_generate_response(self):
        resp = SectorAnalysisGenerateResponse.model_validate({
            "report_id": "sa_202611051200", "stock_code": "600519",
            "stock_name": "贵州茅台", "markdown": "# 报告",
            "analysis": _valid_payload(),
        })
        assert resp.analysis.payload_version == 2

    def test_generate_response_empty_markdown_rejected(self):
        with pytest.raises(ValidationError):
            SectorAnalysisGenerateResponse.model_validate({
                "report_id": "sa_202611051200", "stock_code": "600519",
                "stock_name": "贵州茅台", "markdown": "",
                "analysis": _valid_payload(),
            })

    def test_list_response(self):
        resp = SectorAnalysisListResponse.model_validate(
            {"success": True, "data": [self._valid_item()], "total": 1}
        )
        assert resp.total == 1

    def test_detail_response(self):
        resp = SectorAnalysisDetailResponse.model_validate(
            {"success": True, "data": {**self._valid_item(), "markdown": "# 报告",
                                       "analysis_json": {"payload_version": 2}}}
        )
        assert resp.data.markdown == "# 报告"

    def test_delete_response(self):
        resp = SectorAnalysisDeleteResponse.model_validate(
            {"success": True, "deleted": "sa_202611051200"}
        )
        assert resp.deleted == "sa_202611051200"

    def test_assessment_reasons_limit(self):
        with pytest.raises(ValidationError):
            AssessmentBlock.model_validate(
                {"status": "insufficient_data", "score": None,
                 "reasons": [f"原因{i}" for i in range(11)]}
            )

    def test_report_item_score_out_of_range_rejected(self):
        """基线 TestReportItem 保留：列表 sector_score 超出 [0, 100] 拒绝。"""
        bad = self._valid_item() | {"sector_score": 101.0}
        with pytest.raises(ValidationError):
            SectorAnalysisReportItem.model_validate(bad)

    def test_empty_list_response_passes(self):
        """基线 TestListResponse 保留：空历史列表合法。"""
        resp = SectorAnalysisListResponse.model_validate(
            {"success": True, "data": [], "total": 0}
        )
        assert resp.data == [] and resp.total == 0


def test_board_population_is_not_capped_at_two_hundred():
    """基线模块级保留：全表规模上限不受 200 约束（496 板块合法）。"""
    pillar = IntradayPillar(rank_of=205, total_boards=496)
    assert pillar.rank_of == 205 and pillar.total_boards == 496
    with pytest.raises(ValidationError):
        IntradayPillar(rank_of=497, total_boards=496)


class TestSectorTrendAndPeers:
    """issue #82 追加块（Layer 3）：严格模式与字段边界。"""

    def _trend(self) -> Dict[str, Any]:
        return dict(_valid_payload()["sector_trend"])

    def _peers(self) -> Dict[str, Any]:
        return dict(_valid_payload()["sector_peers"])

    def test_trend_valid_roundtrip(self):
        from src.schemas.sector_analysis import SectorTrendBlock

        block = SectorTrendBlock.model_validate(self._trend())
        assert block.eligible is True and block.interval_count == 19
        assert block.benchmark is not None and block.benchmark.qualified_sessions == 20

    def test_trend_window_literal_enforced(self):
        """窗口/间隔是 20/19 字面量，不冒充「20日收益」口径。"""
        from src.schemas.sector_analysis import SectorTrendBlock

        bad = self._trend() | {"window_completed_sessions": 21}
        with pytest.raises(ValidationError):
            SectorTrendBlock.model_validate(bad)
        bad2 = self._trend() | {"interval_count": 20}
        with pytest.raises(ValidationError):
            SectorTrendBlock.model_validate(bad2)

    def test_trend_expected_dates_cap_20_and_iso_len(self):
        from src.schemas.sector_analysis import SectorTrendBlock

        dates = [f"2026-10-{d:02d}" for d in range(1, 21)]
        block = SectorTrendBlock.model_validate(self._trend() | {"expected_dates": dates})
        assert len(block.expected_dates) == 20
        with pytest.raises(ValidationError):
            SectorTrendBlock.model_validate(
                self._trend() | {"expected_dates": dates + ["2026-11-05"]}
            )
        with pytest.raises(ValidationError):
            SectorTrendBlock.model_validate(
                self._trend() | {"expected_dates": ["2026-10-9"]}  # 非 10 位 ISO
            )

    def test_trend_rejects_unknown_fields(self):
        from src.schemas.sector_analysis import SectorTrendBlock

        bad = self._trend() | {"sector_score": 60.0}
        with pytest.raises(ValidationError):
            SectorTrendBlock.model_validate(bad)

    def test_trend_eligible_false_default_block(self):
        """unconfirmed 资格块：eligible=False + benchmark None 合法。"""
        from src.schemas.sector_analysis import SectorTrendBlock

        block = SectorTrendBlock.model_validate({
            "eligible": False, "gaps": ["主行业身份未确认，不执行板块趋势查询"],
        })
        assert block.benchmark is None and block.expected_dates == []

    def test_peers_items_cap_6_and_code_pattern(self):
        from src.schemas.sector_analysis import SectorPeersBlock

        items = [
            {"code": f"60051{i}", "name": f"股{i}", "amount": 1e9 + i,
             "change_pct": 1.0, "is_target": False}
            for i in range(6)
        ]
        block = SectorPeersBlock.model_validate(self._peers() | {"items": items})
        assert block.sample_size == 2  # sample_size 是独立字段，不随 items 自动变
        with pytest.raises(ValidationError):
            SectorPeersBlock.model_validate(
                self._peers() | {"items": items + [items[0] | {"code": "000001"}]}
            )
        bad_code = items[0] | {"code": "60051"}
        with pytest.raises(ValidationError):
            SectorPeersBlock.model_validate(self._peers() | {"items": [bad_code]})

    def test_peers_rank_bounds(self):
        from src.schemas.sector_analysis import SectorPeersBlock

        with pytest.raises(ValidationError):
            SectorPeersBlock.model_validate(self._peers() | {"target_change_pct_rank": 0})
        with pytest.raises(ValidationError):
            SectorPeersBlock.model_validate(self._peers() | {"target_change_pct_rank": 7})

    def test_payload_optional_new_keys_compat_with_81(self):
        """两新块为追加可选（issue #82 rework）：已验收 #81 形状 payload（缺键）
        仍通过严格校验且为 None；compose 新报告总是产出（有键时正常校验）。"""
        payload = SectorReportPayload.model_validate(_valid_payload())
        assert payload.sector_trend is not None and payload.sector_peers is not None
        # #81 旧 payload 形状（无 sector_trend/sector_peers 键）不再被严格模型拒绝
        old81 = {k: v for k, v in _valid_payload().items() if k not in ("sector_trend", "sector_peers")}
        legacy = SectorReportPayload.model_validate(old81)
        assert legacy.sector_trend is None and legacy.sector_peers is None

    def test_trend_observation_bounds(self):
        """实际日线观测：>20 条拒绝、非正收盘拒绝、日期长度 10。"""
        from src.schemas.sector_analysis import SectorTrendBlock

        base = self._trend()
        obs = [{"date": f"2026-10-{d:02d}", "close": 1000.0 + d} for d in range(1, 21)]
        block = SectorTrendBlock.model_validate(base | {"observations": obs,
                                                       "qualified_sessions": 20})
        assert len(block.observations) == 20
        with pytest.raises(ValidationError):
            SectorTrendBlock.model_validate(
                base | {"observations": obs + [{"date": "2026-11-05", "close": 1.0}]}
            )
        with pytest.raises(ValidationError):
            SectorTrendBlock.model_validate(
                base | {"observations": [{"date": "2026-10-09", "close": 0.0}]}
            )
        with pytest.raises(ValidationError):
            SectorTrendBlock.model_validate(
                base | {"observations": [{"date": "2026-10-9", "close": 1.0}]}
            )
        # qualified_sessions 范围 0—20
        with pytest.raises(ValidationError):
            SectorTrendBlock.model_validate(base | {"qualified_sessions": 21})

    def test_peers_amount_unit_literal_and_scope_fields(self):
        """成交额单位 Literal「元」+ provenance + 观测/可排名行数边界。"""
        from src.schemas.sector_analysis import SectorPeersBlock

        block = SectorPeersBlock.model_validate(self._peers())
        assert block.amount_unit == "元"
        assert "推断" in block.amount_provenance
        assert block.snapshot_rows == 0 and block.rankable_rows == 0
        ok = SectorPeersBlock.model_validate(self._peers() | {"snapshot_rows": 5000,
                                                             "rankable_rows": 5000})
        assert ok.rankable_rows == 5000
        with pytest.raises(ValidationError):
            SectorPeersBlock.model_validate(self._peers() | {"amount_unit": "万元"})
        with pytest.raises(ValidationError):
            SectorPeersBlock.model_validate(self._peers() | {"rankable_rows": -1})
