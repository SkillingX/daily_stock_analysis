# -*- coding: utf-8 -*-
"""个股板块分析服务：板块识别 → 三支柱（政策倾向/行业基率/板块景气）→ 综合评分 → 报告。

三支柱数据来源（全部已有数据源，不新增外部依赖）：
- 政策倾向：行业 DNA 库（industry_dna_loader，本地资产）
- 行业基率：industry_base_rates.json（启发式默认，可迭代，报告强制透出表版本）
- 板块景气：东财行业板块全表（akshare stock_board_industry_name_em：涨跌幅/换手率/
  总市值/上涨家数），同花顺个股信息（fuyao）辅助板块识别

权重：政策 30% + 基率 30% + 景气 40%（用户拍板）。
缺数据记缺口不编造（共享宪法第 2 条）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd
from icontract import ensure, require

logger = logging.getLogger(__name__)

_ID_PATTERN = "sa_{ts:%Y%m%d%H%M}"
_REPORT_DIR = Path(__file__).parent.parent.parent / "reports" / "sector_analysis"

# 综合评分权重
_W_POLICY = 0.30
_W_BASE = 0.30
_W_PROSPERITY = 0.40

# 政策倾向三档分（对齐 F2 sector_dim 口径）
_POLICY_SCORE = {"supportive": 66.0, "neutral": 50.0, "restrictive": 34.0}
_POLICY_LABEL = {"supportive": "支持", "neutral": "中性", "restrictive": "限制", None: "未知"}


def _ak_with_retry(call: Callable[..., Any], *args: Any,
                   retries: int = 1, delay: float = 0.5,
                   **kwargs: Any) -> Any:
    """akshare 接口包一层瞬时网络重试：默认 1 次重试 + 0.5s 退避。

    场景：东财 push2 接口在网络波动时常抛 ``RemoteDisconnected`` /
    ``Max retries exceeded``，多一次重试即可恢复，避免直接 fallback 丢精度。
    非网络错误（如参数错）会原样抛出，不浪费时间。
    """
    last_exc: Optional[BaseException] = None
    for i in range(retries + 1):
        try:
            return call(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 保留原 except 兼容
            last_exc = exc
            if i < retries:
                time.sleep(delay)
    assert last_exc is not None  # 至少跑了一次循环
    raise last_exc


def get_report_dir() -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return _REPORT_DIR


def _resolve_unique_id(base: str) -> str:
    """报告 id 分钟级冲突时追加字母后缀（对齐 financial-analysis 同函数语义）。"""
    candidate = base
    for suffix in "abcdefgh":
        if not (get_report_dir() / f"{candidate}.md").exists():
            return candidate
        candidate = f"{base}{suffix}"
    return f"{base}x"


# ---------------------------------------------------------------------------
# P0b 申万行业链反查：证监会/东财行业名 → 三级候选 → 成分股验证 → 一/二/三级链
# ---------------------------------------------------------------------------

_SW_TABLES_CACHE: Optional[Dict[str, Any]] = None
_SW_CHAIN_CACHE: Dict[str, Dict[str, Any]] = {}


def _sw_tables() -> Optional[Dict[str, Any]]:
    """申万一/二/三级指数表（进程内缓存，日级快照由缓存键日期保证）。"""
    global _SW_TABLES_CACHE
    if _SW_TABLES_CACHE is not None:
        return _SW_TABLES_CACHE
    try:
        import akshare as ak

        third = _ak_with_retry(ak.sw_index_third_info)
        second = _ak_with_retry(ak.sw_index_second_info)
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
    """
    text = industry_text or ""
    for w in _SW_STOPWORDS:
        text = text.replace(w, " ")
    units = [t for t in re.split(r"[、，,\s/：:；;（）()]+", text) if t]
    kws: List[str] = []
    for u in dict.fromkeys(units):  # 保序去重
        if len(u) < 2:
            if u in _SW_SINGLE_CHAR and u not in kws:
                kws.append(u)
            continue
        if u not in kws:
            kws.append(u)
        if len(u) >= 4:  # 滑窗补充 4/3/2 字词根
            for n in (4, 3, 2):
                for i in range(len(u) - n + 1):
                    gram = u[i : i + n]
                    if gram not in kws:
                        kws.append(gram)
    return kws


