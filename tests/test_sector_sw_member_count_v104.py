# -*- coding: utf-8 -*-
"""供应商成分计数接口与成分 wire 增强（issue #104）。

测试缝（与既有板块测试约定一致）：替换 ``sector_sw_market`` 模块的传输
与认证缝，记录请求体并返回预置响应；响应形状取自 2026-11-20 真实脱敏
样本（counts_20261120.json / members_801194.SL.json）。

- ``fetch_index_member_count``（wire）：``basic_data_service`` +
  ``ths_constituent_num_index`` 显式传同一交易日（YYYYMMDD），单指数绑定
  请求（codes 只含一个指数）；解析结果表 ``thscode``、指标值（唯一无冲突
  正整数）与 ``inputParams`` 回显中的 ``FDATE``（日期不在结果表内）；
  FDATE 缺失/不符、同码多表、非请求指数行、非正整数、超上界一律拒绝。
- ``fetch_index_members`` 增强：空日期行与异日行**结构化计数**（全量资格
  「原始行先校验后过滤」的分母依据——过滤后数量吻合但存在脏行时，调用方
  依计数拒绝全量资格），以及响应表回显身份与请求指数冲突的显式标注。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from src.services import sector_sw_market as sw
from src.services.sector_sw_market import SwSectorFetchError


def _count_payload(values: Dict[str, List[Any]], *,
                   fdate_echo: Optional[str] = "20261120",
                   extra_tables: Optional[List[Dict[str, Any]]] = None,
                   ) -> Dict[str, Any]:
    """basic_data_service ths_constituent_num_index 成功响应（真实形状）。"""
    tables = [{"thscode": code, "table": {"ths_constituent_num_index": vals}}
              for code, vals in values.items()]
    tables.extend(extra_tables or [])
    params: List[Dict[str, str]] = [
        {"name": "THSCODE", "system": "false", "value": ""},
        {"name": "FDIR", "system": "false", "value": ""},
    ]
    if fdate_echo is not None:
        params.insert(1, {"name": "FDATE", "system": "false",
                          "value": fdate_echo})
    return {
        "errorcode": 0,
        "errmsg": "",
        "tables": tables,
        "datatype": [{"itemid": "ths_constituent_num_index",
                      "type": "DT_INTEGER"}],
        "inputParams": {"jsonrpc": True, "params": [{
            "function": "ths_constituent_num_index", "id": "652793",
            "params": params,
        }]},
        "dataVol": len(tables),
    }


def _count_missing_payload() -> Dict[str, Any]:
    """未传日期时供应商回全 null（2026-11-20 实测 member_count_default）。"""
    payload = _count_payload({"801194.SL": [None]}, fdate_echo="")
    payload["inputParams"]["params"][0]["params"] = [
        {"name": "THSCODE", "system": "false", "value": ""},
        {"name": "FDATE", "system": "false", "value": ""},
        {"name": "FDIR", "system": "false", "value": ""},
    ]
    return payload


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


class TestFetchIndexMemberCount:
    """计数接口 wire：请求绑定、值与 FDATE 回显校验（issue #104）。"""

    def _fetch(self, rec: _TransportRecorder, index_code: str = "801194.SL",
               observation_date: str = "2026-11-20") -> Dict[str, Any]:
        return sw.fetch_index_member_count(
            index_code, observation_date,
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None,
        )

    def test_positive_sample_parses_count_and_fdate_echo(self, recorder) -> None:
        """留存正例（2026-11-20 保险Ⅱ）：计数 5、FDATE 回显一致。"""
        rec = recorder({"basic_data_service": _count_payload({"801194.SL": [5]})})
        out = self._fetch(rec)
        assert out["count"] == 5
        assert out["fdate_echo"] == "20261120"
        assert out["index_code"] == "801194.SL"
        assert out["gaps"] == []
        # 请求绑定单一指数 + 显式交易日（YYYYMMDD）
        assert len(rec.calls) == 1
        url, body = rec.calls[0]
        assert "basic_data_service" in url
        assert body["codes"] == "801194.SL"
        assert body["indipara"] == [{
            "indicator": "ths_constituent_num_index",
            "indiparams": ["20261120"],
        }]

    def test_missing_date_returns_null_count_rejected(self, recorder) -> None:
        """供应商回 null 计数（未传日期形态）：计数缺失，拒绝。"""
        rec = recorder({"basic_data_service": _count_missing_payload()})
        with pytest.raises(SwSectorFetchError, match="计数"):
            self._fetch(rec)

    def test_fdate_echo_mismatch_rejected(self, recorder) -> None:
        """输入参数回显 FDATE 与请求日不符：身份/日期绑定不可证，拒绝。"""
        rec = recorder({"basic_data_service": _count_payload(
            {"801194.SL": [5]}, fdate_echo="20261118")})
        with pytest.raises(SwSectorFetchError, match="FDATE"):
            self._fetch(rec)

    def test_fdate_echo_missing_rejected(self, recorder) -> None:
        """回显缺 FDATE 参数：日期证据缺失，拒绝。"""
        rec = recorder({"basic_data_service": _count_payload(
            {"801194.SL": [5]}, fdate_echo=None)})
        with pytest.raises(SwSectorFetchError, match="FDATE"):
            self._fetch(rec)

    def test_same_code_multiple_tables_conflict_rejected(self, recorder) -> None:
        """同码多表计数不一致：冲突拒绝（不取其一）。"""
        rec = recorder({"basic_data_service": _count_payload(
            {},
            extra_tables=[
                {"thscode": "801194.SL",
                 "table": {"ths_constituent_num_index": [5]}},
                {"thscode": "801194.SL",
                 "table": {"ths_constituent_num_index": [6]}},
            ])})
        with pytest.raises(SwSectorFetchError, match="冲突"):
            self._fetch(rec)

    def test_same_code_multiple_tables_agreeing_still_rejected(self, recorder) -> None:
        """同码多表即使计数一致：唯一性不可证，保守拒绝。"""
        rec = recorder({"basic_data_service": _count_payload(
            {},
            extra_tables=[
                {"thscode": "801194.SL",
                 "table": {"ths_constituent_num_index": [5]}},
                {"thscode": "801194.SL",
                 "table": {"ths_constituent_num_index": [5]}},
            ])})
        with pytest.raises(SwSectorFetchError):
            self._fetch(rec)

    def test_unrequested_index_rows_rejected(self, recorder) -> None:
        """单指数绑定请求却回其他指数行：形状异常，拒绝。"""
        rec = recorder({"basic_data_service": _count_payload(
            {"801194.SL": [5]},
            extra_tables=[{"thscode": "801790.SL",
                           "table": {"ths_constituent_num_index": [77]}}])})
        with pytest.raises(SwSectorFetchError):
            self._fetch(rec)

    def test_missing_target_table_rejected(self, recorder) -> None:
        """响应无请求指数行：计数缺失，拒绝。"""
        rec = recorder({"basic_data_service": _count_payload(
            {"801790.SL": [77]})})
        with pytest.raises(SwSectorFetchError):
            self._fetch(rec)

    @pytest.mark.parametrize("raw", [[0], [-3], [2.5], ["5"], [True], [], [5, 6]])
    def test_non_positive_integer_values_rejected(self, recorder, raw) -> None:
        """零/负/非整数/字符串/布尔/多值/空值列表：唯一无冲突正整数不成立。"""
        rec = recorder({"basic_data_service": _count_payload(
            {"801194.SL": raw})})
        with pytest.raises(SwSectorFetchError):
            self._fetch(rec)

    def test_whole_float_integer_accepted(self, recorder) -> None:
        """DT_INTEGER 语义下的整值浮点（5.0）数值等价正整数，接受。"""
        rec = recorder({"basic_data_service": _count_payload(
            {"801194.SL": [5.0]})})
        out = self._fetch(rec)
        assert out["count"] == 5

    def test_count_over_publish_limit_rejected(self, recorder) -> None:
        """计数超出可发布成员上界：与成分 wire 同一上界纪律，拒绝。"""
        rec = recorder({"basic_data_service": _count_payload(
            {"801194.SL": [10001]})})
        with pytest.raises(SwSectorFetchError):
            self._fetch(rec)

    def test_missing_indiparams_echo_rejected(self, recorder) -> None:
        """inputParams 缺失（形状异常）：日期回显不可核，拒绝。"""
        payload = _count_payload({"801194.SL": [5]})
        payload.pop("inputParams")
        rec = recorder({"basic_data_service": payload})
        with pytest.raises(SwSectorFetchError):
            self._fetch(rec)

    def test_contradictory_fdate_echoes_rejected(self, recorder) -> None:
        """重复回显 [错误, 正确]：last-wins 不允许——矛盾回显拒绝。"""
        payload = _count_payload({"801194.SL": [5]})
        params = payload["inputParams"]["params"][0]["params"]
        params[1]["value"] = "20261118"  # 第一组
        groups = payload["inputParams"]["params"]
        groups.append({"function": "ths_constituent_num_index", "id": "2",
                       "params": [
                           {"name": "THSCODE", "system": "false", "value": ""},
                           {"name": "FDATE", "system": "false",
                            "value": "20261120"},
                           {"name": "FDIR", "system": "false", "value": ""},
                       ]})
        rec = recorder({"basic_data_service": payload})
        with pytest.raises(SwSectorFetchError, match="FDATE"):
            self._fetch(rec)

    def test_identical_repeated_fdate_echoes_deduped(self, recorder) -> None:
        """相同重复回显可去重：唯一无矛盾值接受。"""
        payload = _count_payload({"801194.SL": [5]})
        groups = payload["inputParams"]["params"]
        groups.append({"function": "ths_constituent_num_index", "id": "2",
                       "params": [
                           {"name": "THSCODE", "system": "false", "value": ""},
                           {"name": "FDATE", "system": "false",
                            "value": "20261120"},
                           {"name": "FDIR", "system": "false", "value": ""},
                       ]})
        rec = recorder({"basic_data_service": payload})
        out = self._fetch(rec)
        assert out["count"] == 5 and out["fdate_echo"] == "20261120"

    def test_blank_then_correct_fdate_echo_rejected(self, recorder) -> None:
        """匹配指标组内的显式空白 FDATE 条目是无效源结果：与有效回显并存
        时不得 qualify（不允许跳过空白后接受剩余值）。"""
        payload = _count_payload({"801194.SL": [5]})
        groups = payload["inputParams"]["params"]
        groups.append({"function": "ths_constituent_num_index", "id": "2",
                       "params": [
                           {"name": "THSCODE", "system": "false", "value": ""},
                           {"name": "FDATE", "system": "false", "value": ""},
                           {"name": "FDIR", "system": "false", "value": ""},
                       ]})
        rec = recorder({"basic_data_service": payload})
        with pytest.raises(SwSectorFetchError, match="FDATE"):
            self._fetch(rec)

    def test_null_then_correct_fdate_echo_rejected(self, recorder) -> None:
        """显式 null FDATE 条目与有效回显并存：同样拒绝。"""
        payload = _count_payload({"801194.SL": [5]})
        params = payload["inputParams"]["params"][0]["params"]
        params[1]["value"] = None
        groups = payload["inputParams"]["params"]
        groups.append({"function": "ths_constituent_num_index", "id": "2",
                       "params": [
                           {"name": "THSCODE", "system": "false", "value": ""},
                           {"name": "FDATE", "system": "false",
                            "value": "20261120"},
                           {"name": "FDIR", "system": "false", "value": ""},
                       ]})
        rec = recorder({"basic_data_service": payload})
        with pytest.raises(SwSectorFetchError, match="FDATE"):
            self._fetch(rec)

    def test_fdate_mismatch_gap_does_not_echo_remote_value(
            self, recorder) -> None:
        """FDATE 不符/矛盾的 gap 文本不回显供应商返回的任意字符串。"""
        rec = recorder({"basic_data_service": _count_payload(
            {"801194.SL": [5]}, fdate_echo="2026-11-20T00:00:00Z_long_x")})
        with pytest.raises(SwSectorFetchError, match="FDATE") as excinfo:
            self._fetch(rec)
        message = str(excinfo.value)
        assert "2026-11-20T00:00:00Z" not in message

    def test_non_string_fdate_echo_rejected(self, recorder) -> None:
        """回显值非字符串（如整数）：不 str() 合法化，无效回显拒绝。"""
        payload = _count_payload({"801194.SL": [5]})
        params = payload["inputParams"]["params"][0]["params"]
        params[1]["value"] = 20261120  # 非字符串
        rec = recorder({"basic_data_service": payload})
        with pytest.raises(SwSectorFetchError, match="FDATE"):
            self._fetch(rec)

    def test_non_object_table_rejected_not_filtered(self, recorder) -> None:
        """结果表含非对象项：拒绝（不静默过滤后再做唯一性判定）。"""
        payload = _count_payload({"801194.SL": [5]})
        payload["tables"].append("malformed")
        rec = recorder({"basic_data_service": payload})
        with pytest.raises(SwSectorFetchError):
            self._fetch(rec)


