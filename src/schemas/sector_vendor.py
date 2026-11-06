# -*- coding: utf-8 -*-
"""板块适配东财供应商响应 I/O 边界（Type-Contract-Data Defense Layer 3）。

与载荷 schema（sector_analysis.py）分离：本文件只描述供应商 wire 形状。
契约原则（2026-11-07 收敛轮，root 反例驱动）：

- **严格校验消费字段**：参与身份/时间/完整性/金融公式的字段逐个显式建模
  并严格校验（market 回显、time 数据时点、trendsTotal 声明行数、preClose
  有限正），类型漂移按源错误拒绝，不做 ``str()`` 合法化。
- **兼容无关合法元数据**：真实响应携带大量本实现不消费的字段（type/status/
  decimal/preSettlement/beticks/kind/prePrice、datacenter 的 BOARD_RANK 等
  扩展列）——``extra="allow"`` 原样放行，不因供应商新增合法字段整体误拒
  （旧 ``extra="forbid"`` 曾把完整真实响应整体拒绝，B6 stderr 实证）。
- 数值缺值惯例：东财以字符串 ``"-"`` 表示缺值，字段级**显式**归一 None；
  ``int → float`` 为显式声明的同精度转换；bool/非数值字符串原样透传给
  strict 校验拒绝（不 raise TypeError 绕过源错误降级）。

参考 ``docs/type-contract-data-defense.md`` 第 4 节 Pydantic 配置模式。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _VendorWireBase(BaseModel):
    """供应商 wire 模型基类：strict + frozen + validate-on-assign。

    ``extra="allow"``：供应商合法扩展字段原样兼容（不消费、不拒绝）；
    消费字段全部显式声明并严格校验——拒绝关键字段漂移与放过无关元数据
    两件事分开。
    """

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        validate_assignment=True,
        extra="allow",
    )


_Secucode = Annotated[str, Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")]
_SixDigitCode = Annotated[str, Field(pattern=r"^\d{6}$")]
_BoardCode = Annotated[str, Field(pattern=r"^BK\d{4}$")]
_NonEmptyName = Annotated[str, Field(min_length=1, max_length=64)]


def _dash_to_none_or_float(v: object) -> object:
    """数值位 before 归一：``"-"`` → None（缺值）；int → float（同精度）。

    bool 原样透传（strict float 拒绝 bool → 源错误路径，不 raise TypeError）；
    其余原样透传给 strict float / Field 约束拒绝。
    """
    if v == "-":
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return float(v)
    return v


class F10BoardRow(_VendorWireBase):
    """RPT_F10_CORETHEME_BOARDTYPE 单行（个股声明与 BK 反向成员同一行形状）。

    消费字段：双股票键（SECUCODE/SECURITY_CODE）、板块键（NEW_BOARD_CODE/
    BOARD_NAME）、类型层级（BOARD_TYPE/BOARD_LEVEL）。交易所归属一致性由
    service 层按 :func:`data_provider.base.is_bse_code` 权威识别校验（920
    北交新段 → BJ，900 沪 B 不误判）。BOARD_CODE/BOARD_RANK 等扩展列由
    extra=allow 兼容。
    """

    SECUCODE: _Secucode
    SECURITY_CODE: _SixDigitCode
    SECURITY_NAME_ABBR: _NonEmptyName
    NEW_BOARD_CODE: _BoardCode
    BOARD_NAME: _NonEmptyName
    BOARD_TYPE: Optional[Annotated[str, Field(max_length=16)]] = None
    BOARD_LEVEL: Optional[Annotated[str, Field(max_length=8)]] = None


class F10ResultInner(_VendorWireBase):
    """datacenter securities API result 内层：分页元数据 + 行数组。

    分页完整性（每页行数契约、跨页 count/pages 一致、总数核齐、跨页去重）
    由 service 层核验；本模型只约束 wire 形状与页数下界。
    """

    pages: int = Field(ge=1)
    count: int = Field(ge=0)
    data: List[F10BoardRow] = Field(max_length=500)


class F10BoardResponse(_VendorWireBase):
    """datacenter securities API v1 响应（RPT_F10_CORETHEME_BOARDTYPE）。

    成功 envelope 边界（root final 反例：Pydantic ``Literal[True]``/``Literal[0]``
    在 strict 下仍接受 ``success=1``/``code=false``——Python ``1 == True`` 的
    等值语义不等于 wire 类型）：``success`` 必须精确 ``bool`` 且为 ``True``，
    ``code`` 必须精确 ``int``（非 bool）且为 ``0``；类型漂移与业务失败都在
    模型层整体拒绝，残留 result/data 不得被采纳。version/message 为合法元数据，
    extra=allow 兼容。
    """

    success: bool
    code: int
    result: F10ResultInner

    @field_validator("success")
    @classmethod
    def _success_must_be_true(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("success must be exactly boolean true")
        return v

    @field_validator("code")
    @classmethod
    def _code_must_be_zero(cls, v: int) -> int:
        if v != 0:
            raise ValueError("business code must be exactly integer 0")
        return v


class StockQuoteBase(_VendorWireBase):
    """push2 stock/get data 内层公共形状（f57/f58 同键 + 数值缺值归一）。

    数值位供应商缺值惯例为字符串 ``"-"``，字段级显式归一 None；int → float
    为显式声明的同精度转换（拒绝 bool/str/其他类型）；allow_inf_nan=False
    拒绝 NaN/±Infinity、``ge=0`` 拒绝负值在 I/O 边界（消费的金额/时间戳/
    换手/市值都是非负量——0 是已知零观测、None 是缺失，两者语义分开；
    涨跌幅 f170 符号合法不约束）。``f57`` 基类为 6 位个股代码（成员形态），
    BoardQuote 覆盖为 BK 板块代码模式。
    """

    f57: _SixDigitCode
    f58: _NonEmptyName
    f48: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)   # 成交额（元）
    f86: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)   # 行情时间戳（epoch 秒）
    f116: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)  # 总市值（元）
    f168: Optional[float] = Field(default=None, ge=0, allow_inf_nan=False)  # 换手率（%）
    f170: Optional[float] = Field(default=None, allow_inf_nan=False)        # 涨跌幅（%）

    @field_validator("f48", "f86", "f116", "f168", "f170", mode="before")
    @classmethod
    def _normalize_numeric(cls, v: object) -> object:
        return _dash_to_none_or_float(v)


class BoardQuote(StockQuoteBase):
    """板块单标的快照（secid=90.BK****；f57 必须为 BK 板块代码）。"""

    f57: _BoardCode  # type: ignore[assignment]


class MemberQuote(StockQuoteBase):
    """个股成员行情快照（secid={market}.{6位代码}；f57 必须为 6 位代码）。"""


class Trends2Response(_VendorWireBase):
    """push2his trends2 data 内层 wire 形状（分钟行 CSV 字符串数组）。

    消费字段（root 反例驱动，全部显式建模）：
    - ``code``/``name``：与请求同键校验（身份）；
    - ``market``：请求 secid 市场前缀回显（身份；沪 1/深北 0/板块 90）；
    - ``preClose``：**必须有限正**（金融公式分母；``"Infinity"``/``"-"``/0/
      负数按源错误拒绝，不泄 InvalidOperation）；
    - ``time``：供应商行情时点 epoch 秒（as_of 依据，须不早于分钟会话日）；
    - ``trendsTotal``：供应商声明分钟行数（完整性依据：与实际行数核齐）；
    - ``trends``：分钟行 CSV 数组（行级契约——8 列、单一会话日、严格递增、
      覆盖连续、价格正/量额非负——由 service 层解析）。
    type/status/decimal/preSettlement/beticks/kind/prePrice 为合法元数据，
    extra=allow 兼容（旧模型因 missing 声明 + extra=forbid 整体误拒真实响应）。
    """

    code: Annotated[str, Field(min_length=2, max_length=16)]
    name: _NonEmptyName
    market: int = Field(ge=0, le=1000)
    preClose: float = Field(gt=0, allow_inf_nan=False)
    time: int = Field(ge=0)
    trendsTotal: int = Field(ge=0)
    trends: List[Annotated[str, Field(min_length=10, max_length=120)]] = Field(
        max_length=1200,
    )

    @field_validator("preClose", mode="before")
    @classmethod
    def _normalize_preclose(cls, v: object) -> object:
        """``"-"`` → None（必填约束拒绝，源错误）；数字字符串显式转 float
        （东财 wire 数字位字符串形态实测存在）；``"Infinity"``/``"NaN"`` 经
        float() 产生 inf/nan 后由 allow_inf_nan=False 拒绝；非数值字符串与
        bool 原样透传给 strict 校验拒绝。"""
        if v == "-":
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, str)):
            try:
                return float(v)
            except (TypeError, ValueError):
                return v
        return v


_TencentAmountSource = Literal["exact35", "rounded37"]
"""腾讯行成交额来源：exact35=字段35三元组第三段精确金额（元）；
rounded37=字段37万元口 ×10000 换算（仅精确金额缺值时的兜底口径）。"""


class TencentMemberRow(_VendorWireBase):
    """腾讯公开行情批量行的解析归一记录（Layer 3 供应商 wire 边界）。

    消费字段严格 typed（无 Any 键字符串映射跨层传递）：
    - ``provider_time``：字段30 市场本地供应时点（%Y%m%d%H%M%S，非请求时刻）；
    - ``change_pct``：字段32 涨跌幅，可选且 ``allow_inf_nan=False``——供应
      商可表达值必须可向外表示为有限 float（如 Decimal 有限但 float 溢出的
      ``1e999`` 按源错误拒绝，不产出 inf）；
    - ``amount``/``amount_known``/``amount_source``：字段35 三元组第三段
      精确金额（Decimal 元）优先、字段37 万元 ×10000 兜底的**真实来源**
      贯穿记录；畸形/负值/非有限 → 金额未知（不回退、不补 0 冒充）。
    未消费的合法元数据由 extra=allow 兼容。解析层负责从原始字段归一，
    本模型负责消费字段边界与可表示性校验。
    """

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        validate_assignment=True,
        extra="allow",
    )

    symbol: Annotated[str, Field(min_length=8, max_length=8,
                                 pattern=r"^(sh|sz|bj)\d{6}$")]
    code: _SixDigitCode
    name: _NonEmptyName
    provider_time: datetime
    change_pct: Optional[float] = Field(default=None, allow_inf_nan=False)
    amount: Optional[Decimal] = Field(default=None, ge=0, allow_inf_nan=False)
    amount_known: bool
    amount_source: Optional[_TencentAmountSource] = None
