# -*- coding: utf-8 -*-
"""申万二级行业指数目录与唯一映射（issue #103；工单 01）。

覆盖：
- 申万 2021 二级目录完整性（124 代码+正式名称+父一级、唯一性、版本与
  来源含采集日期可追溯）——2026-11-19 实测核定的核定配置数据；
- ``resolve_sw_l2_index``：仅在父一级与二级名称都能在目录中唯一映射到
  同一指数时采纳；父行业不符、名称未知、歧义一律缺口（不近似匹配）；
- 载荷 schema 追加兼容：``sector_l2_identity`` / ``sector_trend_windows``
  为可选默认 None 键（旧 v3 载荷与新 v3 载荷都通过严格校验）。

目录实测方法与证据：801001-801999 全区间扫描（响应只含有效指数行）+
31 一级逐一比对后，对全部候选按成分股 L1/L2 归属反推（成分多数票），
恰好 124 个二级确认、覆盖 31 个一级、名称无重复；样本点（保险Ⅱ/工程
机械等）与既有脱敏实测一致。探针与中间产物在仓库外临时目录，不随
仓库分发；目录本体是核定配置数据（与 SW_L1_DIRECTORY 同模式）。
"""

from __future__ import annotations

import pytest

from src.services import sector_sw_market as sw


class TestSwL2Directory:
    def test_exactly_124_entries_with_unique_code_and_name(self):
        assert len(sw.SW_L2_DIRECTORY) == 124
        codes = [c for c, _n, _p in sw.SW_L2_DIRECTORY]
        names = [n for _c, n, _p in sw.SW_L2_DIRECTORY]
        assert len(set(codes)) == 124
        assert len(set(names)) == 124

    def test_entries_are_code_name_parent_triples(self):
        for code, name, parent in sw.SW_L2_DIRECTORY:
            assert code.endswith(".SL") and code[:6].isdigit()
            assert name and isinstance(name, str)
            assert parent and isinstance(parent, str)

    def test_every_parent_is_a_directory_l1_name(self):
        l1_names = {n for _, n in sw.SW_L1_DIRECTORY}
        for _code, _name, parent in sw.SW_L2_DIRECTORY:
            assert parent in l1_names

    def test_every_l1_has_confirmed_children(self):
        parents = {p for _c, _n, p in sw.SW_L2_DIRECTORY}
        l1_names = {n for _, n in sw.SW_L1_DIRECTORY}
        assert parents == l1_names

    def test_known_samples_from_live_validation(self):
        """2026-11-19 实测核定的样本对照（验收例）。"""
        entries = {(c, n, p) for c, n, p in sw.SW_L2_DIRECTORY}
        assert ("801194.SL", "保险Ⅱ", "非银金融") in entries
        assert ("801077.SL", "工程机械", "机械设备") in entries
        assert ("801125.SL", "白酒Ⅱ", "食品饮料") in entries
        assert ("801951.SL", "煤炭开采", "煤炭") in entries


class TestSwL2OfficialDirectory:
    """官方发布目录快照（分类与指数发布目录的主证据，spec :31）。"""

    def test_official_directory_has_131_unique_entries(self):
        assert len(sw.SW_L2_OFFICIAL_DIRECTORY) == 131
        codes = [c for c, _n, _p in sw.SW_L2_OFFICIAL_DIRECTORY]
        names = [n for _c, n, _p in sw.SW_L2_OFFICIAL_DIRECTORY]
        assert len(set(codes)) == 131
        assert len(set(names)) == 131

    def test_official_parents_are_l1_names(self):
        l1_names = {n for _, n in sw.SW_L1_DIRECTORY}
        for _c, _n, parent in sw.SW_L2_OFFICIAL_DIRECTORY:
            assert parent in l1_names

    def test_ifind_subset_joins_official_catalog_exactly(self):
        """124 项 iFinD 核验子集必须逐项对齐官方目录（名称+父行业）。"""
        official = {c: (n, p) for c, n, p in sw.SW_L2_OFFICIAL_DIRECTORY}
        verified = {c.split(".")[0]: (n, p) for c, n, p in sw.SW_L2_DIRECTORY}
        assert len(verified) == 124
        for code6, (name, parent) in verified.items():
            assert code6 in official, f"{code6} 不在官方目录"
            assert official[code6] == (name, parent), \
                f"{code6} 名称/父行业与官方目录不一致"

    def test_unverified_entries_are_official_only(self):
        """7 项未核验条目必须在官方目录中且不在 iFinD 核验子集。"""
        official = {c: (n, p) for c, n, p in sw.SW_L2_OFFICIAL_DIRECTORY}
        verified_codes = {c.split(".")[0] for c, _n, _p in sw.SW_L2_DIRECTORY}
        assert len(sw.SW_L2_UNVERIFIED_BY_IFIND) == 7
        assert len(official) == len(verified_codes) + 7
        for code6, name, parent in sw.SW_L2_UNVERIFIED_BY_IFIND:
            assert official[code6] == (name, parent)
            assert code6 not in verified_codes

    def test_official_version_and_source_recorded(self):
        assert sw.SW_L2_OFFICIAL_DIRECTORY_VERSION
        assert "20261120" in sw.SW_L2_OFFICIAL_DIRECTORY_VERSION
        assert sw.SW_L2_OFFICIAL_DIRECTORY_SOURCE
        assert "2026-11-20" in sw.SW_L2_OFFICIAL_DIRECTORY_SOURCE
        assert "131" in sw.SW_L2_OFFICIAL_DIRECTORY_SOURCE

    def test_directory_version_source_records_official_provenance(self):
        assert sw.SW_L2_DIRECTORY_VERSION == "sw2021-l2-20261120"
        assert "官方" in sw.SW_L2_DIRECTORY_SOURCE
        assert "124" in sw.SW_L2_DIRECTORY_SOURCE
        assert "SW_L2_UNVERIFIED_BY_IFIND" in sw.SW_L2_DIRECTORY_SOURCE


