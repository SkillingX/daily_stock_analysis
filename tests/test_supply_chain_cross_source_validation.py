# -*- coding: utf-8 -*-
"""供应链双源校验（东方财富 + 同花顺）单元测试。

设计（KISS + 高内聚低耦合，镜像 tests/test_supply_chain_clue_hype_tool.py）：
- 纯逻辑（归一 / 匹配 / 重合度 / 决策表）零 IO，直接断言；
- IO 探针用鸭子类型 fake 注入（DI），无真实网络；
- 覆盖 fail-open（探针异常 / 源不可用不影响另一源）、状态决策表全分支、
  工具注册与 handler 输出契约、prompt 双源校验约束。
"""

from __future__ import annotations

import sys
import types
from typing import Any, List, Tuple

import pytest

from data_provider.supply_chain.cross_source import (
    BOARD_MATCH_CONTAINS,
    BOARD_MATCH_EXACT,
    SourceEvidence,
    SupplyChainCrossSourceValidator,
    board_match_level,
    code_in_constituents,
    compute_matched_by,
    constituent_overlap_ratio,
    find_matched_boards,
    judge_verification,
    normalize_a_share_code,
    normalize_name,
)
import src.agent.tools.supply_chain_tools as sct
from src.agent.tools.supply_chain_tools import (
    ALL_SUPPLY_CHAIN_TOOLS,
)
from src.agent.supply_chain_executor import _SUPPLY_CHAIN_SYSTEM_PROMPT_TEMPLATE

PROMPT = _SUPPLY_CHAIN_SYSTEM_PROMPT_TEMPLATE


# ============================================================
# 辅助：构造证据 / fake 探针 / fake provider / fake ak
# ============================================================

def _ev(
    source: str = "eastmoney",
    available: bool = True,
    matched: bool = False,
    boards: Tuple[str, ...] = (),
    constituents: Tuple[str, ...] = (),
    error: str = None,
) -> SourceEvidence:
    return SourceEvidence(source, available, matched, boards, constituents, error)


class _RecordingProbe:
    """记录每次 probe 入参的 fake 探针，返回固定证据或抛指定异常。"""

    def __init__(self, name: str, evidence: Any) -> None:
        self.name = name
        self._evidence = evidence
        self.calls: List[Tuple[str, str, str]] = []

    def probe(self, code: str, name: str, keyword: str) -> SourceEvidence:
        self.calls.append((code, name, keyword))
        if isinstance(self._evidence, Exception):
            raise self._evidence
        return self._evidence


class _FakeConceptProvider:
    """东财 ConceptBoardProvider 的鸭子类型 fake。"""

    def __init__(self, boards: list, constituents_map: dict) -> None:
        self._boards = boards
        self._cons = constituents_map

    def get_concept_boards(self) -> list:
        return self._boards

    def get_concept_constituents(self, name: str) -> list:
        return self._cons.get(name, [])


class _FakeDf:
    """akshare DataFrame 的最小鸭子类型：``.columns`` + ``.get(col)`` 返回列或 None。"""

    def __init__(self, columns: dict) -> None:
        self._data = columns
        self.columns = list(columns.keys())

    def get(self, col: str):
        return self._data.get(col)


class _SeriesLike:
    """模拟 pandas Series：可迭代，但 ``bool()`` 歧义（``series or []`` 抛 ValueError）。

    用于回归：真实 akshare 返回 pandas Series，``get(col) or []`` 会触发
    "truth value of a Series is ambiguous"；fake 返回 list 时无法复现此 bug。
    """

    def __init__(self, values: list) -> None:
        self._values = list(values)

    def __iter__(self):
        return iter(self._values)

    def __bool__(self) -> bool:
        raise ValueError("The truth value of a Series is ambiguous.")


class _FakeAk:
    """akshare 模块的鸭子类型 fake（同花顺概念列表；成分股由注入的 http_get 提供）。"""

    def __init__(self, name_df: _FakeDf) -> None:
        self._name_df = name_df

    def stock_board_concept_name_ths(self) -> _FakeDf:
        return self._name_df