class TestFetchIndexMembersRowAudit:
    """成分 wire 原始行审计计数（issue #104：过滤前校验的结构化分母）。

    审计仅二级全量资格链显式启用（issue #105 修复：
    ``full_constituent_audit=True``）；一级消费语义（默认 False）见
    :class:`TestFetchIndexMembersL1Compat`。"""

    def _fetch(self, rec: _TransportRecorder,
              index_code: str = "801194.SL",
              observation_date: str = "2026-11-20") -> Dict[str, Any]:
        return sw.fetch_index_members(
            index_code, observation_date,
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, full_constituent_audit=True,
        )

    def test_clean_sample_zero_audit_counters(self, recorder) -> None:
        """干净样本（真实 801194.SL 形状）：全部审计计数为零。"""
        codes = ["601318.SH", "601319.SH", "601336.SH", "601601.SH", "601628.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{"table": {
                "p03473_f001": ["2026-11-20"] * 5,
                "p03473_f002": codes,
                "p03473_f003": ["中国平安", "中国人保", "新华保险", "中国太保",
                                "中国人寿"],
            }}],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert out["blank_date_rows"] == 0
        assert out["off_date_rows"] == 0
        assert out["identity_echo_conflict"] is False
        assert out["shape_gap_count"] == 0
        assert len(out["member_codes"]) == 5

    def test_shape_gap_table_counted(self, recorder) -> None:
        """表形状异常（列不对齐）：该表不采用并结构化计数。"""
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [
                {"table": {
                    "p03473_f001": ["2026-11-20", "2026-11-20"],
                    "p03473_f002": ["601318.SH"],
                    "p03473_f003": ["中国平安"],
                }},
                {"table": {
                    "p03473_f001": ["2026-11-20", "2026-11-20"],
                    "p03473_f002": ["601319.SH", "601336.SH"],
                    "p03473_f003": ["中国人保", "新华保险"],
                }},
            ],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert out["shape_gap_count"] == 1
        # 表 2 对齐正常采用（2 个成员）；表 1 不对齐整表不采用
        assert len(out["member_codes"]) == 2

    def test_blank_date_row_counted_not_silently_dropped(self, recorder) -> None:
        """空日期行：剔除但显式计数（同表观计数脏行拒绝的分母依据）。"""
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{"table": {
                "p03473_f001": ["2026-11-20"] * 5 + [""],
                "p03473_f002": ["601318.SH", "601319.SH", "601336.SH",
                                "601601.SH", "601628.SH", "601699.SH"],
                "p03473_f003": ["中国平安", "中国人保", "新华保险", "中国太保",
                                "中国人寿", "邮储银行"],
            }}],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert len(out["member_codes"]) == 5
        assert out["rows_total"] == 6
        assert out["blank_date_rows"] == 1
        assert any("空日期" in g for g in out["gaps"])

    def test_off_date_row_counted(self, recorder) -> None:
        """异日行：剔除并结构化计数。"""
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{"table": {
                "p03473_f001": ["2026-11-20"] * 5 + ["2026-11-18"],
                "p03473_f002": ["601318.SH", "601319.SH", "601336.SH",
                                "601601.SH", "601628.SH", "601699.SH"],
                "p03473_f003": ["中国平安", "中国人保", "新华保险", "中国太保",
                                "中国人寿", "邮储银行"],
            }}],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert len(out["member_codes"]) == 5
        assert out["off_date_rows"] == 1

    def test_identity_echo_conflict_flagged(self, recorder) -> None:
        """响应表回显与请求指数冲突的身份：整表不采用（行不进成员集合，
        目标成员身份不可采信），显式标注且不回显原始长值。"""
        codes = ["601318.SH", "601319.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{
                "thscode": "801790.SL",  # 回显 ≠ 请求的 801194.SL
                "table": {
                    "p03473_f001": ["2026-11-20"] * 2,
                    "p03473_f002": codes,
                    "p03473_f003": ["中国平安", "中国人保"],
                },
            }],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert out["identity_echo_conflict"] is True
        assert out["member_codes"] == []  # 冲突表行不进成员集合
        assert out["rows_total"] == 2  # 原始行计数保留（审计分母）
        assert any("回显" in g for g in out["gaps"])
        assert all("801790.SL" not in g for g in out["gaps"])  # 不回显原始值

    def test_non_object_table_counted_as_shape_gap(self, recorder) -> None:
        """成分响应含非对象表项：标记表形状异常（不静默忽略），全量资格
        拒绝依据。"""
        codes = ["601318.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [
                {"table": {
                    "p03473_f001": ["2026-11-20"],
                    "p03473_f002": codes,
                    "p03473_f003": ["中国平安"],
                }},
                "malformed",
            ],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert out["shape_gap_count"] >= 1
        assert any("形状" in g for g in out["gaps"])

    def test_conflicting_duplicate_names_counted(self, recorder) -> None:
        """同码重复行名称矛盾（非空白且不同）：结构化计数，拒绝全量资格
        依据；一致重复正常去重不受影响。"""
        codes = ["601318.SH", "601318.SH", "601319.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{
                "table": {
                    "p03473_f001": ["2026-11-20"] * 3,
                    "p03473_f002": codes,
                    "p03473_f003": ["中国平安", "平安银行", "中国人保"],
                },
            }],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        # 一致成员集合保持（身份明确；矛盾只记数）
        assert len(out["member_codes"]) == 2
        assert out["conflicting_name_rows"] == 1
        assert any("名称矛盾" in g for g in out["gaps"])
        # 矛盾成员名称不发布（任意择一名不诚实），按代码显示
        assert out["member_names"].get("601318.SH", "") == ""

    def test_blank_then_conflicting_names_baseline_is_first_nonblank(
            self, recorder) -> None:
        """审计基线 = 首个非空可信名：["", "Alpha", "Beta"] 三行同码——
        空白首行不遮蔽矛盾（Alpha vs Beta 必须计矛盾），矛盾成员不发布
        任意择一名。"""
        codes = ["601318.SH", "601318.SH", "601318.SH", "601319.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{
                "table": {
                    "p03473_f001": ["2026-11-20"] * 4,
                    "p03473_f002": codes,
                    "p03473_f003": ["", "Alpha", "Beta", "中国人保"],
                },
            }],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert out["conflicting_name_rows"] == 1
        assert len(out["member_codes"]) == 2
        # 矛盾成员不发布任意名（Alpha/Beta 都不发布），按代码显示
        assert out["member_names"].get("601318.SH", "") == ""
        assert out["member_names"].get("601319.SH") == "中国人保"
        assert any("名称矛盾" in g for g in out["gaps"])

    def test_initial_blank_then_one_name_preserved(self, recorder) -> None:
        """空白首行 + 单一非空名：blank→one name 语义保留（不误报矛盾）。"""
        codes = ["601318.SH", "601318.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{
                "table": {
                    "p03473_f001": ["2026-11-20"] * 2,
                    "p03473_f002": codes,
                    "p03473_f003": ["", "中国平安"],
                },
            }],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert len(out["member_codes"]) == 1
        assert out["conflicting_name_rows"] == 0
        assert out["member_names"].get("601318.SH") == "中国平安"

    def test_identical_duplicate_names_not_conflict(self, recorder) -> None:
        """完全一致重复行（同日同码同名）：正常去重，零矛盾计数。"""
        codes = ["601318.SH", "601318.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{
                "table": {
                    "p03473_f001": ["2026-11-20"] * 2,
                    "p03473_f002": codes,
                    "p03473_f003": ["中国平安", "中国平安"],
                },
            }],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert len(out["member_codes"]) == 1
        assert out["conflicting_name_rows"] == 0
        assert out["member_names"].get("601318.SH") == "中国平安"

    def test_identity_echo_agreement_not_conflict(self, recorder) -> None:
        """回显与请求指数一致：不算冲突。"""
        codes = ["601318.SH", "601319.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{
                "thscode": "801194.SL",
                "table": {
                    "p03473_f001": ["2026-11-20"] * 2,
                    "p03473_f002": codes,
                    "p03473_f003": ["中国平安", "中国人保"],
                },
            }],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert out["identity_echo_conflict"] is False

    def test_no_identity_echo_not_conflict(self, recorder) -> None:
        """无身份回显（当前 p03473 真实形态）：不算冲突。"""
        codes = ["601318.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{"table": {
                "p03473_f001": ["2026-11-20"],
                "p03473_f002": codes,
                "p03473_f003": ["中国平安"],
            }}],
        }
        rec = recorder({"data_pool": payload})
        out = self._fetch(rec)
        assert out["identity_echo_conflict"] is False