def _resolve_sw_chain(code: str, industry_text: str) -> Dict[str, Any]:
    """个股 → 申万一/二/三级链。

    路径：行业文本拆词 → 三级名候选（按命中词数排序，最多 5 个送成分验证）
    → index_component_sw 验证个股在成分中 → 三级行「上级行业」爬二级 → 一级。
    全链失败返回空链 + 缺口（如实，不编造）。结果按 自然日+代码 缓存。
    """
    chain: Dict[str, Any] = {"l1": "", "l2": "", "l3": "", "l3_code": "", "gaps": []}
    cache_key = f"{code}:{datetime.now().strftime('%Y%m%d')}"
    if cache_key in _SW_CHAIN_CACHE:
        return _SW_CHAIN_CACHE[cache_key]

    tables = _sw_tables()
    if tables is None:
        chain["gaps"].append("申万链：申万指数表不可用")
        return chain
    third, second = tables["third"], tables["second"]

    kws = _sw_keywords(industry_text)
    if not kws:
        chain["gaps"].append(f"申万链：行业文本「{industry_text}」拆词后无有效关键词")
        return chain

    def _hit_count(name: Any) -> int:
        return sum(1 for k in kws if k in str(name))

    third = third.copy()
    third["_hits"] = third["行业名称"].map(_hit_count)
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
        code_col = str(row.get("行业代码") or "").replace(".SI", "")
        try:
            import akshare as ak

            # 注意：此处不包 _ak_with_retry — 候选循环里 1-8 个候选要快速失败切换
            # 加 retry 会让最坏情况延迟从 ~0.5s/cand 变成 ~5s/cand
            cons = ak.index_component_sw(symbol=code_col)
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
# P1 板块识别：个股 → 行业归属链（东财 → 巨潮 → 同花顺兜底）+ 申万链反查
# ---------------------------------------------------------------------------

