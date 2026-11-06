# -*- coding: utf-8 -*-
"""主行业身份 v2（issue #81）：同一东财行业板块（精确名称+板块代码）+ 成分验证。

- 不近似取首行：板块表无精确命中时不按包含/前缀近似锁定，近似名如实披露。
- 多候选/成分不含目标：subject_status 精确表达，不静默确证。
- 申万参考链 Ⅱ/Ⅲ 后缀命名归一（本文件局部修复，不扩大为数据源重构）。
- 修复伪造 _Ctx 调用：移除经 ``_safe_fundamental`` 的 fuyao 兜底隐式大聚合，
  实现错误不再被包装成供应商失败文案；不向无效代码隐藏出站聚合查询。
- 获取时间（observed_at）与供应商 as_of（未返回保持 None）区分。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.services.sector_analysis_service import ReportBudget, _resolve_em_board


def _budget() -> ReportBudget:
    return ReportBudget(30.0)


def _board_table(rows: list[tuple[str, str]]) -> pd.DataFrame:
    """构造东财行业板块全表（列名与 akshare 实测一致）。"""
    return pd.DataFrame(
        {"板块名称": [r[0] for r in rows], "板块代码": [r[1] for r in rows],
         "涨跌幅": [1.0] * len(rows)}
    )


class TestResolveEmBoard:
    """东财板块锁定：精确名称/板块代码，不近似取首行。"""

    def test_exact_single_match_locks_name_and_code(self):
        df = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        out = _resolve_em_board("白酒Ⅱ", df)
        assert out["board_name"] == "白酒Ⅱ"
        assert out["board_code"] == "BK1277"
        assert out["locked"] is True
        assert out["gaps"] == []

    def test_no_exact_match_does_not_take_first_near_row(self):
        """只有近似名（白酒/啤酒）时不得取首行，近似名如实披露。"""
        df = _board_table([("白酒", "BK0001"), ("啤酒", "BK0002")])
        out = _resolve_em_board("白酒Ⅱ", df)
        assert out["locked"] is False
        assert out["board_name"] == ""
        assert out["board_code"] == ""
        assert "白酒" in out["near_names"]  # 近似名透出供人工核对
        assert any("近似" in g for g in out["gaps"])

    def test_multiple_exact_matches_are_ambiguous(self):
        df = _board_table([("电池", "BK1033"), ("电池", "BK9999")])
        out = _resolve_em_board("电池", df)
        assert out["locked"] is False
        assert any(len(out["gaps"]) > 0 for _ in [0])
        assert any("无法唯一锁定" in g for g in out["gaps"])

    def test_board_code_is_recorded_when_name_column_missing_code(self):
        df = pd.DataFrame({"板块名称": ["白酒Ⅱ"], "涨跌幅": [1.0]})
        out = _resolve_em_board("白酒Ⅱ", df)
        assert out["locked"] is False  # 无代码列时不可锁定（名称+代码双确认）
        assert any("代码" in g for g in out["gaps"])


class TestSwKeywordLevelNormalization:
    """申万参考链 Ⅱ/Ⅲ 命名归一（真实反例：600519 白酒Ⅱ → 白酒Ⅲ 851251.SI）。"""

    def test_level_suffix_unit_yields_base_keyword(self):
        from src.services.sector_analysis_service import _sw_keywords

        kws = _sw_keywords("白酒Ⅱ")
        assert "白酒Ⅱ" in kws
        assert "白酒" in kws  # 归一基词，能命中三级名「白酒Ⅲ」

    def test_normalized_base_hits_third_level_name(self):
        from src.services.sector_analysis_service import _sw_hit_count, _sw_keywords

        kws = _sw_keywords("白酒Ⅱ")
        assert _sw_hit_count(kws, "白酒Ⅲ") > 0
        assert _sw_hit_count(kws, "白酒") > 0
        assert _sw_hit_count(kws, "电池") == 0


class TestIdentifySectorV2:
    """识别链 v2 行为（供应商边界全部隔离，形状参考 2026-11-05 实测 JSON）。"""

    def _ak_mocks(self, monkeypatch: pytest.MonkeyPatch, *, info: pd.DataFrame | None,
                  boards: pd.DataFrame | None, cons: pd.DataFrame | None) -> dict:
        import akshare as ak

        calls: dict[str, int] = {"individual": 0, "boards": 0, "cons": 0}

        def _info(symbol):
            calls["individual"] += 1
            if info is None:
                raise RuntimeError("em individual down")
            return info

        def _boards():
            calls["boards"] += 1
            if boards is None:
                raise RuntimeError("em boards down")
            return boards

        def _cons(symbol):
            calls["cons"] += 1
            if cons is None:
                raise RuntimeError("em cons down")
            return cons

        monkeypatch.setattr(ak, "stock_individual_info_em", _info)
        monkeypatch.setattr(ak, "stock_board_industry_name_em", _boards)
        monkeypatch.setattr(ak, "stock_board_industry_cons_em", _cons)
        return calls

    @staticmethod
    def _info_df(industry: str) -> pd.DataFrame:
        return pd.DataFrame({"item": ["行业", "总股本"], "value": [industry, "1256197800"]})

    @staticmethod
    def _cons_df(codes: list[str]) -> pd.DataFrame:
        return pd.DataFrame({"代码": codes, "名称": [f"股票{c}" for c in codes]})

    def test_confirmed_membership(self, monkeypatch: pytest.MonkeyPatch):
        from src.services.sector_analysis_service import _identify_sector

        boards = _board_table([("白酒Ⅱ", "BK1277"), ("电池", "BK1033")])
        cons = self._cons_df(["600519", "000568"])
        self._ak_mocks(monkeypatch, info=self._info_df("白酒Ⅱ"), boards=boards, cons=cons)
        # 申万链（参考）离线不可用时如实记缺口，不影响东财主身份
        monkeypatch.setattr(
            "src.services.sector_analysis_service._resolve_sw_chain",
            lambda code, text, budget: {"l1": "", "l2": "", "l3": "", "l3_code": "",
                                        "gaps": ["申万链：申万指数表不可用"]},
        )
        ident = _identify_sector("600519", budget=_budget())
        assert ident["industry_em"] == "白酒Ⅱ"
        assert ident["board_name"] == "白酒Ⅱ"
        assert ident["board_code"] == "BK1277"
        assert ident["subject_status"] == "confirmed"
        assert ident["membership"]["verified"] is True
        assert ident["membership"]["member_count"] == 2
        assert ident["observed_at"] is not None
        assert ident["vendor_as_of"] is None  # 供应商未回显 as_of，如实为未知

    def test_membership_not_containing_target_is_unverified(self, monkeypatch: pytest.MonkeyPatch):
        """成分不含目标股：身份未验证，不静默确证。"""
        from src.services.sector_analysis_service import _identify_sector

        boards = _board_table([("白酒Ⅱ", "BK1277")])
        cons = self._cons_df(["000568", "000596"])
        self._ak_mocks(monkeypatch, info=self._info_df("白酒Ⅱ"), boards=boards, cons=cons)
        monkeypatch.setattr(
            "src.services.sector_analysis_service._resolve_sw_chain",
            lambda code, text, budget: {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
        )
        ident = _identify_sector("600519", budget=_budget())
        assert ident["board_code"] == "BK1277"
        assert ident["subject_status"] == "unverified"
        assert ident["membership"]["verified"] is False
        assert any("成分" in g for g in ident["gaps"])

    def test_near_name_board_not_locked(self, monkeypatch: pytest.MonkeyPatch):
        from src.services.sector_analysis_service import _identify_sector

        boards = _board_table([("白酒", "BK0001"), ("啤酒", "BK0002")])
        cons = self._cons_df(["600519"])
        self._ak_mocks(monkeypatch, info=self._info_df("白酒Ⅱ"), boards=boards, cons=cons)
        monkeypatch.setattr(
            "src.services.sector_analysis_service._resolve_sw_chain",
            lambda code, text, budget: {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
        )
        ident = _identify_sector("600519", budget=_budget())
        assert ident["board_name"] == ""
        assert ident["subject_status"] == "ambiguous"
        assert any("白酒" in g for g in ident["gaps"])

    def test_no_industry_data_is_missing(self, monkeypatch: pytest.MonkeyPatch):
        """东财+巨潮双失败：subject_status=missing；不得出现伪造 _Ctx 包装文案。"""
        from src.services.sector_analysis_service import _identify_sector

        self._ak_mocks(monkeypatch, info=None, boards=None, cons=None)
        import src.deep_research_dims.context as ctx_mod

        monkeypatch.setattr(
            ctx_mod, "_safe_fundamental",
            lambda *a, **k: pytest.fail("识别链不得再经 _safe_fundamental 隐式大聚合"),
        )

        def _cninfo_fail(symbol):
            raise RuntimeError("cninfo down")

        import akshare as ak

        monkeypatch.setattr(ak, "stock_profile_cninfo", _cninfo_fail)
        ident = _identify_sector("999999", budget=_budget())
        assert ident["subject_status"] == "missing"
        assert any("无行业数据" in g for g in ident["gaps"])
        assert not any("_Ctx" in g for g in ident["gaps"])  # 实现错误不伪装成供应商失败

    def test_observed_at_distinct_from_vendor_as_of(self, monkeypatch: pytest.MonkeyPatch):
        from src.services.sector_analysis_service import _identify_sector

        boards = _board_table([("电池", "BK1033")])
        cons = self._cons_df(["300750"])
        self._ak_mocks(monkeypatch, info=self._info_df("电池"), boards=boards, cons=cons)
        monkeypatch.setattr(
            "src.services.sector_analysis_service._resolve_sw_chain",
            lambda code, text, budget: {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []},
        )
        ident = _identify_sector("300750", budget=_budget())
        assert ident["observed_at"] is not None
        assert ident["vendor_as_of"] is None
