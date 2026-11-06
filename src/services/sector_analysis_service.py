# -*- coding: utf-8 -*-
"""个股板块分析服务（v2，issue #81）：主行业身份 → 背景支柱 → 概念列示 → 报告。

数据来源（全部已有数据源，不新增外部依赖）：
- 主行业身份：东财个股行业名 → 东财行业板块全表精确名称+板块代码锁定 →
  同板块成分表验证目标股；申万一/二/三级链仅作归属参考单列（不与东财混算）
- 政策倾向：行业 DNA 库（industry_dna_loader，本地静态规则背景）
- 行业基率：industry_base_rates.json（启发式先验背景，报告透出表版本）
- 当日行情：东财行业板块全表（价格强弱/活跃度/广度——不是产业景气、
  成交额活跃度不是资金净流入）
- 相关概念：iFinD 已适配入口仅列示（可选阶段，失败/超时记缺口不拖垮主报告）

v2 不产出综合总分、分档或行动结论（旧 30/30/40 权重与 fuyao 兜底已退役）；
「未评分」（评分政策）与「资料不足」/「完整性 partial」（资料状态）语义分开。
同一报告总预算钳制取数/重试/等待（取数截止提前收尾余量），缺数据记缺口不编造
（共享宪法第 2 条），预算耗尽不存档、保存失败不留孤儿文件。
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
import requests
from icontract import ensure, require
from pydantic import ValidationError

from src.core.trading_calendar import recent_completed_trading_sessions
from src.schemas.sector_analysis import SectorReportPayload

if TYPE_CHECKING:  # 跨模块字符串注解（避免循环 import；见三层防御 Layer 1）
    from src.services.sector_research import ArticleDateEvidence, EvidenceRegistry

logger = logging.getLogger(__name__)

_ID_PATTERN = "sa_{ts:%Y%m%d%H%M}"
_REPORT_DIR = Path(__file__).parent.parent.parent / "reports" / "sector_analysis"

# 政策倾向标签（对齐 F2 sector_dim 口径）。首版新报告不再产出综合总分，
# 政策只保留方向标签作为静态规则背景。
_POLICY_LABEL = {"supportive": "支持", "neutral": "中性", "restrictive": "限制", None: "未知"}

# 概念展示上限（schema ConceptsBlock.items max_length 同值一致）：
# 供应商合法返回超过上限时截取展示并标注总量，不当作供应商故障。
_CONCEPT_DISPLAY_CAP = 50

# 收尾余量（秒）：从总预算划出给渲染/严格校验/持久化的固定预留上限。
# 实际余量 = min(本值, 总预算/2)——渲染+校验+保存为毫秒级本地操作，300s 预算
# 留 5s 足够；1—5s 合法小预算下缩放为一半，避免取数时间在起点即被吃光。
# 不作为配置项暴露。
_FINISH_MARGIN_SECONDS = 5.0


# ---------------------------------------------------------------------------
# 报告总预算（issue #79 实施默认：独立板块总预算默认 300s、合法 1—300；
# 同一预算入口供后续趋势/催化票复用；取数/重试/等待全部钳制剩余预算）
# ---------------------------------------------------------------------------

class BudgetExhausted(Exception):
    """报告总预算已耗尽：终结为超时，不采纳晚到 IO、不保存伪成功。"""


class ReportBudget:
    """单次报告生成的总预算（monotonic 绝对截止时刻）。

    ``deadline`` 是绝对单调时刻（可直接传给支持 deadline 的适配器入口，
    如 iFinD 适配器的 ``deadline`` 参数）；``remaining()``/``expired()``/
    ``require()`` 用于渲染/校验/持久化等收尾边界的预算检查。

    ``finish_margin`` 从总预算中划出收尾余量：所有取数出站按
    ``fetch_deadline = deadline - finish_margin`` 钳制，保证渲染/严格校验/
    持久化在主截止内始终有剩余时间（可选阶段耗尽取数预算不会丢掉收尾余量）。
    """

    def __init__(self, total_seconds: float, *, finish_margin: float = 0.0) -> None:
        if not total_seconds or total_seconds <= 0:
            raise ValueError(f"报告总预算必须为正秒数：{total_seconds}")
        self.total_seconds = float(total_seconds)
        self.finish_margin = max(float(finish_margin), 0.0)
        self.deadline = time.monotonic() + self.total_seconds

    @property
    def fetch_deadline(self) -> float:
        """取数截止：主截止提前收尾余量。所有新出站/重试/退避检查同一截止。"""
        return self.deadline - self.finish_margin

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0

    def require(self) -> None:
        if self.expired():
            raise BudgetExhausted(
                f"报告总预算（{self.total_seconds:.0f} 秒）已耗尽，本次不采纳结果、不保存报告"
            )

    def fetch_remaining(self) -> float:
        return self.fetch_deadline - time.monotonic()

    def fetch_expired(self) -> bool:
        return self.fetch_remaining() <= 0

    def require_fetch(self) -> None:
        if self.fetch_expired():
            raise BudgetExhausted(
                f"取数预算已耗尽（总预算 {self.total_seconds:.0f} 秒，"
                f"预留 {self.finish_margin:.0f} 秒收尾余量）：不再发起新出站"
            )


# 有界取数专用线程池：进程级共享、不在请求路径上 shutdown/join。
# 超时只是放弃等待（供应商在途工作可能继续，如实披露），fut 的结果被丢弃，
# 不会进入任何已终结请求的存档 —— 这不是硬截止，也不冒充硬截止。
_FETCH_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="sector-fetch")


def bounded_call(fn: Callable[..., Any], *args: Any, budget: ReportBudget,
                 **kwargs: Any) -> Any:
    """在剩余取数预算内等待一次取数调用。

    - 取数预算已耗尽：直接拒绝，不派发新出站。
    - 等待超过剩余取数预算：放弃等待并抛 BudgetExhausted（线程内调用继续，
      结果被丢弃；供应商在途工作是否结束不由本进程等待语义决定）。
    - 被执行函数自身抛出的 ``TimeoutError``（Py3.11 与 ``futures.TimeoutError``
      同类）按局部来源错误原样传播，走调用方的缺口路径——不谎报预算耗尽。
    """
    budget.require_fetch()
    fut: Future = _FETCH_EXECUTOR.submit(fn, *args, **kwargs)
    try:
        result = fut.result(timeout=max(budget.fetch_remaining(), 0.001))
    except FutureTimeoutError:
        # 区分等待超时与 fn 自身异常：future 已因 fn 抛异常完成时，
        # fut.result 抛出的 TimeoutError 其实是 fn 的供应商超时 re-raise。
        if fut.done():
            fn_exc = fut.exception()
            if fn_exc is not None:
                raise fn_exc
        fut.cancel()
        raise BudgetExhausted(
            f"取数等待超过剩余取数预算（剩余 {budget.fetch_remaining():.1f} 秒）："
            "本次放弃等待；供应商侧调用可能仍在途，其结果不会被采纳或存档"
        ) from None
    budget.require()  # 竞态守卫：只在主截止前采纳结果
    return result


def _ak_with_retry(call: Callable[..., Any], *args: Any,
                   retries: int = 1, delay: float = 0.5,
                   retry_budget: ReportBudget,
                   **kwargs: Any) -> Any:
    """akshare 接口包一层瞬时网络重试：默认 1 次重试 + 0.5s 退避。

    场景：东财 push2 接口在网络波动时常抛 ``RemoteDisconnected`` /
    ``Max retries exceeded``，多一次重试即可恢复，避免直接 fallback 丢精度。
    非网络错误（如参数错）会原样抛出，不浪费时间。

    预算纪律（``retry_budget`` 由 :func:`_bounded_ak` 注入）：每次新出站前
    检查同一取数截止（含从队列晚获得执行权的任务——首次 require_fetch 即
    失败，不再发新调用）；退避时长钳制剩余取数预算。

    root-decision 线程内边界（PR84 复审 P2）：真正执行 SDK 的本工作线程
    内幂等安装 ``eastmoney_patch(budget_only=True)`` 并 bind 同一
    ``fetch_deadline`` 到 ``request_deadline``——AkShare 内部分页/重试
    （如 ``stock_board_industry_name_em`` → ``fetch_paginated_data``）在
    调用方放弃后发出的新 ``Session.request`` 同样服从取数截止（截止后
    派发被拒绝，不产生新 wire）。成功/供应商失败/截止异常三类退出都在
    finally reset；无 deadline 的无关 requests 不受影响，同线程后续任务
    不残留上下文。
    """
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline

    last_exc: Optional[BaseException] = None
    token: Optional["object"] = None
    eastmoney_patch(budget_only=True)
    token = request_deadline.set(retry_budget.fetch_deadline)
    try:
        for i in range(retries + 1):
            retry_budget.require_fetch()
            try:
                return call(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 保留原 except 兼容
                last_exc = exc
                if i < retries:
                    time.sleep(min(delay, max(retry_budget.fetch_remaining(), 0.0)))
        assert last_exc is not None  # 至少跑了一次循环
        raise last_exc
    finally:
        request_deadline.reset(token)


def _bounded_ak(fn: Callable[..., Any], *args: Any, budget: ReportBudget,
                retries: int = 1, delay: float = 0.5,
                **kwargs: Any) -> Any:
    """东财/申万 akshare 出站统一入口：等待、每次出站与退避钳制同一取数截止。

    ``budget`` 供 :func:`bounded_call` 等待钳制，同一对象以 ``retry_budget``
    注入线程内重试器——取数线程晚于截止获得执行权时首个出站即被拒绝。
    """
    return bounded_call(_ak_with_retry, fn, *args, retries=retries, delay=delay,
                        budget=budget, retry_budget=budget, **kwargs)


def get_report_dir() -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return _REPORT_DIR


def _claim_report_file(base: str) -> Optional[Tuple[str, Path]]:
    """原子独占创建报告文件（O_CREAT|O_EXCL），返回 (报告 id, 路径)。

    创建成功者独占该 ID 与文件：同分钟并发请求不会共享同一文件
    （先 exists 后 write 的竞态会让失败方 unlink 掉成功方的存档）。
    分钟级冲突自动追加字母后缀；全部后缀被占用时返回 None。
    只清理本函数返回路径的文件——失败方 unlink 的是自己独占创建的文件。
    """
    dir_ = get_report_dir()
    for candidate in [base] + [f"{base}{s}" for s in "abcdefgh"]:
        path = dir_ / f"{candidate}.md"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return candidate, path
        except FileExistsError:
            continue
    return None


# ---------------------------------------------------------------------------
# P0b 申万行业链反查：证监会/东财行业名 → 三级候选 → 成分股验证 → 一/二/三级链
# ---------------------------------------------------------------------------

_SW_TABLES_CACHE: Optional[Dict[str, Any]] = None
_SW_CHAIN_CACHE: Dict[str, Dict[str, Any]] = {}


def _sw_tables(budget: ReportBudget) -> Optional[Dict[str, Any]]:
    """申万一/二/三级指数表（进程内缓存，日级快照由缓存键日期保证）。"""
    global _SW_TABLES_CACHE
    if _SW_TABLES_CACHE is not None:
        return _SW_TABLES_CACHE
    try:
        import akshare as ak

        third = _bounded_ak(ak.sw_index_third_info, budget=budget)
        second = _bounded_ak(ak.sw_index_second_info, budget=budget)
        if third is None or second is None or third.empty or second.empty:
            return None
        _SW_TABLES_CACHE = {"third": third, "second": second}
        return _SW_TABLES_CACHE
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SectorAnalysis] 申万指数表获取失败: %s", str(exc)[:80])
        return None


# 证监会行业名里的通用词（匹配时剔除，提高申万三级名命中率）
_SW_STOPWORDS = ("制造业", "服务业", "生产", "供应", "和", "其他", "业", "的")

# 单字行业词白名单（≥2 过滤会丢掉「酒/药/车」等关键单字词根）
_SW_SINGLE_CHAR = frozenset("酒药茶车军银钢煤纸油气电医食纺服")


def _sw_keywords(industry_text: str) -> List[str]:
    """行业/主营文本 → 关键词集合。

    中文连续文本无空格分词（「半导体湿法清洗设备」是一个整词），直接整词匹配
    申万三级名必然 miss——对 ≥4 字的单元做 2-4 字滑窗补充（半导体/湿法/清洗/设备
    等词根进入集合）；单字单元仅白名单词根保留（酒/药/车等）。

    Ⅱ/Ⅲ 级后缀归一（issue #81 局部命名修复，真实反例 600519「白酒Ⅱ」）：
    带级位的单元同时加入去级位基词（白酒Ⅱ → 白酒），否则 3 字单元不滑窗、
    与申万三级名「白酒Ⅲ」整词不匹配，反查恒空。匹配仍以成分验证收口。
    """
    text = industry_text or ""
    for w in _SW_STOPWORDS:
        text = text.replace(w, " ")
    units = [t for t in re.split(r"[、，,\s/：:；;（）()]+", text) if t]
    kws: List[str] = []
    for u in dict.fromkeys(units):  # 保序去重
        base = _SW_LEVEL_SUFFIX_RE.sub("", u)
        if len(u) < 2:
            if u in _SW_SINGLE_CHAR and u not in kws:
                kws.append(u)
            continue
        if u not in kws:
            kws.append(u)
        if base != u and len(base) >= 2 and base not in kws:
            kws.append(base)
        effective = base if base != u else u
        if len(effective) >= 4:  # 滑窗补充 4/3/2 字词根
            for n in (4, 3, 2):
                for i in range(len(effective) - n + 1):
                    gram = effective[i : i + n]
                    if gram not in kws:
                        kws.append(gram)
    return kws


_SW_LEVEL_SUFFIX_RE = re.compile(r"[ⅠⅡⅢⅣⅤ]+$")


def _sw_hit_count(kws: List[str], name: Any) -> int:
    """关键词对申万行业名的命中数（Ⅱ/Ⅲ 归一基词参与匹配）。"""
    return sum(1 for k in kws if k in str(name))


def _resolve_sw_chain(code: str, industry_text: str, *, budget: "ReportBudget") -> Dict[str, Any]:
    """个股 → 申万一/二/三级链（参考口径，单列披露，不与东财成分混算）。

    路径：行业文本拆词 → 三级名候选（按命中词数排序，最多 8 个送成分验证）
    → index_component_sw 验证个股在成分中 → 三级行「上级行业」爬二级 → 一级。
    全链失败返回空链 + 缺口（如实，不编造）。结果按 自然日+代码 缓存。

    可选阶段隔离：取数预算耗尽记缺口返回，不向上传播 BudgetExhausted——
    申万参考链不影响东财主身份与主体报告存档。
    """
    chain: Dict[str, Any] = {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []}
    cache_key = f"{code}:{datetime.now().strftime('%Y%m%d')}"
    if cache_key in _SW_CHAIN_CACHE:
        return _SW_CHAIN_CACHE[cache_key]

    try:
        tables = bounded_call(_sw_tables, budget, budget=budget)
    except BudgetExhausted:
        chain["gaps"].append("申万链：取数预算不足，参考链未取得（参考口径，不影响主报告）")
        return chain
    if tables is None:
        chain["gaps"].append("申万链：申万指数表不可用")
        return chain
    third, second = tables["third"], tables["second"]

    kws = _sw_keywords(industry_text)
    if not kws:
        chain["gaps"].append(f"申万链：行业文本「{industry_text}」拆词后无有效关键词")
        return chain

    third = third.copy()
    third["_hits"] = third["行业名称"].map(lambda n: _sw_hit_count(kws, n))
    third["_nlen"] = third["行业名称"].map(lambda n: len(str(n)))
    # 同 hits 按名称长度升序：更精确的行业名（白酒）优先于边角长尾（其他农产品加工）
    candidates = (
        third[third["_hits"] > 0]
        .sort_values(["_hits", "_nlen"], ascending=[False, True])
        .head(8)
    )
    if candidates.empty:
        chain["gaps"].append(f"申万链：三级表无命中「{industry_text}」的候选板块")
        return chain

    locked = None
    for _, row in candidates.iterrows():
        if budget.expired():
            chain["gaps"].append("申万链：剩余预算不足，候选成分验证中止（如实披露）")
            break
        code_col = str(row.get("行业代码") or "").replace(".SI", "")
        try:
            import akshare as ak

            # 候选循环里 1-8 个候选要快速失败切换，不走重试（retries=0）——但
            # 必须经 _bounded_ak：bounded_call 的 require_fetch 只在 submit 前
            # 主线程检查，直接传 vendor 会让工作线程晚获得执行权时绕过 fetch
            # 截止（反例实测 vendor 在截止后 +0.16s 才启动）。线程内首出站
            # 检查同一取数截止，retries=0 保持快速切换语义。
            cons = _bounded_ak(ak.index_component_sw, symbol=code_col,
                               budget=budget, retries=0)
        except BudgetExhausted:
            chain["gaps"].append("申万链：剩余预算不足，候选成分验证中止（如实披露）")
            break
        except Exception:  # noqa: BLE001 - 单个候选失败继续下一个
            continue
        if cons is None or cons.empty or "证券代码" not in cons.columns:
            continue
        if cons["证券代码"].astype(str).str.contains(code).any():
            locked = row
            break
    if locked is None:
        chain["gaps"].append(f"申万链：{len(candidates)} 个候选三级板块成分验证均未命中 {code}")
        return chain

    chain["l3"] = str(locked.get("行业名称") or "")
    chain["l3_code"] = str(locked.get("行业代码") or "")
    l2_name = str(locked.get("上级行业") or "")
    chain["l2"] = l2_name
    if l2_name and not second.empty:
        l2_row = second[second["行业名称"] == l2_name]
        if not l2_row.empty:
            chain["l1"] = str(l2_row.iloc[0].get("上级行业") or "")
    _SW_CHAIN_CACHE[cache_key] = chain
    return chain


# ---------------------------------------------------------------------------
# P1 主行业身份（issue #81）：东财个股行业 → 东财行业板块锁定（精确名称+板块代码）
# → 同板块成分验证目标股 → 申万参考链（单列，不与东财成分混算）
# ---------------------------------------------------------------------------

def _resolve_em_board(industry_name: str, board_table: pd.DataFrame) -> Dict[str, Any]:
    """东财行业板块全表 → 精确名称锁定（板块名称+板块代码双确认）。

    不做包含/前缀近似匹配：无精确命中时不取首行，近似名如实透出供人工核对；
    多行精确命中按多候选处理，不静默确证；缺板块代码列时同样不可锁定。
    """
    out: Dict[str, Any] = {
        "locked": False, "board_name": "", "board_code": "",
        "near_names": [], "gaps": [],
    }
    if board_table is None or board_table.empty or "板块名称" not in board_table.columns:
        out["gaps"].append("东财行业板块表为空或缺名称列，板块身份未锁定")
        return out
    exact = board_table[board_table["板块名称"].astype(str) == industry_name]
    if len(exact) > 1:
        codes = sorted({str(c) for c in exact.get("板块代码", pd.Series(dtype=str)).tolist()})
        out["gaps"].append(
            f"板块表「{industry_name}」精确命中 {len(exact)} 行（代码 {codes[:5]}），无法唯一锁定"
        )
        return out
    if len(exact) == 1:
        board_code = str(exact.iloc[0].get("板块代码") or "").strip()
        if not board_code or board_code.lower() == "nan":
            out["gaps"].append(f"板块表「{industry_name}」命中行缺少板块代码，名称+代码双确认不成立")
            return out
        out["locked"] = True
        out["board_name"] = industry_name
        out["board_code"] = board_code
        return out
    # 无精确命中：披露近似名（不做包含/截短匹配，不取首行）。
    # 近似 = 板块名与行业名互为子串（白酒 ⊂ 白酒Ⅱ / 电池 ⊂ 电池薄膜），仅透出核对。
    near = board_table[
        board_table["板块名称"].map(
            lambda n: bool(str(n)) and (str(n) in industry_name or industry_name in str(n))
        )
    ]
    near_names = sorted({str(n) for n in near["板块名称"].tolist()})[:5]
    out["near_names"] = near_names
    hint = f"；存在近似名 {near_names}（不近似取首行）" if near_names else ""
    out["gaps"].append(f"板块表无「{industry_name}」精确命中，板块身份未锁定{hint}")
    return out


def _identify_sector(code: str, *, budget: ReportBudget) -> Dict[str, Any]:
    """识别个股主行业身份（同一东财行业板块口径，issue #78/#81）。

    subject_status 语义（贯穿载荷/正文/页面，不静默取近似名）：
    - confirmed：东财板块锁定（精确名称+板块代码）且成分表含目标股
    - unverified：板块已锁定但成分验证失败或不含目标股（归属未确认）
    - ambiguous：行业名存在但板块身份无法唯一锁定（无精确命中/多候选/表不可用）
    - missing：无行业数据（识别链完全失败）

    供应商 as_of 未回显时 ``vendor_as_of`` 保持 None；``observed_at`` 是本系统
    采集时点，两者不混用。巨潮仅作行业名参考兜底。旧 fuyao 兜底（伪造 _Ctx
    调用 + 经深研聚合的隐式大出站）已移除：实现错误不再被包装成供应商失败。
    """
    out: Dict[str, Any] = {
        "industry_em": "",        # 东财个股信息行业名（无代码/层级/日期元数据）
        "industry_cninfo": "",    # 巨潮证监会行业（兜底参考）
        "business_scope": "",     # 巨潮主营业务文本（申万反查/政策/基率的关键词补充）
        "board_name": "",         # 东财行业板块精确名称
        "board_code": "",         # 东财板块代码（BK****）
        "subject_status": "missing",
        "membership": {"verified": False, "member_count": None},
        "observed_at": None,      # 本系统采集时点（与供应商 as_of 区分）
        "vendor_as_of": None,     # 供应商未回显 as_of，如实为未知
        "classification_system": None,   # 分类体系（锁定口径）；无行业数据时 None
        "classification_level": None,    # 供应商未返回层级，如实未知不推算
        "classification_version": None,  # 供应商未返回版本，如实未知不推算
        "sw_chain": {},           # 申万一/二/三级参考链（成分验证；口径单列）
        "sector_name": "",        # 当日行情查询键（锁定的板块名）
        "gaps": [],
        "_board_table": None,     # 非载荷键：身份阶段板块全表快照（行情支柱复用）
        "_cons_snapshot": None,   # 非载荷键：成分验证同一快照（同业支柱复用，issue #82）
        "_cons_observed_at": None,  # 非载荷键：成分快照采集时点（与板块表时点分开）
    }
    # 1) 东财个股信息（行业名；字段仅名称，无层级/代码/日期元数据，如实未知）
    try:
        import akshare as ak

        df = _bounded_ak(ak.stock_individual_info_em, symbol=code, budget=budget)
        if df is not None and not df.empty:
            kv = dict(zip(df["item"], df["value"]))
            out["industry_em"] = str(kv.get("行业") or "").strip()
    except BudgetExhausted:
        raise
    except Exception as exc:  # noqa: BLE001
        out["gaps"].append(f"东财个股行业查询失败: {str(exc)[:50]}")

    # 2) 巨潮资讯（证监会行业 + 主营业务文本，独立链路兜底参考）
    if not out["industry_em"]:
        try:
            import akshare as ak

            df = _bounded_ak(ak.stock_profile_cninfo, symbol=code, budget=budget)
            if df is not None and not df.empty:
                row0 = df.iloc[0]
                industry = str(row0.get("所属行业") or "").strip()
                if industry:
                    out["industry_cninfo"] = industry
                # 主营业务文本：证监会行业名常不含赛道词（至纯=专用设备 vs 申万=半导体设备），
                # 主营业务补足赛道关键词，提高申万链/政策/基率命中率
                biz = str(row0.get("主营业务") or "").strip()
                if biz and biz != "nan":
                    out["business_scope"] = biz[:200]
        except BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out["gaps"].append(f"巨潮行业查询失败: {str(exc)[:50]}")

    lookup_industry = out["industry_em"] or out["industry_cninfo"]
    # 3) 东财行业板块锁定（全表一次取回，身份与当日行情共用同一口径）
    board_table: Optional[pd.DataFrame] = None
    if lookup_industry:
        try:
            import akshare as ak

            board_table = _bounded_ak(ak.stock_board_industry_name_em, budget=budget)
            out["observed_at"] = datetime.now().isoformat(timespec="seconds")
            out["_board_table"] = board_table  # 非载荷键：行情支柱复用同一快照
        except BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out["gaps"].append(f"东财行业板块表查询失败: {str(exc)[:50]}")

    if lookup_industry and board_table is not None and not board_table.empty:
        resolved = _resolve_em_board(lookup_industry, board_table)
        out["gaps"].extend(resolved["gaps"])
        if resolved["locked"]:
            out["board_name"] = resolved["board_name"]
            out["board_code"] = resolved["board_code"]
            # 4) 同板块成分验证目标股（归属确认；申万成分不替代东财成分）
            try:
                import akshare as ak

                cons = _bounded_ak(
                    ak.stock_board_industry_cons_em,
                    symbol=out["board_code"], budget=budget,
                )
                if cons is not None and not cons.empty and "代码" in cons.columns:
                    cons_codes = cons["代码"].astype(str)
                    out["membership"]["member_count"] = int(len(cons_codes))
                    if (cons_codes == code).any():
                        out["membership"]["verified"] = True
                    else:
                        out["gaps"].append(
                            f"东财板块「{out['board_name']}」（{out['board_code']}）成分 "
                            f"{len(cons_codes)} 只不含目标股 {code}，归属未确认"
                        )
                    # issue #82：同一已验证快照保留给同业支柱（避免重复请求与
                    # 盘中漂移）；非载荷键，compose_report pop。
                    out["_cons_snapshot"] = cons
                    out["_cons_observed_at"] = datetime.now().isoformat(timespec="seconds")
                else:
                    out["gaps"].append("东财板块成分表为空或缺代码列，归属未确认")
            except BudgetExhausted:
                raise
            except Exception as exc:  # noqa: BLE001
                out["gaps"].append(f"东财板块成分查询失败: {str(exc)[:50]}，归属未确认")

    # 5) 申万一/二/三级参考链移至 compose_report（issue #82）：趋势/同业等
    # 必需新块必须先于申万参考等可选长链出站，不能让可选链吃完取数预算；
    # 申万链在此处为空 dict，由编排层回填同一结构。

    # 6) subject_status 判定
    if not lookup_industry:
        out["subject_status"] = "missing"
        out["gaps"].append("板块识别失败：无行业数据")
    elif out["board_code"]:
        out["subject_status"] = "confirmed" if out["membership"]["verified"] else "unverified"
    else:
        out["subject_status"] = "ambiguous"
    out["sector_name"] = out["board_name"] or lookup_industry
    # 分类元数据：体系名=本报告锁定的东财行业板块口径；层级/版本供应商未返回
    # 恒为 None（正文如实标未知，不推算、不用查询时间顶替）。
    if lookup_industry:
        out["classification_system"] = "东财行业板块"
    return out


# ---------------------------------------------------------------------------
# P2 政策倾向支柱（行业 DNA 库）
# ---------------------------------------------------------------------------

def _policy_pillar(industry_text: str) -> Dict[str, Any]:
    lean: Optional[str] = None
    gaps: List[str] = []
    try:
        from src.services.supply_chain.industry_dna_loader import (
            get_all_dna,
            lookup_industry_policy_lean,
        )

        # 先走官方短词查询（语义对齐 F2 维度）
        lean = lookup_industry_policy_lean(industry_text)
        if lean is None:
            # loader 是反向匹配（候选词须完整出现在 DNA 关键词里），长文本必 miss——
            # 反查：遍历 DNA 关键词，找出现在文本中的词，取首个命中 DNA 的政策倾向
            text = (industry_text or "").lower()
            for dna in get_all_dna().values():
                if any(str(k).lower() in text for k in dna.keywords):
                    lean = getattr(dna, "extra", {}).get("policy_lean") if isinstance(
                        getattr(dna, "extra", None), dict
                    ) else None
                    if lean is None:
                        lean = getattr(dna, "policy_lean", None)
                    if lean in ("supportive", "neutral", "restrictive"):
                        break
                    lean = None
    except Exception as exc:  # noqa: BLE001
        gaps.append(f"政策倾向查询失败: {str(exc)[:60]}")
    # v2 载荷不再携带政策档位分：政策 DNA 是静态规则背景，方向标签（支持/中性/
    # 限制）足够表达，分数容易与综合评分混淆
    return {
        "lean": lean,
        "label": _POLICY_LABEL.get(lean, "未知"),
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# P3 行业基率支柱（启发式默认表，可迭代，版本透出）
# ---------------------------------------------------------------------------

def _base_rate_pillar(lookup_text: str) -> Dict[str, Any]:
    from src.deep_research_dims.industry_base_rate import (
        all_rates,
        lookup_base_rate,
        rate_percentile,
        table_meta,
    )

    rate, basis = lookup_base_rate(lookup_text)
    meta = table_meta()
    return {
        "base_rate": rate,
        "basis": basis,
        "hit_keyword": basis.split(":", 1)[1] if ":" in basis else "",
        "is_default": basis == "neutral_default",
        "table_version": meta["version"],
        "table_updated": meta["updated"],
        "table_industry_count": meta["industry_count"],
        "percentile": rate_percentile(rate),
        "all_rates": all_rates(),
    }


# ---------------------------------------------------------------------------
# P4 当日行情支柱（东财行业板块全表：动量/换手活跃度/成交额活跃度/广度）
#
# 口径纪律（issue #78/#81）：同一东财板块（名称+板块代码）精确命中，不做
# 名称近似/截短匹配，不回退新浪证监会口径板块表（不同分类不混算）；
# 「成交额活跃度」是成交额相对水平，不是资金净流入；当日价格强弱不是产业景气。
# ---------------------------------------------------------------------------

def _median(values: List[Optional[float]]) -> Optional[float]:
    """中位数计算。接受 ``Optional[float]``，自动过滤 None。"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