def _identify_sector(code: str) -> Dict[str, Any]:
    """识别个股所属板块。返回 industry 链 + 板块名 + 缺口。

    顺序按链路耗时：东财(~1s) → 巨潮(~0.7s) → 同花顺 fuyao(~25s 慢链兜底)。
    """
    out: Dict[str, Any] = {
        "industry_em": "",        # 东财行业（优先，申万风格口径）
        "industry_cninfo": "",    # 巨潮证监会行业（兜底）
        "business_scope": "",     # 巨潮主营业务文本（申万反查/政策/基率的关键词补充）
        "sw_chain": {},           # 申万一/二/三级链（成分验证）
        "sector_name": "",        # 景气查询用的板块名
        "gaps": [],
    }
    # 1) 东财个股信息（申万风格行业，恢复后最准）
    try:
        import akshare as ak

        df = _ak_with_retry(ak.stock_individual_info_em, symbol=code)
        if df is not None and not df.empty:
            kv = dict(zip(df["item"], df["value"]))
            out["industry_em"] = str(kv.get("行业") or "").strip()
            if not out["sector_name"]:
                out["sector_name"] = out["industry_em"]
    except Exception as exc:  # noqa: BLE001
        out["gaps"].append(f"东财个股行业查询失败: {str(exc)[:50]}")

    # 2) 巨潮资讯（证监会行业 + 主营业务文本，~0.7s 独立链路）
    if not out["industry_em"]:
        try:
            import akshare as ak

            df = _ak_with_retry(ak.stock_profile_cninfo, symbol=code)
            if df is not None and not df.empty:
                row0 = df.iloc[0]
                industry = str(row0.get("所属行业") or "").strip()
                if industry:
                    out["industry_cninfo"] = industry
                    out["sector_name"] = industry
                # 主营业务文本：证监会行业名常不含赛道词（至纯=专用设备 vs 申万=半导体设备），
                # 主营业务补足赛道关键词，显著提高申万链/政策/基率命中率
                biz = str(row0.get("主营业务") or "").strip()
                if biz and biz != "nan":
                    out["business_scope"] = biz[:200]
        except Exception as exc:  # noqa: BLE001
            out["gaps"].append(f"巨潮行业查询失败: {str(exc)[:50]}")

    # 3) 同花顺 fuyao 兜底（慢链 ~25s，最后）
    if not out["sector_name"]:
        try:
            from src.deep_research_dims.context import _safe_fundamental

            class _Ctx:
                fundamental: Dict[str, Any] = {}

            fund = _safe_fundamental(code, _Ctx())  # type: ignore[arg-type]
            hint = str(fund.get("industry_hint") or "").strip()
            if hint:
                out["industry_em"] = hint
                out["sector_name"] = hint
        except Exception as exc:  # noqa: BLE001
            out["gaps"].append(f"同花顺行业提示查询失败: {str(exc)[:50]}")

    # 4) 申万一/二/三级链反查（行业文本 + 主营业务文本合并拆词；反哺政策/基率准确性）
    lookup_industry = out["industry_em"] or out["industry_cninfo"]
    if lookup_industry:
        sw = _resolve_sw_chain(code, f"{lookup_industry} {out.get('business_scope') or ''}")
        out["sw_chain"] = sw
        out["gaps"].extend(sw.get("gaps") or [])

    if not out["sector_name"]:
        out["gaps"].append("板块识别失败：无行业数据")
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
    return {
        "lean": lean,
        "label": _POLICY_LABEL.get(lean, "未知"),
        "score": _POLICY_SCORE.get(lean or ""),
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
# P4 板块景气支柱（东财行业板块全表：动量/活跃度/资金/广度）
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
@ensure(
    lambda result: (
        isinstance(result, dict)
        and "sector_found" in result
        and "gaps" in result
    ),
    "prosperity pillar must return dict with sector_found/gaps keys",
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
        for dim in ("momentum", "activity", "capital", "breadth")
    ),
    "prosperity dims must be None or in [0, 100]",
)
def _prosperity_pillar(sector_name: str) -> Dict[str, Any]:
    """东财行业板块全表 → 目标板块行 + 四维景气评分（0-100）。

    四维：动量（涨跌幅全表分位）/活跃度（换手率 vs 全表中位）/
    资金（成交额 vs 全表中位）/广度（上涨家数占比）。
    """
    out: Dict[str, Any] = {
        "sector_found": False,
        "momentum": None, "activity": None, "capital": None, "breadth": None,
        "rank_of": None, "total_boards": None,
        "row": {},
        "gaps": [],
    }
    if not sector_name:
        out["gaps"].append("板块景气：板块名为空")
        return out
    df = None
    table_kind = ""
    try:
        import akshare as ak

        df = _ak_with_retry(ak.stock_board_industry_name_em)
        table_kind = "em"
    except Exception as exc:  # noqa: BLE001
        out["gaps"].append(f"板块景气：东财板块表查询失败 {str(exc)[:50]}，尝试新浪")
    if df is None or df.empty:
        try:
            import akshare as ak

            df = _ak_with_retry(ak.stock_sector_spot, indicator="行业")
            table_kind = "sina"
        except Exception as exc:  # noqa: BLE001
            out["gaps"].append(f"板块景气：新浪板块表也失败 {str(exc)[:50]}")
            return out
    if df is None or df.empty:
        out["gaps"].append("板块景气：板块表为空")
        return out

    # 列名映射（东财/新浪两套；新浪为证监会口径板块表，列结构完全不同）
    col_map = (
        {"name": "板块名称", "change": "涨跌幅", "turnover": "换手率",
         "amount": "成交额", "cap": "总市值", "up": "上涨家数", "down": "下跌家数",
         "leader": "领涨股票", "leader_chg": "领涨股票-涨跌幅", "volume": None, "co_count": None}
        if table_kind == "em"
        else {"name": "板块", "change": "涨跌幅", "turnover": None,
              "amount": "总成交额", "cap": None, "up": None, "down": None,
              "leader": None, "leader_chg": None, "volume": "总成交量", "co_count": "公司家数"}
    )
    name_col = col_map["name"]
    if name_col not in df.columns:
        out["gaps"].append("板块景气：板块表缺少名称列")
        return out

    df = df.copy()
    total = len(df)
    out["total_boards"] = total

    # 命中行：精确匹配 → 包含匹配 → 前缀截短反查
    hit = df[df[name_col] == sector_name]
    if hit.empty:
        hit = df[df[name_col].astype(str).str.contains(sector_name, na=False)]
    if hit.empty and len(sector_name) >= 2:
        for n in (4, 3, 2):
            hit = df[df[name_col].astype(str).str.contains(sector_name[:n], na=False)]
            if not hit.empty:
                break
    if hit.empty:
        out["gaps"].append(f"板块景气：全表 {total} 个板块未命中「{sector_name}」")
        return out

    def _col(row: Any, key: str) -> Any:
        col = col_map.get(key)
        return row.get(col) if col else None

    row = hit.iloc[0]
    out["sector_found"] = True
    out["row"] = {
        "board_name": str(row.get(name_col) or ""),
        "change_pct": _num(_col(row, "change")),
        "turnover_rate": _num(_col(row, "turnover")),
        "total_market_cap": _num(_col(row, "cap")),
        "amount": _num(_col(row, "amount")),
        "up_count": _num(_col(row, "up")),
        "down_count": _num(_col(row, "down")),
        "leader": str(_col(row, "leader") or ""),
        "leader_change_pct": _num(_col(row, "leader_chg")),
        "volume": _num(_col(row, "volume")),
        "company_count": _num(_col(row, "co_count")),
    }

    # 动量：涨跌幅全表排名分位（前 10% = 100 分）
    change_col = col_map["change"]
    if change_col in df.columns:
        df[change_col] = pd.to_numeric(df[change_col], errors="coerce")
        valid = df[change_col].dropna()
        target = out["row"]["change_pct"]
        if target is not None and len(valid) > 1:
            below = int((valid < target).sum())
            out["rank_of"] = total - below  # 1 = 涨幅最高
            out["momentum"] = round(below / (len(valid) - 1) * 100, 1)

    # 活跃度：东财=换手率/全表中位；新浪=户均成交量（总成交量/公司家数）/全表中位
    turnover_col = col_map["turnover"]
    if turnover_col and turnover_col in df.columns:
        med = _median([_num(v) for v in df[turnover_col].tolist()])
        t = out["row"]["turnover_rate"]
        if med and t is not None and med > 0:
            out["activity"] = round(min(100.0, max(0.0, t / med * 50.0)), 1)
    elif table_kind == "sina" and col_map["volume"] and col_map["volume"] in df.columns:
        vol_col, cnt_col = col_map["volume"], col_map["co_count"]
        if cnt_col and cnt_col in df.columns:
            per_co: List[Optional[float]] = []
            for v, c in zip(df[vol_col].tolist(), df[cnt_col].tolist()):
                v_n, c_n = _num(v), _num(c)
                if v_n is not None and c_n:
                    per_co.append(v_n / c_n)
            med = _median(per_co)
            tv, tc = out["row"]["volume"], out["row"]["company_count"]
            if med and tv is not None and tc:
                t = tv / tc
                out["activity"] = round(min(100.0, max(0.0, t / med * 50.0)), 1)
        else:
            out["gaps"].append("板块景气-活跃度：新浪表缺公司家数列")
    else:
        out["gaps"].append("板块景气-活跃度：板块表缺列")

    # 资金：成交额 / 全表中位（口径同活跃度）
    amount_col = col_map["amount"]
    if amount_col in df.columns:
        med = _median([_num(v) for v in df[amount_col].tolist()])
        a = out["row"]["amount"]
        if med and a is not None and med > 0:
            out["capital"] = round(min(100.0, max(0.0, a / med * 50.0)), 1)

    # 广度：上涨家数占比（东财专属列；新浪表无此列记缺口）
    up_col, down_col = col_map["up"], col_map["down"]
    if up_col and down_col and up_col in df.columns and down_col in df.columns:
        up, down = out["row"]["up_count"], out["row"]["down_count"]
        if up is not None and down is not None and (up + down) > 0:
            out["breadth"] = round(up / (up + down) * 100, 1)
    elif table_kind == "sina":
        out["gaps"].append("板块景气-广度：新浪表无上涨家数列")

    for dim, label in (("momentum", "动量"), ("activity", "活跃度"), ("capital", "资金"), ("breadth", "广度")):
        if out[dim] is None and not any(label in g for g in out["gaps"]):
            out["gaps"].append(f"板块景气-{label}：板块表缺列或数据无效")
    return out


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "" or v == "-":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# P5 综合评分与结论
# ---------------------------------------------------------------------------

