# -*- coding: utf-8 -*-
"""基本面分析模块：复用 business/sector/financial 三研究员出专项报告（决策3：同步生成）。

与深度投研同源：研究员 Skill 直接复用 + dim_cache 快照红利（24h TTL）。
报告落盘 reports/fundamentals/ + SQLite fundamentals_reports。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, cast

logger = logging.getLogger(__name__)

_REPORT_DIR = Path(__file__).parent.parent.parent / "reports" / "fundamentals"
_ID_PATTERN = "fd_{ts:%Y%m%d%H%M}"


def get_fundamentals_dir() -> Path:
    _REPORT_DIR.mkdir(parents=True, exist_ok=True)
    return _REPORT_DIR


def _resolve_unique_id(base: str) -> str:
    from src.storage import get_db

    rid = base
    seq = 1
    while get_db().get_fundamentals_report(rid) is not None:
        rid = f"{base}_{seq}"
        seq += 1
    return rid


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


def _research_dim(dim_id: str, code: str, name: str) -> Dict[str, Any]:
    """跑一个研究员（带维度缓存），失败返回降级占位。"""
    from src.deep_research.researchers import REGISTRY, RUNNER_NAMES
    import src.agent.deep_research.orchestrator as orch

    from src.deep_research_dims.dim_cache import load_cached_dim, save_cached_dim

    cached = load_cached_dim(code, dim_id)
    if cached is not None:
        # 契约校验：旧 schema 缓存值（如 str 时代的 Dict 字段）非法即 miss 重算
        try:
            from src.schemas.deep_research_dims import parse_dim

            parse_dim(dim_id, cached)
            return cached
        except Exception:  # noqa: BLE001
            pass
    try:
        runner = getattr(orch, RUNNER_NAMES[dim_id])
        out = runner(code, name, orch._get_dual_track_adapter() if hasattr(orch, "_get_dual_track_adapter") else __import__("src.services.deep_research_service", fromlist=["_get_dual_track_adapter"])._get_dual_track_adapter(), None, 8)
        if out.get("ok"):
            dim, _ = REGISTRY[dim_id].parser(out["data"], int(out.get("steps") or 0))
            payload = dim.model_dump()
            if dim.status == "ok":
                save_cached_dim(code, dim_id, payload)
            return cast(Dict[str, Any], payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Fundamentals] 研究员 %s 失败: %s", dim_id, exc)
    return cast(Dict[str, Any], {"dim": dim_id, "status": "degraded", "degraded_reason": "研究员不可用"})


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
                    return full
    except Exception:  # noqa: BLE001
        pass
    return None


def generate_fundamentals_report(raw_code: str, raw_name: Optional[str] = None) -> Dict[str, Any]:
    """生成基本面专项报告（经营模式/主营产品/行业地位/龙头与大盘对比 + 财务体检）。"""
    from src.agent.tools.data_tools import _get_fetcher_manager

    # 归一化：兼容 300260.SZ / SH600519 / 文本中夹代码 等输入（与深度投研 normalize_a_share 同口径）
    from src.services.stock_code_utils import normalize_code

    raw = str(raw_code or "").strip()
    code = normalize_code(raw) if raw else ""
    if not code or not code.isdigit() or len(code) != 6:
        raise ValueError(f"仅支持 A 股 6 位代码：{raw_code}")
    name = (raw_name or "").strip()
    # 名称里夹带代码后缀时剥掉（如"新莱应材 300260.SZ"）
    if name and code in name:
        name = name.replace(code, "").replace(".SZ", "").replace(".SH", "").strip()
    if not name:
        try:
            quote = _get_fetcher_manager().get_realtime_quote(code)
            name = str(getattr(quote, "name", "") or "").strip() or code
        except Exception:  # noqa: BLE001
            name = code

    # 当天已生成过则直接返回已有报告，避免重复
    existing = _same_day_dedup_report(code)
    if existing is not None:
        try:
            md = Path(existing["md_path"]).read_text(encoding="utf-8")
        except OSError:
            md = ""
        return {
            "report_id": existing["id"],
            "stock_code": code,
            "stock_name": existing.get("stock_name") or name,
            "status": "already_exists",
            "markdown": md,
            "dims": json.loads(existing.get("dims_json") or "{}"),
        }

    from src.deep_research_dims.context import build_shared_context

    ctx = build_shared_context(code, name)
    business = _research_dim("business", code, name)
    dims = {
        "business": business,
        "sector": _rule_dim("sector", code, name, ctx, _position_text(business)),
        "financial": _rule_dim("fundamental", code, name, ctx),
    }
    as_of = datetime.now().isoformat(timespec="seconds")
    rid = _resolve_unique_id(_ID_PATTERN.format(ts=datetime.now()))
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
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Fundamentals] 落库失败: %s", exc)
    return {
        "report_id": rid if ok else None,
        "stock_code": code, "stock_name": name,
        "status": "success" if ok else "failed",
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

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent.parent.parent / "templates"),
        autoescape=False, trim_blocks=True, lstrip_blocks=True,
    )
    md = env.get_template("fundamentals_report.j2").render(
        stock_name=name, stock_code=code, as_of=as_of, report_id=report_id,
        business=dims.get("business") or {}, sector=dims.get("sector") or {},
        financial=dims.get("financial") or {},
        degraded_note=_degraded_note,
    )
    md = _pangu_spacing(md)
    # 红色双保险：🔴 加粗短语外包 <font color>（跨端显红，前缀保底）
    import re as _re

    return _re.sub(
        r"🔴 \*\*([^*\n]+)\*\*",
        r'<font color="#e03131">🔴 **\1**</font>',
        md,
    )


def _degraded_note(dim: Dict[str, Any]) -> str:
    if (dim or {}).get("status") == "degraded":
        return f"> ⚠️ 本节生成不充分：{dim.get('degraded_reason') or '未知原因'}\n\n"
    return ""