class TestL2IndexAmbiguousRuntimeIsolation:
    """目录歧义键运行时隔离（issue #105 修复）：构建期把歧义键标记为不可
    解析（映射 None 语义）并单独收集，不抛错、不阻断模块加载与其他键；
    正式目录的发布前数据错误由本文件上方目录唯一性/覆盖测试负责发现。"""

    def test_duplicate_parent_name_key_isolated_not_raised(self):
        """受控歧义映射：重复键不抛错、不映射到任一代码（运行时隔离）。"""
        index, ambiguous = sw._build_l2_parent_name_index(
            (("801001.SL", "同名Ⅱ", "父行业"), ("801002.SL", "同名Ⅱ", "父行业")))
        # 歧义键不可解析：不在唯一映射中（resolve 语义返回 None），
        # 且被单独标记为歧义（调用方据此输出专门缺口）
        assert ("父行业", "同名Ⅱ") not in index
        assert ("父行业", "同名Ⅱ") in ambiguous
        assert len(ambiguous) == 1

    def test_other_keys_survive_ambiguity(self):
        """歧义键只降该键：同一受控目录中健康键照常唯一解析。"""
        entries = (
            ("801001.SL", "同名Ⅱ", "父行业"),
            ("801002.SL", "同名Ⅱ", "父行业"),
            ("801003.SL", "健康Ⅱ", "健康行业"),
        )
        index, ambiguous = sw._build_l2_parent_name_index(entries)
        assert index == {("健康行业", "健康Ⅱ"): "801003.SL"}
        assert ambiguous == (("父行业", "同名Ⅱ"),)

    def test_triple_duplicate_key_isolated_once(self):
        """同一键三次出现：全部标记歧义、不取任何一个代码。"""
        index, ambiguous = sw._build_l2_parent_name_index(
            (("801001.SL", "同名Ⅱ", "父行业"),
             ("801002.SL", "同名Ⅱ", "父行业"),
             ("801003.SL", "同名Ⅱ", "父行业")))
        assert ("父行业", "同名Ⅱ") not in index
        assert ambiguous == (("父行业", "同名Ⅱ"),)

    def test_current_directory_builds_without_ambiguity(self):
        """现行核定目录（124 项）无歧义键，唯一映射完整。"""
        index, ambiguous = sw._build_l2_parent_name_index(sw.SW_L2_DIRECTORY)
        assert len(index) == 124
        assert ambiguous == ()

    def test_ambiguous_key_resolution_is_none(self):
        """构建期标记歧义后，运行期对歧义键不产生任一代码命中。"""
        broken = (("801001.SL", "同名Ⅱ", "父行业"), ("801002.SL", "同名Ⅱ", "父行业"))
        index, ambiguous = sw._build_l2_parent_name_index(broken)
        assert index.get(("父行业", "同名Ⅱ")) is None
        # 现行目录（无重复键）语义：唯一命中才返回
        assert sw.resolve_sw_l2_index("非银金融", "保险Ⅱ") == "801194.SL"

    def test_sw_l2_key_ambiguous_on_current_directory(self):
        """现行目录无歧义键：sw_l2_key_ambiguous 对任何现行输入为 False。"""
        assert not sw.sw_l2_key_ambiguous("非银金融", "保险Ⅱ")
        assert not sw.sw_l2_key_ambiguous("非银金融", "不存在的二级")
        assert not sw.sw_l2_key_ambiguous("", "")


class TestResolveSwL2Index:
    def test_unique_hit_returns_index_code(self):
        assert sw.resolve_sw_l2_index("非银金融", "保险Ⅱ") == "801194.SL"
        assert sw.resolve_sw_l2_index("机械设备", "工程机械") == "801077.SL"

    def test_unknown_l2_name_returns_none(self):
        assert sw.resolve_sw_l2_index("非银金融", "不存在的二级") is None

    def test_unknown_l1_parent_returns_none(self):
        assert sw.resolve_sw_l2_index("不存在的行业", "保险Ⅱ") is None

    def test_parent_mismatch_returns_none(self):
        # 名称存在于目录但父行业不一致：不采纳（无唯一父子映射）
        assert sw.resolve_sw_l2_index("煤炭", "保险Ⅱ") is None

    def test_official_only_unverified_classification_returns_none(self):
        """官方目录存在但 iFinD 侧未核验（林业Ⅱ）：不产生代码映射。"""
        assert sw.resolve_sw_l2_index("农林牧渔", "林业Ⅱ") is None

    def test_blank_inputs_return_none(self):
        assert sw.resolve_sw_l2_index("", "保险Ⅱ") is None
        assert sw.resolve_sw_l2_index("非银金融", "") is None
        assert sw.resolve_sw_l2_index(" ", "保险Ⅱ") is None