_PROSPERITY_DIMS = ("momentum", "activity", "capital", "breadth")


def _PROSPERITY_DIMS_OK(p: Any) -> bool:
    """icontract 安全的 precondition 检查（避免 p 非 dict 时 p.get() 炸）。"""
    if not isinstance(p, dict):
        return False
    for d in _PROSPERITY_DIMS:
        v = p.get(d)
        if v is not None and not isinstance(v, (int, float)):
            return False
    return True


@require(lambda p: _PROSPERITY_DIMS_OK(p),
         "p must be dict and dims must be None or numeric")
@ensure(
    lambda result: result is None or 0.0 <= result <= 100.0,
    "result must be None or in [0, 100]",
)
def _prosperity_score(p: Dict[str, Any]) -> Optional[float]:
    vals: List[Optional[float]] = [p.get(d) for d in _PROSPERITY_DIMS]
    nums: List[float] = [v for v in vals if isinstance(v, (int, float))]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


@require(
    lambda sector_score: sector_score is None or 0.0 <= sector_score <= 100.0,
    "sector_score must be None or in [0, 100]",
)
@ensure(
    lambda result: (
        isinstance(result, tuple) and len(result) == 2
        and all(isinstance(x, str) and x for x in result)
    ),
    "verdict must be (non-empty band, non-empty beta_meaning)",
)
def _verdict(sector_score: Optional[float]) -> Tuple[str, str]:
    """（分档标签, 对个股的 β 含义）。标尺：50 逆风 / 65 中性 / 75 顺风。"""
    if sector_score is None:
        return "数据不足", "板块维度无法给出 β 判断"
    if sector_score >= 75:
        return "顺风", "板块β为正贡献，个股回调时优先找板块内买点"
    if sector_score >= 65:
        return "偏顺风", "板块β小幅正贡献，可正常参与"
    if sector_score >= 50:
        return "中性", "板块β基本中性，个股α主导定价"
    return "逆风", "板块β为负贡献，仓位打折、买点从严"


