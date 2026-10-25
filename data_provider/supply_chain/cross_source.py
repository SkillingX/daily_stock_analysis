# -*- coding: utf-8 -*-
"""供应链归属校验纯判定层（issue #45：东财/同花顺公开探针已退役）。

本模块只保留纯判定函数与数据结构；公开探针（东财概念板块、同花顺概念网页）
已删除，生产路径不再构造默认校验器。个股概念归属改由 iFinD 适配入口提供。

设计要点（高内聚低耦合，镜像 :mod:`data_provider.cross_source_validator`）：
- 本模块**只做判定**，判定逻辑全部是纯函数（``_judge_*`` / ``normalize_*``），
  无副作用、无网络，易做 100% 单测。
- IO（取板块 / 成分股）由调用方注入的 ``SupplyChainSourceProbe`` 负责，通过
  Protocol 解耦 —— 换源 / 测试注入 fake 都不改本模块。
- ``SupplyChainCrossSourceValidator.verify`` 编排「归一 → 收集 → 判定」，
  收集时**逐源 fail-open**：单源异常 / 不可用绝不拖垮另一源（"未核验"语义，
  不是"否定"）。
- 非 A 股（港股 / 美股 / 无法归一到 6 位代码）直接 ``not_applicable``，不联网。

状态语义（``status``）：
- ``confirmed``     东财 + 同花顺均命中目标公司
- ``partial``       仅一源命中（另一源不可用 / 未定位到相关板块）
- ``conflict``      两源均可用、均定位到相关板块，但对目标公司归属相反
- ``unverified``    两源均不可用，或均可用但都未命中
- ``not_applicable``非 A 股

置信度语义（``confidence``）：
- ``high``   双源命中（``confirmed``）
- ``medium`` 单源命中（``partial``）
- ``low``    冲突 / 双源缺失 / 非适用范围

对应方案 ``docs/supply-chain-cross-source-validation-plan.md``。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Sequence, Tuple

import icontract

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 归一化纯函数
# ------------------------------------------------------------------

# A 股 6 位代码：允许 SH/SZ/BJ 前缀或 .SH/.SZ/.BJ 后缀，内部归一为 6 位。
_ASHARE_PREFIX_RE = re.compile(r"^(?:sh|sz|bj)", re.IGNORECASE)
_ASHARE_SUFFIX_RE = re.compile(r"\.(?:sh|sz|bj)$", re.IGNORECASE)
_SIX_DIGIT_RE = re.compile(r"^\d{6}$")

# A 股首位合法数字：6xx 沪 / 0xx·3xx 深 / 4xx·8xx·9xx 北交所及老三板。
_ASHARE_FIRST_DIGITS = frozenset("034689")

# 名称 / 板块名标准化时剥离的噪声字符（空白 + 常见分隔 / 标点）。
_NAME_NOISE_RE = re.compile(r"[\s\-_/\\,，。·、()（）]+")


@icontract.ensure(
    lambda result: (
        result is None
        or (
            isinstance(result, str)
            and len(result) == 6
            and result.isdigit()
            and result[0] in "034689"
        )
    ),
    "normalize_a_share_code 必须返回 None 或 6 位 A 股代码（首位 0/3/4/6/8/9）",
)
def normalize_a_share_code(code: Any) -> Optional[str]:
    """把股票代码归一为 A 股 6 位字符串。

    接受 ``300750`` / ``sz300750`` / ``300750.SZ`` / ``SH600519`` 等写法；
    港股（5 位）/ 美股（字母）/ 非法格式 / 非 A 股首位 → 返回 ``None``。
    """
    if not isinstance(code, str):
        return None
    s = code.strip()
    s = _ASHARE_PREFIX_RE.sub("", s)
    s = _ASHARE_SUFFIX_RE.sub("", s)
    if not _SIX_DIGIT_RE.match(s):
        return None
    if s[0] not in _ASHARE_FIRST_DIGITS:
        return None
    return s


def normalize_name(name: Any) -> str:
    """标准化名称 / 板块名：去首尾空白（含全角空格）+ 去常见分隔标点。"""
    if not isinstance(name, str):
        return ""
    s = name.strip().replace("　", "")
    return _NAME_NOISE_RE.sub("", s)


# ------------------------------------------------------------------
# 匹配 / 重合度纯函数
# ------------------------------------------------------------------

# 板块名匹配等级（高 → 低）。
BOARD_MATCH_EXACT = "exact"
BOARD_MATCH_CONTAINS = "contains"


@icontract.ensure(
    lambda result: result is None or result in {"exact", "contains"},
    "board_match_level 必须返回 None / 'exact' / 'contains'",
)
def board_match_level(keyword: Any, board_name: Any) -> Optional[str]:
    """判定 keyword 与板块名的匹配等级。

    返回 ``exact``（标准化后相等）/ ``contains``（互为子串）/ ``None``（不匹配）。
    """
    kw = normalize_name(keyword)
    bn = normalize_name(board_name)
    if not kw or not bn:
        return None
    if kw == bn:
        return BOARD_MATCH_EXACT
    if kw in bn or bn in kw:
        return BOARD_MATCH_CONTAINS
    return None


def find_matched_boards(keyword: Any, board_names: Sequence[Any]) -> Tuple[str, ...]:
    """从板块名列表中筛出与 keyword 匹配的板块名（保持顺序、去重）。"""
    seen: list[str] = []
    for name in board_names or ():
        name_str = str(name).strip()
        if not name_str:
            continue
        if board_match_level(keyword, name_str) is not None and name_str not in seen:
            seen.append(name_str)
    return tuple(seen)


def code_in_constituents(code: str, constituents: Sequence[Any]) -> bool:
    """目标代码是否出现在成分股集合中（成分股逐个归一后比较，健壮）。"""
    if not code:
        return False
    for raw in constituents or ():
        if normalize_a_share_code(raw) == code:
            return True
    return False


@icontract.ensure(
    lambda result: 0.0 <= result <= 1.0,
    "constituent_overlap_ratio 必须落在 [0, 1]",
)
def constituent_overlap_ratio(a: Sequence[Any], b: Sequence[Any]) -> float:
    """两个成分股集合的 Jaccard 重合度（交集 / 并集）。

    任一为空且另一非空时并集非空但交集为空 → ``0.0``；双空 → ``0.0``。
    """
    set_a = {norm for raw in (a or ()) if (norm := normalize_a_share_code(raw))}
    set_b = {norm for raw in (b or ()) if (norm := normalize_a_share_code(raw))}
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def compute_matched_by(
    code: str,
    em: "SourceEvidence",
    ths: "SourceEvidence",
    board_hint: Any,
) -> Tuple[str, ...]:
    """汇总命中的证据维度：``code``（代码入成分股）/ ``board_name``（板块名命中）。"""
    dims: list[str] = []
    all_constituents = tuple(em.constituents) + tuple(ths.constituents)
    if code_in_constituents(code, all_constituents):
        dims.append("code")
    if board_hint:
        for board in tuple(em.boards) + tuple(ths.boards):
            if board_match_level(board_hint, board) is not None:
                dims.append("board_name")
                break
    return tuple(dims)


# ------------------------------------------------------------------
# 数据结构（不可变）
# ------------------------------------------------------------------
@dataclass(frozen=True)
class SourceEvidence:
    """单个数据源对一次校验的结构化证据。"""

    source: str  # "eastmoney" | "akshare_ths" | ...
    available: bool  # 源是否可用（响应了 / 解析成功）
    matched: bool  # 目标公司是否命中（代码入成分股）
    boards: Tuple[str, ...] = ()  # 命中（与关键词匹配）的板块名
    constituents: Tuple[str, ...] = ()  # 命中板块的成分股代码
    error: Optional[str] = None  # 失败摘要（成功时为 None）

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "available": self.available,
            "matched": self.matched,
            "boards": list(self.boards),
            "constituents": list(self.constituents),
            "source": self.source,
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class VerificationResult:
    """双源校验结果（不可变）。"""

    stock_code: str
    stock_name: str
    scope: str  # "a_share" | "not_applicable"
    status: str  # confirmed | partial | conflict | unverified | not_applicable
    confidence: str  # high | medium | low
    eastmoney: SourceEvidence
    ths: SourceEvidence
    overlap_ratio: float
    matched_by: Tuple[str, ...]
    note: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "scope": self.scope,
            "status": self.status,
            "confidence": self.confidence,
            "eastmoney": self.eastmoney.to_dict(),
            "ths": self.ths.to_dict(),
            "overlap": {
                "constituent_overlap_ratio": round(self.overlap_ratio, 4),
                "matched_by": list(self.matched_by),
            },
            "note": self.note,
        }


# ------------------------------------------------------------------
# SupplyChainSourceProbe Protocol（N 源可插拔）
# ------------------------------------------------------------------
class SupplyChainSourceProbe(Protocol):
    """数据源探针协议。实现 ``probe`` 即可被 validator 调用。"""

    name: str

    def probe(self, code: str, name: str, keyword: str) -> SourceEvidence:
        """读取某股票在某关键词（板块 / 主题）下的归属证据。

        失败应在内部捕获并返回 ``available=False`` 的证据（fail-open），不抛异常。
        """
        ...


# ------------------------------------------------------------------
# 判定纯函数（决策表）
# ------------------------------------------------------------------
@icontract.ensure(
    lambda result: (
        result.status
        in {
            "confirmed",
            "partial",
            "conflict",
            "unverified",
            "not_applicable",
        }
    ),
    "judge_verification.status 必须落在 5 档判定之一",
)
@icontract.ensure(
    lambda result: result.confidence in {"high", "medium", "low"},
    "judge_verification.confidence 必须落在 3 档置信度之一",
)
@icontract.ensure(
    lambda result: 0.0 <= result.overlap_ratio <= 1.0,
    "judge_verification.overlap_ratio 必须落在 [0, 1]",
)
def judge_verification(
    code: str,
    name: str,
    em: SourceEvidence,
    ths: SourceEvidence,
    board_hint: Any,
) -> VerificationResult:
    """按决策表判定双源校验状态（纯函数，无副作用）。

    决策表（见方案「状态决策」）：

    | 条件 | status | confidence |
    | --- | --- | --- |
    | 东财和同花顺均命中目标公司 | confirmed | high |
    | 两源均可用、均定位到相关板块，归属相反 | conflict | low |
    | 仅一源命中（另一源不可用 / 未定位到板块） | partial | medium |
    | 两源均不可用，或均可用但都未命中 | unverified | low |
    """
    overlap = constituent_overlap_ratio(em.constituents, ths.constituents)
    matched_by = compute_matched_by(code, em, ths, board_hint)

    em_hit = em.available and em.matched
    ths_hit = ths.available and ths.matched
    em_found = em.available and len(em.boards) > 0
    ths_found = ths.available and len(ths.boards) > 0

    if em_hit and ths_hit:
        return _result(
            code,
            name,
            "confirmed",
            "high",
            em,
            ths,
            overlap,
            matched_by,
            "东方财富和同花顺均支持该公司属于相关板块，双源确认",
        )

    if em.available and ths.available:
        if not em_hit and not ths_hit:
            return _result(
                code,
                name,
                "unverified",
                "low",
                em,
                ths,
                overlap,
                matched_by,
                "东方财富和同花顺均可用，但两源均未命中目标公司，待核验",
            )
        if em_found and ths_found:
            return _result(
                code,
                name,
                "conflict",
                "low",
                em,
                ths,
                overlap,
                matched_by,
                "东方财富/同花顺口径冲突：两源均定位到相关板块，但对目标公司归属不一致",
            )
        return _result(
            code,
            name,
            "partial",
            "medium",
            em,
            ths,
            overlap,
            matched_by,
            "单源支持，待另一源核验",
        )

    if em_hit or ths_hit:
        return _result(
            code,
            name,
            "partial",
            "medium",
            em,
            ths,
            overlap,
            matched_by,
            "单源支持（另一源不可用），待另一源核验",
        )

    return _result(
        code,
        name,
        "unverified",
        "low",
        em,
        ths,
        overlap,
        matched_by,
        "东方财富和同花顺均不可用或无命中，待核验",
    )


def _not_applicable_result(stock_code: Any, stock_name: Any) -> VerificationResult:
    """非 A 股：直接返回 not_applicable，不联网。"""
    empty_em = SourceEvidence("eastmoney", available=False, matched=False)
    empty_ths = SourceEvidence("ths", available=False, matched=False)
    return VerificationResult(
        stock_code=str(stock_code or ""),
        stock_name=str(stock_name or ""),
        scope="not_applicable",
        status="not_applicable",
        confidence="low",
        eastmoney=empty_em,
        ths=empty_ths,
        overlap_ratio=0.0,
        matched_by=(),
        note="非 A 股标的或无法归一到 A 股 6 位代码，双源校验不适用",
    )


def _result(
    code: str,
    name: str,
    status: str,
    confidence: str,
    em: SourceEvidence,
    ths: SourceEvidence,
    overlap: float,
    matched_by: Tuple[str, ...],
    note: str,
) -> VerificationResult:
    return VerificationResult(
        stock_code=code,
        stock_name=name,
        scope="a_share",
        status=status,
        confidence=confidence,
        eastmoney=em,
        ths=ths,
        overlap_ratio=overlap,
        matched_by=matched_by,
        note=note,
    )


# ------------------------------------------------------------------
# fail-open IO 探针（实现 Protocol）
# ------------------------------------------------------------------
class SupplyChainCrossSourceValidator:
    """供应链双源校验器。

    用法::

        validator = SupplyChainCrossSourceValidator(EastMoneyProbe(), ThsAkshareProbe())
        result = validator.verify("300750", "宁德时代", board_hint="动力电池")
        # result.status in {"confirmed","partial","conflict","unverified","not_applicable"}
    """

    def __init__(
        self,
        eastmoney_probe: SupplyChainSourceProbe,
        ths_probe: SupplyChainSourceProbe,
    ) -> None:
        self._em = eastmoney_probe
        self._ths = ths_probe

    def verify(
        self,
        stock_code: Any,
        stock_name: Any,
        claim: str = "",
        board_hint: str = "",
        topic: str = "",
    ) -> VerificationResult:
        """校验单条供应链事实。永不抛异常（fail-open）。

        ``claim`` 为 LLM 语义上下文（自然语言陈述），不参与结构化判定；
        ``board_hint`` / ``topic`` 用于定位板块，``board_hint`` 优先，缺省时回退
        ``topic``，再缺省回退 ``stock_name``。
        """
        code = normalize_a_share_code(stock_code)
        if code is None:
            return _not_applicable_result(stock_code, stock_name)

        keyword = (board_hint or topic or stock_name or "").strip()
        name = str(stock_name or "")
        em = self._safe_probe(self._em, code, name, keyword)
        ths = self._safe_probe(self._ths, code, name, keyword)
        return judge_verification(code, name, em, ths, board_hint)

    @staticmethod
    def _safe_probe(
        probe: SupplyChainSourceProbe, code: str, name: str, keyword: str
    ) -> SourceEvidence:
        """逐源 fail-open：探针异常返回 available=False 证据，绝不拖垮另一源。"""
        try:
            return probe.probe(code, name, keyword)
        except Exception as exc:  # noqa: BLE001 — fail-open：源异常不影响其他源
            logger.debug(
                "[SupplyChainCrossValidate] probe %s failed: %s",
                getattr(probe, "name", "unknown"),
                exc,
            )
            return SourceEvidence(
                source=getattr(probe, "name", "unknown"),
                available=False,
                matched=False,
                error=f"{type(exc).__name__}: {exc}",
            )


# ------------------------------------------------------------------
# 默认校验器（工具层懒加载，测试可 monkeypatch）
# ------------------------------------------------------------------
