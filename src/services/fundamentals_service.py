# -*- coding: utf-8 -*-
"""基本面分析模块：复用 business/sector/financial 三研究员出专项报告（决策3：同步生成）。

与深度投研同源：研究员 Skill 直接复用 + dim_cache 快照红利（24h TTL）。
报告落盘 reports/fundamentals/ + SQLite fundamentals_reports。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4
from time import monotonic
from pydantic import ValidationError

logger = logging.getLogger(__name__)

_REPORT_DIR = Path(__file__).parent.parent.parent / "reports" / "fundamentals"
_ID_PATTERN = "fd_{ts:%Y%m%d%H%M}"


def get_fundamentals_dir() -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return _REPORT_DIR


def _resolve_unique_id(base: str) -> str:
    # Numeric suffix preserves the existing report-ID API and rollback readers.
    return f"{base}_{uuid4().int}"


def _rule_dim(dim_id: str, code: str, name: str, ctx: Any, position_text: str = "") -> Dict[str, Any]:
    """规则维度（sector/fundamental）：走各自构建器（修复注册表 KeyError  bug）。

    sector 携带产业链/业务定位文本做基率二次匹配；fundamental 吃基本面快照。
    """
    try:
        if dim_id == "sector":
            from src.deep_research_dims.sector_dim import build_sector_dim

            dim = build_sector_dim(ctx, position_text or None)
            return dim.model_dump()
        if dim_id == "fundamental":
            from src.deep_research_dims.fundamental_dim import build_fundamental_dim

            return build_fundamental_dim(ctx).model_dump()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Fundamentals] 规则维度 %s 失败: %s", dim_id, exc)
    return {"dim": dim_id, "status": "degraded", "degraded_reason": "维度构建失败"}


def _position_text(business_payload: Dict[str, Any]) -> str:
    """从 business 产出提取定位文本（供 sector 基率二次匹配）。"""
    parts = []
    for key in ("competitive_position", "business_model"):
        value = (business_payload or {}).get(key)
        if isinstance(value, dict):
            parts.extend(str(v) for v in value.values() if isinstance(v, str))
        elif isinstance(value, str):
            parts.append(value)
    return " ".join(parts)[:400]


def _research_dim(dim_id: str, code: str, name: str, *, deadline: Optional[float] = None, force_refresh: bool = False, research: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """跑一个研究员；失败时保留供应商事实，降级章节不写成功缓存。"""
    from src.deep_research.researchers import REGISTRY, RUNNER_NAMES
    import src.agent.deep_research.orchestrator as orch

    from src.deep_research_dims.dim_cache import load_cached_dim, save_cached_dim

    cached = None if force_refresh else load_cached_dim(code, dim_id)
    if cached is not None:
        # 契约校验：旧 schema 缓存值（如 str 时代的 Dict 字段）非法即 miss 重算
        try:
            from src.schemas.deep_research_dims import parse_dim

            parse_dim(dim_id, cached)
            return cached
        except Exception:  # noqa: BLE001
            pass
    reason = "研究员不可用"
    vendor_research = dict(research or {})
    try:
        runner = getattr(orch, RUNNER_NAMES[dim_id])
        from src.services.deep_research_service import _get_dual_track_adapter
        out = runner(code, name, _get_dual_track_adapter(), None, 8, deadline=deadline, **({"research": research} if dim_id == "business" else {}))
        vendor_research.update(out.get("vendor_research") or {})
        if out.get("ok"):
            dim, _ = REGISTRY[dim_id].parser(out["data"], int(out.get("steps") or 0))
            dim = orch.restore_vendor_facts(dim_id, dim, vendor_research)
            payload = dim.model_dump()
            if dim.status == "ok":
                save_cached_dim(code, dim_id, payload)
            return payload
        error = str(out.get("error") or "")
        reason = "模型未成功完成研究，业务画像未完成"
        for marker, explanation in (("时限", "研究时限耗尽"), ("length", "模型输出被截断"),
                                    ("content_filter", "模型输出被服务商过滤"), ("JSON", "模型输出格式无效")):
            if marker in error:
                reason = explanation + "，业务画像未完成"
                break
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Fundamentals] 研究员 %s 失败: %s", dim_id, exc)
        reason = "研究内容未通过格式校验，业务画像未完成" if isinstance(exc, ValidationError) else "研究员未成功完成，业务画像未完成"
    from src.schemas.deep_research_dims import DIM_MODELS
    dim = orch.restore_vendor_facts(dim_id, DIM_MODELS[dim_id](status="degraded", degraded_reason=reason), vendor_research)
    return dim.model_dump()


def _same_day_dedup_report(code: str) -> Optional[Dict[str, Any]]:
    """当天同代码已生成过报告则返回已有记录，避免重复生成。"""
    from datetime import date
    today = date.today().isoformat()[:10]  # "2026-10-05"
    try:
        from src.storage import get_db
        db = get_db()
        rows, _ = db.list_fundamentals_reports(code, limit=10, offset=0)
        for r in rows:
            created = str(r.get("created_at") or "")
            if created.startswith(today):
                # 列表只返回少量字段，再查一次拿完整字段
                full = db.get_fundamentals_report(r["id"])
                if full:
                    from src.config import get_config
                    contract = get_config().data_source_contract
                    if contract != "legacy" and json.loads(full.get("dims_json") or "{}").get("_data_contract") != contract:
                        continue
                    dims = json.loads(full.get("dims_json") or "{}")
                    if contract != "legacy" and (dims.get("business") or {}).get("status") == "degraded":
                        continue
                    financial = dims.get("financial") or {}
                    if contract != "legacy" and any(field.get("source_errors") for field in (financial.get("financial_snapshot") or {}).values()):
                        continue
                    if ((dims.get("business") or {}).get("product_segments") or {}).get("errors"):
                        continue
                    return full
    except Exception:  # noqa: BLE001
        pass
    return None


def report_markdown(record: Dict[str, Any]) -> str:
    """Read historical files without rewriting them; legacy quality remains unknown."""
    try:
        markdown = Path(record["md_path"]).read_text(encoding="utf-8")
    except OSError:
        markdown = ""
    dims = json.loads(record.get("dims_json") or "{}")
    # Re-present old structured snapshots without recomputing or rewriting them.
    snapshot_fields = {
        "business": {"business_model", "main_products", "competitive_position", "vs_market_leader", "narrative", "data_gaps"},
        "sector": {"narrative", "data_gaps"},
        "financial": {"profitability", "growth_quality", "valuation_detail", "narrative", "data_gaps"},
    }
    report_date = re.search(r"(?m)^> 报告日期：(.+?) ｜", markdown)
    if ('<font color="#e03131">' in markdown and report_date
            and all(isinstance(dims.get(key), dict) and fields <= dims[key].keys()
                    for key, fields in snapshot_fields.items())
            and any(dims["business"].get(key) for key in snapshot_fields["business"])):
        markdown = _render_markdown(record["stock_name"], record["stock_code"],
                                    report_date.group(1), dims, record["id"])
    if not (dims.get("financial") or {}).get("data_quality"):
        markdown = "> 财务数据未核验：历史报告没有字段质量证据，不能认定已验证。\n\n" + markdown
    return markdown


def generate_fundamentals_report(raw_code: str, raw_name: Optional[str] = None, force_refresh: bool = False) -> Dict[str, Any]:
    """生成基本面专项报告（经营模式/主营产品/行业地位/龙头与大盘对比 + 财务体检）。"""
    from src.config import get_config
    deadline = monotonic() + get_config().fundamentals_report_timeout_seconds

    # 归一化：兼容 300260.SZ / SH600519 / 文本中夹代码 等输入（与深度投研 normalize_a_share 同口径）
    from src.services.stock_code_utils import normalize_code

    raw = str(raw_code or "").strip()
    code = normalize_code(raw) if raw else ""
    if not code or not code.isdigit() or len(code) != 6:
        raise ValueError(f"仅支持 A 股 6 位代码：{raw_code}")
    created_at = datetime.now()
    name = (raw_name or "").strip()
    # 名称里夹带代码后缀时剥掉（如"新莱应材 300260.SZ"）
    if name and code in name:
        name = name.replace(code, "").replace(".SZ", "").replace(".SH", "").strip()
    name = name or code

    # 当天已生成过则直接返回已有报告，避免重复
    existing = None if force_refresh else _same_day_dedup_report(code)
    if existing is not None:
        md = report_markdown(existing)
        return {
            "report_id": existing["id"],
            "stock_code": code,
            "stock_name": existing.get("stock_name") or name,
            "status": "already_exists",
            "markdown": md,
            "dims": json.loads(existing.get("dims_json") or "{}"),
        }

    from src.deep_research_dims.context import build_shared_context

    from data_provider.mx_data_adapter import financial_refresh
    token = financial_refresh.set((code, uuid4().hex) if force_refresh else None)
    try:
        ctx = build_shared_context(code, name, deadline=deadline)
        if name == code and ctx.quote.get("name"):
            name = str(ctx.quote["name"])
            ctx.stock_name = name
        business = _research_dim("business", code, name, deadline=deadline, force_refresh=force_refresh, research=ctx.fundamental.get("research"))
    finally:
        financial_refresh.reset(token)
    dims = {
        "business": business,
        "sector": _rule_dim("sector", code, name, ctx, _position_text(business)),
        "financial": _rule_dim("fundamental", code, name, ctx),
    }
    from src.config import get_config
    dims["_data_contract"] = get_config().data_source_contract
    as_of = datetime.now().isoformat(timespec="seconds")
    rid = _resolve_unique_id(_ID_PATTERN.format(ts=created_at))
    md = _render_markdown(name, code, as_of, dims, rid)
    md_path = get_fundamentals_dir() / f"{rid}.md"
    md_path.write_text(md, encoding="utf-8")
    ok = False
    try:
        from src.storage import get_db

        ok = get_db().save_fundamentals_report(
            {
                "id": rid, "stock_code": code, "stock_name": name,
                "md_path": str(md_path),
                "dims_json": json.dumps(dims, ensure_ascii=False, default=str),
                "created_at": created_at,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Fundamentals] 落库失败: %s", exc)
    if not ok:
        md_path.unlink(missing_ok=True)
    return {
        "report_id": rid if ok else None,
        "stock_code": code, "stock_name": name,
        "status": ("partial" if any(dim.get("status") != "ok" for key, dim in dims.items() if not key.startswith("_")) else "success") if ok else "failed",
        "markdown": md, "dims": dims,
    }


def _pangu_spacing(text: str) -> str:
    """盘古之白：汉字与英文/数字之间补空格（表格行/链接行跳过，防断格式）。"""
    import re

    out_lines = []
    for line in text.split("\n"):
        if line.lstrip().startswith("|") or "](" in line:
            out_lines.append(line)
            continue
        line = re.sub(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z0-9])", " ", line)
        line = re.sub(r"(?<=[A-Za-z0-9%])(?=[\u4e00-\u9fff])", " ", line)
        out_lines.append(line)
    return "\n".join(out_lines)


def _render_markdown(name: str, code: str, as_of: str, dims: Dict[str, Any], report_id: str = "") -> str:
    from jinja2 import Environment, FileSystemLoader
    from functools import partial
    from src.deep_research_dims.render import KEY_ZH_MAP, _format_cn

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent.parent.parent / "templates"),
        autoescape=False, trim_blocks=True, lstrip_blocks=True,
    )
    env.filters["cn"] = partial(_format_cn, labels={
        **KEY_ZH_MAP, "value": "说明", "industry_rank": "行业地位", "demand_backdrop": "需求背景",
    })
    md = env.get_template("fundamentals_report.j2").render(
        stock_name=name, stock_code=code, as_of=as_of, report_id=report_id,
        business=dims.get("business") or {}, sector=dims.get("sector") or {},
        financial=dims.get("financial") or {},
        degraded_note=_degraded_note,
    )
    return _pangu_spacing(md)


def _degraded_note(dim: Dict[str, Any]) -> str:
    if (dim or {}).get("status") == "degraded":
        return f"> ⚠️ 本节生成不充分：{dim.get('degraded_reason') or '未知原因'}\n\n"
    return ""
