# -*- coding: utf-8 -*-
"""申万指数成分 wire 与成分边界判定（issue #100；父规格 #98 决策 3）。

测试缝（与既有板块测试约定一致）：
- ``fetch_index_members``（wire）：专题报表 p03473（iv_date + iv_zsdm）的
  请求形状、成员行解析（带交易所后缀身份）、行级同日资格、完整性元数据
  缺失的如实标注；响应形状取自 2026-11-18 真实脱敏样本；
- ``classify_membership``（纯判定）：已纳入（positive evidence）、明确
  未纳入（完整集合证据，受控路径）、未知（现场常态——无完整性证据的
  列表未找到目标股不能证明非成员）；带交易所精确身份匹配。

不覆盖：成员行情分批与编排接线（compose 层测试）、API 闭环（API 层测试）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.services import sector_sw_market as sw


def _members_payload(codes: List[str], names: List[str],
                     dates: Optional[List[str]] = None) -> Dict[str, Any]:
    """data_pool p03473 成功响应（真实形状：单 table，三列按行对齐）。"""
    if dates is None:
        dates = ["2026-11-18"] * len(codes)
    return {
        "errorcode": 0,
        "errmsg": "",
        "tables": [{
            "table": {
                "p03473_f001": dates,
                "p03473_f002": codes,
                "p03473_f003": names,
            },
        }],
    }


def _coal_sample() -> Dict[str, Any]:
    """煤炭 801950.SL 成分脱敏样本形状（2026-11-18 实测，前几只）。"""
    codes = ["000552.SZ", "000571.SZ", "000723.SZ", "600188.SH", "601225.SH"]
    names = ["甘肃能化", "新大洲A", "美锦能源", "兖矿能源", "陕西煤业"]
    return _members_payload(codes, names)


class _TransportRecorder:
    """替换 sw 模块的传输与认证缝，记录请求体并返回预置响应。"""

    def __init__(self, responses: Dict[str, Any]) -> None:
        self.responses = responses
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def post(self, url: str, headers: Dict[str, str],
             body: Optional[bytes], *, label: str) -> Tuple[int, bytes]:
        assert body is not None
        parsed = json.loads(body.decode())
        self.calls.append((url, parsed))
        for key, payload in self.responses.items():
            if key in url:
                return 200, json.dumps(payload).encode()
        raise AssertionError(f"未预置响应的端点: {url}")

    def resolve_token(self, refresh_token: str, *, force_refresh: bool = False) -> str:
        return "stub-access-token-value"


@pytest.fixture()
def recorder(monkeypatch):
    def _install(responses: Dict[str, Any]) -> _TransportRecorder:
        rec = _TransportRecorder(responses)
        monkeypatch.setattr(sw, "_http_api_curl_post", rec.post)
        monkeypatch.setattr(sw, "_resolve_access_token", rec.resolve_token)
        return rec
    return _install


def _fetch(rec, index_code: str = "801950.SL",
           observation_date: str = "2026-11-18") -> Dict[str, Any]:
    return sw.fetch_index_members(
        index_code, observation_date,
        refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
        deadline=None,
    )


class TestFetchIndexMembers:
    def test_request_carries_report_and_same_day_params(self, recorder):
        rec = recorder({"data_pool": _coal_sample()})
        _fetch(rec)
        assert len(rec.calls) == 1
        url, payload = rec.calls[0]
        assert "data_pool" in url
        assert payload["reportname"] == "p03473"
        # 观察日去横线传参（与既有层级查询同一日期口径）
        assert payload["functionpara"] == {"iv_date": "20261118",
                                           "iv_zsdm": "801950.SL"}
        assert payload["outputpara"] == "p03473_f001,p03473_f002,p03473_f003"

    def test_parses_member_rows_from_real_sample_shape(self, recorder):
        rec = recorder({"data_pool": _coal_sample()})
        out = _fetch(rec)
        assert out["member_codes"] == ["000552.SZ", "000571.SZ", "000723.SZ",
                                       "600188.SH", "601225.SH"]
        assert out["member_names"]["600188.SH"] == "兖矿能源"
        assert out["rows_total"] == 5
        assert out["dates_seen"] == ["2026-11-18"]
        assert out["gaps"] == []

    def test_completeness_evidence_absent_on_vendor_response(self, recorder):
        """p03473 响应无总数/分页元数据：完整性证据恒缺失（如实，不推导）。"""
        rec = recorder({"data_pool": _coal_sample()})
        out = _fetch(rec)
        assert out["completeness_evidence"] is None

    def test_off_date_rows_excluded_and_recorded(self, recorder):
        rec = recorder({"data_pool": _members_payload(
            ["000552.SZ", "600188.SH", "601225.SH"],
            ["甘肃能化", "兖矿能源", "陕西煤业"],
            dates=["2026-11-17", "2026-11-18", "2026-11-18"],
        )})
        out = _fetch(rec)
        assert out["member_codes"] == ["600188.SH", "601225.SH"]
        assert sorted(out["dates_seen"]) == ["2026-11-17", "2026-11-18"]
        assert out["rows_total"] == 3
        assert out["gaps"] and "2026-11-17" in out["gaps"][0]

    def test_all_rows_off_date_means_members_not_obtained(self, recorder):
        """全部行异日 = 未取得该日成分（非空成分），如实缺口。"""
        rec = recorder({"data_pool": _members_payload(
            ["000552.SZ"], ["甘肃能化"], dates=["2026-11-17"])})
        out = _fetch(rec)
        assert out["member_codes"] == []
        assert out["gaps"] and "2026-11-18" in out["gaps"][0]

    def test_suffixless_code_rows_rejected_not_identity_matched(self, recorder):
        """成员代码缺交易所后缀：无法做带交易所匹配，该行不采用（记缺口）。"""
        rec = recorder({"data_pool": _members_payload(
            ["600188", "601225.SH"], ["兖矿能源", "陕西煤业"])})
        out = _fetch(rec)
        assert out["member_codes"] == ["601225.SH"]
        assert out["gaps"] and "1" in out["gaps"][0]

    def test_non_numeric_code_rows_rejected_without_echo(self, recorder):
        """畸形身份（abc.SH 等）：行不采用，缺口只含计数、不回显原始值。"""
        rec = recorder({"data_pool": _members_payload(
            ["abc.SH", "600188.SH", "12ab56.SZ"],
            ["X", "兖矿能源", "Y"])})
        out = _fetch(rec)
        assert out["member_codes"] == ["600188.SH"]
        assert out["rejected_identity_rows"] == 2
        joined = " ".join(out["gaps"])
        assert "2" in joined
        assert "abc" not in joined and "12ab56" not in joined

    def test_unknown_exchange_suffix_rejected(self, recorder):
        """非沪/深/北后缀（如 .HK/.XX）：身份无效，不猜交易所。"""
        rec = recorder({"data_pool": _members_payload(
            ["600188.HK", "600188.SH"], ["X", "兖矿能源"])})
        out = _fetch(rec)
        assert out["member_codes"] == ["600188.SH"]
        assert out["rejected_identity_rows"] == 1

    def test_duplicate_stripped_code_conflict_group_rejected_with_count(
            self, recorder):
        """剥后缀后同 6 位码多后缀：身份矛盾整组剔除（计数入结构化字段）。"""
        rec = recorder({"data_pool": _members_payload(
            ["600188.SH", "600188.SZ", "601225.SH"], ["A", "B", "C"])})
        out = _fetch(rec)
        assert out["member_codes"] == ["601225.SH"]
        assert out["conflicting_code_members"] == 2
        assert out["gaps"] and "600188" in out["gaps"][0]

    def test_structured_rejection_counts_default_zero(self, recorder):
        rec = recorder({"data_pool": _coal_sample()})
        out = _fetch(rec)
        assert out["rejected_identity_rows"] == 0
        assert out["conflicting_code_members"] == 0

    def test_misaligned_columns_row_rejected(self, recorder):
        """列长不对齐（缺名称位）：该行不采用，不按位置猜值。"""
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{"table": {
                "p03473_f001": ["2026-11-18", "2026-11-18"],
                "p03473_f002": ["600188.SH"],
                "p03473_f003": ["兖矿能源"],
            }}],
        }
        rec = recorder({"data_pool": payload})
        out = _fetch(rec)
        assert out["member_codes"] == []
        assert out["gaps"]

    def test_missing_code_column_is_fetch_gap_not_empty_membership(self, recorder):
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{"table": {
                "p03473_f001": ["2026-11-18"],
                "p03473_f003": ["兖矿能源"],
            }}],
        }
        rec = recorder({"data_pool": payload})
        out = _fetch(rec)
        assert out["member_codes"] == []
        assert out["gaps"] and "p03473_f002" in out["gaps"][0]

    def test_vendor_errorcode_raises_fetch_error(self, recorder):
        rec = recorder({"data_pool": {"errorcode": -4001,
                                      "errmsg": "no data", "tables": []}})
        with pytest.raises(sw.SwSectorFetchError):
            _fetch(rec)

    def test_empty_tables_raise_fetch_error(self, recorder):
        rec = recorder({"data_pool": {"errorcode": 0, "errmsg": "",
                                      "tables": []}})
        with pytest.raises(sw.SwSectorFetchError):
            _fetch(rec)


# ---------------------------------------------------------------------------
# 成分边界三态判定（#98 决策 3：已纳入 / 明确未纳入 / 未知）
# ---------------------------------------------------------------------------


def _members_out(codes: List[str], *, evidence: Optional[str] = None,
                 gaps: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "member_codes": codes,
        "member_names": {c: f"名称{c}" for c in codes},
        "dates_seen": ["2026-11-18"],
        "rows_total": len(codes),
        "completeness_evidence": evidence,
        "gaps": gaps or [],
    }


class TestClassifyMembership:
    def test_target_in_members_is_included_without_completeness(self):
        """positive evidence：目标在成分内即已纳入（无需完整性证明）。"""
        out = sw.classify_membership(
            "600188.SH",
            _members_out(["000552.SZ", "600188.SH", "601225.SH"]),
            observation_date="2026-11-18",
        )
        assert out["status"] == "included"
        assert out["member_count"] == 3
        assert out["gaps"] == []

    def test_exchange_mismatch_is_not_positive_evidence(self):
        """带交易所精确匹配：600188.SH ≠ 600188.SZ（身份不同）。"""
        out = sw.classify_membership(
            "600188.SH", _members_out(["600188.SZ"]),
            observation_date="2026-11-18",
        )
        assert out["status"] != "included"

    def test_missing_without_evidence_is_unknown(self):
        """无完整性证据的列表未找到目标股：只能标未知（不能证明非成员）。"""
        out = sw.classify_membership(
            "920101.BJ", _members_out(["000552.SZ", "600188.SH"]),
            observation_date="2026-11-18",
        )
        assert out["status"] == "unknown"
        assert out["completeness_evidence"] is None

    def test_missing_with_full_set_evidence_is_excluded(self):
        """受控路径：完整集合证据可证 + 目标未命中 = 明确未纳入。"""
        out = sw.classify_membership(
            "920101.BJ",
            _members_out(["000552.SZ", "600188.SH"],
                         evidence="完整集合受控样本（总数核验一致）"),
            observation_date="2026-11-18",
        )
        assert out["status"] == "excluded"
        assert out["completeness_evidence"] == "完整集合受控样本（总数核验一致）"

    def test_members_not_obtained_is_unknown_not_excluded(self):
        """成分未取得（空列表 + 查询性缺口）：未知，不冒充非成员。"""
        out = sw.classify_membership(
            "920101.BJ", _members_out([], gaps=["成分查询失败（HTTP 超时）"]),
            observation_date="2026-11-18",
        )
        assert out["status"] == "unknown"
        assert out["gaps"] and "成分查询失败" in out["gaps"][0]

    def test_members_not_obtained_with_evidence_stays_unknown(self):
        """未取得与完整集合矛盾：证据不成立时不判排除（保守）。"""
        out = sw.classify_membership(
            "920101.BJ", _members_out([], evidence="完整集合"),
            observation_date="2026-11-18",
        )
        assert out["status"] == "unknown"

    def test_target_identifies_in_result_for_traceability(self):
        out = sw.classify_membership(
            "920101.BJ", _members_out(["600188.SH"]),
            observation_date="2026-11-18",
        )
        assert out["target_secucode"] == "920101.BJ"
        assert out["observation_date"] == "2026-11-18"


class TestMemberRowLimitsAndRawAmount:
    """可发布上限与原始金额保真（amend 二：schema/诊断反例）。"""

    def test_oversized_response_rejected_without_publishable_coverage(
            self, recorder):
        """行数超出可发布上限：成员集合不发布、明确有界缺口（不 500）。"""
        codes = [f"{600000 + i}.SZ" for i in range(10001)]
        names = [f"成员{i}" for i in range(10001)]
        rec = recorder({"data_pool": _members_payload(codes, names)})
        out = _fetch(rec)
        assert out["member_codes"] == []
        assert out["rows_total"] == 10001
        assert out["over_publish_limit"] is True
        assert out["gaps"] and "10001" in out["gaps"][0]
        assert out["completeness_evidence"] is None

    def test_at_limit_response_still_publishes(self, recorder):
        codes = [f"{600000 + i}.SZ" for i in range(10000)]
        names = [f"成员{i}" for i in range(10000)]
        rec = recorder({"data_pool": _members_payload(codes, names)})
        out = _fetch(rec)
        assert len(out["member_codes"]) == 10000
        assert out["over_publish_limit"] is False

    def test_raw_amount_keeps_int_without_float_cast(self, recorder):
        """成员行情路径：原始整型金额原样保留（不经 float cast）。"""
        row = {"marketCategory": "1", "pricetype": 1, "thscode": "600188.SH",
               "time": ["2026-11-18 22:00:46"],
               "table": {"changeRatio": [1.0], "amount": [83725554],
                         "tradeTime": ["22:00:46"], "latest": [101.0],
                         "preClose": [100.0]}}
        rec = recorder({"real_time_quotation": {"errorcode": 0, "errmsg": "",
                                                "tables": [row]}})
        rows = sw.fetch_index_quotes(
            "600188.SH", refresh_token="x", timeout_seconds=10.0,
            deadline=None,
            indicators="changeRatio,amount,tradeTime,latest,preClose",
            raw_amount=True)
        assert rows[0]["amount"] == 83725554
        assert isinstance(rows[0]["amount"], int)

    def test_default_amount_path_unchanged_float(self, recorder):
        """默认（31 指数快照）路径行为不变：amount 仍经既有 float 解析。"""
        row = {"marketCategory": "1", "pricetype": 1, "thscode": "801950.SL",
               "time": ["2026-11-18 22:00:46"],
               "table": {"changeRatio": [1.0], "amount": [83725554],
                         "tradeTime": ["22:00:46"], "latest": [101.0],
                         "preClose": [100.0]}}
        rec = recorder({"real_time_quotation": {"errorcode": 0, "errmsg": "",
                                                "tables": [row]}})
        rows = sw.fetch_index_quotes(
            "801950.SL", refresh_token="x", timeout_seconds=10.0, deadline=None)
        assert isinstance(rows[0]["amount"], float)