# ============================================================
# 纯逻辑：代码归一
# ============================================================

class TestNormalizeAShareCode:
    def test_plain_six_digit_all_valid_prefixes(self):
        assert normalize_a_share_code("300750") == "300750"  # 深市 创业板
        assert normalize_a_share_code("000001") == "000001"  # 深市 主板
        assert normalize_a_share_code("600519") == "600519"  # 沪市
        assert normalize_a_share_code("688981") == "688981"  # 沪市 科创板
        assert normalize_a_share_code("430047") == "430047"  # 北交所
        assert normalize_a_share_code("830799") == "830799"  # 北交所
        assert normalize_a_share_code("920099") == "920099"  # 北交所

    def test_strips_prefix_and_suffix(self):
        assert normalize_a_share_code("sz300750") == "300750"
        assert normalize_a_share_code("SH600519") == "600519"
        assert normalize_a_share_code("bj430047") == "430047"
        assert normalize_a_share_code("300750.SZ") == "300750"
        assert normalize_a_share_code("600519.SH") == "600519"
        assert normalize_a_share_code("430047.BJ") == "430047"

    @pytest.mark.parametrize("code", ["100001", "200002", "500001", "700001"])
    def test_invalid_first_digit_returns_none(self, code):
        assert normalize_a_share_code(code) is None

    @pytest.mark.parametrize(
        "code",
        ["hk00700", "00700", "AAPL", "TSLA", "3007501", "12345", "", "   ", None, 300750],
    )
    def test_non_a_share_or_invalid_returns_none(self, code):
        assert normalize_a_share_code(code) is None


# ============================================================
# 纯逻辑：名称 / 板块名标准化与匹配
# ============================================================

class TestNormalizeName:
    def test_strips_whitespace_and_punct(self):
        assert normalize_name(" 宁德 时代 ") == "宁德时代"
        assert normalize_name("动力　电池") == "动力电池"  # 全角空格
        assert normalize_name("HBM-3E/CoWoS") == "HBM3ECoWoS"

    def test_non_string_returns_empty(self):
        assert normalize_name(None) == ""
        assert normalize_name(123) == ""


class TestBoardMatchLevel:
    def test_exact(self):
        assert board_match_level("动力电池", "动力电池") == BOARD_MATCH_EXACT

    def test_exact_after_normalize(self):
        assert board_match_level(" 动力电池 ", "动力电池") == BOARD_MATCH_EXACT

    def test_contains_keyword_in_board(self):
        assert board_match_level("动力电池", "动力电池产业链") == BOARD_MATCH_CONTAINS

    def test_contains_board_in_keyword(self):
        assert board_match_level("宁德时代概念", "宁德时代") == BOARD_MATCH_CONTAINS

    def test_no_match(self):
        assert board_match_level("动力电池", "半导体") is None

    def test_empty_returns_none(self):
        assert board_match_level("", "动力电池") is None
        assert board_match_level("动力电池", "") is None


class TestFindMatchedBoards:
    def test_filters_and_dedup_in_order(self):
        names = ["半导体", "动力电池", "动力电池概念", "白酒"]
        assert find_matched_boards("动力电池", names) == ("动力电池", "动力电池概念")

    def test_dedup_exact_duplicates(self):
        assert find_matched_boards("动力电池", ["动力电池", "动力电池"]) == ("动力电池",)

    def test_empty_input(self):
        assert find_matched_boards("动力电池", []) == ()
        assert find_matched_boards("动力电池", ["", None]) == ()


# ============================================================
# 纯逻辑：成分股命中与重合度
# ============================================================