# ---------------------------------------------------------------------------
# 载荷 schema 追加兼容（可选默认 None 键）
# ---------------------------------------------------------------------------


class TestPayloadAppendCompat:
    def _minimal_v3_payload(self) -> dict:
        from src.schemas.sector_analysis import SectorReportPayload

        return SectorReportPayload.model_validate({
            "payload_version": 3,
            "research_basis": "sw2021_l1",
            "research_outcome": "kept_sw_stage",
            "stock_code": "601318",
            "stock_name": "中国平安",
            "generated_at": "2026-11-19T15:30:00",
            "identity": {
                "subject_status": "missing",
                "membership": {"verified": False},
            },
            "background": {
                "policy": {"lean": None, "label": None},
                "base_rate": {"base_rate": 0.5, "is_default": True,
                              "table_version": "v1", "table_updated": "2026-01-01",
                              "table_industry_count": 31},
                "intraday": {"sector_found": False, "row": {}},
            },
            "concepts": {"items": [], "total_returned": 0, "truncated": False},
            "assessment": {"status": "insufficient_data", "score": None,
                           "reasons": ["申万主行业不可确认"]},
            "identification": {"sw_chain": {}, "gaps": []},
            "policy": {"lean": None, "label": None, "score": None},
            "base_rate": {"base_rate": 0.5, "is_default": True,
                          "table_version": "v1", "table_updated": "2026-01-01",
                          "table_industry_count": 31},
            "prosperity": {"sector_found": False, "row": {}},
        }).model_dump()

    def test_legacy_v3_payload_without_new_keys_still_validates(self):
        from src.schemas.sector_analysis import SectorReportPayload

        payload = self._minimal_v3_payload()
        assert "sector_l2_identity" not in payload or (
            payload.get("sector_l2_identity") is None)
        assert payload.get("sector_trend_windows") is None
        # 原样（不含新键）读回也必须通过——存档读回路径
        raw = {k: v for k, v in payload.items()
               if k not in ("sector_l2_identity", "sector_trend_windows")}
        assert SectorReportPayload.model_validate(raw).sector_trend_windows is None

    def test_new_keys_accept_l2_identity_and_trend_windows(self):
        from src.schemas.sector_analysis import SectorReportPayload

        payload = self._minimal_v3_payload()
        payload["sector_l2_identity"] = {
            "status": "confirmed",
            "l2_name": "保险Ⅱ",
            "l2_index_code": "801194.SL",
            "l1_parent_name": "非银金融",
            "directory_version": sw.SW_L2_DIRECTORY_VERSION,
            "observation_date": "2026-11-19",
            "gaps": [],
        }
        payload["sector_trend_windows"] = [{
            "window_days": 5,
            "point_count": 6,
            "start_date": "2026-11-12",
            "end_date": "2026-11-19",
            "expected_dates": ["2026-11-12", "2026-11-13", "2026-11-14",
                               "2026-11-17", "2026-11-18", "2026-11-19"],
            "series": [{
                "role": "l2_industry",
                "name": "保险Ⅱ",
                "index_code": "801194.SL",
                "source": "iFinD 标准HTTP指数日线",
                "expected_sessions": 6,
                "qualified_sessions": 6,
                "observations": [
                    {"date": "2026-11-19", "close": 1000.0},
                ],
                "period_return_pct": 1.5,
                "gaps": [],
            }],
            "gaps": [],
        }]
        validated = SectorReportPayload.model_validate(payload)
        assert validated.sector_l2_identity.l2_index_code == "801194.SL"
        assert validated.sector_trend_windows is not None
        assert validated.sector_trend_windows[0].series[0].role == "l2_industry"

    def test_invalid_window_days_rejected(self):
        from pydantic import ValidationError

        from src.schemas.sector_analysis import SectorReportPayload

        payload = self._minimal_v3_payload()
        payload["sector_trend_windows"] = [{
            "window_days": 10,
            "point_count": 11,
            "start_date": "2026-11-01",
            "end_date": "2026-11-19",
            "expected_dates": [],
            "series": [],
            "gaps": [],
        }]
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(payload)

    def test_confirmed_l2_identity_requires_index_code(self):
        from pydantic import ValidationError

        from src.schemas.sector_analysis import SectorReportPayload

        payload = self._minimal_v3_payload()
        payload["sector_l2_identity"] = {
            "status": "confirmed",
            "l2_name": None,
            "l2_index_code": None,
            "l1_parent_name": None,
            "directory_version": sw.SW_L2_DIRECTORY_VERSION,
            "observation_date": "2026-11-19",
            "gaps": [],
        }
        with pytest.raises(ValidationError):
            SectorReportPayload.model_validate(payload)