@require(lambda code: isinstance(code, str) and len(code) == 6 and code.isdigit(),
         "code must be a 6-digit string")
@ensure(
    lambda result: (
        isinstance(result, dict)
        and {"identification", "policy", "base_rate", "prosperity",
             "weights", "gaps", "sector_score", "band"}.issubset(result.keys())
    ),
    "compose_analysis must return the documented dict contract",
)
@ensure(
    lambda result: abs(sum(result["weights"].values()) - 1.0) < 1e-6,
    "weights must sum to 1.0",
)
@ensure(
    lambda result: (
        result["sector_score"] is None
        or 0.0 <= result["sector_score"] <= 100.0
    ),
    "sector_score must be None or in [0, 100]",
)
def compose_analysis(code: str, name: str) -> Dict[str, Any]:
    """编排：识别 → 三支柱 → 综合分。缺数据记缺口不编造。

    申万链文本反哺政策/基率：证监会口径（专用设备制造业）常不命中 DNA/基率，
    申万口径（半导体设备）含行业关键词，能显著提高两支柱命中率。
    """
    ident = _identify_sector(code)
    sw = ident.get("sw_chain") or {}
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
    prosper = _prosperity_pillar(ident["sector_name"])

    parts: List[Tuple[float, float]] = []  # (score, weight)
    if isinstance(policy["score"], (int, float)):
        parts.append((float(policy["score"]), _W_POLICY))
    if base["base_rate"] is not None:
        parts.append((30.0 + base["base_rate"] * 60.0, _W_BASE))  # 基率 0-1 → 30-90（对齐 F2）
    p_score = _prosperity_score(prosper)
    if p_score is not None:
        parts.append((p_score, _W_PROSPERITY))

    sector_score = (
        round(sum(s * w for s, w in parts) / sum(w for _, w in parts), 2) if parts else None
    )
    band, beta = _verdict(sector_score)

    gaps: List[str] = list(ident["gaps"])
    for block in (policy, prosper):
        gaps.extend(block.get("gaps") or [])
    if base["is_default"]:
        gaps.append("行业基率未命中，使用板块中性 0.5（可迭代基率表收录该行业）")

    return {
        "stock_name": name,
        "stock_code": code,
        "identification": ident,
        "policy": policy,
        "base_rate": base,
        "prosperity": prosper,
        "prosperity_score": p_score,
        "sector_score": sector_score,
        "band": band,
        "beta_meaning": beta,
        "weights": {"policy": _W_POLICY, "base_rate": _W_BASE, "prosperity": _W_PROSPERITY},
        "gaps": gaps,
    }