class TestCodeInConstituents:
    def test_present(self):
        assert code_in_constituents("300750", ["300750", "002594"]) is True

    def test_present_with_suffix(self):
        assert code_in_constituents("300750", ["300750.SZ", "sz002594"]) is True

    def test_absent(self):
        assert code_in_constituents("300750", ["002594", "600519"]) is False

    def test_empty_code_or_constituents(self):
        assert code_in_constituents("", ["300750"]) is False
        assert code_in_constituents("300750", []) is False
        assert code_in_constituents("300750", None) is False


class TestConstituentOverlapRatio:
    def test_partial_overlap(self):
        # {300750,002594} vs {300750,600519} → 交集1 / 并集3
        assert constituent_overlap_ratio(["300750", "002594"], ["300750", "600519"]) == pytest.approx(1 / 3)

    def test_full_overlap(self):
        assert constituent_overlap_ratio(["300750", "002594"], ["002594", "300750"]) == 1.0

    def test_disjoint(self):
        assert constituent_overlap_ratio(["300750"], ["600519"]) == 0.0

    def test_both_empty(self):
        assert constituent_overlap_ratio([], []) == 0.0

    def test_one_empty(self):
        assert constituent_overlap_ratio(["300750"], []) == 0.0

    def test_normalizes_prefixed_codes(self):
        # 前缀写法归一后仍算同一只
        assert constituent_overlap_ratio(["300750.SZ"], ["sz300750"]) == 1.0


# ============================================================
# 纯逻辑：matched_by 维度汇总
# ============================================================

class TestComputeMatchedBy:
    def test_code_dim(self):
        em = _ev(constituents=("300750",))
        ths = _ev(source="akshare_ths")
        assert compute_matched_by("300750", em, ths, "动力电池") == ("code",)

    def test_code_and_board_name_dims(self):
        em = _ev(boards=("动力电池",), constituents=("300750",))
        ths = _ev(source="akshare_ths", boards=("动力电池",))
        assert compute_matched_by("300750", em, ths, "动力电池") == ("code", "board_name")

    def test_board_name_only(self):
        em = _ev(boards=("动力电池",))
        ths = _ev(source="akshare_ths", boards=("动力电池",))
        # 公司不在成分股，但板块名命中
        assert compute_matched_by("300750", em, ths, "动力电池") == ("board_name",)

    def test_no_dim_when_nothing_matches(self):
        em = _ev(boards=("半导体",))
        ths = _ev(source="akshare_ths")
        assert compute_matched_by("300750", em, ths, "动力电池") == ()

    def test_no_board_name_when_hint_empty(self):
        em = _ev(boards=("动力电池",), constituents=("300750",))
        ths = _ev(source="akshare_ths")
        assert compute_matched_by("300750", em, ths, "") == ("code",)


# ============================================================
# 纯逻辑：决策表（judge_verification 全分支）
# ============================================================

