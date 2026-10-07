# -*- coding: utf-8 -*-
"""深度投研报告编排服务（A股）。

职责（编排层，不含 LLM/数据获取细节）：
1. A 股代码校验与归一化（强制 cn 市场，拒绝港股/美股）。
2. 调用 :class:`DeepResearchExecutor` 生成报告（五层穿透 + 质量校验 + 降级）。
3. 报告存盘：Markdown 写文件（``reports/deep_research/``）+ 元数据写 SQLite
   （并发安全，替代易损坏的 index.json）。
4. 超额清理（删元数据同步删文件）。
5. 列表/详情/删除代理 + PDF 惰性生成入口。

不直接做 PDF 渲染（在 ``src/md2pdf.py``，P1 接入）；不直接跑 ReAct 循环（在 executor）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from src.config import get_config
from src.services.stock_code_utils import normalize_code
from src.storage import get_db

logger = logging.getLogger(__name__)

# ── 报告级日缓存（同一天内同一股票不重复生成报告，内容完全一致）────────────────
# key格式：{code}:{date}，date = YYYYMMDD，按自然日缓存，次日自动失效
_report_cache: Dict[str, Tuple[str, float, Dict[str, Any]]] = {}  # key→(report_id, ts, full_result)
_cache_lock = threading.Lock()


def _date_key(code: str, dims: Optional[List[str]] = None) -> str:
    """返回今日缓存 key（按自然日 + 维度子集指纹，同一天同子集复用同一报告）。

    dims 指纹：省钱模式下不同维度子集各自独立缓存，避免全量/子集报告互相串包。
    """
    dims_fp = ",".join(sorted(d for d in (dims or []) if d)) or "full"
    return f"{code}:{datetime.now().strftime('%Y%m%d')}:{dims_fp}"


# 报告产物目录：项目根/reports/deep_research/（对齐 notification.py 的 reports/ 约定）
_REPORTS_ROOT = Path(__file__).parent.parent.parent / "reports"
_DEEP_RESEARCH_DIR = _REPORTS_ROOT / "deep_research"

# report_id 格式：{6位A股代码}_{YYYYMMDDHHmm}（与下载白名单 ^\d{6}_\d{12}$ 对齐）
_REPORT_ID_PATTERN = "{code}_{ts:%Y%m%d%H%M}"

# executor 单例缓存（config 不常变；LLMToolAdapter/ToolRegistry 较重，避免每次重建）
_executor_instance: Optional[Any] = None

# 双轨引擎 LLM adapter 单例（同样较重）
_dual_track_adapter: Optional[Any] = None


def _get_dual_track_adapter() -> Any:
    """获取（缓存的）双轨引擎 LLM adapter。"""
    global _dual_track_adapter
    if _dual_track_adapter is None:
        from src.agent.llm_adapter import LLMToolAdapter

        _dual_track_adapter = LLMToolAdapter(get_config())
    return _dual_track_adapter


def get_deep_research_engine() -> str:
    """当前深度投研引擎：legacy | dual_track（默认 legacy，见 config_registry）。"""
    engine = str(getattr(get_config(), "deep_research_engine", "legacy") or "legacy")
    return engine if engine in ("legacy", "dual_track") else "legacy"


class DeepResearchInputError(ValueError):
    """输入校验错误（非 A 股、格式非法等），endpoint 转 HTTP 400。"""


def get_deep_research_dir() -> Path:
    """返回深度投研报告目录，确保存在（Docker volume 子目录首次写入需 mkdir）。"""
    _DEEP_RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    return _DEEP_RESEARCH_DIR


def _max_reports() -> int:
    """保留报告数量上限（默认 200，P1 从 config.deep_research_max_reports 读取）。"""
    try:
        return int(getattr(get_config(), "deep_research_max_reports", 200))
    except Exception:
        return 200


def _lookup_stock_name(code: str) -> str:
    """反查股票中文名（raw_name 缺省时用）。

    复用 ``get_realtime_quote``（深度投研本就会调用，这里仅多一次轻量查询）。
    失败返回空串，由调用方 fallback 到 code，不阻塞生成。
    """
    try:
        from src.agent.tools.data_tools import _get_fetcher_manager

        quote = _get_fetcher_manager().get_realtime_quote(code)
        if quote and quote.name:
            return str(quote.name).strip()
    except Exception as exc:
        logger.debug("[DeepResearch] 反查股票名称失败 %s: %s", code, exc)
    return ""


def _resolve_unique_report_id(base_id: str) -> str:
    """确保 report_id 唯一：若 base_id 已存在于 DB，追加 _1/_2/... 序号后缀。

    防同分钟同股票报告 id 冲突（save 用 merge 会覆盖旧记录，导致旧 .md/.pdf
    文件成孤儿）。白名单 ``^\\d{6}_\\d{12}(_\\d+)?$`` 允许该后缀。
    """
    report_id = base_id
    seq = 1
    while get_db().get_deep_research_report(report_id) is not None:
        report_id = f"{base_id}_{seq}"
        seq += 1
    return report_id


def _get_executor() -> Any:
    """获取（缓存的）DeepResearchExecutor 单例。"""
    global _executor_instance
    if _executor_instance is None:
        from src.agent.factory import build_deep_research_executor

        _executor_instance = build_deep_research_executor()
    return _executor_instance


def normalize_a_share(raw_code: str) -> str:
    """归一化并校验为 A 股代码。

    A 股 = 归一化后 6 位纯数字（沪深京：60xxxx/00xxxx/30xxxx/688xxx/920xxx/430xxx/83xxxx）。
    非 A 股抛 ``DeepResearchInputError``（endpoint 转为 HTTP 400）。
    """
    if not raw_code or not str(raw_code).strip():
        raise DeepResearchInputError("股票代码不能为空")

    normalized = normalize_code(str(raw_code).strip())
    if not normalized:
        raise DeepResearchInputError(f"无法识别的股票代码：{raw_code}")

    # A 股判定：6 位纯数字（HK 为 5 位，US 为字母）
    if not (normalized.isdigit() and len(normalized) == 6):
        raise DeepResearchInputError(
            f"深度投研报告当前仅支持 A 股，代码 {raw_code}（归一化为 {normalized}）非 A 股"
        )
    return normalized


class DeepResearchService:
    """深度投研报告编排服务（无状态，方法可独立调用）。"""

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------

    def generate_report(
        self,
        raw_code: str,
        raw_name: Optional[str] = None,
        report_type: str = "deep",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        dims: Optional[List[str]] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """生成一份深度投研报告并落盘。返回 {report_id, status, markdown, ...}。

        dims：维度子集（省钱模式），None/空 = 全部；force_refresh：跳过维度缓存。
        30min 内同一股票重复查询直接返回缓存（报告完全一致）。
        """
        code = normalize_a_share(raw_code)
        name = (raw_name or "").strip()
        if not name:
            # 前端未传名称时反查真实中文名，避免元数据 stock_name 退化成代码
            name = _lookup_stock_name(code) or code

        # ── 报告级日缓存查找（同一天内不重复生成，force_refresh 时跳过）──────────
        if not force_refresh:
            cache_key = _date_key(code, dims)
            now = time.time()
            with _cache_lock:
                cached = _report_cache.get(cache_key)
            if cached:
                cached_id, cached_ts, cached_result = cached
                logger.info(
                    "[DeepResearch] 日缓存命中 %s（%.0fs前生成），直接返回缓存报告",
                    code,
                    now - cached_ts,
                )
                # 通知前端这是缓存命中（thinking + done 都必须推，否则 SSE 端等不到 done 挂起）
                if progress_callback:
                    progress_callback({
                        "type": "thinking",
                        "step": 0,
                        "message": f"📦 日缓存命中（{int((now - cached_ts) / 60)}min前），直接返回今日报告",
                    })
                    done_event: Dict[str, Any] = {
                        "type": "done",
                        "report_id": cached_result.get("report_id"),
                        "status": cached_result.get("status"),
                        "quality_score": cached_result.get("quality_score"),
                        "missing_layers": cached_result.get("missing_layers") or [],
                        "markdown": cached_result.get("markdown") or "",
                        "error": None,
                        "cache_hit": True,
                    }
                    # 透传双轨增量字段（engine/dimensions/guardrail_events/dims_degraded）
                    for _k in ("engine", "dimensions", "guardrail_events", "dims_degraded", "final_conclusion"):
                        if _k in cached_result:
                            done_event[_k] = cached_result[_k]
                    progress_callback(done_event)
                # 追加 cache_hit 标记，前端据此显示"来自缓存"
                cached_result = dict(cached_result)
                cached_result["cache_hit"] = True
                return cached_result

        report_id = _REPORT_ID_PATTERN.format(code=code, ts=datetime.now())
        # 防同分钟同股票 id 冲突（save 用 merge 会覆盖旧记录导致文件孤儿）
        report_id = _resolve_unique_report_id(report_id)
        report_dir = get_deep_research_dir()
        md_path = report_dir / f"{report_id}.md"

        # 引擎分发：dual_track=双轨多维度；legacy=单循环（默认）
        dims_payload: Optional[Dict[str, Any]] = None
        guardrail_events: List[Any] = []
        if get_deep_research_engine() == "dual_track":
            _emit = progress_callback or (lambda _e: None)
            _emit({"type": "thinking", "step": 0, "message": "使用双轨多维度引擎生成..."})
            from src.agent.deep_research.orchestrator import run_dual_track

            dt_result = run_dual_track(
                stock_code=code,
                stock_name=name,
                llm_adapter=_get_dual_track_adapter(),
                progress_callback=progress_callback,
                explore_max_steps=int(
                    getattr(get_config(), "deep_research_dim_max_steps", 8) or 8
                ),
                dims_filter=set(dims) if dims else None,
                force_refresh=force_refresh,
                report_id=report_id,
            )
            result = SimpleNamespace(
                success=dt_result.success,
                status=dt_result.status,
                markdown=dt_result.markdown,
                quality_score=dt_result.quality_score,
                missing_layers=dt_result.degraded_dims,
                total_steps=dt_result.total_steps,
                total_tokens=dt_result.total_tokens,
                provider=dt_result.provider,
                error=dt_result.error,
                final_conclusion=dt_result.final_conclusion,
            )
            dims_payload = dt_result.dims_payload
            guardrail_events = [
                e.model_dump() for e in dt_result.guardrail_events
            ]
        else:
            executor = _get_executor()
            result = executor.generate(
                stock_code=code,
                stock_name=name,
                report_type=report_type,
                progress_callback=progress_callback,
            )

        # 写 Markdown 文件（即使 partial 也写，保证有产物）
        markdown = result.markdown or ""
        write_ok = False
        if markdown:
            try:
                md_path.write_text(markdown, encoding="utf-8")
                write_ok = True
            except OSError as exc:
                logger.error("[DeepResearch] 写报告文件失败 %s: %s", md_path, exc)

        # 双轨引擎：按维度切分子报告落盘（dim_<id>.md，单维度查看/下载用）
        if write_ok and dims_payload is not None:
            self._write_dim_reports(report_id, markdown, name, code)

        # 双轨引擎：维度 JSON 产物落盘（复算/钻取用）
        if write_ok and dims_payload is not None:
            dims_path = report_dir / f"{report_id}_dims.json"
            try:
                import json

                dims_path.write_text(
                    json.dumps(
                        {
                            "report_id": report_id,
                            "engine": "dual_track",
                            "guardrail_events": guardrail_events,
                            "dimensions": dims_payload,
                        },
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                    encoding="utf-8",
                )
            except (OSError, TypeError, ValueError) as exc:
                logger.warning("[DeepResearch] 写维度产物失败 %s: %s", dims_path, exc)

        # 写元数据到 SQLite（md_path 用绝对路径字符串）
        if write_ok:
            get_db().save_deep_research_report(
                report_id=report_id,
                stock_code=code,
                stock_name=name,
                md_path=str(md_path),
                status=result.status,
                quality_score=result.quality_score,
                missing_layers=result.missing_layers,
                total_steps=result.total_steps,
                total_tokens=result.total_tokens,
                provider=result.provider,
            )
            # 清理超额（事务内删元数据，事务外删文件）
            self._prune_and_clean_files(_max_reports())

        done_event: Dict[str, Any] = {
            "type": "done",
            "report_id": report_id if write_ok else None,
            "status": result.status,
            "quality_score": result.quality_score,
            "missing_layers": result.missing_layers,
            "markdown": markdown,
            "error": result.error if not result.success else None,
        }
        if dims_payload is not None:
            # 增量字段：旧前端可忽略（兼容 §7.3）
            done_event["engine"] = "dual_track"
            done_event["dimensions"] = dims_payload
            done_event["guardrail_events"] = guardrail_events
            done_event["dims_degraded"] = list(result.missing_layers)
            done_event["final_conclusion"] = getattr(result, "final_conclusion", "") or ""
        if progress_callback:
            progress_callback(done_event)

        return_dict: Dict[str, Any] = {
            "report_id": report_id if write_ok else None,
            "stock_code": code,
            "stock_name": name,
            "status": result.status,
            "quality_score": result.quality_score,
            "missing_layers": result.missing_layers,
            "markdown": markdown,
            "md_path": str(md_path) if write_ok else None,
            "total_steps": result.total_steps,
            "total_tokens": result.total_tokens,
            "provider": result.provider,
            "error": result.error if not result.success else None,
        }
        if dims_payload is not None:
            return_dict["engine"] = "dual_track"
            return_dict["dimensions"] = dims_payload
            return_dict["guardrail_events"] = guardrail_events
            # 终读结论必须进缓存 payload，否则日缓存命中路径的 done 事件会丢字段
            return_dict["final_conclusion"] = (
                getattr(result, "final_conclusion", "") or ""
            )

        # ── 写入报告日缓存（成功时，同一自然日内复用）────────────────────────
        if write_ok and return_dict.get("markdown"):
            cache_key = _date_key(code, dims)
            now = time.time()
            with _cache_lock:
                _report_cache[cache_key] = (report_id, now, return_dict)
            logger.info("[DeepResearch] 日缓存已写入 %s → %s", cache_key, report_id)

        return return_dict

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_reports(
        self, limit: int = 50, offset: int = 0, stock_code: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], int]:
        """分页列表（不含 Markdown 正文，仅元数据）。"""
        rows, total = get_db().get_deep_research_reports(
            stock_code=stock_code, offset=offset, limit=limit
        )
        return [r.to_dict() for r in rows], total

    def get_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        """单条报告详情（含 Markdown 正文，从文件读取）。"""
        record = get_db().get_deep_research_report(report_id)
        if record is None:
            return None
        data = record.to_dict()
        # 读取 Markdown 正文
        markdown = ""
        try:
            md_path = Path(record.md_path)
            if md_path.exists():
                markdown = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("[DeepResearch] 读取报告正文失败 %s: %s", record.md_path, exc)
        data["markdown"] = markdown
        return data

    def get_dims_payload(self, report_id: str) -> Optional[Dict[str, Any]]:
        """双轨维度 JSON 产物（legacy 报告无产物返回 None）。"""
        record = get_db().get_deep_research_report(report_id)
        if record is None:
            return None
        dims_path = get_deep_research_dir() / f"{report_id}_dims.json"
        try:
            if dims_path.exists():
                import json

                return cast(Dict[str, Any], json.loads(dims_path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            logger.warning("[DeepResearch] 读取维度产物失败 %s: %s", dims_path, exc)
        return None

    # ------------------------------------------------------------------
    # 按维度子报告（方向 A：dim_<id>.md 落盘/清理）
    # ------------------------------------------------------------------

    def _write_dim_reports(
        self, report_id: str, markdown: str, stock_name: str, stock_code: str
    ) -> None:
        """把整份报告按章节锚点切成 11 个维度子报告落盘。"""
        from src.deep_research_dims.render import build_dim_report, split_dim_sections

        sections = split_dim_sections(markdown)
        report_dir = get_deep_research_dir()
        for dim_id, section_md in sections.items():
            try:
                content = build_dim_report(
                    dim_id, section_md, stock_name, stock_code,
                    datetime.now().isoformat(timespec="seconds"),
                    report_id=report_id,
                )
                (report_dir / f"{report_id}_dim_{dim_id}.md").write_text(
                    content, encoding="utf-8"
                )
            except (OSError, KeyError) as exc:
                logger.warning(
                    "[DeepResearch] 写维度子报告失败 %s/%s: %s", report_id, dim_id, exc
                )

    def _dim_report_path(self, report_id: str, dim_id: str) -> Optional[Path]:
        """返回维度子报告路径（文件必须存在）。"""
        path = get_deep_research_dir() / f"{report_id}_dim_{dim_id}.md"
        return path if path.exists() else None

    def get_dim_report(
        self, report_id: str, dim_id: str
    ) -> Optional[Dict[str, Any]]:
        """维度子报告（含正文），供端点直接返回。"""
        path = self._dim_report_path(report_id, dim_id)
        if path is None:
            return None
        try:
            return {
                "dim": dim_id,
                "path": str(path),
                "markdown": path.read_text(encoding="utf-8"),
            }
        except OSError as exc:
            logger.warning("[DeepResearch] 读维度子报告失败 %s: %s", path, exc)
            return None

    @staticmethod
    def _remove_dim_artifacts(report_id: str) -> None:
        """删除维度产物（_dims.json + 全部 _dim_<id>.md）。"""
        report_dir = get_deep_research_dir()
        targets = [report_dir / f"{report_id}_dims.json"]
        targets.extend(report_dir.glob(f"{report_id}_dim_*.md"))
        for p in targets:
            try:
                p.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("[DeepResearch] 删除维度产物失败 %s: %s", p, exc)

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    def delete_report(self, report_id: str) -> bool:
        """删除报告（元数据 + .md + .pdf + 维度产物文件）。返回是否删除成功。"""
        paths = get_db().delete_deep_research_report(report_id)
        if paths is None:
            return False
        # 删文件（事务外，失败只记日志不影响元数据删除）
        for key in ("md_path", "pdf_path"):
            p = paths.get(key)
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("[DeepResearch] 删除文件失败 %s: %s", p, exc)
        self._remove_dim_artifacts(report_id)
        logger.info("[DeepResearch] 已删除报告 %s", report_id)
        return True

    # ------------------------------------------------------------------
    # PDF（P1 接入 md2pdf 后实现）
    # ------------------------------------------------------------------

    def get_pdf_path(self, report_id: str) -> Optional[str]:
        """返回报告 PDF 路径。若未生成则触发惰性生成（P1 实现）。"""
        record = get_db().get_deep_research_report(report_id)
        if record is None:
            return None
        if record.pdf_path:
            # 已生成，校验文件存在
            if Path(record.pdf_path).exists():
                return record.pdf_path
        # 惰性生成（P1 实现，当前返回 None）
        return self._generate_pdf(record)

    def _generate_pdf(self, record: Any) -> Optional[str]:
        """惰性生成 PDF（P1 接入 src/md2pdf.py）。"""
        try:
            from src.md2pdf import markdown_to_pdf_file
        except ImportError:
            logger.warning("[DeepResearch] md2pdf 未就绪（P1），PDF 暂不可用")
            return None

        md_path = Path(record.md_path)
        if not md_path.exists():
            return None
        markdown = md_path.read_text(encoding="utf-8")
        pdf_path = str(md_path.with_suffix(".pdf"))

        result_path = markdown_to_pdf_file(markdown, pdf_path)
        if result_path:
            get_db().set_deep_research_pdf_path(record.id, result_path)
            return result_path
        return None

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def _prune_and_clean_files(self, max_reports: int) -> None:
        """清理超额报告：删元数据（事务内）+ 删文件（事务外，含维度产物）。"""
        if max_reports <= 0:
            return
        pruned = get_db().prune_deep_research_reports(max_reports)
        for paths in pruned:
            for key in ("md_path", "pdf_path"):
                p = paths.get(key)
                if p:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except OSError as exc:
                        logger.warning("[DeepResearch] 清理删除文件失败 %s: %s", p, exc)
            md = paths.get("md_path")
            if md:
                self._remove_dim_artifacts(Path(str(md)).stem)
        if pruned:
            logger.info("[DeepResearch] 清理超额报告 %d 份", len(pruned))


# 模块级单例（与现有 service 风格一致）
deep_research_service = DeepResearchService()