# ---------------------------------------------------------------------------
# 报告生成入口
# ---------------------------------------------------------------------------

def generate_report(raw_code: str, raw_name: Optional[str] = None) -> Dict[str, Any]:
    """生成个股板块分析专项报告。"""
    from src.services.stock_code_utils import normalize_code

    raw = str(raw_code or "").strip()
    code = normalize_code(raw) if raw else ""
    if not code or not code.isdigit() or len(code) != 6:
        raise ValueError(f"仅支持 A 股 6 位代码：{raw_code}")
    name = (raw_name or "").strip()
    if name and code in name:
        name = name.replace(code, "").replace(".SZ", "").replace(".SH", "").strip()
    if not name:
        name = code
        try:
            from src.agent.tools.data_tools import _get_fetcher_manager

            quote = _get_fetcher_manager().get_realtime_quote(code)
            name = str(getattr(quote, "name", "") or "").strip() or code
        except Exception:  # noqa: BLE001
            pass

    analysis = compose_analysis(code, name)
    as_of = datetime.now().isoformat(timespec="seconds")
    record: Optional[Dict[str, Any]] = None
    md = ""
    md_path = Path()
    rid = ""
    # 同分钟并发/重试可能撞 id：UNIQUE 冲突换新 id 重写一次
    for _attempt in range(2):
        rid = _resolve_unique_id(_ID_PATTERN.format(ts=datetime.now()))
        md = _render(name, code, as_of, rid, analysis)
        md_path = get_report_dir() / f"{rid}.md"
        md_path.write_text(md, encoding="utf-8")
        record = {
            "id": rid, "stock_code": code, "stock_name": name,
            "md_path": str(md_path), "sector_score": analysis["sector_score"],
            "analysis_json": json.dumps(analysis, ensure_ascii=False, default=str),
        }
        try:
            from src.storage import get_db

            if get_db().save_sector_analysis_report(record):
                return {"report_id": rid, "stock_code": code, "stock_name": name,
                        "status": "success", "markdown": md, "analysis": analysis}
            break  # 非冲突失败（DB 不可用）不再重试
        except Exception as exc:  # noqa: BLE001
            logger.warning("[SectorAnalysis] 落库失败(尝试%d): %s", _attempt + 1, exc)
            if "UNIQUE" not in str(exc).upper():
                break
            md_path.unlink(missing_ok=True)
    return {"report_id": None, "stock_code": code, "stock_name": name,
            "status": "failed", "markdown": md, "analysis": analysis}


def _render(name: str, code: str, as_of: str, rid: str, a: Dict[str, Any]) -> str:
    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent.parent.parent / "templates"),
        autoescape=False, trim_blocks=True, lstrip_blocks=True,
    )
    md = env.get_template("sector_analysis_report.j2").render(
        stock_name=name, stock_code=code, as_of=as_of, report_id=rid, a=a,
    )
    # 排版后处理：盘古空格（中英间）+ 红色双保险，对齐 financial-analysis 口径
    md = re.sub(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", md)
    md = re.sub(r"(?<=[A-Za-z0-9%])(?=[\u4e00-\u9fff])", " ", md)
    return re.sub(r"🔴 \*\*([^*\n]+)\*\*", r'<font color="#e03131">🔴 **\1**</font>', md)