class TestJudgeVerification:
    CODE = "300750"
    NAME = "宁德时代"
    HINT = "动力电池"

    def test_confirmed_when_both_matched(self):
        em = _ev(matched=True, boards=("动力电池",), constituents=("300750",))
        ths = _ev(source="akshare_ths", matched=True, boards=("动力电池",), constituents=("300750",))
        r = judge_verification(self.CODE, self.NAME, em, ths, self.HINT)
        assert (r.status, r.confidence) == ("confirmed", "high")
        assert r.scope == "a_share"
        assert set(r.matched_by) == {"code", "board_name"}

    def test_conflict_when_both_found_theme_but_disagree(self):
        # 两源都定位到「动力电池」，但同花顺成分股不含目标公司
        em = _ev(matched=True, boards=("动力电池",), constituents=("300750", "002594"))
        ths = _ev(source="akshare_ths", matched=False, boards=("动力电池",), constituents=("002594", "300788"))
        r = judge_verification(self.CODE, self.NAME, em, ths, self.HINT)
        assert (r.status, r.confidence) == ("conflict", "low")
        assert "冲突" in r.note

    def test_partial_when_one_matched_other_found_no_theme(self):
        em = _ev(matched=True, boards=("动力电池",), constituents=("300750",))
        ths = _ev(source="akshare_ths", matched=False, boards=(), constituents=())
        r = judge_verification(self.CODE, self.NAME, em, ths, self.HINT)
        assert (r.status, r.confidence) == ("partial", "medium")

    def test_partial_when_one_source_unavailable_but_other_matched(self):
        em = _ev(matched=True, boards=("动力电池",), constituents=("300750",))
        ths = _ev(source="akshare_ths", available=False, matched=False)
        r = judge_verification(self.CODE, self.NAME, em, ths, self.HINT)
        assert (r.status, r.confidence) == ("partial", "medium")

    def test_partial_when_only_ths_matched(self):
        em = _ev(matched=False, available=True, boards=(), constituents=())
        ths = _ev(source="akshare_ths", matched=True, boards=("动力电池",), constituents=("300750",))
        r = judge_verification(self.CODE, self.NAME, em, ths, self.HINT)
        assert (r.status, r.confidence) == ("partial", "medium")

    def test_unverified_when_both_available_but_neither_matched(self):
        em = _ev(matched=False, boards=(), constituents=())
        ths = _ev(source="akshare_ths", matched=False, boards=(), constituents=())
        r = judge_verification(self.CODE, self.NAME, em, ths, self.HINT)
        assert (r.status, r.confidence) == ("unverified", "low")
        assert "待核验" in r.note

    def test_unverified_when_one_available_not_matched_other_unavailable(self):
        em = _ev(matched=False, boards=(), constituents=())
        ths = _ev(source="akshare_ths", available=False, matched=False)
        r = judge_verification(self.CODE, self.NAME, em, ths, self.HINT)
        assert (r.status, r.confidence) == ("unverified", "low")

    def test_unverified_when_both_unavailable(self):
        em = _ev(available=False, matched=False)
        ths = _ev(source="akshare_ths", available=False, matched=False)
        r = judge_verification(self.CODE, self.NAME, em, ths, self.HINT)
        assert (r.status, r.confidence) == ("unverified", "low")

    def test_overlap_ratio_reported(self):
        em = _ev(matched=True, boards=("动力电池",), constituents=("300750", "002594"))
        ths = _ev(source="akshare_ths", matched=True, boards=("动力电池",), constituents=("300750", "600519"))
        r = judge_verification(self.CODE, self.NAME, em, ths, self.HINT)
        # {300750,002594} ∩ {300750,600519} = {300750}；并集3 → 1/3
        assert r.overlap_ratio == pytest.approx(1 / 3)


# ============================================================
# 数据结构：to_dict 序列化
# ============================================================

class TestSerialization:
    def test_source_evidence_to_dict_without_error(self):
        ev = _ev(matched=True, boards=("动力电池",), constituents=("300750",))
        d = ev.to_dict()
        assert d == {
            "available": True,
            "matched": True,
            "boards": ["动力电池"],
            "constituents": ["300750"],
            "source": "eastmoney",
        }
        assert "error" not in d

    def test_source_evidence_to_dict_with_error(self):
        ev = _ev(available=False, error="boom")
        assert ev.to_dict()["error"] == "boom"

    def test_verification_result_to_dict_shape(self):
        em = _ev(matched=True, boards=("动力电池",), constituents=("300750",))
        ths = _ev(source="akshare_ths", matched=True, boards=("动力电池",), constituents=("300750",))
        r = judge_verification("300750", "宁德时代", em, ths, "动力电池")
        d = r.to_dict()
        assert set(d) == {
            "stock_code", "stock_name", "scope", "status", "confidence",
            "eastmoney", "ths", "overlap", "note",
        }
        assert d["stock_code"] == "300750"
        assert d["status"] == "confirmed"
        assert d["overlap"]["matched_by"] == ["code", "board_name"]
        assert isinstance(d["overlap"]["constituent_overlap_ratio"], float)
        assert d["eastmoney"]["source"] == "eastmoney"
        assert d["ths"]["source"] == "akshare_ths"


# ============================================================