@require(lambda sector_name: isinstance(sector_name, str),
         "sector_name must be a string")
@require(lambda budget: isinstance(budget, ReportBudget) and not budget.expired(),
         "budget must be a live ReportBudget")
@require(lambda board_table: board_table is None or isinstance(board_table, pd.DataFrame),
         "board_table snapshot must be None or a DataFrame")
@ensure(
    lambda result: (
        isinstance(result, dict)
        and "sector_found" in result
        and "gaps" in result
    ),
    "intraday pillar must return dict with sector_found/gaps keys",
)
@ensure(
    lambda result: (
        result.get("rank_of") is None
        or result.get("total_boards") is None
        or 1 <= result["rank_of"] <= result["total_boards"]
    ),
    "rank_of must be in [1, total_boards] when both are set",
)
@ensure(
    lambda result: all(
        result.get(dim) is None or 0.0 <= result[dim] <= 100.0
        for dim in ("momentum", "activity", "amount_activity", "breadth")
    ),
    "intraday dims must be None or in [0, 100]",
)
def _prosperity_pillar(sector_name: str, *, budget: ReportBudget,
                       board_code: Optional[str] = None,
                       board_table: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """东财行业板块全表 → 目标板块行 + 四维当日行情分（0-100）。

    四维：动量（涨跌幅有效样本分位 = 严格低于目标的有效样本数 ÷
    (有效样本数−1) × 100，并列不计入「低于」数，并列最高不恒为 100、
    全相等为 0）/ 换手活跃度（换手率 vs 全表中位）/成交额活跃度（成交额
    vs 全表中位，非资金净流入）/广度（上涨家数占比）。全表排名只在涨跌幅
    全部有效时输出（名次 = 严格高于目标的个数 + 1，并列最高同为 1）；有效
    样本不足 2 个 → 比较样本不足，动量与全表排名保守 N/A；2 ≤ 有效样本 <
    观测总量（同行涨幅缺失/非有限）时有效集合 ≠ 观测全表，不输出无依据的
    全表名次，动量仍按有效样本计算并在缺口注明资格（rank_of=None）。
    ``total_boards`` 保持观测板块总量语义。

    命中纪律（issue #81 第二轮：同键唯一，绝不 iloc[0] 裁决）：已知板块代码
    （身份锁定同键）时命中行必须「板块代码与锁定代码一致**且**板块名称与
    锁定名称一致」且唯一——同键恰 1 行才命中；同键多行（重复同键）、代码
    命中但无同名行（错名/快照漂移）、代码列缺失（同键不成立）都记缺口，
    不取首行、不回退名称或近似名（同名不同代码会绑错板块）。无代码时按
    唯一精确名称（多行命中不可锁定、不取首行）；包含/前缀截短近似不命中。
    东财表不可用时不回退新浪口径板块表。``board_table`` 传入身份阶段的
    同一快照时直接复用，不再二次出站（身份与行情同一口径快照）。
    """
    out: Dict[str, Any] = {
        "sector_found": False,
        "momentum": None, "activity": None, "amount_activity": None, "breadth": None,
        "rank_of": None, "total_boards": None,
        "row": {},
        "gaps": [],
    }
    if not sector_name:
        out["gaps"].append("当日行情：板块名为空")
        return out
    if board_table is not None:
        df = board_table
    else:
        try:
            import akshare as ak

            df = _bounded_ak(ak.stock_board_industry_name_em, budget=budget)
        except BudgetExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            out["gaps"].append(f"当日行情：东财行业板块表查询失败 {str(exc)[:50]}")
            return out
    if df is None or df.empty:
        out["gaps"].append("当日行情：东财行业板块表为空")
        return out
    if "板块名称" not in df.columns:
        out["gaps"].append("当日行情：板块表缺少名称列")
        return out

    df = df.copy()
    total = len(df)
    out["total_boards"] = total

    # 命中行：已知板块代码 → 名称+代码同键唯一命中（同键冲突/错名/缺列记缺口，
    # 绝不 iloc[0] 静默裁决）；无代码 → 唯一精确名称
    hit: Optional[pd.DataFrame] = None
    if board_code:
        if "板块代码" not in df.columns:
            out["gaps"].append(
                f"当日行情：行情表缺板块代码列，无法与锁定代码 {board_code} 同键命中，"
                "不按名称回退取行"
            )
            return out
        by_code = df.loc[df["板块代码"].astype(str) == str(board_code)]
        if by_code.empty:
            out["gaps"].append(
                f"当日行情：身份锁定板块代码 {board_code} 未出现在行情全表，"
                "不按名称回退取行（同名板块可能不同代码）"
            )
            return out
        same_key = by_code.loc[by_code["板块名称"].astype(str) == str(sector_name)]
        if len(same_key) == 1:
            hit = same_key
        elif len(same_key) > 1:
            out["gaps"].append(
                f"当日行情：板块「{sector_name}」（{board_code}）同键命中 {len(same_key)} 行，"
                "无法唯一锁定，不取首行"
            )
            return out
        else:
            wrong = sorted({str(n) for n in by_code["板块名称"].tolist()})[:5]
            out["gaps"].append(
                f"当日行情：板块代码 {board_code} 在行情表对应名称 {wrong}，"
                f"与锁定名称「{sector_name}」不一致（同代码可能绑多板块/快照漂移），"
                "不取首行"
            )
            return out
    if hit is None or hit.empty:
        hit = df.loc[df["板块名称"].astype(str) == sector_name]
        if len(hit) > 1:
            out["gaps"].append(
                f"当日行情：名称「{sector_name}」命中 {len(hit)} 行，无法唯一锁定，不取首行"
            )
            return out
    if hit.empty:
        out["gaps"].append(
            f"当日行情：全表 {total} 个板块未精确命中「{sector_name}」"
            + (f"（板块代码 {board_code}）" if board_code else "")
            + "，不按近似名取行"
        )
        return out

    row = hit.iloc[0]
    out["sector_found"] = True
    out["row"] = {
        "board_name": str(row.get("板块名称") or ""),
        "board_code": str(row.get("板块代码") or "") if "板块代码" in df.columns else "",
        "change_pct": _num(row.get("涨跌幅")),
        "turnover_rate": _num(row.get("换手率")),
        "total_market_cap": _num(row.get("总市值")),
        "amount": _num(row.get("成交额")),
        "up_count": _num(row.get("上涨家数")),
        "down_count": _num(row.get("下跌家数")),
        "leader": str(row.get("领涨股票") or ""),
        "leader_change_pct": _num(row.get("领涨股票-涨跌幅")),
    }

    # 动量：涨跌幅有效样本分位 = 严格低于目标的有效样本数 ÷ (有效样本数−1) × 100
    # （并列值不计入「低于」数，故并列最高不恒为 100 分、全相等为 0 分）。
    # NaN 由 coerce/dropna 剔除，±inf 同样不是有效行情。排名/动量资格说明
    # 统一在此给出，不依赖目标涨跌幅是否有效、不依赖能否计算动量：
    # 有效样本 < 2 → 比较样本不足，不计算动量分、不提供全表排名（保守 N/A）；
    # 2 ≤ 有效样本 < 观测总量 → 无法确认缺失板块名次，不提供全表排名
    # （动量仍按有效样本计算）；全表全部有效时名次 = 严格高于目标的个数 + 1
    # （最高=1，并列最高同为 1）
    if "涨跌幅" in df.columns:
        df["涨跌幅"] = pd.to_numeric(df["涨跌幅"], errors="coerce")
        numeric = df["涨跌幅"].dropna()
        valid = numeric[numeric.map(math.isfinite)]
        target = out["row"]["change_pct"]
        if len(valid) < 2:
            out["gaps"].append(
                f"当日行情-动量与全表排名：观测 {total} 个板块中涨跌幅有效样本仅 "
                f"{len(valid)} 个（缺失/非有限行不参与比较），比较样本不足，"
                "不计算动量分、不提供全表排名"
            )
        elif len(valid) < total:
            out["gaps"].append(
                f"当日行情-全表排名：观测 {total} 个板块中涨跌幅有效样本仅 "
                f"{len(valid)} 个（缺失/非有限行不参与名次比较），"
                "无法确认缺失板块名次，不提供全表排名"
            )
        if target is not None and len(valid) > 1:
            below = int((valid < target).sum())
            if len(valid) == total:
                out["rank_of"] = 1 + int((valid > target).sum())
            out["momentum"] = round(below / (len(valid) - 1) * 100, 1)

    # 换手活跃度：换手率 / 全表中位
    if "换手率" in df.columns:
        med = _median([_num(v) for v in df["换手率"].tolist()])
        t = out["row"]["turnover_rate"]
        if med and t is not None and med > 0:
            out["activity"] = round(min(100.0, max(0.0, t / med * 50.0)), 1)
    else:
        out["gaps"].append("当日行情-换手活跃度：板块表缺换手率列")

    # 成交额活跃度：成交额 / 全表中位（相对水平，非资金净流入；
    # 当日全表缺成交额列时如实记缺口——历史日线成交额不能冒充全表分位）
    if "成交额" in df.columns:
        med = _median([_num(v) for v in df["成交额"].tolist()])
        a = out["row"]["amount"]
        if med and a is not None and med > 0:
            out["amount_activity"] = round(min(100.0, max(0.0, a / med * 50.0)), 1)
    else:
        out["gaps"].append("当日行情-成交额活跃度：当日全表缺成交额列（非净流入口径）")

    # 广度：上涨家数占比
    if "上涨家数" in df.columns and "下跌家数" in df.columns:
        up, down = out["row"]["up_count"], out["row"]["down_count"]
        if up is not None and down is not None and (up + down) > 0:
            out["breadth"] = round(up / (up + down) * 100, 1)
    else:
        out["gaps"].append("当日行情-广度：板块表缺上涨/下跌家数列")

    for dim, label in (("momentum", "动量"), ("activity", "换手活跃度"),
                       ("amount_activity", "成交额活跃度"), ("breadth", "广度")):
        if out[dim] is None and not any(label in g for g in out["gaps"]):
            out["gaps"].append(f"当日行情-{label}：板块表缺列或数据无效")
    return out


def _num(v: Any) -> Optional[float]:
    """数值适配入口：非有限数（NaN/±inf，含字符串形式）不是有效行情，归一为缺失。"""
    try:
        if v is None or v == "" or v == "-":
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ---------------------------------------------------------------------------
# P4b 近期板块趋势与同业比较（issue #82）：
# - 趋势：严格日历最近 20 个已完成交易日；板块日线（东财 BK 直查）与沪深300
#   指数日线各自对齐同一期望窗口，20/20 合格才各自有区间收益（19 间隔复合，
#   Decimal 计算）；相对差（百分点）仅两源对同一窗口均合格时输出。
# - 同业：复用身份阶段已验证的同一成分快照，按成交额前五 + 目标股去重选
#   「成交活跃代表」（非市值/基本面龙头）；目标位置按快照当日涨跌幅。
# 口径纪律（issue #78）：不用「20日收益」冒充 19 间隔；不补自然日、不取交集
# 凑 20；快照当日口径与 20 日趋势窗口是不同时间口径，正文分开标注。
# ---------------------------------------------------------------------------

_DEC_ONE = Decimal("1")
_DEC_HUNDRED = Decimal("100")


def _decimal_close(v: Any) -> Optional[Decimal]:
    """收盘价适配：可解析为有限正 Decimal 才有效（NaN/±inf/0/负/字符串非数 → None）。"""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    try:
        d = Decimal(str(v).strip())
    except (ArithmeticError, ValueError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return d


@require(lambda closes: len(closes) >= 2 and all(
    isinstance(c, Decimal) and c.is_finite() and c > 0 for c in closes),
    "closes must be >=2 finite positive Decimals")
@ensure(lambda result: isinstance(result, Decimal) and result.is_finite(),
        "period return must be a finite Decimal")
def _period_return_pct(closes: List[Decimal]) -> Decimal:
    """区间收益（百分点）：首尾收盘复合，19 个间隔不冒充「20日收益」。"""
    return (closes[-1] / closes[0] - _DEC_ONE) * _DEC_HUNDRED


def _qualify_daily_closes(df: Optional[pd.DataFrame], date_col: str,
                          close_col: str, expected: List[Any],
                          source_label: str,
                          ) -> Tuple[Dict[Any, Decimal], bool, List[str]]:
    """日线按期望交易日集对齐取收盘：返回（合格观测映射, 是否 20/20 合格, 缺口）。

    观测映射是**唯一资格真源**（display/收益共用，不重复判定）：键=期望窗口
    内日期、值=有限正 Decimal 收盘；部分合格时保留已取得部分（无发明值）。
    ``qualified`` 仅当每个期望日期恰一行且收盘有效（len==20；重复冲突日不进
    映射因此自动不合格）。窗口外行忽略（多给的旧日不能凑 20）；日期解析失败
    的行忽略并注明；重复日期冲突该日不取其一（整源不给区间收益）、其余无冲突
    日期的合格观测保留展示；无效收盘日期留缺口不进观测。
    供应商畸形列形状（重复列名等）与日期列整体解析失败是**源错误**：按缺口
    降块返回，不逃出边界误报为用户请求错误。重复日、收盘无效日、真缺失日三
    类缺陷独立收集、独立有界输出（不因重复日短路其他缺口）；日期一律
    isoformat 紧凑展示 + 上限 5 个 + 总数，单项长度可守 schema 上限。
    """
    gaps: List[str] = []
    if not isinstance(df, pd.DataFrame) or df.empty:
        return {}, False, [f"{source_label}：返回为空或形状异常"]
    if date_col not in df.columns or close_col not in df.columns:
        return {}, False, [f"{source_label}：缺日期列或收盘列"]
    date_series = df[date_col]
    close_series = df[close_col]
    # 重复列名等畸形形状下 df[col] 是 DataFrame 而非 Series：源错误按缺口降块
    if not isinstance(date_series, pd.Series) or not isinstance(close_series, pd.Series):
        return {}, False, [
            f"{source_label}：日期/收盘列形状异常（重复列名等，源错误），本源本窗口不采用"
        ]
    expected_set = set(expected)
    # format="mixed"：供应商日期列混合 str/Timestamp 且可能含不可解析行，
    # 逐元素解析 + coerce（解析失败 NaT 计入缺口），行为与默认一致但免格式推断警告；
    # 整列不可解析等供应商格式异常按源错误降块，不逃出边界（不误报请求 400）
    try:
        parsed = pd.to_datetime(date_series, errors="coerce", format="mixed")
    except (ValueError, TypeError) as exc:
        return {}, False, [
            f"{source_label}：日期列整体解析失败（{str(exc)[:60]}，源错误），本源本窗口不采用"
        ]
    unparseable = int(parsed.isna().sum())
    if unparseable:
        gaps.append(f"{source_label}：{unparseable} 行日期无法解析，已忽略")
    dup_dates: set = set()
    seen: set = set()
    by_date: Dict[Any, Decimal] = {}
    invalid_close_dates: List[Any] = []
    for d_raw, c_raw in zip(parsed, close_series):
        if pd.isna(d_raw):
            continue
        d = d_raw.date() if hasattr(d_raw, "date") else d_raw
        if d not in expected_set:
            continue  # 窗口外行（含多给旧日）不参与，也不缺口——缺天由期望集对齐判定
        if d in seen:
            dup_dates.add(d)
            by_date.pop(d, None)  # 冲突日两个值都不取其一，其余合格观测保留展示
            continue
        seen.add(d)
        close = _decimal_close(c_raw)
        if close is None:
            invalid_close_dates.append(d)
            continue
        by_date[d] = close
    if dup_dates:
        shown = ", ".join(d.isoformat() for d in sorted(dup_dates)[:5])
        more = f" 等 {len(dup_dates)} 天" if len(dup_dates) > 5 else ""
        gaps.append(
            f"{source_label}：期望窗口内 {len(dup_dates)} 天重复日期冲突"
            f"（{shown}{more}；冲突不静默取其一），数据不合格，区间收益不计算"
        )
    if invalid_close_dates:
        shown = ", ".join(d.isoformat() for d in sorted(invalid_close_dates)[:5])
        more = f" 等 {len(invalid_close_dates)} 天" if len(invalid_close_dates) > 5 else ""
        gaps.append(
            f"{source_label}：{len(invalid_close_dates)} 天收盘无效或非正"
            f"（{shown}{more}），对应日期无合格观测"
        )
    # 真缺失=期望窗口内既无合格观测、也未被重复/无效缺口单独报告的日期（不重复计数）
    reported = set(dup_dates) | set(invalid_close_dates)
    missing = [d for d in expected if d not in by_date and d not in reported]
    if missing:
        shown = ", ".join(d.isoformat() for d in missing[:5])
        more = f" 等 {len(missing)} 天" if len(missing) > 5 else ""
        got = len(by_date)
        gaps.append(
            f"{source_label}：期望 {len(expected)} 个已完成交易日中取得 {got} 天合格收盘，"
            f"缺失 {len(missing)} 天（{shown}{more}），"
            "不取交集/旧日凑数，区间收益不计算"
        )
    return by_date, len(by_date) == len(expected) and not dup_dates, gaps


def _observation_rows(obs: Dict[Any, Decimal]) -> List[Dict[str, Any]]:
    """合格观测映射 → 升序边界序列（date ISO str + float close；同一映射，无发明值）。"""
    return [
        {"date": d.isoformat(), "close": float(obs[d])}
        for d in sorted(obs.keys())
        if hasattr(d, "isoformat")
    ]


@ensure(
    lambda result: (
        result.get("relative_return_pp") is None
        or (
            result.get("period_return_pct") is not None
            and isinstance(result.get("benchmark"), dict)
            and result["benchmark"].get("period_return_pct") is not None
        )
    ),
    "relative_return_pp requires both board and benchmark qualified period returns",
)
def _sector_trend_pillar(board_code: str, board_name: str, *, budget: ReportBudget,
                         subject_confirmed: bool = False,
                         current_time: Optional[datetime] = None) -> Dict[str, Any]:
    """板块近期趋势（issue #82）：confirmed 资格在前，板块与基准各自有界。

    资格：主行业 confirmed 才发趋势/基准出站（不变量1；unverified 时板块代码
    虽已锁定但归属未确认，同样不查询）；否则 eligible=False 仅记录未执行原因。
    严格日历不可用 → 明确缺口且不出站（不自然日 fallback）。
    板块与基准各自 20/20 合格才各有区间收益；**实际合格观测（observations）与
    覆盖数（qualified_sessions）独立于区间收益资格保留**：部分合格时保留已取
    得部分（无发明值），板块侧与基准侧独立展示（合格基准不因板块残缺而隐藏）。
    供应商失败/超时/取数预算耗尽只降对应块（已取得内容保留），不向上传播
    异常——总截止收尾由提交边界把关；返回形状异常（如 dict）按源错误记缺口，
    不逃出边界。
    """
    out: Dict[str, Any] = {
        "eligible": False,
        "window_completed_sessions": 20,
        "interval_count": 19,
        "start_date": None, "end_date": None,
        "expected_dates": [],
        "qualified_sessions": None,
        "observations": [],
        "period_return_pct": None,
        "benchmark": None,
        "relative_return_pp": None,
        "source": None,
        "observed_at": None,
        "gaps": [],
    }
    observed_at = datetime.now().isoformat(timespec="seconds")
    out["observed_at"] = observed_at
    if not subject_confirmed or not board_code:
        out["gaps"].append("板块趋势：主行业身份未确认（confirmed 前置不满足），不执行板块趋势查询")
        return out
    out["eligible"] = True
    sessions = recent_completed_trading_sessions("cn", 20, current_time=current_time)
    if not sessions:
        out["gaps"].append(
            "板块趋势：严格交易日历不可用，无法确定最近 20 个已完成交易日，"
            "本次不查询板块/基准日线（不按自然日补齐）"
        )
        return out
    expected = sorted(sessions)  # 升序（newest-first 输入 → 升序窗口）
    out["expected_dates"] = [d.isoformat() for d in expected]
    out["start_date"] = expected[0].isoformat()
    out["end_date"] = expected[-1].isoformat()
    start_yyyymmdd = expected[0].strftime("%Y%m%d")
    end_yyyymmdd = expected[-1].strftime("%Y%m%d")

    # 板块日线（东财 BK 代码直查，避免内部按名称再映射）
    import akshare as ak

    board_obs: Dict[Any, Decimal] = {}
    board_qualified = False
    try:
        board_df = _bounded_ak(
            ak.stock_board_industry_hist_em, symbol=board_code,
            start_date=start_yyyymmdd, end_date=end_yyyymmdd,
            period="日k", adjust="", budget=budget,
        )
        if not isinstance(board_df, pd.DataFrame):
            # 错误形状（如 dict/str/None）按供应商源错误降块，不逃出边界成 500
            shape = type(board_df).__name__
            board_df = None
            out["gaps"].append(
                f"板块趋势：板块日线返回形状异常（{shape}，源错误），本次不采用"
            )
    except BudgetExhausted:
        out["gaps"].append("板块趋势：取数预算不足，板块日线未取得（已取得内容保留）")
        board_df = None
    except Exception as exc:  # noqa: BLE001 - 供应商失败降块（含 TimeoutError）
        out["gaps"].append(
            f"板块趋势：板块日线查询失败: {str(exc)[:60]}（供应商局部失败，只降趋势块）"
        )
        board_df = None
    if isinstance(board_df, pd.DataFrame):
        board_obs, board_qualified, board_gaps = _qualify_daily_closes(
            board_df, "日期", "收盘", expected,
            f"板块日线（{board_name} {board_code}）",
        )
        out["gaps"].extend(board_gaps)
    out["observations"] = _observation_rows(board_obs)
    out["qualified_sessions"] = len(board_obs)
    if board_qualified:
        out["source"] = "东财行业板块日线"
        board_closes = [board_obs[d] for d in expected]
        ret = _period_return_pct(board_closes)
        out["period_return_pct"] = float(round(ret, 4))

    # 基准日线（沪深300，同一期望窗口；失败/不合格单独降基准块）
    bench_gaps: List[str] = []
    try:
        bench_df = _bounded_ak(
            ak.stock_zh_index_daily_em, symbol="sh000300",
            start_date=start_yyyymmdd, end_date=end_yyyymmdd, budget=budget,
        )
        if not isinstance(bench_df, pd.DataFrame):
            shape = type(bench_df).__name__
            bench_df = None
            bench_gaps = [f"基准日线（沪深300）：返回形状异常（{shape}，源错误），本次不采用"]
    except BudgetExhausted:
        out["gaps"].append("板块趋势：取数预算不足，基准日线未取得（板块已取得部分保留）")
        bench_df = None
        bench_gaps = ["基准日线未取得：取数预算不足"]
    except Exception as exc:  # noqa: BLE001
        out["gaps"].append(
            f"板块趋势：基准（沪深300）日线查询失败: {str(exc)[:60]}（只降基准块）"
        )
        bench_df = None
        bench_gaps = [f"基准日线查询失败: {str(exc)[:60]}"]
    bench_obs: Dict[Any, Decimal] = {}
    bench_qualified = False
    if isinstance(bench_df, pd.DataFrame):
        bench_obs, bench_qualified, bench_gaps = _qualify_daily_closes(
            bench_df, "date", "close", expected, "基准日线（沪深300）"
        )
    bench_block: Dict[str, Any] = {
        "name": "沪深300", "symbol": "sh000300", "source": "东财指数日线",
        "start_date": out["start_date"], "end_date": out["end_date"],
        "qualified_sessions": len(bench_obs),
        "observations": _observation_rows(bench_obs),
        "period_return_pct": None,
        "gaps": bench_gaps,
    }
    if bench_qualified:
        bench_closes = [bench_obs[d] for d in expected]
        bench_block["period_return_pct"] = float(round(_period_return_pct(bench_closes), 4))
    else:
        bench_block["gaps"] = bench_block["gaps"] or ["基准日线未取得或不合格"]
    out["benchmark"] = bench_block
    if not bench_qualified:
        out["gaps"].extend(bench_gaps or ["板块趋势：基准日线未取得或不合格，相对表现不计算"])
    # 相对表现：两源对同一期望窗口均 20/20 合格才计算；单位=百分点（区间收益差）
    if board_qualified and bench_qualified:
        board_closes = [board_obs[d] for d in expected]
        bench_closes = [bench_obs[d] for d in expected]
        relative = _period_return_pct(board_closes) - _period_return_pct(bench_closes)
        out["relative_return_pp"] = float(round(relative, 4))
    # 缺口有界化（单项长度 + 总条数 ≤ schema 上限）：复合源失败不炸最终 strict 校验
    out["gaps"] = _bound_gaps(out["gaps"], 10)
    return out


def _decimal_amount(v: Any) -> Tuple[Optional[Decimal], str]:
    """成交额适配：原始值直接解析 Decimal（金融排序不经 float 中转）。

    返回 (Decimal 或 None, 原因)：``ok``=有限正；``missing``=缺失/空/NaN；
    ``non_positive``=0/负；``invalid``=无法解析/±inf。分类计数用于过滤前
    缺口（不因剩余足够而吞掉无效行事实）。
    """
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None, "missing"
    s = str(v).strip()
    if s in ("", "-", "nan", "NaN", "None"):
        return None, "missing"
    try:
        d = Decimal(s)
    except (ArithmeticError, ValueError):
        return None, "invalid"
    if not d.is_finite():
        return None, "invalid"
    if d <= 0:
        return None, "non_positive"
    return d, "ok"


def _adapt_peer_name(raw: Any, code: str) -> Tuple[str, bool]:
    """供应商名称边界适配：空白/缺失/nan → 用代码显示（成员资格保留）。

    返回 (显示名, 是否适配)。适配行计缺口（供应商字段问题按源错误处理，
    不让整篇有效报告 500，也不静默）。
    """
    s = str(raw).strip() if raw is not None else ""
    if not s or s in ("nan", "None"):
        return code, True
    return s[:64], False


_GAP_LEN_CAP = 200  # schema _GapList 单项上限：复合源失败下缺口逐条有界，不炸 strict 校验


def _bound_gaps(gaps: List[str], cap: int) -> List[str]:
    """缺口有界化：单项长度与总条数双重钳制（诊断事实保留，超限截断加省略号）。"""
    clamped = [
        g if len(g) <= _GAP_LEN_CAP else g[: _GAP_LEN_CAP - 1] + "…"
        for g in gaps
    ]
    if len(clamped) <= cap:
        return clamped
    omitted = len(clamped) - (cap - 1)
    return clamped[: cap - 1] + [f"（另有 {omitted} 条缺口略，均为同类来源限制）"]


_AMOUNT_PROVENANCE = (
    "成交额单位为元：已装 AkShare 成分接口与全市场快照接口同为东财 clist "
    "f6 字段且 fltt=2、原样映射「成交额」；官方行情文档标注全市场快照成交额"
    "为元，板块接口文档未标注单位，按同字段同接口参数推断"
)


@ensure(
    lambda result: (
        result.get("target_change_pct_rank") is None
        or (
            isinstance(result.get("sample_size"), int)
            and 1 <= result["target_change_pct_rank"] <= max(result["sample_size"], 1)
        )
    ),
    "target_change_pct_rank must be within the actual sample range",
)
def _peers_pillar(code: str, cons_snapshot: Optional[pd.DataFrame], *,
                  budget: ReportBudget,
                  snapshot_observed_at: Optional[str] = None,
                  subject_confirmed: bool = False) -> Dict[str, Any]:
    """同业比较（issue #82）：身份验证同一成分快照按成交额选「成交活跃代表」。

    规则（issue #78）：同一快照按成交额前五 + 目标股去重（目标在前五则 5 家，
    否则 6 家）；成交额原始值经 Decimal 判定后降序、并列按代码升序（稳定可
    解释）。**成员资格与成交额可排名资格分开**：无效成交额行（缺失/0/负/非
    有限）不参与排名但目标股成员仍保留（不误报「非成员」）；无效行计数在
    过滤**前**统计并暴露观测/可排名范围，剩余有效行足够也不吞限制；重复代码
    冲突整组剔除记缺口；样本不足 5 家如实标注实际范围。目标位置按快照当日
    涨跌幅降序（名次 = 严格更高数 + 1，并列同名次）；样本内存在涨跌幅缺失
    同行时**不发全样本名次**（withhold + 有效比较范围缺口），不把缺失同行
    默默排后。供应商空白/无效名称在边界适配为代码显示 + 缺口（其余有效块
    存活，不整篇 500）。不逐股补市值、不混用其他分类权重、不称基本面/市值
    龙头。资格：主行业 confirmed 才选取；资格满足而快照未取得时样本为空 +
    缺口（数据未取得，与资格不足分开表达）。
    """
    out: Dict[str, Any] = {
        "eligible": False,
        "selection_rule": "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）",
        "items": [],
        "target_in_amount_top5": None,
        "target_change_pct_rank": None,
        "amount_unit": "元",
        "amount_provenance": _AMOUNT_PROVENANCE,
        "snapshot_rows": 0,
        "rankable_rows": 0,
        "sample_size": 0,
        "snapshot_observed_at": snapshot_observed_at,
        "source": None,
        "gaps": [],
    }
    if not subject_confirmed:
        out["gaps"].append("同业比较：主行业身份未确认（confirmed 前置不满足），不执行同业选取")
        return out
    out["eligible"] = True
    if not isinstance(cons_snapshot, pd.DataFrame) or cons_snapshot.empty:
        out["gaps"].append(
            "同业比较：成分快照未取得或形状异常，样本为空（不伪排名）"
        )
        return out
    out["source"] = "东财行业板块成分（身份验证同一快照）"
    df = cons_snapshot
    out["snapshot_rows"] = int(len(df))
    if "代码" not in df.columns:
        out["gaps"].append("同业比较：成分快照缺代码列，无法选取样本")
        return out

    # 行资格：6 位数字代码；重复代码冲突整组剔除
    code_series = df["代码"].astype(str).str.strip()
    valid_mask = code_series.str.fullmatch(r"\d{6}").fillna(False)
    bad_codes = int((~valid_mask).sum())
    if bad_codes:
        out["gaps"].append(f"同业比较：成分快照中 {bad_codes} 行代码非法（非 6 位数字），已剔除")
    dup_mask = code_series.duplicated(keep=False) & valid_mask
    dup_codes = sorted(set(code_series[dup_mask].tolist()))
    if dup_codes:
        shown = ", ".join(dup_codes[:5])
        more = f" 等 {len(dup_codes)} 个" if len(dup_codes) > 5 else ""
        out["gaps"].append(
            f"同业比较：成分快照存在重复代码 {shown}{more}（冲突不静默取其一），"
            "相关行全部剔除"
        )
    name_raw = df["名称"] if "名称" in df.columns else None
    amount_raw = df["成交额"] if "成交额" in df.columns else None
    change_raw = df["涨跌幅"] if "涨跌幅" in df.columns else None

    # 逐行适配（成员资格=代码有效+无重复冲突；成交额/涨跌幅/名称独立判定）。
    # 成交额整列缺失与逐行金额缺失同语义：成员保留、金额 None、不产金额排名
    # （不把未知金额成员列成伪 top5，也不为缺列丢弃已确认目标股）。
    blank_names = 0
    amount_reasons = {"missing": 0, "non_positive": 0, "invalid": 0}
    members: List[Dict[str, Any]] = []
    member_index = valid_mask.to_numpy() & ~dup_mask.to_numpy()
    for i in range(len(df)):
        if not member_index[i]:
            continue
        member_code = str(code_series.iloc[i])
        name, adapted = _adapt_peer_name(
            name_raw.iloc[i] if name_raw is not None else None, member_code,
        )
        if adapted:
            blank_names += 1
        if amount_raw is not None:
            amount, reason = _decimal_amount(amount_raw.iloc[i])
            if reason != "ok":
                amount_reasons[reason] += 1
        else:
            amount, reason = None, "missing"
        change = _num(change_raw.iloc[i]) if change_raw is not None else None
        members.append({
            "code": member_code, "name": name, "amount": amount,
            "change_pct": change, "is_target": member_code == code,
        })
    if blank_names:
        out["gaps"].append(
            f"同业比较：{blank_names} 行供应商名称空白/缺失，按代码显示（成员保留，源数据限制）"
        )
    if amount_raw is None:
        out["gaps"].append(
            "同业比较：成分快照无成交额列，成交额均无数据（缺列不逐行伪计），"
            "无法按成交活跃选取代表，不输出成交额前五（成员与实际字段保留）"
        )
        out["rankable_rows"] = 0
    else:
        out["rankable_rows"] = sum(1 for m in members if m["amount"] is not None)
        invalid_amount_total = len(members) - out["rankable_rows"]
        if invalid_amount_total:
            parts = []
            if amount_reasons["missing"]:
                parts.append(f"缺失 {amount_reasons['missing']}")
            if amount_reasons["non_positive"]:
                parts.append(f"零或负 {amount_reasons['non_positive']}")
            if amount_reasons["invalid"]:
                parts.append(f"非数值 {amount_reasons['invalid']}")
            out["gaps"].append(
                f"同业比较：成分快照 {invalid_amount_total} 行成交额无效（{'、'.join(parts)}），"
                f"不参与成交额排名；观测 {len(members)} 行、成交额可排名 {out['rankable_rows']} 行"
            )

    # 成交额降序（Decimal 排序）、并列代码升序（稳定可解释）；无效额不进排名
    rankable = sorted(
        (m for m in members if m["amount"] is not None),
        key=lambda m: (-m["amount"], m["code"]),
    )
    ranked_codes = [m["code"] for m in rankable]
    member_by_code = {m["code"]: m for m in members}

    target_member = member_by_code.get(code)
    if target_member is None:
        out["gaps"].append(
            f"同业比较：目标股 {code} 不在有效成分行中（与成分验证资格不符或数据异常），"
            "目标未加入样本、不输出相对位置"
        )
        out["target_in_amount_top5"] = None
    elif target_member["amount"] is None:
        out["gaps"].append(
            f"同业比较：目标股 {code} 快照成交额缺失或无效，不参与成交额排名"
            "（成员保留，top5 资格无法判定）"
        )
        out["target_in_amount_top5"] = None  # 无法判定，不误报 False
    else:
        out["target_in_amount_top5"] = code in ranked_codes[:5]

    picked = list(ranked_codes[:5])
    if target_member is not None and code not in picked:
        picked.append(code)

    items: List[Dict[str, Any]] = []
    for c in picked:
        m = member_by_code[c]
        items.append({
            "code": c,
            "name": m["name"],
            "amount": float(m["amount"]) if m["amount"] is not None else None,
            "change_pct": m["change_pct"],
            "is_target": c == code,
        })
    out["items"] = items
    out["sample_size"] = len(items)
    if out["rankable_rows"] == 0 and items:
        out["gaps"].append(
            f"同业比较：无成交额可排名同行，样本仅保留目标股 {code}"
            "（无同业可比代表，相对位置不输出，不产出 1/1 伪名次）"
        )
    elif len(items) < 5:
        out["gaps"].append(
            f"同业比较：成交额可排名样本仅 {len(items)} 家（不足 5 家），"
            "按实际范围列示，不伪完整排名"
        )

    # 目标位置：样本内按当日涨跌幅降序，名次 = 严格更高的个数 + 1（并列同名次）；
    # 样本内存在涨跌幅缺失同行时不发全样本名次（withhold + 覆盖缺口），
    # 不把缺失同行默默排到目标之后；样本仅目标股自身（无可比同行）同样不发名次。
    if target_member is not None:
        target_change = target_member["change_pct"]
        if target_change is None:
            out["gaps"].append(
                f"同业比较：目标股 {code} 快照当日涨跌幅缺失，相对位置不输出（不按其他指标顶替）"
            )
        elif len(picked) <= 1:
            out["gaps"].append(
                "同业比较：样本内无可比同行（仅目标股自身），相对位置不输出"
                "（单成员样本不构成比较）"
            )
        else:
            valid_changes = [
                m["change_pct"] for m in (member_by_code[c] for c in picked)
                if m["change_pct"] is not None
            ]
            missing_changes = len(picked) - len(valid_changes)
            if missing_changes:
                out["gaps"].append(
                    f"同业比较：样本内 {missing_changes} 家当日涨跌幅缺失，"
                    f"有效比较范围 {len(valid_changes)}/{len(picked)} 家，"
                    "不输出全样本名次（不伪完整排名）"
                )
            else:
                out["target_change_pct_rank"] = (
                    1 + sum(1 for ch in valid_changes if ch > target_change)
                )
    out["gaps"] = _bound_gaps(out["gaps"], 14)
    return out


# ---------------------------------------------------------------------------
# P4c 行业催化（issue #83）：统一准入 + 有界文章日期证据（必需研究，
# 先于申万/概念可选长链出站）。判定逻辑在 sector_research（纯函数）；
# 本段只做编排：供应商 fallback、预算钳制、文章探查与升级重入。
# ---------------------------------------------------------------------------

def _probe_no_evidence(url: str) -> "ArticleDateEvidence":
    """探查失败路径的统一无证据对象（URL 声明日期仅作冲突判定记录）。"""
    from src.services import sector_research

    return sector_research.ArticleDateEvidence(
        None, sector_research.probe_url_declared_date(url), None,
    )


def _probe_article_date_evidence(url: str, budget: ReportBudget
                                 ) -> Tuple["ArticleDateEvidence", Optional[str],
                                            Optional[str]]:
    """有界文章日期证据探查（显式错误分类；同一线程同一取数截止）。

    同一有界 I/O 契约（与来源准入共用 ``validate_public_http_url``）：
    - 出站前 URL 公共 HTTP(S) 结构校验（注册时已验，重入防御）
    - requests ``stream=True`` 逐块读取，per-response 字节上限
      ``CATALYST_ARTICLE_MAX_BYTES`` **读取期间强制**，超限立即停止并关闭
      响应（已装 newspaper 0.2.8 ``Article.download`` 是非 stream 全量读，
      探查只需要出版元数据，不走它）；解析只在有界内容上复用已装
      ``Parser.fromstring`` + ``get_publishing_date``（不全文提取）
    - redirects 手动逐跳（``allow_redirects=False`` + Location 前置校验），
      每一跳在**连接前**过同一边界契约，跳数有界；自动跟随会让 requests
      先连出目标再检查，逃逸已经发生
    - ``request_deadline`` 线程内 bind/finally reset + 出站前显式幂等安装
      ``eastmoney_patch(budget_only=True)``（fresh process 中 ContextVar
      只有装了 patch 才真正钳制 requests 出站；不启用 auth/共享默认不变）

    返回 ``(evidence, failure_reason, final_url)``：
    - ``failure_reason`` None → evidence 有效（可能「无日期证据」）；
      ``final_url`` 是实际取得响应的**终态 URL**（重定向后的实际文章地址；
      日期证据冲突判定与升级登记的溯源域名一律以它为准，原 URL 保留引用
      身份由 caller 维护）。无重定向时 final_url == 原 URL。
    - 显式预期失败 → ``(无证据对象, 类别, 终态URL|None)``：``transport``
      （requests RequestException）、``probe_timeout``、``article_oversize``、
      ``article_parse_failed``（newspaper fromstring 显式 None 契约）、
      ``redirect_escaped:*``、``source_invalid:*``（出站前结构校验失败）。
      **已到达终态的失败**（终态非 2xx、2xx 后读取超限/超时/解析失败）
      仍返回该终态 URL——安全失败背景同样保留实际文章地址与 provenance，
      不丢弃；终态未到达（出站前拒绝/redirect 边界/连接失败）为 None。
    - ``BudgetExhausted`` 原样上抛（caller 局部终止探查，用收尾余量出
      partial 报告）；TypeError/AssertionError 等程序/契约错误**原样传播**
      （诚实失败，不伪装成「无日期」数据缺口）
    诊断文本只含静态类别名（``type(exc).__name__`` 经 sanitize），不带
    原始 URL/响应内容。
    """
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline
    from src.services import sector_research
    from src.services.run_diagnostics import sanitize_diagnostic_text

    ok, reason = sector_research.validate_public_http_url(url)
    if not ok:
        return _probe_no_evidence(url), f"source_invalid:{reason}", None

    budget.require_fetch()
    # 幂等安装既有 requests 预算边界（ContextVar 只有 patch 在位才真正
    # 钳制 requests 出站；budget_only 不启用 auth，不改共享默认行为）
    eastmoney_patch(budget_only=True)
    token = request_deadline.set(budget.fetch_deadline)
    current_url = url
    reached_terminal: Optional[str] = None  # 实际到达的终态（provenance）
    hops = 0
    try:
        # redirects 手动逐跳（allow_redirects=False）：**先校验后连接**——
        # 每一跳的 Location 在出站前过同一公共 HTTP(S) 结构契约，重定向
        # 不得实际连出边界（自动跟随会让 requests 先连到目标再检查，
        # 逃逸已经发生）；每跳同一次取数截止，跳数有界防循环
        response: Optional[requests.Response] = None
        while True:
            budget.require_fetch()
            response = requests.get(
                current_url,
                stream=True,
                timeout=max(min(5.0, budget.fetch_remaining()), 0.1),
                allow_redirects=False,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36"
                    ),
                },
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = str(response.headers.get("Location", "") or "")
                response.close()
                hops += 1
                if hops > 5:
                    return _probe_no_evidence(url), (
                        "redirect_escaped:too_many_hops"), None
                if not location:
                    return _probe_no_evidence(url), (
                        "redirect_escaped:missing_location"), None
                from urllib.parse import urljoin

                current_url = urljoin(response.url, location)
                hop_ok, hop_reason = sector_research.validate_public_http_url(
                    current_url,
                )
                if not hop_ok:
                    return _probe_no_evidence(url), (
                        f"redirect_escaped:{hop_reason}"), None
                continue
            # 重定向终态已到达：读/解析前校验**最终响应成功性**——非 2xx
            # （403/404/500/304 等）是本地文章来源失败，错误页携带的出版
            # meta 不得作日期证据（零日期升级）；安全类别只含状态码，不读
            # body/header，零查询串/内容泄漏。来源失败 ≠ 真「无新闻」，
            # 调用方按背景降级保留受限条目与已取得事实。终态地址已实际
            # 到达，随失败返回（背景保留实际文章 provenance，不丢弃）。
            if not (200 <= response.status_code < 300):
                status_code = response.status_code
                response.close()
                return _probe_no_evidence(url), (
                    f"article_http_error:{status_code}"), current_url
            reached_terminal = current_url
            break
        chunks: List[bytes] = []
        total = 0
        with response:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if budget.fetch_expired():
                    return _probe_no_evidence(url), "probe_timeout", reached_terminal
                if chunk:
                    total += len(chunk)
                    if total > sector_research.CATALYST_ARTICLE_MAX_BYTES:
                        return _probe_no_evidence(url), "article_oversize", (
                            reached_terminal)
                    chunks.append(chunk)
        if not total:
            html = ""
        else:
            # stream 已消费，response.text 不可再访问；在**有界字节**上用
            # 响应头 charset 解码（无声明时保守 utf-8 + replace——乱码页面
            # 提取不到元数据日期即保守无日期，不误报）
            charset = (response.encoding or "utf-8").strip() or "utf-8"
            try:
                html = b"".join(chunks).decode(charset, errors="replace")
            except (LookupError, TypeError):
                html = b"".join(chunks).decode("utf-8", errors="replace")
        # 日期证据以**实际终态 URL** 为准：URL 声明日期（冲突判定）与实际
        # 来源域名都取重定向后的真实文章地址——重定向不改变 provenance 事实
        # （original.html→302→2020 终态→200 metadata 2026 = URL/meta 冲突，
        # 不得借原始无日期 URL 绕过冲突合同）；原 URL 引用身份由 caller 保留
        evidence = sector_research.extract_article_date_evidence(
            html, current_url,
        )
        return evidence, None, current_url
    except BudgetExhausted:
        raise
    except sector_research.ArticleParseFailure as exc:
        # newspaper fromstring 显式 None 契约：预期解析数据错误，降级背景；
        # 解析发生在终态内容上——终态地址已知，随失败返回（不丢 provenance）
        safe = sanitize_diagnostic_text(str(exc), max_length=120)
        logger.info("article date probe parse failure: %s", safe)
        return _probe_no_evidence(url), "article_parse_failed", reached_terminal
    except requests.RequestException as exc:  # noqa: PERF203 - 显式边界
        # 已识别传输错误（连接/超时等）：预期网络失败类别，降级背景。
        # 终态已到达后的读取传输失败同样保留终态地址；连接/跳转阶段失败
        # 终态未到达（reached_terminal=None），不猜终态
        safe = sanitize_diagnostic_text(type(exc).__name__, max_length=40)
        logger.info("article date probe transport failure: %s", safe)
        return _probe_no_evidence(url), "transport", reached_terminal
    finally:
        request_deadline.reset(token)


@ensure(
    lambda result: (
        isinstance(result, dict) and "eligible" in result and "items" in result
        and "gaps" in result
    ),
    "catalysts pillar must return dict with eligible/items/gaps keys",
)
@ensure(
    lambda result: all(
        c.get("published_date") for c in result.get("items", [])
    ),
    "confirmed catalyst items must carry a qualified published_date",
)
def _catalysts_pillar(board_name: str, *, budget: ReportBudget,
                      subject_confirmed: bool = False,
                      stock_name: str = "") -> Dict[str, Any]:
    """行业催化（issue #83）：通用搜索边界 + **统一准入路径**。

    资格：主行业 confirmed 才发搜索出站（unverified 归属未确认同样不查）；
    否则 eligible=False 仅记录未执行原因。查询不走 ``search_stock_news``
    （个股新闻语义）；直接用既有 provider 链的通用 ``search`` 边界。

    统一准入（sector_research；provider-dated 与 article-dated 只在「日期
    获得」一步不同，事件资格/去重登记完全共享，背景不存在绕过事件资格的
    升级捷径）：
    1. source 验证（公共 HTTP(S) URL 结构契约：scheme/host/userinfo/端口/
       私有 literal IP/类型与长度；无效供应商字段本地拒绝计数，不 str(...)
       伪合法、不截断伪造；溯源域名一律取实际 URL host——provider source
       标签不是域名，类型非法时忽略不 500）
    2. 行业相关性（行业名/基词命中标题或摘要；个股活动词合同剔除）
    3. 日期获得：供应商发布字段（共享归一化语义）→ 合格日期或无日期候选
       → 无日期候选有界文章探查（requests 有界 stream 读取 + 字节上限 +
       每跳 redirect 边界 + 真实 newspaper 元数据提取、禁 URL 启发式、
       raw meta 依据；显式错误分类降背景，程序错误传播；
       ``BudgetExhausted`` 局部终止探查，已取得内容保留——文章探查
       超时是部分报告，不是整报 504）
    4. **同一**事件资格（窗口内不含未来日；标题声明主题时点早于窗口起始
       的历史回顾降级背景，无标题豁免词表，正文引用旧年份不参与）+
       **同一**去重登记（URL 归一化 / 同域同题 / 跨域同题+明确转载标记
       合并同一事件，升级重入完整资格——跨站新华社通稿是一个事件，不产生
       第二个 ID）
    发布日期与事件日期始终分离；事件日期未知不发明。

    预算纪律：搜索等待、每次出站、线程内首出站、文章探查全部钳制同一取数
    截止；出站前显式幂等安装 ``eastmoney_patch(budget_only=True)``（绝对
    requests 预算真正装到 wire，不只在 ContextVar）；worker 只写自己的局部
    缺口，等待成功后由 caller 合并——迟到 worker 不触碰已返回结果。供应商
    失败响应 ≠「无新闻」（sanitized 诊断保留供应商名与错误类别）；已识别
    传输错误降本块，程序错误原样可见。
    """
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline
    from src.search_service import SearchService
    from src.services import sector_research
    from src.services.run_diagnostics import sanitize_diagnostic_text

    # 幂等安装既有 requests 预算边界：本 pillar 与其线程 worker 的全部
    # requests 出站（供应商搜索、文章探查）在 fetch_deadline 后不得出 wire
    # （budget_only 不启用 auth，不改共享默认/全局 provider 行为）
    eastmoney_patch(budget_only=True)

    out: Dict[str, Any] = {
        "eligible": False,
        "query": None,
        "window_days": None, "window_start": None, "window_end": None,
        "observed_at": None, "provider": None,
        "items": [], "background_items": [],
        "dropped_duplicate_url": 0, "dropped_syndicated": 0,
        "dropped_offwindow": 0, "dropped_offtopic": 0,
        "dropped_invalid_source": 0, "dropped_invalid_field": 0,
        "source": None,
        "gaps": [],
    }
    observed_at = datetime.now().isoformat(timespec="seconds")
    out["observed_at"] = observed_at
    base_term = sector_research.catalyst_query_term(board_name)
    if not subject_confirmed or not base_term:
        out["gaps"].append(
            "行业催化：主行业身份未确认（confirmed 前置不满足），不执行行业新闻搜索"
        )
        return out
    out["eligible"] = True

    from src.search_service import get_search_service

    svc = get_search_service()
    providers = [p for p in svc._providers if p.is_available]
    if not providers:
        out["gaps"].append("行业催化：搜索入口未配置（套餐搜索不可用），本次未搜索")
        return out

    search_days = int(svc.news_window_days)
    query = f"{base_term} 行业 政策 新闻"
    out["query"] = query
    out["window_days"] = search_days
    today = datetime.now().date()
    earliest = today - timedelta(days=max(0, search_days - 1))
    # 板块窗口不含未来日（共享全局过滤的 +1 未来容忍不照搬到 board 口径）
    out["window_start"] = earliest.isoformat()
    out["window_end"] = today.isoformat()
    out["source"] = "套餐搜索（通用行业查询）"

    def _thread_search() -> Tuple[str, Any, List[str], int, int]:
        """provider fallback 搜索：返回 (provider 名, 响应或 None, 局部缺口,
        失败供应商数, 供应商侧畸形行累计)。

        线程内首出站/每次出站前检查同一取数截止；deadline bind + finally
        reset（对齐 ResearchRun.bind 语义）。失败 SearchResponse ≠ 无新闻：
        保留 sanitized 供应商名与错误类别。供应商 raw 行类型畸形计数
        （``SearchResponse.malformed_rows``，结构化字段）对**检查过的每个
        响应**累计——包括失败尝试与 fallback（那些行是真实被拒的供应商
        数据），不解析 error_message 文本，也不与本地准入拒绝双重计数
        （供应商侧拒绝的行根本不会进入本地准入）。
        """
        budget.require_fetch()
        token = request_deadline.set(budget.fetch_deadline)
        worker_gaps: List[str] = []
        failed_providers = 0
        malformed_total = 0
        last: Any = None
        try:
            for provider in providers:
                budget.require_fetch()
                provider_name = str(getattr(provider, "name", "?"))
                try:
                    resp = provider.search(
                        query, sector_research.CATALYST_SEARCH_LIMIT,
                        days=search_days,
                    )
                except BudgetExhausted:
                    raise
                except requests.RequestException as exc:
                    # 已识别传输错误：记录供应商失败，尝试下一 provider
                    safe = sanitize_diagnostic_text(
                        f"{provider_name} {type(exc).__name__}: {exc}", max_length=120,
                    ) or f"{provider_name} 传输失败"
                    worker_gaps.append(f"行业催化：搜索供应商 {safe} 传输失败")
                    failed_providers += 1
                    last = None
                    continue
                malformed_total += int(getattr(resp, "malformed_rows", 0) or 0)
                if getattr(resp, "success", False) and getattr(resp, "results", None):
                    return provider_name, resp, worker_gaps, failed_providers, (
                        malformed_total)
                if resp is not None and not getattr(resp, "success", False):
                    # 显式失败响应 ≠「无新闻」：sanitized 错误类别保留
                    safe = sanitize_diagnostic_text(
                        str(getattr(resp, "error_message", "") or "未知原因"),
                        max_length=120,
                    ) or "未知原因"
                    worker_gaps.append(
                        f"行业催化：搜索供应商 {provider_name} 返回失败（{safe}）"
                    )
                    failed_providers += 1
                if last is None or getattr(resp, "success", False):
                    last = resp
            return "", last, worker_gaps, failed_providers, malformed_total
        finally:
            request_deadline.reset(token)

    try:
        provider_name, resp, worker_gaps, failed_providers, malformed_total = (
            bounded_call(_thread_search, budget=budget,)
        )
    except BudgetExhausted:
        out["gaps"].append("行业催化：取数预算不足，行业新闻搜索未完成（已取得内容保留）")
        return out
    # 供应商 raw 畸形行计数并入 invalid_field 总数（诚实总数 = 供应商侧
    # 拒绝 + 本地准入拒绝；两者作用对象不相交，无双重计数）
    if malformed_total:
        out["dropped_invalid_field"] += malformed_total
    # 迟到 worker 只写自己的 worker_gaps；本 out 已被 caller 持有
    out["gaps"].extend(worker_gaps)
    if not provider_name or resp is None:
        if failed_providers:
            out["gaps"].append(
                "行业催化：搜索供应商均返回失败（见上方分类），不冒充「窗口内无新闻」"
            )
        elif not out["gaps"]:
            out["gaps"].append(
                "行业催化：搜索未返回结果（窗口内无可用新闻或供应商覆盖限制）"
            )
        out["gaps"] = _bound_gaps(out["gaps"], 14)
        return out
    out["provider"] = str(provider_name)

    board_name_s = str(board_name)
    stock_name_s = str(stock_name or "").strip()

    # ---- 统一登记闭包：事件资格 + 去重合并（两个日期路径共用同一步） ----
    seen_urls: set = set()
    seen_syndication: set = set()
    seen_headline: Dict[str, str] = {}
    items: List[Dict[str, Any]] = []
    background: List[Dict[str, Any]] = []
    seq = [0]
    bg_seq = [0]
    undated_count = [0]
    _title_to_snippet: Dict[str, str] = {}

    def url_key_of(url: str) -> str:
        return sector_research.normalize_catalyst_url(url)

    def _dedup_register(url_key: str, domain: str, title_key: str) -> str:
        """URL/同域同题去重登记。返回 ok/duplicate_url/syndicated。"""
        if not url_key or url_key in seen_urls:
            return "duplicate_url"
        synd_key = (domain.lower(), title_key)
        if synd_key in seen_syndication:
            return "syndicated"
        seen_urls.add(url_key)
        seen_syndication.add(synd_key)
        return "ok"

    def _merge_same_event(title_key: str, url: str, snippet: str) -> bool:
        """跨域同题 + 明确转载标记 → 同一事件（补充链接，不产生第二个 ID）。"""
        prior_id = seen_headline.get(title_key)
        if prior_id is None:
            return False
        first_snippet = _title_to_snippet.get(title_key, "")
        if not (sector_research.SYNDICATION_SOURCE_RE.search(snippet)
                or sector_research.SYNDICATION_SOURCE_RE.search(first_snippet)
                or sector_research.SYNDICATION_SOURCE_RE.search(title_key)):
            return False
        for entry in items:
            if entry["evidence_id"] == prior_id and len(
                entry["same_event_urls"]
            ) < sector_research.SAME_EVENT_URLS_MAX:
                entry["same_event_urls"].append(url[:sector_research.CATALYST_URL_MAX])
                break
        out["dropped_syndicated"] += 1
        return True

    def _clear_candidate_registration(url_key: str, domain: str,
                                      title_key: str) -> None:
        """摘除**本候选**在背景登记时的去重键（升级重入同一资格，不算重复）。"""
        seen_urls.discard(url_key)
        seen_syndication.discard((domain.lower(), title_key))

    def _qualify_and_register(published: date, date_basis: str,
                              date_basis_detail: Optional[str],
                              title: str, url: str, domain: str,
                              snippet: Optional[str],
                              final_url: Optional[str] = None) -> str:
        """统一事件资格：窗口 → 历史回顾 → 去重登记 → 已证实条目。

        返回 "ok" / "offwindow" / "retrospective" / "duplicate_url" /
        "syndicated"；provider-dated 与 article-dated 路径共用（升级重入
        完整资格）。历史回顾降级由 caller 落背景（带发布日期与降级原因）。

        ``final_url``（article_metadata 口径传入）：实际取得响应的终态
        URL——去重键与同域判定以**实际文章**为准（两个 redirect alias 是
        同一篇文章，不是独立确认），条目 ``url`` 保留原引用身份，
        ``final_url`` 如实区分。
        """
        title_key = re.sub(r"\s+", "", title)
        evidence_url = final_url or url
        # 1) 窗口资格（发布日期只作资格依据；事件日期未知不发明）
        reject = sector_research.qualifies_event(published, earliest, today)
        if reject is not None:
            out["dropped_offwindow"] += 1
            return "offwindow"
        # 2) 历史回顾判定（标题主题时点口径；两个日期路径一致）——只判资格
        #    与去重登记，背景落库由 caller 负责（provider 循环 / probe 升级
        #    各自携带自己的降级原因，同一候选不会双份落背景）
        if sector_research.is_retrospective_title(title, earliest):
            dup_kind = _dedup_register(url_key_of(evidence_url), domain,
                                       title_key)
            if dup_kind != "ok":
                out[f"dropped_{dup_kind}"] += 1
                return dup_kind
            return "retrospective"
        # 3) 去重登记（URL/同域同题；键取实际文章 URL）
        dup_kind = _dedup_register(url_key_of(evidence_url), domain, title_key)
        if dup_kind != "ok":
            out[f"dropped_{dup_kind}"] += 1
            return dup_kind
        # 4) 跨域同源转载合并（同题 + 明确转载标记 = 同一事件）
        if title_key in seen_headline:
            if _merge_same_event(title_key, url, snippet or ""):
                return "syndicated"
            # 跨域同题但无转载标记：保守不合并，按独立条目继续（display cap 内）
        if len(items) < sector_research.CATALYST_DISPLAY_CAP:
            seq[0] += 1
            entry: Dict[str, Any] = {
                "evidence_id": f"cat-{seq[0]}",
                "title": title[:sector_research.CATALYST_TITLE_MAX],
                "url": url[:sector_research.CATALYST_URL_MAX],
                "source_domain": domain,
                "snippet": (snippet or "")[:sector_research.CATALYST_SNIPPET_MAX] or None,
                "published_date": published.isoformat(),
                "date_basis": date_basis,
                "date_basis_detail": date_basis_detail,
                "same_event_urls": [],
                "final_url": final_url,
            }
            items.append(entry)
            seen_headline[title_key] = entry["evidence_id"]
            _title_to_snippet[title_key] = snippet or ""
            return "ok"
        return "ok"

    for raw_item in resp.results:
        # ---- 候选文本类型契约（单一准入入口；两个日期分支共享）----
        # 供应商字段先查**实际类型**再做语义处理：title/url 是必需的非空
        # str，snippet 可选（None=缺失，合法）但出现即必须是 str。畸形行
        # （dict/bytes/数字等集合/非文本类型）本地拒绝计数，不 str(...) 把
        # 结构/字节/数字伪合法成"标题/摘要"（真实 MiniMax JSON title=dict
        # 曾被字符串化为假标题登记 cat-1）；剩余有效行/事实全部保留，缺失
        # 可选文本只是缺字段，不是错误、不 500。
        raw_title = getattr(raw_item, "title", None)
        raw_url = getattr(raw_item, "url", None)
        raw_snippet = getattr(raw_item, "snippet", None)
        if not isinstance(raw_title, str) or not raw_title.strip():
            out["dropped_invalid_field"] += 1
            continue
        if not isinstance(raw_url, str) or not raw_url.strip():
            out["dropped_invalid_field"] += 1
            continue
        if raw_snippet is not None and not isinstance(raw_snippet, str):
            out["dropped_invalid_field"] += 1
            continue
        title = raw_title.strip()
        url = raw_url.strip()
        snippet = raw_snippet.strip() if isinstance(raw_snippet, str) else None
        # 1) source 验证先于一切分支（已证实与背景同一准入）：公共 HTTP(S)
        #    结构契约；无效字段类型/超长本地拒绝计数，不字符串化/截断伪合法
        url_ok, url_reason = sector_research.validate_public_http_url(url)
        if not url_ok:
            out["dropped_invalid_source"] += 1
            logger.info(
                "catalyst source rejected (%s): %s",
                url_reason, sanitize_diagnostic_text(url, max_length=120),
            )
            continue
        url_key = url_key_of(url)
        if not url_key:
            out["dropped_invalid_source"] += 1
            continue
        # 2) 行业相关性（同一准入；个股活动合同剔除）
        text = f"{title} {snippet or ''}"
        if board_name_s not in text and base_term not in text:
            out["dropped_offtopic"] += 1
            continue
        if stock_name_s and stock_name_s in title and sector_research.STOCK_ONLY_EVENT_RE.search(title):
            out["dropped_offtopic"] += 1
            continue
        # 溯源域名 = 实际 URL 的**完整**解析 host（结构校验已保证非空且
        # ≤253，不截断——截断段不是来源身份）；provider source 标签不是
        # 域名替身，类型/内容非法时忽略即可，不影响准入、不 500
        host = sector_research.urlsplit_host(url)
        domain = host
        # 3) 日期获得：供应商发布字段（共享归一化语义）
        published = SearchService._normalize_news_publish_date(
            getattr(raw_item, "published_date", None)
        )
        if published is not None:
            # provider-dated：直接进入统一事件资格（与 article-dated 同一步）
            verdict = _qualify_and_register(
                published, "provider_published", None,
                title, url, domain, snippet,
            )
            if verdict == "retrospective":
                # 历史回顾：统一资格判定通过（URL 已登记），落明确限制背景
                if len(background) < sector_research.CATALYST_BACKGROUND_CAP:
                    bg_seq[0] += 1
                    background.append({
                        "evidence_id": f"bg-{bg_seq[0]}",
                        "title": title[:sector_research.CATALYST_TITLE_MAX],
                        "url": url[:sector_research.CATALYST_URL_MAX],
                        "source_domain": domain,
                        "snippet": (snippet or "")[:sector_research.CATALYST_SNIPPET_MAX] or None,
                        "published_date": published.isoformat(),
                        "downgrade_reason": (
                            f"标题声明主题时点早于窗口起始（{earliest.isoformat()}），"
                            "历史回顾属性，仅背景（发布日期非事件日期）"
                        ),
                        "final_url": None,
                    })
            continue
        # 无日期候选：背景登记（同一准入序列；不做事件资格——尚无日期）
        title_key = re.sub(r"\s+", "", title)
        dup_kind = _dedup_register(url_key, domain, title_key)
        if dup_kind != "ok":
            out[f"dropped_{dup_kind}"] += 1
            continue
        undated_count[0] += 1
        if len(background) < sector_research.CATALYST_BACKGROUND_CAP:
            bg_seq[0] += 1
            background.append({
                "evidence_id": f"bg-{bg_seq[0]}",
                "title": title[:sector_research.CATALYST_TITLE_MAX],
                "url": url[:sector_research.CATALYST_URL_MAX],
                "source_domain": domain,
                "snippet": (snippet or "")[:sector_research.CATALYST_SNIPPET_MAX] or None,
                "published_date": None,
                "downgrade_reason": None,
                "final_url": None,
            })

    # 4) 无日期背景条目：有界文章日期探查（显式错误分类；升级走同一资格）
    probe_used = 0
    upgraded = 0
    kept_background: List[Dict[str, Any]] = []
    for bg_item in background:
        needs_probe = not bg_item.get("published_date")
        if needs_probe and (
            probe_used >= sector_research.CATALYST_ARTICLE_PROBE_CAP
            or len(items) >= sector_research.CATALYST_DISPLAY_CAP
            or budget.fetch_expired()
        ):
            kept_background.append(bg_item)
            continue
        if needs_probe:
            probe_used += 1
            try:
                evidence, probe_failure, final_url = bounded_call(
                    _probe_article_date_evidence, bg_item["url"],
                    budget, budget=budget,
                )
            except BudgetExhausted:
                # 文章探查耗尽取数预算：结束探查（部分报告），已取得内容保留，
                # 收尾用主截止内的既有收尾余量——不传播整报超时
                out["gaps"].append(
                    "行业催化：文章日期探查因取数预算不足提前结束"
                    "（已取得催化与背景内容保留）"
                )
                kept_background.append(bg_item)
                kept_background.extend(background[background.index(bg_item) + 1:])
                break
            # 终态已知（探查成功，或已到达终态的安全失败：非 2xx/读取超限/
            # 超时/解析失败）→ 资格判定**前**统一把本候选重键到**实际文章**
            # 身份：先按原登记键摘除（不先改域名再摘，避免摘错键），再按
            # 终态键重注册——去重身份与展示 provenance 一致；背景保留原引用
            # url + 终态 final_url + 实际完整 hostname（冲突/回顾/重复别名/
            # 安全失败背景同样不丢实际文章溯源）。同终态已被其他条目登记
            # （同文 alias 先行升级/先到背景）→ 按重复计数并保留背景+终态。
            final_host = sector_research.urlsplit_host(final_url or "")
            bg_title_key = re.sub(r"\s+", "", bg_item["title"])
            if final_url and final_host:
                _clear_candidate_registration(
                    url_key_of(bg_item["url"]), bg_item["source_domain"],
                    bg_title_key,
                )
                bg_item["final_url"] = final_url
                bg_item["source_domain"] = final_host
                rekey = _dedup_register(url_key_of(final_url), final_host,
                                        bg_title_key)
                if rekey != "ok":
                    out[f"dropped_{rekey}"] += 1
                    bg_item["downgrade_reason"] = {
                        "duplicate_url": (
                            "实际文章（终态地址）与已取得条目重复，仅保留先到条目"
                        ),
                        "syndicated": (
                            "与已取得条目为同一事件（同源转载），不重复计入"
                        ),
                    }.get(rekey, "实际文章与已取得条目重复，仅背景")
                    kept_background.append(bg_item)
                    continue
            if probe_failure is not None:
                # 显式预期失败（传输/超限/解析数据/redirect 边界）→ 背景保留
                # + 安全原因；程序/契约错误已原样传播（bounded_call 不吞）。
                # 终态已到达的失败上方已重键并携带终态 provenance。
                bg_item["downgrade_reason"] = {
                    "transport": "文章日期探查传输失败（已识别网络错误类别），仅背景",
                    "probe_timeout": "文章日期探查超过取数截止，仅背景",
                    "article_oversize": (
                        f"文章响应超过有界读取上限"
                        f"（{sector_research.CATALYST_ARTICLE_MAX_BYTES} 字节），仅背景"
                    ),
                    "article_parse_failed": (
                        "文章内容无法被解析器识别（解析数据错误），仅背景"
                    ),
                }.get(
                    probe_failure.split(":", 1)[0],
                    f"文章探查未确证（{probe_failure.split(':', 1)[0]}），仅背景",
                )[:200]
                kept_background.append(bg_item)
                continue
            qualified, reason = sector_research.qualifies_article_declared_date(
                evidence, earliest, today,
            )
            if qualified is not None:
                # 升级走**同一**事件资格（窗口/回顾/去重登记不绕过）。本候选
                # 已重键到终态身份：摘除自身终态键（重入非重复）后按终态
                # 注册——溯源域名与去重键取**实际文章**（终态 host 完整保留，
                # redirect alias 不算独立确认）；原 URL 保留引用身份
                # （条目 url 字段），final_url 如实区分。
                _clear_candidate_registration(
                    url_key_of(bg_item.get("final_url") or bg_item["url"]),
                    bg_item["source_domain"], bg_title_key,
                )
                verdict = _qualify_and_register(
                    qualified, "article_metadata",
                    (evidence.raw_basis or "")[:sector_research.CATALYST_SNIPPET_MAX],
                    bg_item["title"], bg_item["url"],
                    bg_item["source_domain"], bg_item.get("snippet"),
                    final_url=final_url,
                )
                if verdict == "ok":
                    upgraded += 1
                    continue
                # 升级被资格/去重拒绝：按原背景保留 + 具体说明（同一候选不双份）
                bg_item["downgrade_reason"] = {
                    "retrospective": (
                        f"文章元数据日期确证后经统一资格判定为历史回顾"
                        f"（标题声明主题时点早于 {earliest.isoformat()}），仅背景"
                    ),
                    "offwindow": "文章元数据日期在窗口外，仅背景",
                    "duplicate_url": (
                        "实际文章（终态地址）与已取得条目重复，仅保留先到条目"
                    ),
                    "syndicated": "与已取得条目为同一事件（同源转载），不重复计入",
                }.get(verdict, "文章元数据日期未通过统一资格判定，仅背景")
                kept_background.append(bg_item)
                continue
            # 未能确证：如实降级说明（URL-only/无依据/冲突/窗口外）；
            # 冲突/窗口外等背景的终态 provenance 已在上方重键时保留
            reason_text = {
                "url_only": "URL 含日期但无文章元数据依据（URL 日期不单独作证据）",
                "no_date_evidence": "文章无可用发布日期证据",
                "metadata_date_without_raw_basis": "文章元数据日期无法定位原始依据（保守拒绝）",
                "url_meta_date_conflict": "URL 日期与文章元数据日期冲突（provenance 冲突）",
                "article_date_offwindow": "文章元数据日期在窗口外",
            }.get(reason, f"文章日期证据不足（{reason}）")
            bg_item["downgrade_reason"] = reason_text[:200]
        kept_background.append(bg_item)
    background = kept_background

    out["items"] = items
    out["background_items"] = background
    if not items:
        if failed_providers:
            out["gaps"].append(
                "行业催化：搜索供应商失败，已证实近期催化未取得（不冒充无新闻）"
            )
        elif undated_count[0] and not out["dropped_offwindow"]:
            out["gaps"].append(
                f"行业催化：搜索返回 {undated_count[0]} 条相关结果均无可用发布日期，"
                "无法确证近期催化（URL 日期不单独作证据；文章探查未能确证）；"
                "无日期条目仅列示为背景线索，不作为已证实近期催化"
            )
        elif undated_count[0]:
            out["gaps"].append(
                "行业催化：窗口外或无日期结果均不构成已证实近期催化（见上方计数）"
            )
        else:
            out["gaps"].append(
                "行业催化：去重与资格过滤后无已证实近期催化（真实无窗口内行业新闻"
                "或供应商覆盖限制，不补造）"
            )
    else:
        if upgraded:
            out["gaps"].append(
                f"其中 {upgraded} 条经文章元数据日期确证"
                "（date_basis=article_metadata，含可保存的 meta 原始依据；"
                "URL 启发式已禁用）"
            )
        if background:
            out["gaps"].append(
                f"另有 {len(background)} 条无日期或降级结果仅列示为背景线索"
                "（bg-*，不作为已证实近期催化，不单独支撑重要结论）"
            )
    out["gaps"] = _bound_gaps(out["gaps"], 14)
    return out


# ---------------------------------------------------------------------------
# P4d 证据化综合研判（issue #83）：既有模型路由 + 登记表校验（必需研究，
# 有合格催化才调用模型；先于申万/概念可选长链）。
# ---------------------------------------------------------------------------

def _build_evidence_registry(stock_name: str, code: str, ident: Dict[str, Any],
                             prosper: Dict[str, Any], trend: Dict[str, Any],
                             peers: Dict[str, Any],
                             catalysts: Dict[str, Any]) -> "EvidenceRegistry":
    """实际取得的合格事实 → 证据登记表（缺失块不注册 → 模型不可引用）。

    只注册 immutable 事实：静态先验（政策 DNA/基率）不注册（仅报告背景，
    不能独立支撑结论）；身份注册但不含行情数值（身份事实不能佐证 fabricated
    metrics）；bg-* 注册为 background（只能附加引用）。材料与 payload 载荷
    同源（cat 条目字段一致）。root-decision 所有权：市场类证据
    （intraday/trend/peers/target）的 raw magnitudes 不进模型投影（由
    ``market_observation_relations`` 定性观察关系替代），非市场事实原样
    投影；模型 claims 纯定性（qualitative 契约，无 numeric token 锚定）。
    """
    from src.services import sector_research

    registry = sector_research.EvidenceRegistry()
    registry.register(sector_research.EvidenceEntry.build(
        "identity", "identity", "主行业身份",
        {
            "stock_name": stock_name,
            "stock_code": code,
            "industry_em": ident.get("industry_em") or None,
            "board_name": ident.get("board_name") or None,
            "board_code": ident.get("board_code") or None,
            "member_count": (ident.get("membership") or {}).get("member_count"),
        },
    ))
    row = prosper.get("row") or {}
    if prosper.get("sector_found"):
        registry.register(sector_research.EvidenceEntry.build(
            "intraday", "fact", "当日板块行情（当前快照，单期观测）",
            {
                "change_pct": row.get("change_pct"),
                "rank_of": prosper.get("rank_of"),
                "total_boards": prosper.get("total_boards"),
                "momentum": prosper.get("momentum"),
                "breadth": prosper.get("breadth"),
                "metric_meaning": {
                    "change_pct": "板块指数当日涨跌幅（百分比，负值为下跌）",
                    "rank_of/total_boards": "按当日涨跌幅在全部行业板块中的"
                                            "排名（当前快照，无历史动量观测）",
                    "momentum": "有界 0-100 横截面涨跌幅分位（有效板块中严格"
                                "低于目标的占比折算），正值不表示收益为正或"
                                "涨势延续",
                    "breadth": "当日上涨家数占比（百分比，当前快照）",
                },
            },
        ))
    if trend.get("eligible") and trend.get("period_return_pct") is not None:
        registry.register(sector_research.EvidenceEntry.build(
            "trend", "fact", "板块近期趋势（最近 20 个已完成交易日）",
            {
                "window": [trend.get("start_date"), trend.get("end_date")],
                "qualified_sessions": trend.get("qualified_sessions"),
                "period_return_pct": trend.get("period_return_pct"),
                "benchmark_period_return_pct": (trend.get("benchmark") or {}).get(
                    "period_return_pct"),
                "relative_return_pp": trend.get("relative_return_pp"),
            },
        ))
    peer_items = [
        {"name": p.get("name"), "is_target": p.get("is_target"),
         "change_pct": p.get("change_pct")}
        for p in (peers.get("items") or [])
    ]
    if peer_items:
        registry.register(sector_research.EvidenceEntry.build(
            "peers", "fact", "同业成交活跃代表（快照当日口径）",
            {
                # 选取规则是权威口径元数据（同一快照成交额前五 + 目标股
                # 去重；活跃代表，非市值/基本面龙头）——投影给模型输入，
                # 防止把活跃代表升格为「龙头」
                "selection_rule": peers.get("selection_rule"),
                "items": peer_items,
            },
        ))
    target_change = next(
        (p.get("change_pct") for p in peer_items if p.get("is_target")), None,
    )
    if target_change is not None:
        registry.register(sector_research.EvidenceEntry.build(
            "target", "fact", "目标股快照当日涨跌幅",
            {"stock_name": stock_name, "snapshot_change_pct": target_change},
        ))
    for c in (catalysts.get("items") or []):
        registry.register(sector_research.EvidenceEntry.build(
            c["evidence_id"], "catalyst", c["title"],
            {
                "title": c["title"],
                "url": c["url"],
                "source_domain": c["source_domain"],
                "snippet": c.get("snippet"),
                "published_date": c["published_date"],
                "date_basis": c.get("date_basis") or "provider_published",
                "same_event_urls": c.get("same_event_urls") or [],
                "final_url": c.get("final_url"),
            },
        ))
    for c in (catalysts.get("background_items") or []):
        payload: Dict[str, Any] = {
            "title": c["title"],
            "url": c["url"],
            "source_domain": c["source_domain"],
            "snippet": c.get("snippet"),
        }
        if c.get("final_url"):
            # 终态已知（冲突/回顾/重复别名/到达终态的安全失败背景同样
            # 保留实际文章地址——模型与公开载荷同源，不丢 provenance）
            payload["final_url"] = c["final_url"]
        if c.get("published_date"):
            payload["published_date"] = c["published_date"]
        if c.get("downgrade_reason"):
            payload["limitation"] = c["downgrade_reason"]
        registry.register(sector_research.EvidenceEntry.build(
            c["evidence_id"], "background", c["title"], payload,
        ))
    return registry


@ensure(
    lambda result: (
        isinstance(result, dict) and "eligible" in result and "status" in result
        and "gaps" in result
    ),
    "synthesis pillar must return dict with eligible/status/gaps keys",
)
@ensure(
    lambda result: (
        result.get("status") != "ok"
        or (
            isinstance(result.get("claims"), list) and result["claims"]
            and all(c.get("evidence_refs") for c in result["claims"])
        )
    ),
    "ok synthesis must carry at least one claim with non-empty evidence_refs",
)
def _synthesis_pillar(stock_name: str, code: str, ident: Dict[str, Any],
                      prosper: Dict[str, Any], trend: Dict[str, Any],
                      peers: Dict[str, Any], catalysts: Dict[str, Any], *,
                      budget: ReportBudget,
                      subject_confirmed: bool = False) -> Dict[str, Any]:
    """证据化综合研判（issue #83）：登记表引用 + 纯定性输出契约 + 绝对预算。

    资格：confirmed 才执行；无已证实催化不调用模型（``no_qualified_facts``，
    先验/背景不产生空泛完整叙事）。模型未配置/失败/畸形是不同 ``status``；
    **程序契约错误（TypeError/AssertionError）经 adapter opt-in 上抛原样
    可见**（root 批准的唯一共享边界变更，``propagate_program_errors=True``），
    不伪装成模型失败或数据缺失；已识别传输/超时错误降本块（sanitized）。

    模型 claims 纯定性：claims 文本不含任何数字值/日期/价格/比率/代码编号，
    定量陈述整条拒绝并记 sanitized 原因，事实全部保留；市场数值关系的
    摘要/观察限制由服务端程序化渲染（``summary_origin="server_facts"``，
    含精确市场数值/日期——数值只来自服务端所有权，不来自模型文本）。

    绝对预算到真实 wire：模型出站线程内绑定 ``ResearchRun``（deadline =
    ReportBudget.fetch_deadline，同一单调时刻域）+ ``request_deadline``，
    finally reset（不向共享池下一个请求泄漏）；出站前显式幂等安装
    ``eastmoney_patch(budget_only=True)``（ContextVar 只有 patch 在位才
    真正钳制 requests/HTTPX 出站）；``limited_acompletion._deadline``
    取 run.deadline 绝对值 → 队列等待/首字节/流/每次 HTTP 重试同一绝对
    截止，不在路由选择后重置相对时钟（timeout 传进入时剩余值，fallback
    循环内递减消耗同一预算）。迟到 worker 只写自己诊断，不 mutate 已返回
    报告。
    """
    from src.core.research_run import ResearchRun, current_research
    from src.patches.eastmoney_patch import eastmoney_patch, request_deadline
    from src.services import sector_research
    from src.services.run_diagnostics import sanitize_diagnostic_text

    # 幂等安装既有 requests 预算边界（模型 SDK 传输与重试同受钳制；
    # budget_only 不启用 auth，不改共享默认/全局 provider 行为）
    eastmoney_patch(budget_only=True)

    out: Dict[str, Any] = {
        "eligible": False,
        "status": "skipped_identity",
        "summary": None, "claims": [], "uncertainty_note": None,
        "rejected_count": 0,
        "provider": None, "model": None,
        "observed_at": None,
        "gaps": [],
    }
    observed_at = datetime.now().isoformat(timespec="seconds")
    out["observed_at"] = observed_at
    if not subject_confirmed:
        out["gaps"].append(
            "证据化综合：主行业身份未确认（confirmed 前置不满足），不执行模型综合"
        )
        return out
    out["eligible"] = True
    if not (catalysts.get("items") or []):
        out["status"] = "no_qualified_facts"
        out["gaps"].append(
            "证据化综合：无已证实近期催化事实，不执行模型综合"
            "（背景/先验不产生完整叙事；趋势/同业事实保留在对应块）"
        )
        return out

    from src.agent.llm_adapter import LLMToolAdapter, _resolve_litellm_exception

    adapter = LLMToolAdapter()
    # 真实契约：bool property（显式断言可用性，不静默跳过）
    if not isinstance(adapter.is_available, bool) or not adapter.is_available:
        out["status"] = "model_unavailable"
        out["gaps"].append("证据化综合：模型未配置（LLM 通道不可用），本次未综合")
        return out

    registry = _build_evidence_registry(
        stock_name, code, ident, prosper, trend, peers, catalysts,
    )
    messages = [
        {"role": "system", "content": sector_research.SYNTH_SYSTEM_PROMPT},
        {"role": "user", "content": sector_research.build_synthesis_user_text(
            registry,
        )},
    ]

    # 已识别模型传输/服务错误类（stub 环境返回 fallback 类，行为一致）
    llm_transport_excs = tuple(
        _resolve_litellm_exception(name)
        for name in ("RateLimitError", "APIError", "AuthenticationError",
                     "BadRequestError", "ServiceUnavailableError",
                     "ContextWindowExceededError")
    )

    def _call_model() -> Any:
        """线程内绝对预算绑定 + opt-in 程序错误上抛（见 docstring）。"""
        budget.require_fetch()
        run = ResearchRun(timeout_seconds=max(budget.fetch_remaining(), 0.001))
        # 绝对修正：ResearchRun.deadline 与 ReportBudget.fetch_deadline 是
        # 同一 monotonic 时刻（构造 + 少量偏移已校正）；queue/fallback/stream
        # /每次重试经 limited_acompletion._deadline 取此绝对值。
        run.deadline = budget.fetch_deadline
        token_run = current_research.set(run)
        token_deadline = request_deadline.set(budget.fetch_deadline)
        try:
            return adapter.call_text(
                messages,
                timeout=max(budget.fetch_remaining(), 0.001),
                max_tokens=sector_research.SYNTHESIS_MAX_TOKENS,
                # root 批准的唯一共享边界变更：程序契约错误保留原样身份
                # 上抛（默认 False 完全保持既有消费者行为）
                propagate_program_errors=True,
            )
        finally:
            request_deadline.reset(token_deadline)
            current_research.reset(token_run)

    try:
        resp = bounded_call(_call_model, budget=budget)
    except BudgetExhausted:
        out["status"] = "model_error"
        out["gaps"].append("证据化综合：取数预算不足，模型综合未完成（合格事实保留）")
        return out
    except TimeoutError as exc:
        # 已识别超时（队列等待/调用超时/预算停止）：模型侧缺口，非预算终态
        safe = sanitize_diagnostic_text(
            f"{type(exc).__name__}: {exc}", max_length=120,
        ) or type(exc).__name__
        out["status"] = "model_error"
        out["gaps"].append(
            f"证据化综合：模型队列或调用超时（{safe}），合格事实保留，不产出综合"
        )
        return out
    except llm_transport_excs as exc:  # noqa: BLE001 - 已识别传输/服务错误降块
        safe = sanitize_diagnostic_text(
            f"{type(exc).__name__}: {exc}", max_length=120,
        ) or type(exc).__name__
        out["status"] = "model_error"
        out["gaps"].append(
            f"证据化综合：模型服务错误（{safe}），合格事实保留，不产出综合"
        )
        return out
    # propagate_program_errors=True 时 TypeError/AssertionError 已从 adapter
    # 原样上抛（不在本函数捕获）——程序契约错误经 compose/generate 可见，
    # API 层按 500 报告，不伪装成模型失败。

    if getattr(resp, "provider", "") == "error" or not getattr(resp, "content", None):
        out["status"] = "model_error"
        safe = sanitize_diagnostic_text(
            str(getattr(resp, "content", "") or "空响应"), max_length=120,
        ) or "空响应"
        out["gaps"].append(f"证据化综合：模型路由失败（{safe}），合格事实保留")
        return out

    parsed = sector_research.parse_synthesis_json(resp.content)
    if parsed is None:
        out["status"] = "malformed_output"
        out["gaps"].append(
            "证据化综合：模型输出不是合法 JSON（已拒绝，不采纳部分文本），"
            "合格事实保留"
        )
        return out
    claims, rejected_reasons = sector_research.validate_synthesis_output(
        parsed, registry,
    )
    if not claims:
        out["status"] = "malformed_output"
        detail = rejected_reasons[0] if rejected_reasons else "unknown"
        safe = sanitize_diagnostic_text(detail, max_length=120) or detail
        out["gaps"].append(
            f"证据化综合：模型结论均未通过定性/引用/结构校验"
            f"（首个原因：{safe}），已整体拒绝，合格事实保留"
        )
        return out
    out["status"] = "ok"
    # root-decision 所有权边界：summary/uncertainty 由服务端从已采纳事实
    # 程序化生成，绝不消费模型 claims 文本——模型错误句不可能进入两个核心
    # 字段（identity/新闻的 raw 值与出处照常进 wire；市场 magnitudes 有意
    # 排除出 wire，以定性观察关系呈现）。
    out["summary"] = sector_research.program_fact_summary(registry)
    out["summary_origin"] = "server_facts"
    out["claims"] = claims
    out["uncertainty_note"] = sector_research.program_fact_limitations(registry)
    out["provider"] = str(getattr(resp, "provider", "") or "")[:64]
    out["model"] = str(getattr(resp, "model", "") or "")[:120]
    if rejected_reasons:
        # 混合输出（部分结论合格 + 部分被拒）：合格结论与全部服务端事实保留，
        # rejected_count 显式携带拒绝规模 → compose 完整性=partial（不呈现
        # 全部合格研究）；报告口径与用户可见状态一致（issue #83 边界终态）。
        out["rejected_count"] = len(rejected_reasons)
        safe = sanitize_diagnostic_text(
            f"{len(rejected_reasons)} 条（{rejected_reasons[0]}）", max_length=120,
        )
        out["gaps"].append(
            f"证据化综合：模型返回 {safe} 未通过校验的结论已整条拒绝"
            "（含数量字符陈述 / 未知证据 ID / 结构畸形 / 行动词 / 超限），未采纳"
        )
    out["gaps"] = _bound_gaps(out["gaps"], 14)
    return out


# ---------------------------------------------------------------------------
# P5 相关概念（仅列示）+ v2 编排（不产出综合总分，issue #78 决策 5 / #79）
# ---------------------------------------------------------------------------

def _concepts_pillar(code: str, *, budget: ReportBudget) -> Dict[str, Any]:
    """个股相关概念，仅列示（iFinD 已适配入口，有界读取）。

    - 概念不替代行业、不展开独立研究、不扫描全部概念板块成分。
    - 入口未启用、供应商失败或取数预算不足记缺口，不拖垮主行业报告
      （可选阶段：BudgetExhausted 不向上传播，已取得事实按部分报告保留）。
    - 边界处理：超过展示上限（50 条）截取并如实标注总量与截取缺口
      （展示截取，非供应商故障）；非法条目逐条丢弃记缺口；结构性程序
      错误不吞。等待与适配有界均按同一取数截止。
    """
    out: Dict[str, Any] = {
        "items": [], "source": "ifind", "total_returned": 0, "truncated": False,
        "gaps": [],
    }
    from src.config import get_config

    config = get_config()
    if not config.enable_ifind_mcp:
        out["gaps"].append("相关概念：iFinD 概念入口未启用，本次未列示")
        return out
    try:
        from data_provider.ifind_fundamental_adapter import IfindFetcher

        fetcher = IfindFetcher(
            config.ifind_mcp_endpoint, config.ifind_mcp_token,
            config.ifind_mcp_timeout_seconds,
        )
        membership = bounded_call(
            fetcher.concept_membership, code, budget=budget,
            deadline=budget.fetch_deadline,
        )
        raw_names = list(membership.concepts)
        out["total_returned"] = len(raw_names)
        items: List[Dict[str, Any]] = []
        dropped = 0
        for n in raw_names:
            name = str(n).strip()
            if not name or len(name) > 64:
                dropped += 1
                continue
            items.append(
                {"name": name, "source": "ifind",
                 "as_of_nature": str(membership.as_of_nature)}
            )
        if len(items) > _CONCEPT_DISPLAY_CAP:
            out["truncated"] = True
            out["items"] = items[:_CONCEPT_DISPLAY_CAP]
            out["gaps"].append(
                f"相关概念：供应商共返回 {out['total_returned']} 条，"
                f"展示上限 {_CONCEPT_DISPLAY_CAP} 条，其余未列示"
                "（展示截取，非供应商故障）"
            )
        else:
            out["items"] = items
        if dropped:
            out["gaps"].append(
                f"相关概念：供应商返回 {dropped} 条非法条目已丢弃（不作为行业依据）"
            )
        if membership.errors:
            out["gaps"].append(f"相关概念：供应商返回异常 {membership.errors[0]}")
    except BudgetExhausted:
        out["gaps"].append("相关概念：取数预算不足，本次未读取（可选阶段，不影响主报告）")
    except Exception as exc:  # noqa: BLE001
        out["gaps"].append(f"相关概念查询失败: {str(exc)[:60]}（不影响主行业报告）")
    return out


def _bounded_reason(text: str, cap: int = 200) -> str:
    """assessment.reasons 有界汇总：单项 ≤ cap（strict schema 上限）。

    详细诊断保留在对应块（trend/peers/prosper）的 gaps 里；此处只做有界短
    汇总，复合源失败下长缺口不再把整份报告炸成 500/零存档。
    """
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def _compose_status(ident: Dict[str, Any]) -> Tuple[str, List[str]]:
    """资料状态判定（正常「未评分」与「资料不足」语义分开）：

    - unscored：有可研究主体（板块锁定，成分验证可缺）——首版不产出综合评分
    - insufficient_data：主行业身份缺失或无法唯一锁定——核心问题无法回答
    """
    subject = ident.get("subject_status") or "missing"
    if subject in ("missing", "ambiguous"):
        if subject == "missing":
            reasons = ["主行业身份未取得：识别链未返回行业数据"]
        else:
            reasons = ["主行业身份无法唯一锁定：东财板块表无精确命中或多候选（不近似取首行）"]
        return "insufficient_data", reasons
    return "unscored", []


@require(lambda code: isinstance(code, str) and len(code) == 6 and code.isdigit(),
         "code must be a 6-digit string")
@require(lambda budget: isinstance(budget, ReportBudget) and not budget.expired(),
         "budget must be a live ReportBudget")
@ensure(
    lambda result: (
        isinstance(result, dict)
        and {"payload_version", "stock_code", "stock_name", "generated_at",
             "identity", "background", "concepts", "assessment", "gaps",
             "sector_trend", "sector_peers",
             "sector_catalysts", "sector_synthesis"}
        .issubset(result.keys())
    ),
    "compose_report must return the v2 payload contract",
)
@ensure(
    lambda result: (
        isinstance(result, dict)
        and result.get("sector_trend", {}).get("eligible")
        == (result.get("identity", {}).get("subject_status") == "confirmed")
        and result.get("sector_peers", {}).get("eligible")
        == (result.get("identity", {}).get("subject_status") == "confirmed")
        and result.get("sector_catalysts", {}).get("eligible")
        == (result.get("identity", {}).get("subject_status") == "confirmed")
        and result.get("sector_synthesis", {}).get("eligible")
        == (result.get("identity", {}).get("subject_status") == "confirmed")
    ),
    "trend/peers/catalysts/synthesis eligibility must follow confirmed subject identity "
    "(issues #82/#83)",
)
@ensure(
    lambda result: (
        result["assessment"]["status"] == "unscored"
        and result["identity"]["subject_status"] in ("confirmed", "unverified")
    ) or (
        result["assessment"]["status"] == "insufficient_data"
        and result["identity"]["subject_status"] in ("missing", "ambiguous")
    ),
    "assessment.status must agree with identity.subject_status",
)
@ensure(
    lambda result: (
        result.get("sector_score") is None and result.get("band") is None
        and result.get("beta_meaning") is None and result.get("weights") is None
        and result.get("prosperity_score") is None
    ),
    "legacy composite score/band fields must stay explicitly None (retired)",
)
def compose_report(code: str, name: str, *, budget: ReportBudget) -> Dict[str, Any]:
    """v2 编排：主行业身份 → 趋势/同业（必需新块）→ 可选长链 → 背景支柱 → 概念列示。

    首版新报告不沿用旧综合总分及其分档结论（assessment.score 显式 None）；
    缺数据记缺口不编造；默认基率 0.5 只是先验背景，不单独形成任何「中性」结论。
    编排顺序（issue #82 预算纪律）：趋势/同业必需块先于申万参考/概念等可选
    长链出站，可选阶段耗尽取数预算不会让必需块根本不尝试；趋势/同业自身
    失败/超时只降对应块，已取得内容保留走总截止收尾。

    资料完整性（completeness）与评分政策（status）分开：未评分是评分政策
    （首版不产分），completeness 反映动态事实的实际取得情况——confirmed
    但当日行情全缺、趋势核心未取得或同业样本为空时首屏可见 partial +
    具体原因，不与正常报告无异。

    v1 兼容投影（追加，非整体换形）：``identification``/``policy``/
    ``base_rate``/``prosperity`` 旧事实路径保留可读；评分字段恒为 None
    （显式退役），不恢复旧总分/分档/行动结论。
    """
    ident = _identify_sector(code, budget=budget)
    # 身份阶段快照：板块全表（当日行情复用）+ 成分表（同业复用，同一已验证
    # 快照，避免重复请求与盘中漂移）。均为非载荷键。
    board_snapshot = ident.pop("_board_table", None)
    cons_snapshot = ident.pop("_cons_snapshot", None)
    cons_observed = ident.pop("_cons_observed_at", None)

    # 必需新块（issue #82）：confirmed 才发出站（支柱内部判定资格），
    # 先于申万参考链等可选长链。
    confirmed = ident["subject_status"] == "confirmed"
    trend = _sector_trend_pillar(
        ident["board_code"] or "", ident["board_name"] or "", budget=budget,
        subject_confirmed=confirmed,
    )
    peers = _peers_pillar(code, cons_snapshot if isinstance(cons_snapshot, pd.DataFrame) else None,
                          budget=budget, snapshot_observed_at=cons_observed,
                          subject_confirmed=confirmed)
    # 当日行情先于催化/综合：来自身份阶段同一板块全表快照（本地计算，无新
    # 出站），综合登记表需要 intraday 事实完整。
    prosper = _prosperity_pillar(
        ident["sector_name"], budget=budget, board_code=ident["board_code"] or None,
        board_table=board_snapshot if isinstance(board_snapshot, pd.DataFrame) else None,
    )

    # 必需研究（issue #83）：催化搜索与证据化综合先于申万参考链/概念等可选
    # 长链出站——必需研究不被可选阶段吃掉取数预算；模型综合依赖催化结果。
    catalysts = _catalysts_pillar(
        ident["board_name"] or "", budget=budget, subject_confirmed=confirmed,
        stock_name=name,
    )
    synthesis = _synthesis_pillar(
        name, code, ident, prosper, trend, peers, catalysts,
        budget=budget, subject_confirmed=confirmed,
    )

    # 申万一/二/三级参考链（可选长链后置；失败如实披露，不影响东财主身份）
    lookup_industry = ident["industry_em"] or ident["industry_cninfo"]
    sw: Dict[str, Any] = {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []}
    if lookup_industry:
        sw = _resolve_sw_chain(
            code, f"{lookup_industry} {ident.get('business_scope') or ''}", budget=budget
        )
        ident["gaps"].extend(sw.get("gaps") or [])
    ident["sw_chain"] = sw
    sw_text = " ".join(x for x in (sw.get("l1"), sw.get("l2"), sw.get("l3")) if x)
    lookup_text = " ".join(
        x for x in (ident["industry_em"], ident["industry_cninfo"], sw_text,
                    ident.get("business_scope") or "", ident["sector_name"], name) if x
    )
    policy = _policy_pillar(
        " ".join(x for x in (ident["industry_em"], ident["industry_cninfo"],
                             ident.get("business_scope") or "", sw_text) if x)
    )
    base = _base_rate_pillar(lookup_text)
    # prosper 已在催化/综合之前取得（身份快照本地计算，见上方）；此处不再重复计算
    concepts = _concepts_pillar(code, budget=budget)

    status, reasons = _compose_status(ident)
    if status == "unscored" and ident["subject_status"] == "unverified":
        reasons = ["主行业归属未确认（成分验证未通过）：结论仅基于板块锁定，归属存疑"]

    # 资料完整性依据**有效动态事实**，独立于评分政策（issue #81 第二轮；
    # issue #82 起缺口原因**累积可见**而非短路——行情动量缺失不掩盖趋势/同业
    # 核心缺口）：行存在 ≠ 数据取得——confirmed 但行情字段全缺、归属未确认、
    # 趋势核心未取得或同业样本为空都如实 partial；部分可选维度缺口（如东财
    # 正常表无成交额列、同业样本 4 家、基准失败但板块区间收益已取得）由
    # gaps 如实标注，不机械降级整份报告。政策/基率是静态背景，其缺失由
    # gaps/is_default 标注，不参与完整性判定。
    completeness = "complete"
    if status == "insufficient_data":
        completeness = "partial"  # reasons 已含身份缺失/歧义原因
    if not prosper.get("sector_found"):
        completeness = "partial"
        reason = prosper["gaps"][0] if prosper.get("gaps") else "未知原因"
        reasons = list(reasons) + [_bounded_reason(f"当日板块行情未取得：{reason}")]
    elif prosper.get("momentum") is None:
        completeness = "partial"
        reason = next(
            (g for g in prosper["gaps"] if "动量" in g),
            prosper["gaps"][0] if prosper.get("gaps") else "未知原因",
        )
        reasons = list(reasons) + [
            _bounded_reason(f"当日行情行存在但动态字段未取得：{reason}")
        ]
    if ident["subject_status"] == "unverified":
        # 归属未确认：reasons 已含具体原因（compose 开头设置）
        completeness = "partial"
    if trend.get("eligible") and trend.get("period_return_pct") is None:
        # 趋势核心（板块侧区间收益）未取得（日历不可用/供应商失败/预算不足/
        # 数据不合格）：不全篇伪 complete，已取得内容保留在块内；
        # 短汇总有界（≤schema 单项上限），详细源限制保留 trend.gaps
        completeness = "partial"
        reason = trend["gaps"][0] if trend.get("gaps") else "未知原因"
        reasons = list(reasons) + [_bounded_reason(f"板块近期趋势未取得：{reason}")]
    if peers.get("eligible") and not peers.get("items"):
        completeness = "partial"
        reason = peers["gaps"][0] if peers.get("gaps") else "未知原因"
        reasons = list(reasons) + [_bounded_reason(f"同业样本未取得：{reason}")]
    if catalysts.get("eligible") and not catalysts.get("items"):
        # issue #83：无已证实近期催化（含供应商失败/无日期/无窗口内新闻）如实
        # partial——缺口原因见 catalysts.gaps，背景线索保留但不冒充催化
        completeness = "partial"
        reason = catalysts["gaps"][0] if catalysts.get("gaps") else "未知原因"
        reasons = list(reasons) + [_bounded_reason(f"行业催化未取得：{reason}")]
    if synthesis.get("eligible") and synthesis.get("status") != "ok":
        completeness = "partial"
        reason = synthesis["gaps"][0] if synthesis.get("gaps") else "未知原因"
        reasons = list(reasons) + [_bounded_reason(f"证据化综合未产出：{reason}")]
    if (synthesis.get("eligible") and synthesis.get("status") == "ok"
            and synthesis.get("rejected_count")):
        # issue #83 边界终态：混合输出（部分结论合格 + 部分被拒）不呈现
        # 「全部合格研究」——合格结论与全部服务端事实保留，完整性如实
        # partial 并带显式 rejected-output 原因（计数来自 schema 字段，
        # 不解析 gap 文本；正常全合格输出 rejected_count=0 不受影响）。
        completeness = "partial"
        reasons = list(reasons) + [_bounded_reason(
            f"模型综合存在已拒绝的结论输出（{synthesis['rejected_count']} 条），"
            "完整性不为完整"
        )]

    gaps: List[str] = list(ident["gaps"])
    for block in (policy, prosper, concepts, catalysts, synthesis):
        gaps.extend(block.get("gaps") or [])
    if base["is_default"]:
        gaps.append("行业基率未命中，使用板块中性 0.5（启发式默认先验，仅背景依据，不据此评分）")

    return {
        "payload_version": 2,
        "stock_name": name,
        "stock_code": code,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "identity": ident,
        "background": {
            "policy": policy,       # 静态规则背景（行业 DNA 库）
            "base_rate": base,      # 启发式先验背景（非实测胜率）
            "intraday": prosper,    # 当日行情（价格强弱/活跃度，非产业景气、非净流入）
        },
        "concepts": concepts,       # 仅列示
        "sector_trend": trend,      # 近期趋势（issue #82；confirmed 资格前置）
        "sector_peers": peers,      # 同业比较（issue #82；同一已验证成分快照）
        "sector_catalysts": catalysts,  # 行业催化（issue #83；统一准入）
        "sector_synthesis": synthesis,  # 证据化综合（issue #83；登记表校验）
        "assessment": {
            "status": status,
            "completeness": completeness,  # 资料完整性（与评分政策分开）
            "score": None,          # 首版不产出综合评分（显式未评分）
            "reasons": reasons,
        },
        # ---- v1 兼容投影：旧非评分事实路径保留可读 ----
        "identification": {
            "industry_em": ident["industry_em"],
            "industry_cninfo": ident["industry_cninfo"],
            "business_scope": ident.get("business_scope") or "",
            "sector_name": ident["sector_name"],
            "sw_chain": sw,
            "gaps": ident["gaps"],
        },
        "policy": {
            "lean": policy["lean"], "label": policy["label"], "gaps": policy["gaps"],
            "score": None,  # 退役恒 None：政策 DNA 只保留方向标签，分数易与综合评分混淆
        },
        "base_rate": base,
        "prosperity": {
            "sector_found": prosper["sector_found"],
            "momentum": prosper["momentum"], "activity": prosper["activity"],
            "capital": None,  # 退役：旧「资金」维度实为成交额分位，v2 更名 amount_activity
            "breadth": prosper["breadth"],
            "rank_of": prosper["rank_of"], "total_boards": prosper["total_boards"],
            "row": prosper["row"],
            "gaps": prosper["gaps"],
        },
        # ---- 评分退役：显式 None 语义（不恢复旧总分/分档/行动结论）----
        "sector_score": None,
        "band": None,
        "beta_meaning": None,
        "weights": None,
        "prosperity_score": None,
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# 报告生成入口
# ---------------------------------------------------------------------------

def generate_report(raw_code: str, raw_name: Optional[str] = None) -> Dict[str, Any]:
    """生成个股板块分析专项报告（v2 载荷，issue #81）。

    终态三分（页面等待超时、请求断开与处理终态不混用）：
    - success：报告已渲染并实际存档（部分报告同样存档，缺口随载荷透出）
    - timeout：报告总预算耗尽 —— 不存档、不留文件（含渲染后/写入后/保存期间
      耗尽）；已放弃等待的供应商在途调用可能仍在进行，其结果不会被采纳
      （等待钳制不伪装硬截止）
    - failed：报告已生成但落库失败 —— 清理本请求独占创建的文件，不留孤儿、
      不返回成功

    提交边界：严格载荷校验先于任何存档动作（写文件/写库都不允许带病提交）；
    渲染前、渲染后、写入后、DB 提交边界分别检查/钳制同一主截止（写入与
    SQLite 锁等待/提交共用 ``budget.deadline``），过期即终止不保存伪成功。
    报告 ID/文件经 ``_claim_report_file`` 原子独占，失败只清理本请求拥有的
    新文件，不动他人存档。

    代码/市场资格错误抛 ValueError（API 层 400），不落入以上终态。
    """
    from src.config import get_config
    from src.services.stock_code_utils import normalize_code

    raw = str(raw_code or "").strip()
    code = normalize_code(raw) if raw else ""
    if not code or not code.isdigit() or len(code) != 6:
        raise ValueError(f"仅支持 A 股 6 位代码：{raw_code}")
    name = (raw_name or "").strip()
    if name and code in name:
        name = name.replace(code, "").replace(".SZ", "").replace(".SH", "").strip()
    budget_seconds = float(get_config().sector_analysis_report_timeout_seconds)
    # 收尾余量随合法预算缩放（issue #81 第二轮）：固定 5s 会把 1—5s 的合法
    # 小预算取数时间直接吃光（fetch 截止在起点即过期）；小预算下留一半给取数。
    budget = ReportBudget(
        budget_seconds,
        finish_margin=min(_FINISH_MARGIN_SECONDS, budget_seconds / 2.0),
    )
    if not name:
        # 无显式名称：名称如实用代码，不为补名打开共享 manager——其内部多源
        # fallback 不感知预算（反例实测第二源在取数截止后 +0.10s 启动新出站）；
        # 页面与常规调用均显式携带 stock_name。
        name = code

    try:
        payload = compose_report(code, name, budget=budget)
    except BudgetExhausted as exc:
        logger.warning("[SectorAnalysis] 生成超时（%s）: %s", code, str(exc)[:80])
        return {"status": "timeout", "report_id": None, "stock_code": code,
                "stock_name": name, "markdown": None, "payload": None,
                "message": "生成超时：板块报告总预算（"
                           f"{budget.total_seconds:.0f} 秒）已耗尽，本次未保存报告；"
                           "可稍后重试（供应商在途调用可能仍在进行，其结果不会被采纳）"}

    # 提交边界 1：严格载荷校验先于任何存档动作（写文件/写库都不带病提交）。
    # 校验失败属于程序错误：不存档、不留文件，API 层按 500 报告。
    try:
        SectorReportPayload.model_validate(payload)
    except ValidationError as exc:
        logger.error("[SectorAnalysis] 载荷契约校验失败（%s）: %s", code, str(exc)[:300])
        return {"status": "failed", "report_id": None, "stock_code": code,
                "stock_name": name, "markdown": None, "payload": None,
                "message": "报告载荷契约校验失败，未存档（程序错误，请反馈）"}

    if budget.expired():  # 提交边界 2：只在主截止内渲染并保存，不采纳晚到结果
        return {"status": "timeout", "report_id": None, "stock_code": code,
                "stock_name": name, "markdown": None, "payload": None,
                "message": "生成超时：板块报告总预算（"
                           f"{budget.total_seconds:.0f} 秒）在存档前耗尽，本次未保存报告；"
                           "可稍后重试"}

    claimed = _claim_report_file(_ID_PATTERN.format(ts=datetime.now()))
    if claimed is None:
        return {"status": "failed", "report_id": None, "stock_code": code,
                "stock_name": name, "markdown": None, "payload": None,
                "message": "无法分配报告文件（同分钟存档已满），未存档；请重试"}
    rid, md_path = claimed
    saved_over_deadline = False
    try:
        md = _render(name, code, payload["generated_at"], rid, payload)
        if budget.expired():  # 提交边界 3：渲染可能耗时，保存前再查主截止
            md_path.unlink(missing_ok=True)
            logger.warning("[SectorAnalysis] 渲染后预算耗尽（%s），不保存", code)
            return {"status": "timeout", "report_id": None, "stock_code": code,
                    "stock_name": name, "markdown": None, "payload": None,
                    "message": "生成超时：板块报告总预算（"
                               f"{budget.total_seconds:.0f} 秒）在存档前耗尽，本次未保存报告；"
                               "可稍后重试"}
        try:
            md_path.write_text(md, encoding="utf-8")
        except OSError as exc:
            # 写入失败：只清理本请求独占创建的文件，不留孤儿、不返回成功
            logger.warning("[SectorAnalysis] 报告文件写入失败（%s）: %s", rid, exc)
            md_path.unlink(missing_ok=True)
            return {"status": "failed", "report_id": None, "stock_code": code,
                    "stock_name": name, "markdown": None, "payload": None,
                    "message": "报告已生成但保存失败，未存档；请重试"}
        # 提交边界 4（issue #81 第二轮）：写入本身可能迟到（慢盘/负载），
        # 写后复核主截止——过期即清理本请求独占文件，不开始 DB 保存。
        if budget.expired():
            md_path.unlink(missing_ok=True)
            logger.warning("[SectorAnalysis] 文件写入后预算耗尽（%s），不开始 DB 保存", code)
            return {"status": "timeout", "report_id": None, "stock_code": code,
                    "stock_name": name, "markdown": None, "payload": None,
                    "message": "生成超时：板块报告总预算（"
                               f"{budget.total_seconds:.0f} 秒）在存档期间耗尽，"
                               "本次未保存报告；可稍后重试"}
        record = {
            "id": rid, "stock_code": code, "stock_name": name,
            "md_path": str(md_path),
            "sector_score": None,  # v2 不产出综合评分；旧列保留给存量记录
            "analysis_json": json.dumps(payload, ensure_ascii=False, default=str),
        }
        from src.storage import get_db

        # 提交边界 5：DB 保存与同一主截止同界——锁等待钳制剩余时间、提交前到期
        # rollback（有界同步提交能力边界见 save_sector_analysis_report 文档）。
        if get_db().save_sector_analysis_report(record, deadline=budget.deadline):
            return {"status": "success", "report_id": rid, "stock_code": code,
                    "stock_name": name, "markdown": md, "payload": payload,
                    "message": ""}
        saved_over_deadline = budget.expired()
    except Exception:  # noqa: BLE001 - 渲染/编排程序错误：清理本请求文件后向上传播
        md_path.unlink(missing_ok=True)
        raise
    # 保存失败：清理本请求独占创建的文件，不留孤儿、不返回成功。
    # 总截止在保存期间耗尽（锁等待/提交前到期）→ timeout；其余 → 保存故障，
    # 两者在响应消息中如实区分（供应商超时/取数截止/总截止/保存故障分层）。
    md_path.unlink(missing_ok=True)
    if saved_over_deadline:
        logger.warning("[SectorAnalysis] 保存期间总预算耗尽（%s），未提交", code)
        return {"status": "timeout", "report_id": None, "stock_code": code,
                "stock_name": name, "markdown": None, "payload": None,
                "message": "生成超时：板块报告总预算（"
                           f"{budget.total_seconds:.0f} 秒）在保存期间耗尽，"
                           "本次未提交存档；可稍后重试"}
    return {"status": "failed", "report_id": None, "stock_code": code,
            "stock_name": name, "markdown": None, "payload": None,
            "message": "报告已生成但保存失败，未存档；请重试"}


def _md_link_text(title: str) -> str:
    """供应商标题投影到 GFM 表格内链接文本的字面转义（渲染层，不改数据）。

    真实标题可含字面 ``|``（如「去库存| 财经日日评」）：GFM 表格把它当列
    定界符，日期/口径/来源列整体左移、末列来源被丢弃。只在 Markdown 投影
    时转义；payload/存档 JSON 中标题原文保持不动：

    - ``\\`` 先转义：标题已有反斜杠时，后续插入的 ``\\|``/``\\[``/``\\]``
      不会与已有反斜杠重组出新的定界符或终止符（``政策\\]解读`` 若只加倍
      反斜杠，``\\`` 渲染为字面反斜杠后紧随的 ``]`` 会提前终止链接文本，
      adopted URL 的 anchor 由 1 变 0——先转义即消除该奇偶交互）；
    - ``|`` → ``\\|``：单元格内渲染为字面竖线，永不成为列边界；
    - ``[``/``]`` → ``\\[``/``\\]``：方括号是链接文本定界符，字面化后标题
      完整保留在单一原始 href anchor 内（``政策\\[解读| 资讯`` 的前缀不再
      脱落到链接外），既有可访问引用投影不回退；
    - CR/LF 折叠为空格：表格行是单行投影，原始换行会把行拆断。
    """
    return (
        title.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _render(name: str, code: str, as_of: str, rid: str, a: Dict[str, Any]) -> str:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent.parent.parent / "templates"),
        autoescape=False, trim_blocks=True, lstrip_blocks=True,
    )
    env.filters["md_link_text"] = _md_link_text
    md = env.get_template("sector_analysis_report.j2").render(
        stock_name=name, stock_code=code, as_of=as_of, report_id=rid, a=a,
    )
    # 排版后处理：盘古空格（中英间），保留标准 Markdown。
    md = re.sub(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", md)
    md = re.sub(r"(?<=[A-Za-z0-9%])(?=[\u4e00-\u9fff])", " ", md)
    return md