class TestFetchIndexMembersL1Compat:
    """一级消费语义与基线等价（issue #105 修复回归）：审计仅二级显式启用，
    一级默认 False 恢复基线可接受的旧选样与资格——同一原始响应在一级路径
    上不因 #104 的绑定身份/重复名称/形状审计改变结果。六位码跨交易所冲突、
    异日/空日期剔除、超上限防护等两级共用既有防护不受开关影响。"""

    def test_l1_echo_conflict_table_still_consumed(self, recorder) -> None:
        """审查反例 1（复现基线）：请求一级 801780.SL、响应表回显
        801790.SL、含有效同日 601318.SH——一级（默认）照常采用该表，
        目标命中 → classify_membership 为 included；审计模式（二级）才
        整表拒绝。"""
        from src.services.sector_sw_market import classify_membership

        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{"thscode": "801790.SL", "table": {
                "p03473_f001": ["2026-11-20"],
                "p03473_f002": ["601318.SH"],
                "p03473_f003": ["中国平安"],
            }}],
        }
        rec = recorder({"data_pool": payload})
        out = sw.fetch_index_members(
            "801780.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, full_constituent_audit=False)
        assert out["member_codes"] == ["601318.SH"]
        assert out["identity_echo_conflict"] is False
        assert not any("回显" in g for g in out["gaps"])
        membership = classify_membership(
            "601318.SH", out, observation_date="2026-11-20")
        assert membership["status"] == "included"
        # 同一响应在审计模式（二级路径）下整表拒绝 → unknown
        out_audit = sw.fetch_index_members(
            "801780.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, full_constituent_audit=True)
        assert out_audit["member_codes"] == []
        assert out_audit["identity_echo_conflict"] is True
        assert any("回显" in g for g in out_audit["gaps"])
        membership_audit = classify_membership(
            "601318.SH", out_audit, observation_date="2026-11-20")
        assert membership_audit["status"] == "unknown"

    def test_l1_duplicate_names_keep_first(self, recorder) -> None:
        """审查反例 2：同码重复行名称矛盾（平安A/平安B）——一级保留首个
        名称（基线语义），不置空、不审计拒绝；审计模式置空 + 计数。"""
        codes = ["601318.SH", "601318.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{"table": {
                "p03473_f001": ["2026-11-20"] * 2,
                "p03473_f002": codes,
                "p03473_f003": ["平安A", "平安B"],
            }}],
        }
        rec = recorder({"data_pool": payload})
        out = sw.fetch_index_members(
            "801780.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, full_constituent_audit=False)
        assert len(out["member_codes"]) == 1
        assert out["conflicting_name_rows"] == 0
        assert out["member_names"].get("601318.SH") == "平安A"
        assert not any("名称矛盾" in g for g in out["gaps"])
        out_audit = sw.fetch_index_members(
            "801780.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, full_constituent_audit=True)
        assert out_audit["conflicting_name_rows"] == 1
        assert out_audit["member_names"].get("601318.SH") == ""

    def test_l1_blank_then_name_not_backfilled(self, recorder) -> None:
        """issue #105 修复二轮：同码首行无名称、后续行名称「中国平安」——
        一级保持基线（名称缺失，不回填后续行名称；支柱按代码显示）；补名
        仅审计模式（二级「首空后名」）生效。"""
        codes = ["601318.SH", "601318.SH"]
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{"table": {
                "p03473_f001": ["2026-11-20"] * 2,
                "p03473_f002": codes,
                "p03473_f003": ["", "中国平安"],
            }}],
        }
        rec = recorder({"data_pool": payload})
        out = sw.fetch_index_members(
            "801780.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, full_constituent_audit=False)
        assert len(out["member_codes"]) == 1
        # 基线：首行空白 → 该成员名称保持缺失（{}），不采用后续名称
        assert "601318.SH" not in out["member_names"]
        assert out["conflicting_name_rows"] == 0
        # 补名（blank→one name）仅二级审计模式
        out_audit = sw.fetch_index_members(
            "801780.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, full_constituent_audit=True)
        assert out_audit["member_names"].get("601318.SH") == "中国平安"
        assert out_audit["conflicting_name_rows"] == 0

    def test_l1_non_object_table_silently_skipped(self, recorder) -> None:
        """非对象表项：一级按基线语义跳过（无形状审计缺口），不改变同日
        合格行；审计模式记形状缺口计数。"""
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [
                "not-a-table",
                {"table": {
                    "p03473_f001": ["2026-11-20"],
                    "p03473_f002": ["601318.SH"],
                    "p03473_f003": ["中国平安"],
                }},
            ],
        }
        rec = recorder({"data_pool": payload})
        out = sw.fetch_index_members(
            "801780.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, full_constituent_audit=False)
        assert out["member_codes"] == ["601318.SH"]
        assert out["shape_gap_count"] == 0
        assert not any("非对象表项" in g for g in out["gaps"])
        out_audit = sw.fetch_index_members(
            "801780.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, full_constituent_audit=True)
        assert out_audit["shape_gap_count"] == 1
        assert any("非对象表项" in g for g in out_audit["gaps"])

    def test_l1_shared_guards_unchanged(self, recorder) -> None:
        """两级共用既有防护不受开关影响：六位码跨交易所矛盾整组剔除、
        异日行剔除（含既有 off-date 缺口文本）、身份格式不合格剔除。"""
        payload = {
            "errorcode": 0, "errmsg": "",
            "tables": [{"table": {
                "p03473_f001": ["2026-11-20", "2026-11-20", "2026-11-19",
                                "2026-11-20"],
                "p03473_f002": ["601318.SH", "601318.SZ", "600000.SH",
                                "bad-code"],
                "p03473_f003": ["平安A", "平安SZ", "异日成员", "畸形"],
            }}],
        }
        rec = recorder({"data_pool": payload})
        out = sw.fetch_index_members(
            "801780.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token", timeout_seconds=10.0,
            deadline=None, full_constituent_audit=False)
        assert out["member_codes"] == []  # 601318 跨交易所矛盾整组剔除
        # 计数口径 = 先到成员回滚 1 + 矛盾行 1（既有语义，两级一致）
        assert out["conflicting_code_members"] == 2
        assert out["off_date_rows"] == 1
        assert out["rejected_identity_rows"] == 1
        assert any("跨交易所" in g for g in out["gaps"])
        assert any("非观察日" in g for g in out["gaps"])
        assert any("身份格式不合格" in g for g in out["gaps"])


class TestCountExactFloatDecode:
    """issue #104 复审：计数响应以 ``parse_float=Decimal`` 精确解码——
    JSON 字面在二进制浮点解码中的损失不可在 decode 后补救。

    反例（修复前）：真实 wire 字面 ``5.0000000000000001`` 经默认 float
    解码四舍五入为 ``5.0``，integer gate 失效，五成分可能发布全量名次。
    """

    def _raw_bytes(self, literal: str) -> bytes:
        """手工构造响应字节：``json.dumps`` 会把该字面失真为 ``5.0``，
        必须以字符串替换写入原始 JSON 字面模拟真实 wire。"""
        payload = _count_payload({"801194.SL": ["__LITERAL__"]})
        text = json.dumps(payload).replace('"__LITERAL__"', literal)
        return text.encode()

    def _install_raw(self, monkeypatch, raw: bytes) -> None:
        def fake_post(url, headers, body, *, label):
            return 200, raw

        monkeypatch.setattr(sw, "_http_api_curl_post", fake_post)
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: "stub-token")

    def test_float_literal_beyond_binary_precision_rejected(
            self, monkeypatch) -> None:
        """字面 5.0000000000000001：精确值含小数 → 计数非法，拒绝。"""
        self._install_raw(monkeypatch, self._raw_bytes("5.0000000000000001"))
        with pytest.raises(SwSectorFetchError, match="含小数"):
            sw.fetch_index_member_count(
                "801194.SL", "2026-11-20",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None)

    def test_whole_float_literal_still_accepted(self, monkeypatch) -> None:
        """DT_INTEGER 语义不变：字面 5.0 数值等价正整数，接受。"""
        self._install_raw(monkeypatch, self._raw_bytes("5.0"))
        out = sw.fetch_index_member_count(
            "801194.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None)
        assert out["count"] == 5
        assert out["fdate_echo"] == "20261120"

    def test_integer_literal_still_accepted(self, monkeypatch) -> None:
        """JSON 整数字面不走 parse_float，行为不变。"""
        self._install_raw(monkeypatch, self._raw_bytes("5"))
        out = sw.fetch_index_member_count(
            "801194.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None)
        assert out["count"] == 5


class TestCountHugeExponentBoundary:
    """issue #104 三轮：有限、整值与区间都在**原值**上校验后才 int()——
    巨数指数字面（如 1e5000）的 int() 展开在真实解码器上触发整数位数上限
    ValueError（修复前反例）；拒绝消息不展开巨数、不回显原始值。"""

    def test_huge_exponent_literal_rejected_at_limit_without_expansion(
            self, monkeypatch) -> None:
        """字面 1e5000：有限正整值但超可发布上限——在 Decimal 原值上拒绝
        （不 int() 展开、错误消息不含巨数/原始值）。"""
        raw = json.dumps(_count_payload({"801194.SL": ["__LITERAL__"]})).replace(
            '"__LITERAL__"', "1e5000").encode()
        assert b"1e5000" in raw
        captured: Dict[str, str] = {}

        def fake_post(url, headers, body, *, label):
            captured["label"] = label
            return 200, raw

        monkeypatch.setattr(sw, "_http_api_curl_post", fake_post)
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: "stub-token")
        with pytest.raises(SwSectorFetchError) as exc_info:
            sw.fetch_index_member_count(
                "801194.SL", "2026-11-20",
                refresh_token="sector-test-refresh-token",
                timeout_seconds=10.0, deadline=None)
        message = str(exc_info.value)
        assert "超出可发布成员上限" in message
        assert "5000" not in message and "1e" not in message  # 不展开/不回显
        assert "ValueError" not in message

    def test_upper_boundary_literal_accepted(self, monkeypatch) -> None:
        """字面 1e4（= 可发布成员上界 10000）：整值区间内，接受。"""
        raw = json.dumps(_count_payload({"801194.SL": ["__LITERAL__"]})).replace(
            '"__LITERAL__"', "1e4").encode()

        def fake_post(url, headers, body, *, label):
            return 200, raw

        monkeypatch.setattr(sw, "_http_api_curl_post", fake_post)
        monkeypatch.setattr(sw, "_resolve_access_token",
                            lambda rt, *, force_refresh=False: "stub-token")
        out = sw.fetch_index_member_count(
            "801194.SL", "2026-11-20",
            refresh_token="sector-test-refresh-token",
            timeout_seconds=10.0, deadline=None)
        assert out["count"] == 10000
