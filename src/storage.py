# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 存储层
===================================

职责：
1. 管理 SQLite 数据库连接（单例模式）
2. 定义 ORM 数据模型
3. 提供数据存取接口
4. 实现智能更新逻辑（断点续传）
"""

import atexit
from contextlib import contextmanager
import hashlib
import json
import logging
import threading
import time
from datetime import date as date_type, datetime, timedelta, timezone
from typing import (
    Optional,
    List,
    Dict,
    Any,
    TYPE_CHECKING,
    Tuple,
    Callable,
    TypeVar,
    Union,
    cast,
)

import pandas as pd
from sqlalchemy import (
    create_engine,
    Column,
    String,
    Float,
    Boolean,
    Date,
    DateTime,
    Integer,
    ForeignKey,
    Index,
    UniqueConstraint,
    Text,
    select,
    and_,
    or_,
    delete,
    asc,
    desc,
    event,
    func,
    inspect,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
    Session,
)
from sqlalchemy.exc import IntegrityError, OperationalError

from src.agent.provider_trace import PROVIDER_TRACE_RETENTION_LIMIT
from src.config import get_config
from src.utils.sniper_points import extract_sniper_points, parse_sniper_value

logger = logging.getLogger(__name__)
T = TypeVar("T")
CURRENT_SCHEMA_VERSION = "2026-06-05-create-all-baseline"


# SQLAlchemy ORM 基类
class Base(DeclarativeBase):
    pass


if TYPE_CHECKING:
    from src.search_service import SearchResponse


def utc_naive_now() -> datetime:
    """Return current UTC time without tzinfo for SQLite DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive_datetime(value: datetime) -> datetime:
    """Normalize aware datetimes to UTC-naive; treat naive values as UTC-naive."""
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


# === 数据模型定义 ===


class DatabaseSchemaMigration(Base):
    """Applied database schema version marker."""

    __tablename__ = "schema_migrations"

    version = Column(String(64), primary_key=True)
    description = Column(String(255), nullable=False)
    applied_at = Column(DateTime, default=datetime.now, nullable=False, index=True)


class StockDaily(Base):
    """
    股票日线数据模型

    存储每日行情数据和计算的技术指标
    支持多股票、多日期的唯一约束
    """

    __tablename__ = "stock_daily"

    # 主键
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 股票代码（如 600519, 000001）
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)

    # 交易日期
    date: Mapped[date_type] = mapped_column("date", Date, nullable=False, index=True)

    # OHLC 数据
    open: Mapped[Optional[float]] = mapped_column(Float)
    high: Mapped[Optional[float]] = mapped_column(Float)
    low: Mapped[Optional[float]] = mapped_column(Float)
    close: Mapped[Optional[float]] = mapped_column(Float)

    # 成交数据
    volume: Mapped[Optional[float]] = mapped_column(Float)  # 成交量（股）
    amount: Mapped[Optional[float]] = mapped_column(Float)  # 成交额（元）
    pct_chg: Mapped[Optional[float]] = mapped_column(Float)  # 涨跌幅（%）

    # 技术指标
    ma5: Mapped[Optional[float]] = mapped_column(Float)
    ma10: Mapped[Optional[float]] = mapped_column(Float)
    ma20: Mapped[Optional[float]] = mapped_column(Float)
    volume_ratio: Mapped[Optional[float]] = mapped_column(Float)  # 量比

    # 数据来源
    data_source: Mapped[Optional[str]] = mapped_column(
        String(50)
    )  # 记录数据来源（如 AkshareFetcher）

    # 更新时间
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now
    )

    # 唯一约束：同一股票同一日期只能有一条数据
    __table_args__ = (
        UniqueConstraint("code", "date", name="uix_code_date"),
        Index("ix_code_date", "code", "date"),
    )

    def __repr__(self):
        return f"<StockDaily(code={self.code}, date={self.date}, close={self.close})>"

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "code": self.code,
            "date": self.date,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
            "pct_chg": self.pct_chg,
            "ma5": self.ma5,
            "ma10": self.ma10,
            "ma20": self.ma20,
            "volume_ratio": self.volume_ratio,
            "data_source": self.data_source,
        }


class NewsIntel(Base):
    """
    新闻情报数据模型

    存储搜索到的新闻情报条目，用于后续分析与查询
    """

    __tablename__ = "news_intel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 关联用户查询操作
    query_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # 股票信息
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(50))

    # 搜索上下文
    dimension: Mapped[Optional[str]] = mapped_column(
        String(32), index=True
    )  # latest_news / risk_check / earnings / market_analysis / industry
    # P3 信息源分层（按 docs/deep-research-chain-news-logic-plan.md §信息源策略）：
    # official / news / industry / community_cn / community_global
    # 老记录 NULL 表示历史 primary/news 入库，不强制 backfill。
    source_type: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    # 可靠性提示：high / medium / low / unverified。社区源固定 low。
    reliability_hint: Mapped[Optional[str]] = mapped_column(String(16), index=True)
    query: Mapped[Optional[str]] = mapped_column(String(255))
    provider: Mapped[Optional[str]] = mapped_column(String(32), index=True)

    # 新闻内容
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    snippet: Mapped[Optional[str]] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(100))
    published_date: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)

    # 入库时间
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )
    query_source: Mapped[Optional[str]] = mapped_column(
        String(32), index=True
    )  # bot/web/cli/system
    requester_platform: Mapped[Optional[str]] = mapped_column(String(20))
    requester_user_id: Mapped[Optional[str]] = mapped_column(String(64))
    requester_user_name: Mapped[Optional[str]] = mapped_column(String(64))
    requester_chat_id: Mapped[Optional[str]] = mapped_column(String(64))
    requester_message_id: Mapped[Optional[str]] = mapped_column(String(64))
    requester_query: Mapped[Optional[str]] = mapped_column(String(255))

    __table_args__ = (
        UniqueConstraint("url", name="uix_news_url"),
        Index("ix_news_code_pub", "code", "published_date"),
    )

    def __repr__(self) -> str:
        return f"<NewsIntel(code={self.code}, title={self.title[:20]}...)>"


class FundamentalSnapshot(Base):
    """
    基本面上下文快照（P0 write-only）。

    仅用于写入，主链路不依赖读取该表，便于后续回测/画像扩展。
    """

    __tablename__ = "fundamental_snapshot"

    id = Column(Integer, primary_key=True, autoincrement=True)
    query_id = Column(String(64), nullable=False, index=True)
    code = Column(String(10), nullable=False, index=True)
    payload = Column(Text, nullable=False)
    source_chain = Column(Text)
    coverage = Column(Text)
    # Date the snapshot's data is anchored to (``YYYY-MM-DD``). NULL when
    # the source did not report an explicit period (legacy rows). The
    # staleness detector in ``data_provider.base`` reads this column
    # before falling back to the in-payload text year scan.
    as_of_date = Column(String(10), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index("ix_fundamental_snapshot_query_code", "query_id", "code"),
        Index("ix_fundamental_snapshot_created", "created_at"),
        Index("ix_fundamental_snapshot_as_of", "as_of_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<FundamentalSnapshot(query_id={self.query_id}, "
            f"code={self.code}, as_of_date={self.as_of_date})>"
        )


class AnalysisHistory(Base):
    """
    分析结果历史记录模型

    保存每次分析结果，支持按 query_id/股票代码检索
    """

    __tablename__ = "analysis_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 关联查询链路
    query_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # 股票信息
    code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(50))
    report_type: Mapped[Optional[str]] = mapped_column(String(16), index=True)

    # 核心结论
    sentiment_score: Mapped[Optional[int]] = mapped_column(Integer)
    operation_advice: Mapped[Optional[str]] = mapped_column(String(20))
    trend_prediction: Mapped[Optional[str]] = mapped_column(String(50))
    analysis_summary: Mapped[Optional[str]] = mapped_column(Text)

    # 详细数据
    raw_result: Mapped[Optional[str]] = mapped_column(Text)
    news_content: Mapped[Optional[str]] = mapped_column(Text)
    context_snapshot: Mapped[Optional[str]] = mapped_column(Text)

    # 狙击点位（用于回测）
    ideal_buy: Mapped[Optional[float]] = mapped_column(Float)
    secondary_buy: Mapped[Optional[float]] = mapped_column(Float)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float)
    take_profit: Mapped[Optional[float]] = mapped_column(Float)

    # 长线投研框架（P1 新增）
    research_framework: Mapped[Optional[str]] = mapped_column(Text)

    # 五段式长线投研报告（P1-P2 新增）
    bayesian_framework: Mapped[Optional[str]] = mapped_column(Text)
    supply_chain: Mapped[Optional[str]] = mapped_column(Text)
    value_scenarios: Mapped[Optional[str]] = mapped_column(Text)
    investment_conclusion: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )

    __table_args__ = (Index("ix_analysis_code_time", "code", "created_at"),)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "query_id": self.query_id,
            "code": self.code,
            "name": self.name,
            "report_type": self.report_type,
            "sentiment_score": self.sentiment_score,
            "operation_advice": self.operation_advice,
            "trend_prediction": self.trend_prediction,
            "analysis_summary": self.analysis_summary,
            "raw_result": self.raw_result,
            "news_content": self.news_content,
            "context_snapshot": self.context_snapshot,
            "ideal_buy": self.ideal_buy,
            "secondary_buy": self.secondary_buy,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "research_framework": self.research_framework,
            "bayesian_framework": self.bayesian_framework,
            "supply_chain": self.supply_chain,
            "value_scenarios": self.value_scenarios,
            "investment_conclusion": self.investment_conclusion,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class FundamentalsReport(Base):
    """基本面分析报告（专项模块）：复用 business/sector/financial 研究员产出。"""

    __tablename__ = "fundamentals_reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # fd_{YYYYMMDDHHmm}(_seq)?
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    stock_name: Mapped[Optional[str]] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    md_path: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_path: Mapped[Optional[str]] = mapped_column(Text)
    dims_json: Mapped[Optional[str]] = mapped_column(Text)  # 三维度 payload 快照


class FinancialAnalysisReport(Base):
    """个股财务分析专项报告。"""

    __tablename__ = "financial_analysis_reports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # fa_{YYYYMMDDHHmm}
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    stock_name: Mapped[Optional[str]] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    md_path: Mapped[str] = mapped_column(Text, nullable=False)
    health_score: Mapped[Optional[float]] = mapped_column(Float)
    analysis_json: Mapped[Optional[str]] = mapped_column(Text)


class RecommendationJournal(Base):
    """操作指令日志（方案 v2.1 自反思雏形）：每次报告的买卖指令结构化留痕。

    模拟盘回放（scripts/replay_journals.py）据此撮合成交并统计兑现率/盈亏。
    """

    __tablename__ = "recommendation_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    stock_name: Mapped[Optional[str]] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # 建仓/加仓/持有/减仓/止损/观察
    rating: Mapped[Optional[str]] = mapped_column(String(8))  # 买入/增持/中性/减持
    score: Mapped[Optional[float]] = mapped_column(Float)  # v2 总评分
    ideal_buy: Mapped[Optional[float]] = mapped_column(Float)
    sell_price: Mapped[Optional[float]] = mapped_column(Float)
    stop_loss: Mapped[Optional[float]] = mapped_column(Float)
    take_profit: Mapped[Optional[float]] = mapped_column(Float)
    report_id: Mapped[Optional[str]] = mapped_column(String(64))
    scoring_version: Mapped[Optional[str]] = mapped_column(String(16))


class ScoreJournal(Base):
    """评分快照日志（自反思雏形）：每次报告的六维评分明细留痕。

    回放脚本据此计算评分与未来收益相关性（样本 <30 不出结论）。
    """

    __tablename__ = "score_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    dimensions_json: Mapped[str] = mapped_column(Text, nullable=False)  # 六维+指标明细
    scoring_version: Mapped[Optional[str]] = mapped_column(String(16))
    report_id: Mapped[Optional[str]] = mapped_column(String(64))

# === journal 模型结束（勿在此后追加字段，需走 schema 迁移） ===


class DeepResearchReport(Base):
    """深度投研报告记录模型（A股深度投研报告功能）。

    与 ``AnalysisHistory`` 隔离：深度研报是独立产物（券商风格 Markdown 报告），
    正文存文件（``reports/deep_research/``），元数据存本表，支持并发安全的
    列表/详情/删除（SQLite + _run_write_transaction 重试，替代易损坏的 index.json）。
    """

    __tablename__ = "deep_research_reports"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )  # {6位code}_{YYYYMMDDHHmm}

    # 股票信息
    stock_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    stock_name: Mapped[Optional[str]] = mapped_column(String(50))

    # 时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )

    # 文件路径
    md_path: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_path: Mapped[Optional[str]] = mapped_column(Text)  # NULL = 尚未惰性生成

    # 质量
    status: Mapped[Optional[str]] = mapped_column(
        String(16)
    )  # success | partial | failed
    quality_score: Mapped[Optional[int]] = mapped_column(Integer)
    missing_layers: Mapped[Optional[str]] = mapped_column(Text)  # JSON array

    # 统计
    total_steps: Mapped[Optional[int]] = mapped_column(Integer)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    provider: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (Index("ix_deep_research_code_time", "stock_code", "created_at"),)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（missing_layers 反序列化为 list）。"""
        return {
            "id": self.id,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "md_path": self.md_path,
            "pdf_path": self.pdf_path,
            "status": self.status,
            "quality_score": self.quality_score,
            "missing_layers": json.loads(self.missing_layers)
            if self.missing_layers
            else [],
            "total_steps": self.total_steps,
            "total_tokens": self.total_tokens,
            "provider": self.provider,
        }


class PolicyMinesweeperReport(Base):
    """政策与公告双维度排雷报告记录模型。

    与 ``DeepResearchReport`` 同理：正文存文件（``reports/policy_minesweeper/``），
    元数据存本表，支持并发安全的列表/详情/删除（SQLite + _run_write_transaction 重试）。
    composite_score/verdict/confidence 为 best-effort 从报告正文解析（可能为 NULL）。
    """

    __tablename__ = "policy_minesweeper_reports"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )  # {6位code}_{YYYYMMDDHHmm}(_\d+)?

    stock_code: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    stock_name: Mapped[Optional[str]] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )

    md_path: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_path: Mapped[Optional[str]] = mapped_column(Text)  # NULL = 尚未惰性生成

    status: Mapped[Optional[str]] = mapped_column(
        String(16)
    )  # success | partial | failed
    horizon: Mapped[Optional[str]] = mapped_column(String(16))  # short | medium | long
    alpha_ok: Mapped[Optional[bool]] = mapped_column(Boolean)
    beta_ok: Mapped[Optional[bool]] = mapped_column(Boolean)
    omega_ok: Mapped[Optional[bool]] = mapped_column(Boolean)

    # best-effort 从正文解析（scorecard banner）；解析不到则为 NULL
    composite_score: Mapped[Optional[int]] = mapped_column(Integer)  # -100~100
    verdict: Mapped[Optional[str]] = mapped_column(String(32))  # 如 "中等利空"
    confidence: Mapped[Optional[int]] = mapped_column(Integer)  # 0~100 百分比

    total_steps: Mapped[Optional[int]] = mapped_column(Integer)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    provider: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_policy_minesweeper_code_time", "stock_code", "created_at"),
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "md_path": self.md_path,
            "pdf_path": self.pdf_path,
            "status": self.status,
            "horizon": self.horizon,
            "alpha_ok": self.alpha_ok,
            "beta_ok": self.beta_ok,
            "omega_ok": self.omega_ok,
            "composite_score": self.composite_score,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "total_steps": self.total_steps,
            "total_tokens": self.total_tokens,
            "provider": self.provider,
        }


class SupplyChainReport(Base):
    """供应链分析报告记录模型（表单式报告流）。

    与 ``DeepResearchReport`` / ``PolicyMinesweeperReport`` 同理：正文存文件
    （``reports/supply_chain/``），元数据存本表，支持并发安全的列表/详情/删除
    （SQLite + _run_write_transaction 重试）。主输入是「分析主题」（非单股票），
    ``research_hint`` 为本轮一次性线索（可为空，与历史报告隔离）。

    按 docs/pdf-download-filename-plan.md：可选绑定单股（``stock_code`` / ``stock_name``）
    使 PDF 文件名遵循 ``股票名（代码）报告类型YYYYMMDD.pdf``；不绑定时仍走主题型命名
    （阶段 1 兜底）。两列 nullable，向后兼容历史报告（NULL 时 fallback 到主题型）。
    """

    __tablename__ = "supply_chain_reports"

    id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )  # sc_{YYYYMMDDHHmm}(_\d+)?

    # 输入
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    research_hint: Mapped[Optional[str]] = mapped_column(Text)
    # 可选单股绑定（按 docs/pdf-download-filename-plan.md §供应链报告边界 阶段 1）
    stock_code: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    stock_name: Mapped[Optional[str]] = mapped_column(String(50))

    # 时间
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )

    # 关联分析追溯 ID（精确关联到某次分析任务，便于后续回填 report_url）
    analysis_trace_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # 文件路径
    md_path: Mapped[str] = mapped_column(Text, nullable=False)
    pdf_path: Mapped[Optional[str]] = mapped_column(Text)  # NULL = 尚未惰性生成

    # [v3 PR-C] 深度小节结构化数据（SupplyChainDeepDiveV3.model_dump_json()）
    # 默认 NULL 向后兼容历史报告；客户端按需消费
    deep_dive_json: Mapped[Optional[str]] = mapped_column(Text)

    # 质量
    status: Mapped[Optional[str]] = mapped_column(
        String(16)
    )  # success | partial | failed

    # 统计
    total_steps: Mapped[Optional[int]] = mapped_column(Integer)
    total_tokens: Mapped[Optional[int]] = mapped_column(Integer)
    provider: Mapped[Optional[str]] = mapped_column(String(64))
    model: Mapped[Optional[str]] = mapped_column(String(128))
    error: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (Index("ix_supply_chain_time", "created_at"),)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "research_hint": self.research_hint,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "analysis_trace_id": self.analysis_trace_id,
            "md_path": self.md_path,
            "pdf_path": self.pdf_path,
            "deep_dive_json": self.deep_dive_json,
            "status": self.status,
            "total_steps": self.total_steps,
            "total_tokens": self.total_tokens,
            "provider": self.provider,
            "model": self.model,
            "error": self.error,
        }


class ScheduledTaskLog(Base):
    """Scheduled task execution log.

    Records every scheduled task run (success, partial, failed, skipped) so the
    frontend can display recent execution history and health status.
    """

    __tablename__ = "scheduled_task_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    detail: Mapped[Optional[str]] = mapped_column(Text)
    report_path: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )

    __table_args__ = (Index("ix_sched_task_name_time", "task_name", "scheduled_at"),)

    def to_dict(self):
        return {
            "id": self.id,
            "task_name": self.task_name,
            "scheduled_at": self.scheduled_at.isoformat()
            if self.scheduled_at
            else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "status": self.status,
            "detail": json.loads(self.detail) if self.detail else None,
            "report_path": self.report_path,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class BacktestResult(Base):
    """单条分析记录的回测结果。"""

    __tablename__ = "backtest_results"

    id = Column(Integer, primary_key=True, autoincrement=True)

    analysis_history_id = Column(
        Integer,
        ForeignKey("analysis_history.id"),
        nullable=False,
        index=True,
    )

    # 冗余字段，便于按股票筛选
    code = Column(String(10), nullable=False, index=True)
    analysis_date = Column(Date, index=True)

    # 回测参数
    eval_window_days = Column(Integer, nullable=False, default=10)
    engine_version = Column(String(16), nullable=False, default="v1")

    # 状态
    eval_status = Column(String(16), nullable=False, default="pending")
    evaluated_at = Column(DateTime, default=datetime.now, index=True)

    # 建议快照（避免未来分析字段变化导致回测不可解释）
    operation_advice = Column(String(20))
    position_recommendation = Column(String(8))  # long/cash

    # 价格与收益
    start_price = Column(Float)
    end_close = Column(Float)
    max_high = Column(Float)
    min_low = Column(Float)
    stock_return_pct = Column(Float)

    # 方向与结果
    direction_expected = Column(String(16))  # up/down/flat/not_down
    direction_correct = Column(Boolean, nullable=True)
    outcome = Column(String(16))  # win/loss/neutral

    # 目标价命中（仅 long 且配置了止盈/止损时有意义）
    stop_loss = Column(Float)
    take_profit = Column(Float)
    hit_stop_loss = Column(Boolean)
    hit_take_profit = Column(Boolean)
    first_hit = Column(
        String(16)
    )  # take_profit/stop_loss/ambiguous/neither/not_applicable
    first_hit_date = Column(Date)
    first_hit_trading_days = Column(Integer)

    # 模拟执行（long-only）
    simulated_entry_price = Column(Float)
    simulated_exit_price = Column(Float)
    simulated_exit_reason = Column(
        String(24)
    )  # stop_loss/take_profit/window_end/cash/ambiguous_stop_loss
    simulated_return_pct = Column(Float)

    __table_args__ = (
        UniqueConstraint(
            "analysis_history_id",
            "eval_window_days",
            "engine_version",
            name="uix_backtest_analysis_window_version",
        ),
        Index("ix_backtest_code_date", "code", "analysis_date"),
    )


class BacktestSummary(Base):
    """回测汇总指标（按股票或全局）。"""

    __tablename__ = "backtest_summaries"

    id = Column(Integer, primary_key=True, autoincrement=True)

    scope = Column(String(16), nullable=False, index=True)  # overall/stock
    code = Column(String(16), index=True)

    eval_window_days = Column(Integer, nullable=False, default=10)
    engine_version = Column(String(16), nullable=False, default="v1")
    computed_at = Column(DateTime, default=datetime.now, index=True)

    # 计数
    total_evaluations = Column(Integer, default=0)
    completed_count = Column(Integer, default=0)
    insufficient_count = Column(Integer, default=0)
    long_count = Column(Integer, default=0)
    cash_count = Column(Integer, default=0)

    win_count = Column(Integer, default=0)
    loss_count = Column(Integer, default=0)
    neutral_count = Column(Integer, default=0)

    # 准确率/胜率
    direction_accuracy_pct = Column(Float)
    win_rate_pct = Column(Float)
    neutral_rate_pct = Column(Float)

    # 收益
    avg_stock_return_pct = Column(Float)
    avg_simulated_return_pct = Column(Float)

    # 目标价触发统计（仅 long 且配置止盈/止损时统计）
    stop_loss_trigger_rate = Column(Float)
    take_profit_trigger_rate = Column(Float)
    ambiguous_rate = Column(Float)
    avg_days_to_first_hit = Column(Float)

    # 诊断字段（JSON 字符串）
    advice_breakdown_json = Column(Text)
    diagnostics_json = Column(Text)

    __table_args__ = (
        UniqueConstraint(
            "scope",
            "code",
            "eval_window_days",
            "engine_version",
            name="uix_backtest_summary_scope_code_window_version",
        ),
    )


class PortfolioAccount(Base):
    """Portfolio account metadata."""

    __tablename__ = "portfolio_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(String(64), index=True)
    name = Column(String(64), nullable=False)
    broker = Column(String(64))
    market = Column(String(8), nullable=False, default="cn", index=True)  # cn/hk/us
    base_currency = Column(String(8), nullable=False, default="CNY")
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_portfolio_account_owner_active", "owner_id", "is_active"),
    )


class PortfolioTrade(Base):
    """Executed trade events used as the source of truth for replay."""

    __tablename__ = "portfolio_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(
        Integer, ForeignKey("portfolio_accounts.id"), nullable=False, index=True
    )
    trade_uid = Column(String(128))
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default="cn")
    currency = Column(String(8), nullable=False, default="CNY")
    trade_date = Column(Date, nullable=False, index=True)
    side = Column(String(8), nullable=False)  # buy/sell
    quantity = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    fee = Column(Float, default=0.0)
    tax = Column(Float, default=0.0)
    note = Column(String(255))
    dedup_hash = Column(String(64), index=True)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        UniqueConstraint("account_id", "trade_uid", name="uix_portfolio_trade_uid"),
        UniqueConstraint(
            "account_id", "dedup_hash", name="uix_portfolio_trade_dedup_hash"
        ),
        Index("ix_portfolio_trade_account_date", "account_id", "trade_date"),
    )


class PortfolioCashLedger(Base):
    """Cash in/out events."""

    __tablename__ = "portfolio_cash_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(
        Integer, ForeignKey("portfolio_accounts.id"), nullable=False, index=True
    )
    event_date = Column(Date, nullable=False, index=True)
    direction = Column(String(8), nullable=False)  # in/out
    amount = Column(Float, nullable=False)
    currency = Column(String(8), nullable=False, default="CNY")
    note = Column(String(255))
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index("ix_portfolio_cash_account_date", "account_id", "event_date"),
    )


class PortfolioCorporateAction(Base):
    """Corporate actions that impact cash or share quantity."""

    __tablename__ = "portfolio_corporate_actions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(
        Integer, ForeignKey("portfolio_accounts.id"), nullable=False, index=True
    )
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default="cn")
    currency = Column(String(8), nullable=False, default="CNY")
    effective_date = Column(Date, nullable=False, index=True)
    action_type = Column(String(24), nullable=False)  # cash_dividend/split_adjustment
    cash_dividend_per_share = Column(Float)
    split_ratio = Column(Float)
    note = Column(String(255))
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index("ix_portfolio_ca_account_date", "account_id", "effective_date"),
    )


class PortfolioPosition(Base):
    """Latest replayed position snapshot for each symbol in one account."""

    __tablename__ = "portfolio_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(
        Integer, ForeignKey("portfolio_accounts.id"), nullable=False, index=True
    )
    cost_method = Column(String(8), nullable=False, default="fifo")
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default="cn")
    currency = Column(String(8), nullable=False, default="CNY")
    quantity = Column(Float, nullable=False, default=0.0)
    avg_cost = Column(Float, nullable=False, default=0.0)
    total_cost = Column(Float, nullable=False, default=0.0)
    last_price = Column(Float, nullable=False, default=0.0)
    market_value_base = Column(Float, nullable=False, default=0.0)
    unrealized_pnl_base = Column(Float, nullable=False, default=0.0)
    valuation_currency = Column(String(8), nullable=False, default="CNY")
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "symbol",
            "market",
            "currency",
            "cost_method",
            name="uix_portfolio_position_account_symbol_market_currency",
        ),
    )


class PortfolioPositionLot(Base):
    """Lot-level remaining quantities used by FIFO replay."""

    __tablename__ = "portfolio_position_lots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(
        Integer, ForeignKey("portfolio_accounts.id"), nullable=False, index=True
    )
    cost_method = Column(String(8), nullable=False, default="fifo")
    symbol = Column(String(16), nullable=False, index=True)
    market = Column(String(8), nullable=False, default="cn")
    currency = Column(String(8), nullable=False, default="CNY")
    open_date = Column(Date, nullable=False, index=True)
    remaining_quantity = Column(Float, nullable=False, default=0.0)
    unit_cost = Column(Float, nullable=False, default=0.0)
    source_trade_id = Column(Integer, ForeignKey("portfolio_trades.id"))
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, index=True
    )

    __table_args__ = (Index("ix_portfolio_lot_account_symbol", "account_id", "symbol"),)


class PortfolioDailySnapshot(Base):
    """Daily account snapshot generated by read-time replay."""

    __tablename__ = "portfolio_daily_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(
        Integer, ForeignKey("portfolio_accounts.id"), nullable=False, index=True
    )
    snapshot_date = Column(Date, nullable=False, index=True)
    cost_method = Column(String(8), nullable=False, default="fifo")  # fifo/avg
    base_currency = Column(String(8), nullable=False, default="CNY")
    total_cash = Column(Float, nullable=False, default=0.0)
    total_market_value = Column(Float, nullable=False, default=0.0)
    total_equity = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    fee_total = Column(Float, nullable=False, default=0.0)
    tax_total = Column(Float, nullable=False, default=0.0)
    fx_stale = Column(Boolean, nullable=False, default=False)
    payload = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "snapshot_date",
            "cost_method",
            name="uix_portfolio_snapshot_account_date_method",
        ),
    )


class PortfolioFxRate(Base):
    """Cached FX rates used for cross-currency portfolio conversion."""

    __tablename__ = "portfolio_fx_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_currency = Column(String(8), nullable=False, index=True)
    to_currency = Column(String(8), nullable=False, index=True)
    rate_date = Column(Date, nullable=False, index=True)
    rate = Column(Float, nullable=False)
    source = Column(String(32), nullable=False, default="manual")
    is_stale = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "from_currency",
            "to_currency",
            "rate_date",
            name="uix_portfolio_fx_pair_date",
        ),
    )


class ConversationMessage(Base):
    """
    Agent 对话历史记录表
    """

    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    role: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # user, assistant, system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )


class ConversationSummary(Base):
    """Rolling summary for visible Agent chat history."""

    __tablename__ = "conversation_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    covered_message_id: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, onupdate=datetime.now, index=True
    )


class AgentProviderTurn(Base):
    """Provider protocol trace required for thinking/tool-call roundtrip."""

    __tablename__ = "agent_provider_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    anchor_user_message_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True
    )
    anchor_assistant_message_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True
    )
    messages_json: Mapped[str] = mapped_column(Text, nullable=False)
    contains_reasoning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    contains_tool_calls: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    contains_thinking_blocks: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    must_roundtrip: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.now, index=True
    )

    __table_args__ = (
        Index(
            "ix_agent_provider_turn_bucket",
            "session_id",
            "provider",
            "model",
            "must_roundtrip",
        ),
    )


class LLMUsage(Base):
    """One row per litellm.completion() call — token-usage audit log."""

    __tablename__ = "llm_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 'analysis' | 'agent' | 'market_review'
    call_type = Column(String(32), nullable=False, index=True)
    model = Column(String(128), nullable=False)
    stock_code = Column(String(16), nullable=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)

    # Sanitized provider usage snapshot; raw prompts, messages, headers, and
    # tokenizer free-text fields are intentionally not persisted here.
    provider_usage_json = Column(Text, nullable=True)
    provider_usage_schema_name = Column(String(64), nullable=True)
    provider_usage_schema_version = Column(String(32), nullable=True)
    provider_usage_observed_at = Column(String(32), nullable=True)

    # Normalized telemetry values are derived from provider usage and may stay
    # NULL when the provider payload is absent or explicitly invalid.
    normalized_prompt_tokens = Column(Integer, nullable=True)
    normalized_completion_tokens = Column(Integer, nullable=True)
    normalized_total_tokens = Column(Integer, nullable=True)
    normalized_cache_read_tokens = Column(Integer, nullable=True)
    normalized_cache_write_tokens = Column(Integer, nullable=True)
    normalized_cache_miss_tokens = Column(Integer, nullable=True)
    normalized_uncached_input_tokens = Column(Integer, nullable=True)
    normalized_cache_eligible_input_tokens = Column(Integer, nullable=True)
    normalized_cache_hit_ratio = Column(Float, nullable=True)
    normalized_cache_write_ratio = Column(Float, nullable=True)
    cache_capability = Column(String(32), nullable=True)
    cache_eligibility = Column(String(32), nullable=True)
    cache_observation = Column(String(32), nullable=True)
    estimated_prefix_tokens = Column(Integer, nullable=True)
    provider_reported_prompt_tokens = Column(Integer, nullable=True)
    provider_reported_cached_tokens = Column(Integer, nullable=True)
    provider_min_cache_tokens = Column(Integer, nullable=True)
    eligibility_confidence = Column(String(32), nullable=True)

    # Kept nullable for schema compatibility; new writes do not store provider
    # or proxy tokenizer free-text values.
    tokenizer_name = Column(String(128), nullable=True)
    tokenizer_version = Column(String(64), nullable=True)

    # HMAC fingerprints let deployments compare message shapes without storing
    # raw prompt/message content.
    messages_hmac = Column(String(64), nullable=True)
    system_message_hmac = Column(String(64), nullable=True)
    user_message_hmac = Column(String(64), nullable=True)
    hmac_key_version = Column(String(64), nullable=True)
    hmac_domain = Column(String(32), nullable=True)
    hash_scope = Column(String(32), nullable=True)
    called_at = Column(DateTime, default=datetime.now, index=True)


_LLM_USAGE_TELEMETRY_COLUMN_SQL: Dict[str, str] = {
    "provider_usage_json": "TEXT",
    "provider_usage_schema_name": "VARCHAR(64)",
    "provider_usage_schema_version": "VARCHAR(32)",
    "provider_usage_observed_at": "VARCHAR(32)",
    "normalized_prompt_tokens": "INTEGER",
    "normalized_completion_tokens": "INTEGER",
    "normalized_total_tokens": "INTEGER",
    "normalized_cache_read_tokens": "INTEGER",
    "normalized_cache_write_tokens": "INTEGER",
    "normalized_cache_miss_tokens": "INTEGER",
    "normalized_uncached_input_tokens": "INTEGER",
    "normalized_cache_eligible_input_tokens": "INTEGER",
    "normalized_cache_hit_ratio": "FLOAT",
    "normalized_cache_write_ratio": "FLOAT",
    "cache_capability": "VARCHAR(32)",
    "cache_eligibility": "VARCHAR(32)",
    "cache_observation": "VARCHAR(32)",
    "estimated_prefix_tokens": "INTEGER",
    "provider_reported_prompt_tokens": "INTEGER",
    "provider_reported_cached_tokens": "INTEGER",
    "provider_min_cache_tokens": "INTEGER",
    "eligibility_confidence": "VARCHAR(32)",
    "tokenizer_name": "VARCHAR(128)",
    "tokenizer_version": "VARCHAR(64)",
    "messages_hmac": "VARCHAR(64)",
    "system_message_hmac": "VARCHAR(64)",
    "user_message_hmac": "VARCHAR(64)",
    "hmac_key_version": "VARCHAR(64)",
    "hmac_domain": "VARCHAR(32)",
    "hash_scope": "VARCHAR(32)",
}
_LLM_USAGE_INTEGER_TELEMETRY_COLUMNS = {
    column
    for column, column_type in _LLM_USAGE_TELEMETRY_COLUMN_SQL.items()
    if column_type == "INTEGER"
}
_LLM_USAGE_DROPPED_FREE_TEXT_COLUMNS = {"tokenizer_name", "tokenizer_version"}


class AlertRuleRecord(Base):
    """Persisted alert rule managed through the Alert API."""

    __tablename__ = "alert_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), nullable=False)
    target_scope = Column(
        String(32), nullable=False, default="single_symbol", index=True
    )
    target = Column(String(64), nullable=False, index=True)
    alert_type = Column(String(32), nullable=False, index=True)
    parameters = Column(Text, nullable=False, default="{}")
    severity = Column(String(16), nullable=False, default="warning", index=True)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    source = Column(String(16), nullable=False, default="api", index=True)
    cooldown_policy = Column(Text)
    notification_policy = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, index=True
    )

    __table_args__ = (Index("ix_alert_rule_type_target", "alert_type", "target"),)


class AlertTriggerRecord(Base):
    """Alert trigger history row.

    P1 exposes read APIs and table shape; runtime writer integration lands in
    later phases.
    """

    __tablename__ = "alert_triggers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, index=True)
    target = Column(String(64), nullable=False, index=True)
    observed_value = Column(Float)
    threshold = Column(Float)
    reason = Column(Text)
    data_source = Column(String(64))
    data_timestamp = Column(DateTime, index=True)
    triggered_at = Column(DateTime, default=datetime.now, index=True)
    status = Column(String(16), nullable=False, default="triggered", index=True)
    diagnostics = Column(Text)

    __table_args__ = (Index("ix_alert_trigger_rule_time", "rule_id", "triggered_at"),)


class AlertNotificationRecord(Base):
    """Notification attempt row for alert triggers.

    P1 exposes read APIs and table shape; runtime writer integration lands in
    later phases.
    """

    __tablename__ = "alert_notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trigger_id = Column(Integer, index=True)
    channel = Column(String(32), nullable=False, index=True)
    attempt = Column(Integer, nullable=False, default=1)
    success = Column(Boolean, nullable=False, default=False, index=True)
    error_code = Column(String(64))
    retryable = Column(Boolean, nullable=False, default=False)
    latency_ms = Column(Integer)
    diagnostics = Column(Text)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index("ix_alert_notification_trigger_channel", "trigger_id", "channel"),
    )


class AlertCooldownRecord(Base):
    """Persisted alert cooldown state for DB-managed alert rules."""

    __tablename__ = "alert_cooldowns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, index=True)
    # Reserved for future non-DB/expanded-scope rules; P4 queries by rule_id.
    rule_key = Column(String(255), index=True)
    target = Column(String(64), nullable=False, index=True)
    severity = Column(String(16), nullable=False, default="warning", index=True)
    last_triggered_at = Column(DateTime, index=True)
    cooldown_until = Column(DateTime, index=True)
    reason = Column(Text)
    state = Column(String(16), nullable=False, default="active", index=True)
    updated_at = Column(
        DateTime, default=datetime.now, onupdate=datetime.now, index=True
    )

    __table_args__ = (
        UniqueConstraint(
            "rule_id",
            "target",
            "severity",
            name="uix_alert_cooldown_rule_target_severity",
        ),
    )


class DecisionSignalRecord(Base):
    """Persisted AI decision signal asset for Issue #1390 P1."""

    __tablename__ = "decision_signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(16), nullable=False, index=True)
    stock_name = Column(String(64))
    market = Column(String(8), nullable=False, index=True)
    source_type = Column(String(32), nullable=False, index=True)
    source_agent = Column(String(64))
    source_report_id = Column(Integer, index=True)
    trace_id = Column(String(64), index=True)
    market_phase = Column(String(24), index=True)
    trigger_source = Column(String(64), nullable=False, index=True)
    action = Column(String(16), nullable=False, index=True)
    action_label = Column(String(32))
    confidence = Column(Float)
    score = Column(Integer)
    horizon = Column(String(16), index=True)
    entry_low = Column(Float)
    entry_high = Column(Float)
    stop_loss = Column(Float)
    target_price = Column(Float)
    invalidation = Column(Text)
    watch_conditions = Column(Text)
    reason = Column(Text)
    risk_summary = Column(Text)
    catalyst_summary = Column(Text)
    evidence_json = Column(Text)
    data_quality_summary_json = Column(Text)
    plan_quality = Column(String(16), nullable=False, default="unknown", index=True)
    status = Column(String(16), nullable=False, default="active", index=True)
    expires_at = Column(DateTime, index=True)
    created_at = Column(DateTime, default=utc_naive_now, index=True)
    updated_at = Column(
        DateTime, default=utc_naive_now, onupdate=utc_naive_now, index=True
    )
    metadata_json = Column(Text)

    __table_args__ = (
        Index(
            "ix_decision_signal_stock_status_time", "stock_code", "status", "created_at"
        ),
        Index(
            "ix_decision_signal_market_status_time", "market", "status", "created_at"
        ),
        Index(
            "ix_decision_signal_report_type_market_stock_action_horizon_phase",
            "source_report_id",
            "source_type",
            "market",
            "stock_code",
            "action",
            "horizon",
            "market_phase",
        ),
        Index(
            "ix_decision_signal_trace_type_market_stock_action_horizon_phase",
            "trace_id",
            "source_type",
            "market",
            "stock_code",
            "action",
            "horizon",
            "market_phase",
        ),
    )


# ==================================================================
# 知识库 (Knowledge Base) ORM 模型
# ==================================================================


class KnowledgeDocument(Base):
    """知识库文档元数据表。

    存储用户上传的文本/Markdown/PDF/URL 文档信息。
    """

    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True
    )  # text/markdown/pdf/url
    source_url: Mapped[Optional[str]] = mapped_column(String(2048))
    file_path: Mapped[Optional[str]] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    tags: Mapped[Optional[str]] = mapped_column(Text)  # JSON string
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_naive_now, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_naive_now, onupdate=utc_naive_now, index=True
    )

    __table_args__ = (Index("ix_kb_doc_title_created", "title", "created_at"),)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        tags_list: List[str] = []
        if self.tags:
            try:
                tags_list = json.loads(self.tags)
            except Exception:
                pass
        return {
            "id": self.id,
            "title": self.title,
            "source_type": self.source_type,
            "source_url": self.source_url,
            "file_path": self.file_path,
            "content_hash": self.content_hash,
            "tags": tags_list,
            "chunk_count": self.chunk_count,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class KnowledgeChunk(Base):
    """知识库文档片段表。

    存储文档切分后的文本片段，用于 FTS5 全文检索。
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("knowledge_documents.id"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[Optional[str]] = mapped_column(Text)  # JSON string
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utc_naive_now, index=True
    )

    __table_args__ = (Index("ix_kb_chunk_doc_index", "document_id", "chunk_index"),)


class _DatabaseManagerMeta(type):
    """Serialize DatabaseManager construction across __new__ and __init__."""

    _init_lock: threading.RLock = threading.RLock()

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        with cls._init_lock:
            return super().__call__(*args, **kwargs)


class DatabaseManager(metaclass=_DatabaseManagerMeta):
    """
    数据库管理器 - 单例模式

    职责：
    1. 管理数据库连接池
    2. 提供 Session 上下文管理
    3. 封装数据存取操作
    """

    _instance: Optional["DatabaseManager"] = None
    _init_lock = threading.RLock()
    _initialized: bool = False
    _engine: Optional[Engine] = None
    _SessionLocal: Optional[sessionmaker[Session]] = None

    def __new__(cls, *args: Any, **kwargs: Any) -> "DatabaseManager":
        """单例模式实现"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, db_url: Optional[str] = None):
        """
        初始化数据库管理器

        Args:
            db_url: 数据库连接 URL（可选，默认从配置读取）
        """
        if getattr(self, "_initialized", False):
            return

        created_engine = None

        try:
            config = get_config()
            if db_url is None:
                db_url = config.get_db_url()

            self._db_url = db_url
            self._sqlite_wal_enabled = config.sqlite_wal_enabled
            self._sqlite_busy_timeout_ms = config.sqlite_busy_timeout_ms
            self._sqlite_write_retry_max = config.sqlite_write_retry_max
            self._sqlite_write_retry_base_delay = config.sqlite_write_retry_base_delay

            engine_kwargs: Dict[str, Any] = {
                "echo": False,
                "pool_pre_ping": True,
            }
            if str(db_url).startswith("sqlite:") and self._sqlite_busy_timeout_ms > 0:
                engine_kwargs["connect_args"] = {
                    "timeout": self._sqlite_busy_timeout_ms / 1000,
                }

            # 创建数据库引擎
            created_engine = create_engine(
                db_url,
                **engine_kwargs,
            )
            self._engine = created_engine
            self._is_sqlite_engine = self._engine.url.get_backend_name() == "sqlite"
            self._sqlite_file_db = (
                self._is_sqlite_engine and self._is_file_sqlite_database()
            )
            self._install_sqlite_pragma_handler()

            # 创建 Session 工厂
            self._SessionLocal = sessionmaker(
                bind=self._engine,
                autocommit=False,
                autoflush=False,
            )

            # 确保所有表模型被导入（包含台账表）
            from src.repositories.position_ledger_repo import PositionLedger
            from src.repositories.score_ledger_repo import ScoreLedger
            from src.repositories.belief_ledger_repo import BeliefLedger

            # 创建所有表
            Base.metadata.create_all(self._engine)
            self._ensure_llm_usage_telemetry_columns()
            self._ensure_supply_chain_stock_columns()
            self._ensure_supply_chain_deep_dive_column()  # [v3 PR-C]
            self._ensure_supply_chain_trace_id_column()  # 方案 C：Serenity 报告关联分析 query_id
            self._ensure_news_intel_source_columns()  # P3
            self._ensure_schema_migration_record()
            self._ensure_knowledge_base_fts()

            self._initialized = True
            logger.info(f"数据库初始化完成: {db_url}")

            # 注册退出钩子，确保程序退出时关闭数据库连接
            atexit.register(DatabaseManager._cleanup_engine, self._engine)
        except Exception:
            self._initialized = False
            try:
                if created_engine is not None:
                    created_engine.dispose()
            except Exception as cleanup_exc:
                logger.warning("数据库初始化失败后的引擎清理也失败: %s", cleanup_exc)
            self._engine = None
            self._SessionLocal = None
            self.__class__._instance = None
            raise

    def _ensure_schema_migration_record(self) -> None:
        if self._SessionLocal is None:
            raise RuntimeError("DatabaseManager 未正确初始化")
        session = self._SessionLocal()
        values = {
            "version": CURRENT_SCHEMA_VERSION,
            "description": "Baseline schema created through SQLAlchemy metadata.create_all",
        }
        try:
            if self._is_sqlite_engine:
                statement = sqlite_insert(DatabaseSchemaMigration).values(**values)
                statement = statement.on_conflict_do_nothing(index_elements=["version"])
                session.execute(statement)
            else:
                session.execute(
                    DatabaseSchemaMigration.__table__.insert().values(**values)  # type: ignore[attr-defined]
                )
            session.commit()
        except IntegrityError:
            session.rollback()
            with self._SessionLocal() as verify_session:
                existing = verify_session.get(
                    DatabaseSchemaMigration, CURRENT_SCHEMA_VERSION
                )
            if existing is None:
                raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _ensure_llm_usage_telemetry_columns(self) -> None:
        """Add nullable P0a usage telemetry columns to existing SQLite DBs."""
        if not self._is_sqlite_engine or self._engine is None:
            return
        try:
            existing = {
                column["name"]
                for column in inspect(self._engine).get_columns(LLMUsage.__tablename__)
            }
        except Exception as exc:
            logger.warning(
                "[LLM usage] failed to inspect telemetry columns; "
                "skipping best-effort SQLite telemetry column backfill: %s",
                exc,
            )
            return

        max_retries = self._sqlite_write_retry_max
        for column, column_type in _LLM_USAGE_TELEMETRY_COLUMN_SQL.items():
            if column in existing:
                continue
            for attempt in range(max_retries + 1):
                try:
                    with self._engine.begin() as connection:
                        connection.exec_driver_sql(
                            f"ALTER TABLE {LLMUsage.__tablename__} "
                            f"ADD COLUMN {column} {column_type}"
                        )
                    existing.add(column)
                    break
                except OperationalError as exc:
                    if self._is_sqlite_duplicate_column_error(exc, column):
                        existing.add(column)
                        break
                    if self._is_sqlite_locked_error(exc) and attempt < max_retries:
                        delay = self._sqlite_write_retry_base_delay * (2**attempt)
                        logger.warning(
                            "[LLM usage] SQLite telemetry column backfill locked, "
                            "retrying: %s (%s/%s, %.2fs)",
                            column,
                            attempt + 1,
                            max_retries,
                            delay,
                        )
                        if delay > 0:
                            time.sleep(delay)
                        continue
                    raise

    def _ensure_supply_chain_stock_columns(self) -> None:
        """Add nullable stock_code / stock_name to existing supply_chain_reports DBs.

        按 docs/pdf-download-filename-plan.md §供应链报告边界 阶段 1：可选绑定单股
        使 PDF 文件名遵循 ``股票名（代码）报告类型YYYYMMDD.pdf``；不绑定时仍走主题型。
        新装用户走 ``Base.metadata.create_all`` 自动建列；已有用户走 best-effort ALTER。
        """
        if not self._is_sqlite_engine or self._engine is None:
            return
        try:
            existing = {
                column["name"]
                for column in inspect(self._engine).get_columns("supply_chain_reports")
            }
        except Exception as exc:
            logger.warning(
                "[SupplyChainReport] failed to inspect stock columns; "
                "skipping best-effort backfill: %s",
                exc,
            )
            return

        # stock_code VARCHAR(10) / stock_name VARCHAR(50)，均为 NULLABLE
        backfills: list[tuple[str, str]] = [
            ("stock_code", "VARCHAR(10)"),
            ("stock_name", "VARCHAR(50)"),
        ]
        for column, column_type in backfills:
            if column in existing:
                continue
            try:
                with self._engine.begin() as connection:
                    connection.exec_driver_sql(
                        f"ALTER TABLE supply_chain_reports "
                        f"ADD COLUMN {column} {column_type}"
                    )
                existing.add(column)
            except Exception as exc:
                logger.warning(
                    "[SupplyChainReport] best-effort ADD COLUMN %s failed: %s",
                    column,
                    exc,
                )

    def _ensure_supply_chain_deep_dive_column(self) -> None:
        """[v3 PR-C] Add nullable deep_dive_json to existing supply_chain_reports DBs.

        新装用户走 ``Base.metadata.create_all`` 自动建列；已有用户走 best-effort ALTER。
        列存 :class:`SupplyChainDeepDiveV3` 的 JSON 字符串，供 API 详情返回 + 报告渲染复用。
        """
        if not self._is_sqlite_engine or self._engine is None:
            return
        try:
            existing = {
                column["name"]
                for column in inspect(self._engine).get_columns("supply_chain_reports")
            }
        except Exception as exc:
            logger.warning(
                "[SupplyChainReport] failed to inspect deep_dive column; "
                "skipping best-effort backfill: %s",
                exc,
            )
            return

        column = "deep_dive_json"
        if column in existing:
            return
        try:
            with self._engine.begin() as connection:
                connection.exec_driver_sql(
                    f"ALTER TABLE supply_chain_reports ADD COLUMN {column} TEXT"
                )
        except Exception as exc:
            logger.warning(
                "[SupplyChainReport] best-effort ADD COLUMN %s failed: %s",
                column,
                exc,
            )

    def _ensure_supply_chain_trace_id_column(self) -> None:
        """[方案 C] Add nullable analysis_trace_id to existing supply_chain_reports DBs.

        新装用户走 ``Base.metadata.create_all`` 自动建列；已有用户走 best-effort ALTER。
        列存关联分析的 query_id，用于 Serenity 报告按 (query_id, code) 精确回填 history。
        """
        if not self._is_sqlite_engine or self._engine is None:
            return
        try:
            existing = {
                column["name"]
                for column in inspect(self._engine).get_columns("supply_chain_reports")
            }
        except Exception as exc:
            logger.warning(
                "[SupplyChainReport] failed to inspect analysis_trace_id column; "
                "skipping best-effort backfill: %s",
                exc,
            )
            return

        column = "analysis_trace_id"
        if column in existing:
            return
        try:
            with self._engine.begin() as connection:
                connection.exec_driver_sql(
                    f"ALTER TABLE supply_chain_reports ADD COLUMN {column} VARCHAR(64)"
                )
        except Exception as exc:
            logger.warning(
                "[SupplyChainReport] best-effort ADD COLUMN %s failed: %s",
                column,
                exc,
            )

    def _ensure_knowledge_base_fts(self) -> None:
        """Create FTS5 virtual table for knowledge base search if not exists."""
        if not self._is_sqlite_engine or self._engine is None:
            return
        try:
            with self._engine.begin() as connection:
                # Check if FTS5 table exists
                result = connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_chunks_fts'"
                ).fetchone()
                if result is None:
                    connection.exec_driver_sql("""
                        CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts
                        USING fts5(content, document_id UNINDEXED, chunk_id UNINDEXED,
                                   content_rowid UNINDEXED)
                    """)
                    logger.info("Created knowledge_chunks_fts virtual table")
        except Exception as exc:
            logger.warning("[Knowledge Base] failed to create FTS5 table: %s", exc)

    def _ensure_news_intel_source_columns(self) -> None:
        """P3 信息源分层（按 docs/deep-research-chain-news-logic-plan.md §信息源策略）。

        news_intel 新增 source_type（official / news / industry / community_cn /
        community_global）+ reliability_hint（high / medium / low / unverified）两列。
        老记录允许 NULL（默认 news_intel 是 primary/news 隐式语义，文档不强求 backfill）。
        新装用户走 ``Base.metadata.create_all`` 自动建列；已有用户走 best-effort ALTER。
        """
        if not self._is_sqlite_engine or self._engine is None:
            return
        try:
            existing = {
                column["name"]
                for column in inspect(self._engine).get_columns("news_intel")
            }
        except Exception as exc:
            logger.warning(
                "[NewsIntel] failed to inspect source columns; "
                "skipping best-effort backfill: %s",
                exc,
            )
            return

        # source_type VARCHAR(32) / reliability_hint VARCHAR(16)，均为 NULLABLE
        backfills: List[Tuple[str, str]] = [
            ("source_type", "VARCHAR(32)"),
            ("reliability_hint", "VARCHAR(16)"),
        ]
        for column, column_type in backfills:
            if column in existing:
                continue
            try:
                with self._engine.begin() as connection:
                    connection.exec_driver_sql(
                        f"ALTER TABLE news_intel ADD COLUMN {column} {column_type}"
                    )
                existing.add(column)
                logger.info(
                    "[NewsIntel] best-effort ADD COLUMN %s %s OK",
                    column,
                    column_type,
                )
            except Exception as exc:
                logger.warning(
                    "[NewsIntel] best-effort ADD COLUMN %s failed: %s",
                    column,
                    exc,
                )

    @classmethod
    def get_instance(cls) -> "DatabaseManager":
        """获取单例实例"""
        with cls._init_lock:
            if cls._instance is None:
                instance = cls()
                if cls._instance is None:
                    cls._instance = instance
            assert cls._instance is not None
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（用于测试）"""
        with cls._init_lock:
            if cls._instance is not None:
                if (
                    hasattr(cls._instance, "_engine")
                    and cls._instance._engine is not None
                ):
                    cls._instance._engine.dispose()
                cls._instance._initialized = False
                cls._instance = None

    @classmethod
    def _cleanup_engine(cls, engine: Optional[Engine]) -> None:
        """
        清理数据库引擎（atexit 钩子）

        确保程序退出时关闭所有数据库连接，避免 ResourceWarning

        Args:
            engine: SQLAlchemy 引擎对象
        """
        try:
            if engine is not None:
                engine.dispose()
                logger.debug("数据库引擎已清理")
        except Exception as e:
            logger.warning(f"清理数据库引擎时出错: {e}")

    def _install_sqlite_pragma_handler(self) -> None:
        """为 SQLite 连接安装竞争保护参数。"""
        if not self._is_sqlite_engine or self._engine is None:
            return

        @event.listens_for(self._engine, "connect")
        def _configure_sqlite_connection(
            dbapi_connection: Any, _connection_record: Any
        ) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(
                    f"PRAGMA busy_timeout={int(self._sqlite_busy_timeout_ms)}"
                )
                if self._sqlite_file_db and self._sqlite_wal_enabled:
                    cursor.execute("PRAGMA journal_mode=WAL")
            except Exception as exc:
                logger.warning("初始化 SQLite PRAGMA 失败: %s", exc)
            finally:
                cursor.close()

    def _is_file_sqlite_database(self) -> bool:
        if self._engine is None:
            return False
        database = (self._engine.url.database or "").strip()
        return bool(database) and database.lower() != ":memory:"

    def _run_write_transaction(
        self,
        operation_name: str,
        write_operation: Callable[[Session], T],
    ) -> T:
        max_retries = self._sqlite_write_retry_max if self._is_sqlite_engine else 0

        for attempt in range(max_retries + 1):
            session = self.get_session()
            try:
                if self._is_sqlite_engine:
                    # Acquire the SQLite writer lock before any reads inside
                    # `write_operation()` so pre-write existence checks and the
                    # later upsert share one consistent write window.
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                result = write_operation(session)
                session.commit()
                return result
            except OperationalError as exc:
                session.rollback()
                if (
                    self._is_sqlite_engine
                    and self._is_sqlite_locked_error(exc)
                    and attempt < max_retries
                ):
                    delay = self._sqlite_write_retry_base_delay * (2**attempt)
                    logger.warning(
                        "SQLite 写入锁冲突，准备重试: %s (%s/%s, %.2fs)",
                        operation_name,
                        attempt + 1,
                        max_retries,
                        delay,
                    )
                    if delay > 0:
                        time.sleep(delay)
                    continue
                raise
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        # 循环内每次尝试要么成功返回，要么 raise；走到此处为不可达路径。
        raise RuntimeError(f"{operation_name}: 写入事务未返回预期结果")

    @staticmethod
    def _is_sqlite_locked_error(exc: OperationalError) -> bool:
        err_text = str(getattr(exc, "orig", exc)).lower()
        return any(
            token in err_text
            for token in (
                "database is locked",
                "database schema is locked",
                "database table is locked",
            )
        )

    @staticmethod
    def _is_sqlite_duplicate_column_error(exc: OperationalError, column: str) -> bool:
        err_text = str(getattr(exc, "orig", exc)).lower()
        return "duplicate column name" in err_text and column.lower() in err_text

    @staticmethod
    def _normalize_daily_date(value: Any) -> Any:
        if isinstance(value, str):
            return datetime.strptime(value, "%Y-%m-%d").date()
        if isinstance(value, pd.Timestamp):
            return value.date()
        if isinstance(value, datetime):
            return value.date()
        return value

    @staticmethod
    def _normalize_sql_value(value: Any) -> Any:
        return None if pd.isna(value) else value

    def get_session(self) -> Session:
        """
        获取数据库 Session

        使用示例:
            with db.get_session() as session:
                # 执行查询
                session.commit()  # 如果需要
        """
        if not getattr(self, "_initialized", False) or self._SessionLocal is None:
            raise RuntimeError(
                "DatabaseManager 未正确初始化。"
                "请确保通过 DatabaseManager.get_instance() 获取实例。"
            )
        session = self._SessionLocal()
        try:
            return session
        except Exception:
            session.close()
            raise

    @contextmanager
    def session_scope(self):
        """Provide a transactional scope around a series of operations."""
        session = self.get_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def has_today_data(
        self, code: str, target_date: Optional[date_type] = None
    ) -> bool:
        """
        检查是否已有指定日期的数据

        用于断点续传逻辑：如果已有数据则跳过网络请求

        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）

        Returns:
            是否存在数据
        """
        if target_date is None:
            target_date = date_type.today()
        # 注意：这里的 target_date 语义是“自然日”，而不是“最新交易日”。
        # 在周末/节假日/非交易日运行时，即使数据库已有最新交易日数据，这里也会返回 False。
        # 该行为目前保留（按需求不改逻辑）。

        with self.get_session() as session:
            result = session.execute(
                select(StockDaily).where(
                    and_(StockDaily.code == code, StockDaily.date == target_date)
                )
            ).scalar_one_or_none()

            return result is not None

    def get_latest_data(self, code: str, days: int = 2) -> List[StockDaily]:
        """
        获取最近 N 天的数据

        用于计算"相比昨日"的变化

        Args:
            code: 股票代码
            days: 获取天数

        Returns:
            StockDaily 对象列表（按日期降序）
        """
        with self.get_session() as session:
            results = (
                session.execute(
                    select(StockDaily)
                    .where(StockDaily.code == code)
                    .order_by(desc(StockDaily.date))
                    .limit(days)
                )
                .scalars()
                .all()
            )

            return list(results)

    def save_news_intel(
        self,
        code: str,
        name: str,
        dimension: str,
        query: str,
        response: "SearchResponse",
        query_context: Optional[Dict[str, str]] = None,
        source_type: Optional[str] = None,
        reliability_hint: Optional[str] = None,
    ) -> int:
        """
        保存新闻情报到数据库

        去重策略：
        - 优先按 URL 去重（唯一约束）
        - URL 缺失时按 title + source + published_date 进行软去重

        关联策略：
        - query_context 记录用户查询信息（平台、用户、会话、原始指令等）
        """
        if not response or not response.results:
            return 0

        saved_count = 0
        query_ctx = query_context or {}
        current_query_id = (query_ctx.get("query_id") or "").strip()

        def _write(session: Session) -> int:
            local_saved_count = 0

            for item in response.results:
                title = (item.title or "").strip()
                url = (item.url or "").strip()
                source = (item.source or "").strip()
                snippet = (item.snippet or "").strip()
                published_date = self._parse_published_date(item.published_date)

                if not title and not url:
                    continue

                url_key = url or self._build_fallback_url_key(
                    code=code, title=title, source=source, published_date=published_date
                )

                existing = session.execute(
                    select(NewsIntel).where(NewsIntel.url == url_key)
                ).scalar_one_or_none()

                if existing:
                    existing.name = name or existing.name
                    existing.dimension = dimension or existing.dimension
                    existing.query = query or existing.query
                    existing.provider = response.provider or existing.provider
                    existing.snippet = snippet or existing.snippet
                    existing.source = source or existing.source
                    existing.published_date = published_date or existing.published_date
                    existing.fetched_at = datetime.now()

                    if query_context:
                        if not existing.query_id and current_query_id:
                            existing.query_id = current_query_id
                        existing.query_source = (
                            query_context.get("query_source") or existing.query_source
                        )
                        existing.requester_platform = (
                            query_context.get("requester_platform")
                            or existing.requester_platform
                        )
                        existing.requester_user_id = (
                            query_context.get("requester_user_id")
                            or existing.requester_user_id
                        )
                        existing.requester_user_name = (
                            query_context.get("requester_user_name")
                            or existing.requester_user_name
                        )
                        existing.requester_chat_id = (
                            query_context.get("requester_chat_id")
                            or existing.requester_chat_id
                        )
                        existing.requester_message_id = (
                            query_context.get("requester_message_id")
                            or existing.requester_message_id
                        )
                        existing.requester_query = (
                            query_context.get("requester_query")
                            or existing.requester_query
                        )
                    continue

                try:
                    with session.begin_nested():
                        record = NewsIntel(
                            code=code,
                            name=name,
                            dimension=dimension,
                            source_type=source_type,
                            reliability_hint=reliability_hint,
                            query=query,
                            provider=response.provider,
                            title=title,
                            snippet=snippet,
                            url=url_key,
                            source=source,
                            published_date=published_date,
                            fetched_at=datetime.now(),
                            query_id=current_query_id or None,
                            query_source=query_ctx.get("query_source"),
                            requester_platform=query_ctx.get("requester_platform"),
                            requester_user_id=query_ctx.get("requester_user_id"),
                            requester_user_name=query_ctx.get("requester_user_name"),
                            requester_chat_id=query_ctx.get("requester_chat_id"),
                            requester_message_id=query_ctx.get("requester_message_id"),
                            requester_query=query_ctx.get("requester_query"),
                        )
                        session.add(record)
                        session.flush()
                    local_saved_count += 1
                except IntegrityError:
                    logger.debug("新闻情报重复（已跳过）: %s %s", code, url_key)

            return local_saved_count

        try:
            saved_count = self._run_write_transaction(
                f"save_news_intel[{code}]",
                _write,
            )
            logger.info(f"保存新闻情报成功: {code}, 新增 {saved_count} 条")
        except Exception as e:
            logger.error(f"保存新闻情报失败: {e}")
            raise

        return saved_count

    def save_fundamental_snapshot(
        self,
        query_id: str,
        code: str,
        payload: Optional[Dict[str, Any]],
        source_chain: Optional[Any] = None,
        coverage: Optional[Any] = None,
        as_of_date: Optional[str] = None,
    ) -> int:
        """
        保存基本面快照（P0 write-only）。失败不抛异常，返回写入条数 0/1。

        ``as_of_date`` is the data anchor (``YYYY-MM-DD``). Pass ``None`` for
        legacy callers; the column is NULL-able and the staleness detector
        will fall back to text-year scan.
        """
        if not query_id or not code or payload is None:
            return 0

        try:

            def _write(session: Session) -> int:
                session.add(
                    FundamentalSnapshot(
                        query_id=query_id,
                        code=code,
                        payload=self._safe_json_dumps(payload),
                        source_chain=self._safe_json_dumps(source_chain or []),
                        coverage=self._safe_json_dumps(coverage or {}),
                        as_of_date=as_of_date,
                    )
                )
                return 1

            return self._run_write_transaction(
                f"save_fundamental_snapshot[{query_id}:{code}]",
                _write,
            )
        except Exception as e:
            logger.debug(
                "基本面快照写入失败（fail-open）: query_id=%s code=%s err=%s",
                query_id,
                code,
                e,
            )
            return 0

    def get_latest_fundamental_snapshot(
        self,
        query_id: str,
        code: str,
    ) -> Optional[Dict[str, Any]]:
        """
        获取指定 query_id + code 的最新基本面快照 payload。

        读取失败或不存在时返回 None（fail-open）。
        """
        if not query_id or not code:
            return None

        with self.get_session() as session:
            try:
                row = session.execute(
                    select(FundamentalSnapshot)
                    .where(
                        and_(
                            FundamentalSnapshot.query_id == query_id,
                            FundamentalSnapshot.code == code,
                        )
                    )
                    .order_by(desc(FundamentalSnapshot.created_at))
                    .limit(1)
                ).scalar_one_or_none()
            except Exception as e:
                logger.debug(
                    "基本面快照读取失败（fail-open）: query_id=%s code=%s err=%s",
                    query_id,
                    code,
                    e,
                )
                return None

            if row is None:
                return None
            try:
                payload = json.loads(cast(str, row.payload) or "{}")
                return payload if isinstance(payload, dict) else None
            except Exception:
                return None

    def get_recent_news(
        self, code: str, days: int = 7, limit: int = 20
    ) -> List[NewsIntel]:
        """
        获取指定股票最近 N 天的新闻情报
        """
        cutoff_date = datetime.now() - timedelta(days=days)

        with self.get_session() as session:
            results = (
                session.execute(
                    select(NewsIntel)
                    .where(
                        and_(
                            NewsIntel.code == code, NewsIntel.fetched_at >= cutoff_date
                        )
                    )
                    .order_by(desc(NewsIntel.fetched_at))
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            return list(results)

    def get_news_intel_by_query_id(
        self, query_id: str, limit: int = 20
    ) -> List[NewsIntel]:
        """
        根据 query_id 获取新闻情报列表

        Args:
            query_id: 分析记录唯一标识
            limit: 返回数量限制

        Returns:
            NewsIntel 列表（按发布时间或抓取时间倒序）
        """
        from sqlalchemy import func

        with self.get_session() as session:
            results = (
                session.execute(
                    select(NewsIntel)
                    .where(NewsIntel.query_id == query_id)
                    .order_by(
                        desc(
                            func.coalesce(
                                NewsIntel.published_date, NewsIntel.fetched_at
                            )
                        ),
                        desc(NewsIntel.fetched_at),
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            return list(results)

    def save_analysis_history(
        self,
        result: Any,
        query_id: str,
        report_type: str,
        news_content: Optional[str],
        context_snapshot: Optional[Dict[str, Any]] = None,
        save_snapshot: bool = True,
    ) -> int:
        """
        保存分析结果历史记录。

        Returns:
            新保存的 AnalysisHistory.id；保存失败返回 0。
        """
        if result is None:
            return 0

        sniper_points = self._extract_sniper_points(result)
        raw_result = self._build_raw_result(result)
        context_text = None
        if save_snapshot and context_snapshot is not None:
            context_text = self._safe_json_dumps(context_snapshot)

        try:
            research_framework_text = None
            if hasattr(result, "research_framework") and result.research_framework:
                research_framework_text = self._safe_json_dumps(
                    result.research_framework
                )

            bayesian_framework_text = None
            if hasattr(result, "bayesian_framework") and result.bayesian_framework:
                bayesian_framework_text = self._safe_json_dumps(
                    result.bayesian_framework
                )

            supply_chain_text = None
            if hasattr(result, "supply_chain") and result.supply_chain:
                supply_chain_text = self._safe_json_dumps(result.supply_chain)

            value_scenarios_text = None
            if hasattr(result, "value_scenarios") and result.value_scenarios:
                value_scenarios_text = self._safe_json_dumps(result.value_scenarios)

            investment_conclusion_text = None
            if (
                hasattr(result, "investment_conclusion")
                and result.investment_conclusion
            ):
                investment_conclusion_text = self._safe_json_dumps(
                    result.investment_conclusion
                )

            def _write(session: Session) -> int:
                history = AnalysisHistory(
                    query_id=query_id,
                    code=result.code,
                    name=result.name,
                    report_type=report_type,
                    sentiment_score=result.sentiment_score,
                    operation_advice=result.operation_advice,
                    trend_prediction=result.trend_prediction,
                    analysis_summary=result.analysis_summary,
                    raw_result=self._safe_json_dumps(raw_result),
                    news_content=news_content,
                    context_snapshot=context_text,
                    ideal_buy=sniper_points.get("ideal_buy"),
                    secondary_buy=sniper_points.get("secondary_buy"),
                    stop_loss=sniper_points.get("stop_loss"),
                    take_profit=sniper_points.get("take_profit"),
                    research_framework=research_framework_text,
                    bayesian_framework=bayesian_framework_text,
                    supply_chain=supply_chain_text,
                    value_scenarios=value_scenarios_text,
                    investment_conclusion=investment_conclusion_text,
                    created_at=datetime.now(),
                )
                session.add(history)
                session.flush()
                return int(history.id or 0)

            return self._run_write_transaction(
                f"save_analysis_history[{result.code}]",
                _write,
            )
        except Exception as e:
            logger.error(f"保存分析历史失败: {e}")
            return 0

    def update_analysis_history_diagnostics(
        self,
        *,
        query_id: str,
        code: Optional[str] = None,
        diagnostics: Optional[Dict[str, Any]] = None,
        notification_runs: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """
        更新已保存分析历史的运行诊断快照。

        通知结果通常在分析历史落库后才产生，因此这里仅补写
        context_snapshot.diagnostics，不改变报告正文或其它历史字段。
        """
        if not query_id or (diagnostics is None and not notification_runs):
            return 0

        try:

            def _write(session: Session) -> int:
                conditions = [AnalysisHistory.query_id == query_id]
                if code:
                    conditions.append(AnalysisHistory.code == code)

                row = (
                    session.execute(
                        select(AnalysisHistory)
                        .where(and_(*conditions))
                        .order_by(desc(AnalysisHistory.created_at))
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )
                if row is None:
                    return 0

                context_snapshot: Dict[str, Any] = {}
                if row.context_snapshot:
                    try:
                        parsed = json.loads(row.context_snapshot)
                        if isinstance(parsed, dict):
                            context_snapshot = parsed
                    except Exception:
                        context_snapshot = {}

                if diagnostics is not None:
                    context_snapshot["diagnostics"] = diagnostics
                else:
                    existing_diagnostics = context_snapshot.get("diagnostics")
                    if not isinstance(existing_diagnostics, dict):
                        existing_diagnostics = {
                            "query_id": query_id,
                            "stock_code": code,
                            "notification_runs": [],
                        }
                    runs = existing_diagnostics.get("notification_runs")
                    if not isinstance(runs, list):
                        runs = []
                    trace_id = existing_diagnostics.get("trace_id")
                    for run in notification_runs or []:
                        if isinstance(run, dict):
                            run_payload = dict(run)
                            if trace_id and not run_payload.get("trace_id"):
                                run_payload["trace_id"] = trace_id
                            runs.append(run_payload)
                    existing_diagnostics["notification_runs"] = runs
                    context_snapshot["diagnostics"] = existing_diagnostics
                row.context_snapshot = self._safe_json_dumps(context_snapshot)
                return 1

            return self._run_write_transaction(
                f"update_analysis_history_diagnostics[{query_id}:{code or '*'}]",
                _write,
            )
        except Exception as e:
            logger.warning(
                "更新分析历史诊断快照失败（fail-open）: query_id=%s code=%s err=%s",
                query_id,
                code,
                e,
            )
            return 0

    def get_analysis_history(
        self,
        code: Optional[str] = None,
        query_id: Optional[str] = None,
        days: int = 30,
        limit: int = 50,
        exclude_query_id: Optional[str] = None,
    ) -> List[AnalysisHistory]:
        """
        Query analysis history records.

        Notes:
        - If query_id is provided, perform exact lookup and ignore days window.
        - If query_id is not provided, apply days-based time filtering.
        - exclude_query_id: exclude records with this query_id (for history comparison).
        """
        cutoff_date = datetime.now() - timedelta(days=days)

        with self.get_session() as session:
            conditions = []

            if query_id:
                conditions.append(AnalysisHistory.query_id == query_id)
            else:
                conditions.append(AnalysisHistory.created_at >= cutoff_date)

            if code:
                conditions.append(AnalysisHistory.code == code)

            # exclude_query_id only applies when not doing exact lookup (query_id is None)
            if exclude_query_id and not query_id:
                conditions.append(AnalysisHistory.query_id != exclude_query_id)

            results = (
                session.execute(
                    select(AnalysisHistory)
                    .where(and_(*conditions))
                    .order_by(desc(AnalysisHistory.created_at))
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            return list(results)

    def search_analysis_history(
        self,
        stock_code: str,
        query_id: Optional[str] = None,
        limit: int = 1,
    ) -> List[AnalysisHistory]:
        """按股票代码查询最新分析记录（用于 Serenity 报告回填）。

        Args:
            stock_code: 股票代码
            query_id: 关联的分析 query_id；给定时只返回该次分析的记录（精确回填）
            limit: 最多返回条数

        Returns:
            按 created_at DESC, id DESC 排列的记录列表（最多 limit 条）。
        """
        stmt = select(AnalysisHistory).where(AnalysisHistory.code == stock_code)
        if query_id:
            stmt = stmt.where(AnalysisHistory.query_id == query_id)
        with self.get_session() as session:
            return list(
                session.execute(
                    stmt.order_by(desc(AnalysisHistory.created_at), desc(AnalysisHistory.id)).limit(limit)
                )
                .scalars()
                .all()
            )

    def update_analysis_history_supply_chain(
        self,
        history_id: int,
        supply_chain_json: str,
    ) -> bool:
        """更新 analysis_history 的 supply_chain JSON 字段（用于 Serenity 报告 URL 回填）。

        Returns:
            True 成功，False 失败。
        """
        def _write(session: Session) -> bool:
            rec = session.get(AnalysisHistory, history_id)
            if rec is None:
                return False
            rec.supply_chain = supply_chain_json
            return True

        try:
            return bool(
                self._run_write_transaction(
                    f"update_sc_supply_chain[{history_id}]", _write
                )
            )
        except Exception as exc:
            logger.error(
                "update_analysis_history_supply_chain failed [%d]: %s", history_id, exc
            )
            return False

    def get_latest_analysis_history_id(
        self,
        *,
        query_id: str,
        code: str,
        report_type: str,
    ) -> Optional[int]:
        """Return the latest matching history id for read-only lookups.

        P2 automatic DecisionSignal extraction receives the freshly saved id
        directly from ``save_analysis_history()`` and does not use this helper.
        """

        if not query_id or not code or not report_type:
            return None

        with self.get_session() as session:
            return session.execute(
                select(AnalysisHistory.id)
                .where(
                    AnalysisHistory.query_id == query_id,
                    AnalysisHistory.code == code,
                    AnalysisHistory.report_type == report_type,
                )
                .order_by(desc(AnalysisHistory.created_at), desc(AnalysisHistory.id))
                .limit(1)
            ).scalar_one_or_none()

    def get_analysis_history_paginated(
        self,
        code: Optional[Union[str, List[str]]] = None,
        report_type: Optional[str] = None,
        start_date: Optional[date_type] = None,
        end_date: Optional[date_type] = None,
        offset: int = 0,
        limit: int = 20,
    ) -> Tuple[List[AnalysisHistory], int]:
        """
        分页查询分析历史记录（带总数）

        Args:
            code: 股票代码筛选
            report_type: 报告类型筛选
            start_date: 开始日期（含）
            end_date: 结束日期（含）
            offset: 偏移量（跳过前 N 条）
            limit: 每页数量

        Returns:
            Tuple[List[AnalysisHistory], int]: (记录列表, 总数)
        """
        from sqlalchemy import func

        with self.get_session() as session:
            conditions: List[Any] = []

            if code:
                if isinstance(code, list):
                    codes = [c for c in code if c]
                    if codes:
                        conditions.append(AnalysisHistory.code.in_(codes))
                else:
                    conditions.append(AnalysisHistory.code == code)
            if report_type:
                conditions.append(AnalysisHistory.report_type == report_type)
            if start_date:
                # created_at >= start_date 00:00:00
                conditions.append(
                    AnalysisHistory.created_at
                    >= datetime.combine(start_date, datetime.min.time())
                )
            if end_date:
                # created_at < end_date+1 00:00:00 (即 <= end_date 23:59:59)
                conditions.append(
                    AnalysisHistory.created_at
                    < datetime.combine(
                        end_date + timedelta(days=1), datetime.min.time()
                    )
                )

            # 构建 where 子句
            where_clause: Any = and_(*conditions) if conditions else True

            # 查询总数
            total_query = select(func.count(AnalysisHistory.id)).where(where_clause)
            total = session.execute(total_query).scalar() or 0

            # 查询分页数据
            data_query = (
                select(AnalysisHistory)
                .where(where_clause)
                .order_by(desc(AnalysisHistory.created_at))
                .offset(offset)
                .limit(limit)
            )
            results = session.execute(data_query).scalars().all()

            return list(results), total

    def get_analysis_history_by_id(self, record_id: int) -> Optional[AnalysisHistory]:
        """
        根据数据库主键 ID 查询单条分析历史记录

        由于 query_id 可能重复（批量分析时多条记录共享同一 query_id），
        使用主键 ID 确保精确查询唯一记录。

        Args:
            record_id: 分析历史记录的主键 ID

        Returns:
            AnalysisHistory 对象，不存在返回 None
        """
        with self.get_session() as session:
            result = (
                session.execute(
                    select(AnalysisHistory).where(AnalysisHistory.id == record_id)
                )
                .scalars()
                .first()
            )
            return result

    def delete_analysis_history_records(self, record_ids: List[int]) -> int:
        """
        删除指定的分析历史记录。

        同时清理依赖这些历史记录的回测结果和分析来源决策信号，避免
        依赖历史记录的派生数据残留。DecisionSignal 的 source_report_id
        允许弱引用，因此这里只清理 source_type=analysis 的真实历史绑定信号。

        Args:
            record_ids: 要删除的历史记录主键 ID 列表

        Returns:
            实际删除的历史记录数量
        """
        ids = sorted(
            {int(record_id) for record_id in record_ids if record_id is not None}
        )
        if not ids:
            return 0

        with self.session_scope() as session:
            existing_ids = sorted(
                session.execute(
                    select(AnalysisHistory.id).where(AnalysisHistory.id.in_(ids))
                )
                .scalars()
                .all()
            )
            if not existing_ids:
                return 0

            session.execute(
                delete(DecisionSignalRecord).where(
                    and_(
                        DecisionSignalRecord.source_type == "analysis",
                        DecisionSignalRecord.source_report_id.in_(existing_ids),
                    )
                )
            )
            session.execute(
                delete(BacktestResult).where(
                    BacktestResult.analysis_history_id.in_(existing_ids)
                )
            )
            result = session.execute(
                delete(AnalysisHistory).where(AnalysisHistory.id.in_(existing_ids))
            )
            return int(getattr(result, "rowcount", None) or 0)

    def save_deep_research_report(
        self,
        report_id: str,
        stock_code: str,
        stock_name: Optional[str],
        md_path: str,
        status: str,
        quality_score: int = 0,
        missing_layers: Optional[List[str]] = None,
        total_steps: int = 0,
        total_tokens: int = 0,
        provider: str = "",
    ) -> bool:
        """保存/更新一条深度投研报告元数据。

        ``report_id`` 重复则覆盖（SQLAlchemy merge）。失败返回 False。
        """

        def _write(session: Session) -> bool:
            record = DeepResearchReport(
                id=report_id,
                stock_code=stock_code,
                stock_name=stock_name,
                created_at=datetime.now(),
                md_path=md_path,
                pdf_path=None,
                status=status,
                quality_score=quality_score,
                missing_layers=json.dumps(missing_layers or [], ensure_ascii=False),
                total_steps=total_steps,
                total_tokens=total_tokens,
                provider=provider,
            )
            session.merge(record)
            return True

        try:
            self._run_write_transaction(f"save_deep_research[{report_id}]", _write)
            return True
        except Exception as exc:
            logger.error("save_deep_research_report failed [%s]: %s", report_id, exc)
            return False

    def set_deep_research_pdf_path(self, report_id: str, pdf_path: str) -> bool:
        """PDF 惰性生成后，回填 pdf_path 到元数据。"""

        def _write(session: Session) -> bool:
            rec = (
                session.execute(
                    select(DeepResearchReport).where(DeepResearchReport.id == report_id)
                )
                .scalars()
                .first()
            )
            if rec is None:
                return False
            rec.pdf_path = pdf_path
            return True

        try:
            result = self._run_write_transaction(f"set_pdf_path[{report_id}]", _write)
            return bool(result)
        except Exception as exc:
            logger.error("set_deep_research_pdf_path failed [%s]: %s", report_id, exc)
            return False

    def get_deep_research_reports(
        self,
        stock_code: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Tuple[List[DeepResearchReport], int]:
        """分页查询深度投研报告列表（按时间倒序）。返回 (记录列表, 总数)。"""
        with self.get_session() as session:
            conditions: List[Any] = []
            if stock_code:
                conditions.append(DeepResearchReport.stock_code == stock_code)
            where_clause: Any = and_(*conditions) if conditions else True

            total = (
                session.execute(
                    select(func.count(DeepResearchReport.id)).where(where_clause)
                ).scalar()
                or 0
            )

            rows = (
                session.execute(
                    select(DeepResearchReport)
                    .where(where_clause)
                    .order_by(desc(DeepResearchReport.created_at))
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return list(rows), int(total)

    def get_deep_research_report(self, report_id: str) -> Optional[DeepResearchReport]:
        """按主键查询单条报告。"""
        with self.get_session() as session:
            return (
                session.execute(
                    select(DeepResearchReport).where(DeepResearchReport.id == report_id)
                )
                .scalars()
                .first()
            )

    def delete_deep_research_report(
        self, report_id: str
    ) -> Optional[Dict[str, Optional[str]]]:
        """删除单条报告元数据，返回被删记录的 md_path/pdf_path（供调用方删文件）。"""

        def _write(session: Session) -> Optional[Dict[str, Optional[str]]]:
            rec = (
                session.execute(
                    select(DeepResearchReport).where(DeepResearchReport.id == report_id)
                )
                .scalars()
                .first()
            )
            if rec is None:
                return None
            paths = {
                "md_path": rec.md_path,
                "pdf_path": rec.pdf_path,
            }
            session.delete(rec)
            return paths

        try:
            return self._run_write_transaction(
                f"delete_deep_research[{report_id}]", _write
            )
        except Exception as exc:
            logger.error("delete_deep_research_report failed [%s]: %s", report_id, exc)
            return None

    def prune_deep_research_reports(
        self, max_reports: int
    ) -> List[Dict[str, Optional[str]]]:
        """清理超额报告（删最旧），返回被删记录的 md_path/pdf_path 列表（供调用方删文件）。

        Args:
            max_reports: 保留最新报告数量上限（>0 才执行）。

        Returns:
            被删记录的 {"md_path": ..., "pdf_path": ...} 列表（pdf_path 可能为 None）。
        """
        if max_reports <= 0:
            return []

        def _write(session: Session) -> List[Dict[str, Optional[str]]]:
            total = (
                session.execute(select(func.count(DeepResearchReport.id))).scalar() or 0
            )
            if total <= max_reports:
                return []
            excess = total - max_reports
            # 取最旧的 N 条 id
            old_ids = (
                session.execute(
                    select(DeepResearchReport.id)
                    .order_by(asc(DeepResearchReport.created_at))
                    .limit(excess)
                )
                .scalars()
                .all()
            )
            if not old_ids:
                return []
            # 再查一次获取完整记录（for 文件路径）
            old_recs = (
                session.execute(
                    select(DeepResearchReport).where(DeepResearchReport.id.in_(old_ids))
                )
                .scalars()
                .all()
            )
            result_paths = [
                {
                    "md_path": r.md_path,
                    "pdf_path": r.pdf_path,
                }
                for r in old_recs
            ]
            # 删除
            session.execute(
                delete(DeepResearchReport).where(DeepResearchReport.id.in_(old_ids))
            )
            return result_paths

        try:
            return self._run_write_transaction("prune_deep_research_reports", _write)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] prune deep_research_reports failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # 自反思日志（方案 v2.1）：操作指令 + 评分快照
    # ------------------------------------------------------------------

    def save_recommendation_journal(self, record: Dict[str, Any]) -> Optional[int]:
        """写入操作指令日志。返回自增 id；失败只记日志返回 None。"""
        session_local = self._SessionLocal
        if session_local is None:
            return None
        try:
            db = session_local()
            try:
                row = RecommendationJournal(
                    stock_code=str(record.get("stock_code") or ""),
                    stock_name=record.get("stock_name"),
                    action=str(record.get("action") or "观察"),
                    rating=record.get("rating"),
                    score=record.get("score"),
                    ideal_buy=record.get("ideal_buy"),
                    sell_price=record.get("sell_price"),
                    stop_loss=record.get("stop_loss"),
                    take_profit=record.get("take_profit"),
                    report_id=record.get("report_id"),
                    scoring_version=record.get("scoring_version"),
                )
                db.add(row)
                db.commit()
                return int(row.id)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] save_recommendation_journal failed: %s", exc)
            return None

    def save_score_journal(self, record: Dict[str, Any]) -> Optional[int]:
        """写入评分快照日志。返回自增 id；失败只记日志返回 None。"""
        session_local = self._SessionLocal
        if session_local is None:
            return None
        try:
            db = session_local()
            try:
                row = ScoreJournal(
                    stock_code=str(record.get("stock_code") or ""),
                    total_score=float(record.get("total_score") or 50.0),
                    dimensions_json=str(record.get("dimensions_json") or "{}"),
                    scoring_version=record.get("scoring_version"),
                    report_id=record.get("report_id"),
                )
                db.add(row)
                db.commit()
                return int(row.id)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] save_score_journal failed: %s", exc)
            return None

    def save_fundamentals_report(self, record: Dict[str, Any]) -> bool:
        session_local = self._SessionLocal
        if session_local is None:
            return False
        try:
            db = session_local()
            try:
                db.add(
                    FundamentalsReport(
                        id=str(record.get("id") or ""),
                        stock_code=str(record.get("stock_code") or ""),
                        stock_name=record.get("stock_name"),
                        md_path=str(record.get("md_path") or ""),
                        dims_json=record.get("dims_json"),
                    )
                )
                db.commit()
                return True
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] save_fundamentals_report failed: %s", exc)
            return False

    def get_fundamentals_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        session_local = self._SessionLocal
        if session_local is None:
            return None
        try:
            db = session_local()
            try:
                r = db.get(FundamentalsReport, report_id)
                if r is None:
                    return None
                return {
                    "id": r.id, "stock_code": r.stock_code, "stock_name": r.stock_name,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "md_path": r.md_path, "pdf_path": r.pdf_path, "dims_json": r.dims_json,
                }
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] get_fundamentals_report failed: %s", exc)
            return None

    def list_fundamentals_reports(
        self, stock_code: Optional[str] = None, limit: int = 50, offset: int = 0
    ) -> tuple[list, int]:
        session_local = self._SessionLocal
        if session_local is None:
            return [], 0
        try:
            db = session_local()
            try:
                query = db.query(FundamentalsReport)
                if stock_code:
                    query = query.filter(FundamentalsReport.stock_code == stock_code)
                total = query.count()
                rows = (
                    query.order_by(FundamentalsReport.created_at.desc())
                    .offset(offset).limit(limit).all()
                )
                return [
                    {
                        "id": r.id, "stock_code": r.stock_code, "stock_name": r.stock_name,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ], int(total)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] list_fundamentals_reports failed: %s", exc)
            return [], 0

    def delete_fundamentals_report(self, report_id: str) -> Optional[Dict[str, Optional[str]]]:
        session_local = self._SessionLocal
        if session_local is None:
            return None
        try:
            db = session_local()
            try:
                r = db.get(FundamentalsReport, report_id)
                if r is None:
                    return None
                paths = {"md_path": r.md_path, "pdf_path": r.pdf_path}
                db.delete(r)
                db.commit()
                return paths
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] delete_fundamentals_report failed: %s", exc)
            return None

    def save_financial_analysis_report(self, record: Dict[str, Any]) -> bool:
        session_local = self._SessionLocal
        if session_local is None:
            return False
        try:
            db = session_local()
            try:
                db.add(FinancialAnalysisReport(
                    id=str(record.get("id") or ""),
                    stock_code=str(record.get("stock_code") or ""),
                    stock_name=record.get("stock_name"),
                    md_path=str(record.get("md_path") or ""),
                    health_score=record.get("health_score"),
                    analysis_json=record.get("analysis_json"),
                ))
                db.commit()
                return True
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] save_financial_analysis_report failed: %s", exc)
            return False

    def get_financial_analysis_report(self, report_id: str) -> Optional[Dict[str, Any]]:
        session_local = self._SessionLocal
        if session_local is None:
            return None
        try:
            db = session_local()
            try:
                r = db.get(FinancialAnalysisReport, report_id)
                if r is None:
                    return None
                return {"id": r.id, "stock_code": r.stock_code, "stock_name": r.stock_name,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "md_path": r.md_path, "health_score": r.health_score,
                        "analysis_json": r.analysis_json}
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] get_financial_analysis_report failed: %s", exc)
            return None

    def list_financial_analysis_reports(self, stock_code=None, limit=50, offset=0):
        session_local = self._SessionLocal
        if session_local is None:
            return [], 0
        try:
            db = session_local()
            try:
                query = db.query(FinancialAnalysisReport)
                if stock_code:
                    query = query.filter(FinancialAnalysisReport.stock_code == stock_code)
                total = query.count()
                rows = query.order_by(FinancialAnalysisReport.created_at.desc()).offset(offset).limit(limit).all()
                return [{"id": r.id, "stock_code": r.stock_code, "stock_name": r.stock_name,
                         "created_at": r.created_at.isoformat() if r.created_at else None,
                         "health_score": r.health_score} for r in rows], int(total)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] list failed: %s", exc)
            return [], 0

    def delete_financial_analysis_report(self, report_id: str):
        session_local = self._SessionLocal
        if session_local is None:
            return None
        try:
            db = session_local()
            try:
                r = db.get(FinancialAnalysisReport, report_id)
                if r is None:
                    return None
                paths = {"md_path": r.md_path, "pdf_path": None}
                db.delete(r)
                db.commit()
                return paths
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] delete failed: %s", exc)
            return None

    def list_recommendation_journal(
        self, stock_code: Optional[str] = None, limit: int = 500
    ) -> List[Dict[str, Any]]:
        """操作指令日志列表（时间倒序）。"""
        session_local = self._SessionLocal
        if session_local is None:
            return []
        try:
            db = session_local()
            try:
                query = db.query(RecommendationJournal)
                if stock_code:
                    query = query.filter(RecommendationJournal.stock_code == stock_code)
                rows = (
                    query.order_by(RecommendationJournal.created_at.desc())
                    .limit(limit)
                    .all()
                )
                return [
                    {
                        "id": r.id,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "stock_code": r.stock_code,
                        "stock_name": r.stock_name,
                        "action": r.action,
                        "rating": r.rating,
                        "score": r.score,
                        "ideal_buy": r.ideal_buy,
                        "sell_price": r.sell_price,
                        "stop_loss": r.stop_loss,
                        "take_profit": r.take_profit,
                        "report_id": r.report_id,
                        "scoring_version": r.scoring_version,
                    }
                    for r in rows
                ]
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] list_recommendation_journal failed: %s", exc)
            return []

    def list_score_journal(
        self, stock_code: Optional[str] = None, limit: int = 500
    ) -> List[Dict[str, Any]]:
        """评分快照列表（时间倒序）。"""
        session_local = self._SessionLocal
        if session_local is None:
            return []
        try:
            db = session_local()
            try:
                query = db.query(ScoreJournal)
                if stock_code:
                    query = query.filter(ScoreJournal.stock_code == stock_code)
                rows = (
                    query.order_by(ScoreJournal.created_at.desc()).limit(limit).all()
                )
                return [
                    {
                        "id": r.id,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "stock_code": r.stock_code,
                        "total_score": r.total_score,
                        "dimensions_json": r.dimensions_json,
                        "scoring_version": r.scoring_version,
                        "report_id": r.report_id,
                    }
                    for r in rows
                ]
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Storage] list_score_journal failed: %s", exc)
            return []

    # ==================================================================
    # 政策与公告双维度排雷（PolicyMinesweeper）CRUD
    # ==================================================================

    def save_policy_minesweeper_report(
        self,
        report_id: str,
        stock_code: str,
        stock_name: Optional[str],
        md_path: str,
        status: str,
        horizon: str = "medium",
        alpha_ok: Optional[bool] = None,
        beta_ok: Optional[bool] = None,
        omega_ok: Optional[bool] = None,
        composite_score: Optional[int] = None,
        verdict: Optional[str] = None,
        confidence: Optional[int] = None,
        total_steps: int = 0,
        total_tokens: int = 0,
        provider: str = "",
    ) -> bool:
        """保存/更新一条政策与公告排雷报告元数据（report_id 重复则 merge 覆盖）。"""

        def _write(session: Session) -> bool:
            record = PolicyMinesweeperReport(
                id=report_id,
                stock_code=stock_code,
                stock_name=stock_name,
                created_at=datetime.now(),
                md_path=md_path,
                pdf_path=None,
                status=status,
                horizon=horizon,
                alpha_ok=alpha_ok,
                beta_ok=beta_ok,
                omega_ok=omega_ok,
                composite_score=composite_score,
                verdict=verdict,
                confidence=confidence,
                total_steps=total_steps,
                total_tokens=total_tokens,
                provider=provider,
            )
            session.merge(record)
            return True

        try:
            self._run_write_transaction(f"save_policy_minesweeper[{report_id}]", _write)
            return True
        except Exception as exc:
            logger.error(
                "save_policy_minesweeper_report failed [%s]: %s", report_id, exc
            )
            return False

    def set_policy_minesweeper_pdf_path(self, report_id: str, pdf_path: str) -> bool:
        """PDF 惰性生成后，回填 pdf_path 到元数据。"""

        def _write(session: Session) -> bool:
            rec = (
                session.execute(
                    select(PolicyMinesweeperReport).where(
                        PolicyMinesweeperReport.id == report_id
                    )
                )
                .scalars()
                .first()
            )
            if rec is None:
                return False
            rec.pdf_path = pdf_path  # type: ignore[assignment]
            return True

        try:
            result = self._run_write_transaction(
                f"set_policy_minesweeper_pdf_path[{report_id}]", _write
            )
            return bool(result)
        except Exception as exc:
            logger.error(
                "set_policy_minesweeper_pdf_path failed [%s]: %s", report_id, exc
            )
            return False

    def get_policy_minesweeper_reports(
        self,
        stock_code: Optional[str] = None,
        offset: int = 0,
        limit: int = 50,
    ) -> Tuple[List[PolicyMinesweeperReport], int]:
        """分页查询排雷报告列表（按时间倒序）。返回 (记录列表, 总数)。"""
        with self.get_session() as session:
            conditions: List[Any] = []
            if stock_code:
                conditions.append(PolicyMinesweeperReport.stock_code == stock_code)
            where_clause: Any = and_(*conditions) if conditions else True

            total = (
                session.execute(
                    select(func.count(PolicyMinesweeperReport.id)).where(where_clause)
                ).scalar()
                or 0
            )

            rows = (
                session.execute(
                    select(PolicyMinesweeperReport)
                    .where(where_clause)
                    .order_by(desc(PolicyMinesweeperReport.created_at))
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return list(rows), int(total)

    def get_policy_minesweeper_report(
        self, report_id: str
    ) -> Optional[PolicyMinesweeperReport]:
        """按主键查询单条报告。"""
        with self.get_session() as session:
            return (
                session.execute(
                    select(PolicyMinesweeperReport).where(
                        PolicyMinesweeperReport.id == report_id
                    )
                )
                .scalars()
                .first()
            )

    def delete_policy_minesweeper_report(
        self, report_id: str
    ) -> Optional[Dict[str, Optional[str]]]:
        """删除单条报告元数据，返回被删记录的 md_path/pdf_path（供调用方删文件）。"""

        def _write(session: Session) -> Optional[Dict[str, Optional[str]]]:
            rec = (
                session.execute(
                    select(PolicyMinesweeperReport).where(
                        PolicyMinesweeperReport.id == report_id
                    )
                )
                .scalars()
                .first()
            )
            if rec is None:
                return None
            paths = {
                "md_path": rec.md_path,
                "pdf_path": rec.pdf_path,
            }
            session.delete(rec)
            return paths

        try:
            return self._run_write_transaction(
                f"delete_policy_minesweeper[{report_id}]", _write
            )
        except Exception as exc:
            logger.error(
                "delete_policy_minesweeper_report failed [%s]: %s", report_id, exc
            )
            return None

    def prune_policy_minesweeper_reports(
        self, max_reports: int
    ) -> List[Dict[str, Optional[str]]]:
        """清理超额排雷报告（删最旧），返回被删记录的 md_path/pdf_path 列表。"""
        if max_reports <= 0:
            return []

        def _write(session: Session) -> List[Dict[str, Optional[str]]]:
            total = (
                session.execute(select(func.count(PolicyMinesweeperReport.id))).scalar()
                or 0
            )
            if total <= max_reports:
                return []
            excess = total - max_reports
            old_ids = (
                session.execute(
                    select(PolicyMinesweeperReport.id)
                    .order_by(asc(PolicyMinesweeperReport.created_at))
                    .limit(excess)
                )
                .scalars()
                .all()
            )
            old_recs = (
                session.execute(
                    select(PolicyMinesweeperReport).where(
                        PolicyMinesweeperReport.id.in_(old_ids)
                    )
                )
                .scalars()
                .all()
            )
            result_paths = [
                {
                    "md_path": r.md_path,
                    "pdf_path": r.pdf_path,
                }
                for r in old_recs
            ]
            session.execute(
                delete(PolicyMinesweeperReport).where(
                    PolicyMinesweeperReport.id.in_(old_ids)
                )
            )
            return result_paths

        try:
            return self._run_write_transaction(
                "prune_policy_minesweeper_reports", _write
            )
        except Exception as exc:
            logger.error("prune_policy_minesweeper_reports failed: %s", exc)
            return []

    # ==================================================================
    # 供应链分析报告（SupplyChain）CRUD
    # ==================================================================

    def save_supply_chain_report(
        self,
        report_id: str,
        topic: str,
        research_hint: Optional[str],
        md_path: str,
        status: str,
        total_steps: int = 0,
        total_tokens: int = 0,
        provider: str = "",
        model: Optional[str] = None,
        error: Optional[str] = None,
        stock_code: Optional[str] = None,
        stock_name: Optional[str] = None,
        deep_dive_json: Optional[str] = None,
        analysis_trace_id: Optional[str] = None,
    ) -> bool:
        """保存/更新一条供应链报告元数据（report_id 重复则 merge 覆盖）。失败返回 False。

        ``stock_code`` / ``stock_name`` 为可选单股绑定（按 docs/pdf-download-filename-plan.md
        §供应链报告边界 阶段 1），用于 PDF 文件名走单股型命名。
        ``deep_dive_json`` 为 [v3 PR-C] :class:`SupplyChainDeepDiveV3` 的 model_dump_json()。
        ``analysis_trace_id`` 用于精确关联到某次分析任务，便于后续回填 report_url。
        """

        def _write(session: Session) -> bool:
            record = SupplyChainReport(
                id=report_id,
                topic=topic,
                research_hint=research_hint,
                stock_code=stock_code,
                stock_name=stock_name,
                created_at=datetime.now(),
                analysis_trace_id=analysis_trace_id,
                md_path=md_path,
                pdf_path=None,
                deep_dive_json=deep_dive_json,
                status=status,
                total_steps=total_steps,
                total_tokens=total_tokens,
                provider=provider,
                model=model,
                error=error,
            )
            session.merge(record)
            return True

        try:
            self._run_write_transaction(f"save_supply_chain[{report_id}]", _write)
            return True
        except Exception as exc:
            logger.error("save_supply_chain_report failed [%s]: %s", report_id, exc)
            return False

    def set_supply_chain_pdf_path(self, report_id: str, pdf_path: str) -> bool:
        """PDF 惰性生成后，回填 pdf_path 到元数据。"""

        def _write(session: Session) -> bool:
            rec = (
                session.execute(
                    select(SupplyChainReport).where(SupplyChainReport.id == report_id)
                )
                .scalars()
                .first()
            )
            if rec is None:
                return False
            rec.pdf_path = pdf_path
            return True

        try:
            result = self._run_write_transaction(
                f"set_supply_chain_pdf[{report_id}]", _write
            )
            return bool(result)
        except Exception as exc:
            logger.error("set_supply_chain_pdf_path failed [%s]: %s", report_id, exc)
            return False

    def get_supply_chain_reports(
        self, offset: int = 0, limit: int = 50
    ) -> Tuple[List[SupplyChainReport], int]:
        """分页查询供应链报告列表（按时间倒序）。返回 (记录列表, 总数)。"""
        with self.get_session() as session:
            total = (
                session.execute(select(func.count(SupplyChainReport.id))).scalar() or 0
            )
            rows = (
                session.execute(
                    select(SupplyChainReport)
                    .order_by(desc(SupplyChainReport.created_at))
                    .offset(offset)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return list(rows), int(total)

    def get_supply_chain_report(self, report_id: str) -> Optional[SupplyChainReport]:
        """按主键查询单条报告。"""
        with self.get_session() as session:
            return (
                session.execute(
                    select(SupplyChainReport).where(SupplyChainReport.id == report_id)
                )
                .scalars()
                .first()
            )

    def get_latest_supply_chain_report_by_stock(
        self, stock_code: str
    ) -> Optional[SupplyChainReport]:
        """按股票代码查询最新的成功报告（created_at DESC）。用于 Serenity 缓存判断。

        只返回 ``status == "success"`` 的报告，partial/failed 不视为可复用缓存。
        """
        with self.get_session() as session:
            return (
                session.execute(
                    select(SupplyChainReport)
                    .where(
                        SupplyChainReport.stock_code == stock_code,
                        SupplyChainReport.status == "success",
                    )
                    .order_by(SupplyChainReport.created_at.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )

    def delete_supply_chain_report(
        self, report_id: str
    ) -> Optional[Dict[str, Optional[str]]]:
        """删除单条报告元数据，返回被删记录的 md_path/pdf_path（供调用方删文件）。"""

        def _write(session: Session) -> Optional[Dict[str, Optional[str]]]:
            rec = (
                session.execute(
                    select(SupplyChainReport).where(SupplyChainReport.id == report_id)
                )
                .scalars()
                .first()
            )
            if rec is None:
                return None
            paths: Dict[str, Optional[str]] = {
                "md_path": rec.md_path,
                "pdf_path": rec.pdf_path,
            }
            session.delete(rec)
            return paths

        try:
            return self._run_write_transaction(
                f"delete_supply_chain[{report_id}]", _write
            )
        except Exception as exc:
            logger.error("delete_supply_chain_report failed [%s]: %s", report_id, exc)
            return None

    def prune_supply_chain_reports(
        self, max_reports: int
    ) -> List[Dict[str, Optional[str]]]:
        """清理超额供应链报告（删最旧），返回被删记录的 md_path/pdf_path 列表。

        Args:
            max_reports: 保留最新报告数量上限（>0 才执行）。

        Returns:
            被删记录的 {"md_path": ..., "pdf_path": ...} 列表（pdf_path 可能为 None）。
        """
        if max_reports <= 0:
            return []

        def _write(session: Session) -> List[Dict[str, Optional[str]]]:
            total = (
                session.execute(select(func.count(SupplyChainReport.id))).scalar() or 0
            )
            if total <= max_reports:
                return []
            excess = total - max_reports
            old_ids = (
                session.execute(
                    select(SupplyChainReport.id)
                    .order_by(asc(SupplyChainReport.created_at))
                    .limit(excess)
                )
                .scalars()
                .all()
            )
            old_recs = (
                session.execute(
                    select(SupplyChainReport).where(SupplyChainReport.id.in_(old_ids))
                )
                .scalars()
                .all()
            )
            result_paths: List[Dict[str, Optional[str]]] = [
                {
                    "md_path": r.md_path,
                    "pdf_path": r.pdf_path,
                }
                for r in old_recs
            ]
            session.execute(
                delete(SupplyChainReport).where(SupplyChainReport.id.in_(old_ids))
            )
            return result_paths

        try:
            return self._run_write_transaction("prune_supply_chain_reports", _write)
        except Exception as exc:
            logger.error("prune_supply_chain_reports failed: %s", exc)
            return []

    def get_distinct_stocks_from_history(
        self,
        start_date: Optional[date_type] = None,
        end_date: Optional[date_type] = None,
        limit: int = 200,
        include_market_review: bool = False,
    ) -> List[AnalysisHistory]:
        """
        获取历史记录中的不重复股票列表，每只股票取最新一条记录。

        使用子查询按 code 分组取 MAX(id)，再 JOIN 回查完整记录。
        默认排除大盘复盘，避免混入普通个股栏。

        Args:
            start_date: 开始日期
            end_date: 结束日期
            limit: 最大返回数量
            include_market_review: 是否包含大盘复盘记录

        Returns:
            每条股票最新一条 AnalysisHistory 记录列表
        """
        with self.get_session() as session:
            subq: Any = select(
                AnalysisHistory.code,
                func.max(AnalysisHistory.id).label("max_id"),
            )
            if start_date:
                subq = subq.where(
                    AnalysisHistory.created_at
                    >= datetime.combine(start_date, datetime.min.time())
                )
            if end_date:
                subq = subq.where(
                    AnalysisHistory.created_at
                    < datetime.combine(
                        end_date + timedelta(days=1), datetime.min.time()
                    )
                )
            if not include_market_review:
                subq = subq.where(
                    and_(
                        AnalysisHistory.code != "MARKET",
                        or_(
                            AnalysisHistory.report_type.is_(None),
                            AnalysisHistory.report_type != "market_review",
                        ),
                    )
                )
            subq = subq.group_by(AnalysisHistory.code).subquery()

            results = (
                session.execute(
                    select(AnalysisHistory)
                    .join(subq, AnalysisHistory.id == subq.c.max_id)
                    .order_by(
                        desc(AnalysisHistory.created_at),
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            return list(results)

    def get_latest_analysis_by_query_id(
        self,
        query_id: str,
        *,
        code: Optional[str] = None,
        report_type: Optional[str] = None,
    ) -> Optional[AnalysisHistory]:
        """
        根据 query_id 查询最新一条分析历史记录

        query_id 在批量分析时可能重复，故返回最近创建的一条。

        Args:
            query_id: 分析记录关联的 query_id
            code: 可选股票代码过滤，用于区分同一 query_id 下的 MARKET 与个股记录
            report_type: 可选报告类型过滤

        Returns:
            AnalysisHistory 对象，不存在返回 None
        """
        with self.get_session() as session:
            conditions = [AnalysisHistory.query_id == query_id]
            if code:
                conditions.append(AnalysisHistory.code == code)
            if report_type:
                conditions.append(AnalysisHistory.report_type == report_type)

            result = (
                session.execute(
                    select(AnalysisHistory)
                    .where(and_(*conditions))
                    .order_by(desc(AnalysisHistory.created_at))
                    .limit(1)
                )
                .scalars()
                .first()
            )
            return result

    def get_data_range(
        self, code: str, start_date: date_type, end_date: date_type
    ) -> List[StockDaily]:
        """
        获取指定日期范围的数据

        Args:
            code: 股票代码
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            StockDaily 对象列表
        """
        with self.get_session() as session:
            results = (
                session.execute(
                    select(StockDaily)
                    .where(
                        and_(
                            StockDaily.code == code,
                            StockDaily.date >= start_date,
                            StockDaily.date <= end_date,
                        )
                    )
                    .order_by(StockDaily.date)
                )
                .scalars()
                .all()
            )

            return list(results)

    def save_daily_data(
        self, df: pd.DataFrame, code: str, data_source: str = "Unknown"
    ) -> int:
        """
        保存日线数据到数据库

        策略：
        - 按 `(code, date)` 做批量 UPSERT，已存在记录会覆盖更新
        - 同一批次内若存在重复日期，以最后一条记录为准
        - SQLite 分支按 chunk 写入以避免绑定参数上限

        Args:
            df: 包含日线数据的 DataFrame
            code: 股票代码
            data_source: 数据来源名称

        Returns:
            本次实际新增的记录数（不含更新）
        """
        if df is None or df.empty:
            logger.warning(f"保存数据为空，跳过 {code}")
            return 0

        now = datetime.now()
        records_by_date: Dict[date_type, Dict[str, Any]] = {}
        for row in df.to_dict(orient="records"):
            row_date = self._normalize_daily_date(row.get("date"))
            records_by_date[row_date] = {
                "code": code,
                "date": row_date,
                "open": self._normalize_sql_value(row.get("open")),
                "high": self._normalize_sql_value(row.get("high")),
                "low": self._normalize_sql_value(row.get("low")),
                "close": self._normalize_sql_value(row.get("close")),
                "volume": self._normalize_sql_value(row.get("volume")),
                "amount": self._normalize_sql_value(row.get("amount")),
                "pct_chg": self._normalize_sql_value(row.get("pct_chg")),
                "ma5": self._normalize_sql_value(row.get("ma5")),
                "ma10": self._normalize_sql_value(row.get("ma10")),
                "ma20": self._normalize_sql_value(row.get("ma20")),
                "volume_ratio": self._normalize_sql_value(row.get("volume_ratio")),
                "data_source": data_source,
                "created_at": now,
                "updated_at": now,
            }

        if not records_by_date:
            return 0

        records = list(records_by_date.values())
        batch_dates = list(records_by_date.keys())

        def _write(session: Session) -> int:
            if self._is_sqlite_engine:
                # SQLite has a per-statement bind-parameter limit (commonly 999).
                # Each record has ~15 columns, so chunk upserts to stay within bounds.
                _SQLITE_CHUNK = 50
                # `_run_write_transaction()` opens SQLite writes with
                # `BEGIN IMMEDIATE`, so existence checks and upsert execute
                # within one stable write window.
                existing_dates: set[Any] = set()
                _COUNT_CHUNK = 500
                for j in range(0, len(batch_dates), _COUNT_CHUNK):
                    chunk_dates = batch_dates[j : j + _COUNT_CHUNK]
                    if not chunk_dates:
                        continue
                    existing_dates.update(
                        session.execute(
                            select(StockDaily.date).where(
                                and_(
                                    StockDaily.code == code,
                                    StockDaily.date.in_(chunk_dates),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                new_records = [
                    record for record in records if record["date"] not in existing_dates
                ]
                for i in range(0, len(records), _SQLITE_CHUNK):
                    chunk = records[i : i + _SQLITE_CHUNK]
                    stmt = sqlite_insert(StockDaily).values(chunk)
                    excluded = stmt.excluded
                    session.execute(
                        stmt.on_conflict_do_update(
                            index_elements=["code", "date"],
                            set_={
                                "open": excluded.open,
                                "high": excluded.high,
                                "low": excluded.low,
                                "close": excluded.close,
                                "volume": excluded.volume,
                                "amount": excluded.amount,
                                "pct_chg": excluded.pct_chg,
                                "ma5": excluded.ma5,
                                "ma10": excluded.ma10,
                                "ma20": excluded.ma20,
                                "volume_ratio": excluded.volume_ratio,
                                "data_source": excluded.data_source,
                                "updated_at": excluded.updated_at,
                            },
                        )
                    )
                return len(new_records)
            else:
                existing_rows = {
                    row.date: row
                    for row in session.execute(
                        select(StockDaily).where(
                            and_(
                                StockDaily.code == code,
                                StockDaily.date.in_(batch_dates),
                            )
                        )
                    )
                    .scalars()
                    .all()
                }
                new_count = 0
                for record in records:
                    existing = existing_rows.get(record["date"])
                    if existing is None:
                        session.add(StockDaily(**record))
                        new_count += 1
                        continue
                    existing.open = record["open"]
                    existing.high = record["high"]
                    existing.low = record["low"]
                    existing.close = record["close"]
                    existing.volume = record["volume"]
                    existing.amount = record["amount"]
                    existing.pct_chg = record["pct_chg"]
                    existing.ma5 = record["ma5"]
                    existing.ma10 = record["ma10"]
                    existing.ma20 = record["ma20"]
                    existing.volume_ratio = record["volume_ratio"]
                    existing.data_source = record["data_source"]
                    existing.updated_at = record["updated_at"]
                return new_count

        try:
            saved_count = self._run_write_transaction(
                f"save_daily_data[{code}]",
                _write,
            )
            logger.info(f"保存 {code} 数据成功，新增 {saved_count} 条")
            return saved_count
        except Exception as e:
            logger.error(f"保存 {code} 数据失败: {e}")
            raise

    def get_analysis_context(
        self, code: str, target_date: Optional[date_type] = None
    ) -> Optional[Dict[str, Any]]:
        """
        获取分析所需的上下文数据

        返回今日数据 + 昨日数据的对比信息

        Args:
            code: 股票代码
            target_date: 目标日期（默认今天）

        Returns:
            包含今日数据、昨日对比等信息的字典
        """
        if target_date is None:
            target_date = date_type.today()
        # 注意：尽管入参提供了 target_date，但当前实现实际使用的是“最新两天数据”（get_latest_data），
        # 并不会按 target_date 精确取当日/前一交易日的上下文。
        # 因此若未来需要支持“按历史某天复盘/重算”的可解释性，这里需要调整。
        # 该行为目前保留（按需求不改逻辑）。

        # 获取最近2天数据
        recent_data = self.get_latest_data(code, days=2)

        if not recent_data:
            logger.warning(f"未找到 {code} 的数据")
            return None

        today_data = recent_data[0]
        yesterday_data = recent_data[1] if len(recent_data) > 1 else None

        context: Dict[str, Any] = {
            "code": code,
            "date": today_data.date.isoformat(),
            "today": today_data.to_dict(),
        }

        if yesterday_data:
            context["yesterday"] = yesterday_data.to_dict()

            # 计算相比昨日的变化
            if (
                yesterday_data.volume is not None
                and yesterday_data.volume > 0
                and today_data.volume is not None
            ):
                context["volume_change_ratio"] = float(
                    today_data.volume / yesterday_data.volume
                )

            if (
                yesterday_data.close is not None
                and yesterday_data.close > 0
                and today_data.close is not None
            ):
                context["price_change_ratio"] = float(
                    (today_data.close - yesterday_data.close)
                    / yesterday_data.close
                    * 100
                )

            # 均线形态判断
            context["ma_status"] = self._analyze_ma_status(today_data)

        return context

    def _analyze_ma_status(self, data: StockDaily) -> str:
        """
        分析均线形态

        判断条件：
        - 多头排列：close > ma5 > ma10 > ma20
        - 空头排列：close < ma5 < ma10 < ma20
        - 震荡整理：其他情况
        """
        # 注意：这里的均线形态判断基于“close/ma5/ma10/ma20”静态比较，
        # 未考虑均线拐点、斜率、或不同数据源复权口径差异。
        # 该行为目前保留（按需求不改逻辑）。
        close = data.close or 0
        ma5 = data.ma5 or 0
        ma10 = data.ma10 or 0
        ma20 = data.ma20 or 0

        if close > ma5 > ma10 > ma20 > 0:
            return "多头排列 📈"
        elif close < ma5 < ma10 < ma20 and ma20 > 0:
            return "空头排列 📉"
        elif close > ma5 and ma5 > ma10:
            return "短期向好 🔼"
        elif close < ma5 and ma5 < ma10:
            return "短期走弱 🔽"
        else:
            return "震荡整理 ↔️"

    @staticmethod
    def _parse_published_date(value: Optional[str]) -> Optional[datetime]:
        """
        解析发布时间字符串（失败返回 None）
        """
        if not value:
            return None

        if isinstance(value, datetime):
            return value

        text = str(value).strip()
        if not text:
            return None

        # 优先尝试 ISO 格式
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y/%m/%d",
        ):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue

        return None

    @staticmethod
    def _safe_json_dumps(data: Any) -> str:
        """
        安全序列化为 JSON 字符串
        """
        try:
            return json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            return json.dumps(str(data), ensure_ascii=False)

    @staticmethod
    def _build_raw_result(result: Any) -> Dict[str, Any]:
        """
        生成完整分析结果字典
        """
        data = result.to_dict() if hasattr(result, "to_dict") else {}
        data.update(
            {
                "data_sources": getattr(result, "data_sources", ""),
                "raw_response": getattr(result, "raw_response", None),
            }
        )
        return data

    @staticmethod
    def _parse_sniper_value(value: Any) -> Optional[float]:
        return parse_sniper_value(value)

    def _extract_sniper_points(self, result: Any) -> Dict[str, Optional[float]]:
        """Extract normalized sniper point values from an AnalysisResult."""

        return extract_sniper_points(result)

    @staticmethod
    def _build_fallback_url_key(
        code: str, title: str, source: str, published_date: Optional[datetime]
    ) -> str:
        """
        生成无 URL 时的去重键（确保稳定且较短）
        """
        date_str = published_date.isoformat() if published_date else ""
        raw_key = f"{code}|{title}|{source}|{date_str}"
        digest = hashlib.md5(raw_key.encode("utf-8")).hexdigest()
        return f"no-url:{code}:{digest}"

    def save_conversation_message(
        self, session_id: str, role: str, content: str
    ) -> int:
        """
        保存 Agent 对话消息
        """
        with self.session_scope() as session:
            msg = ConversationMessage(session_id=session_id, role=role, content=content)
            session.add(msg)
            session.flush()
            return int(msg.id)

    def get_conversation_history(
        self, session_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """
        获取 Agent 对话历史
        """
        with self.session_scope() as session:
            stmt = (
                select(ConversationMessage)
                .filter(ConversationMessage.session_id == session_id)
                .order_by(ConversationMessage.created_at.desc())
                .limit(limit)
            )
            messages = session.execute(stmt).scalars().all()

            # 倒序返回，保证时间顺序
            return [
                {"role": msg.role, "content": msg.content} for msg in reversed(messages)
            ]

    def get_visible_conversation_messages(
        self, session_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Return visible user/assistant conversation messages in chronological order."""
        with self.session_scope() as session:
            stmt = (
                select(ConversationMessage)
                .where(
                    and_(
                        ConversationMessage.session_id == session_id,
                        ConversationMessage.role.in_(["user", "assistant"]),
                    )
                )
                .order_by(ConversationMessage.created_at, ConversationMessage.id)
            )
            if limit is not None:
                stmt = (
                    stmt.order_by(None)
                    .order_by(
                        ConversationMessage.created_at.desc(),
                        ConversationMessage.id.desc(),
                    )
                    .limit(limit)
                )
            messages = session.execute(stmt).scalars().all()
            if limit is not None:
                messages = list(reversed(messages))
            return [
                {
                    "id": msg.id,
                    "role": msg.role,
                    "content": msg.content,
                    "created_at": msg.created_at,
                }
                for msg in messages
                if msg.content
            ]

    def get_conversation_summary(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the rolling summary for a conversation session, if present."""
        with self.session_scope() as session:
            stmt = select(ConversationSummary).where(
                ConversationSummary.session_id == session_id
            )
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return None
            return {
                "id": row.id,
                "session_id": row.session_id,
                "summary": row.summary,
                "covered_message_id": row.covered_message_id,
                "source_message_count": row.source_message_count,
                "estimated_tokens": row.estimated_tokens,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }

    def save_agent_provider_turn(
        self,
        *,
        session_id: str,
        run_id: str,
        provider: str,
        model: str,
        anchor_user_message_id: int,
        anchor_assistant_message_id: int,
        messages: List[Dict[str, Any]],
        contains_reasoning: bool,
        contains_tool_calls: bool,
        contains_thinking_blocks: bool,
        must_roundtrip: bool,
        estimated_tokens: int,
    ) -> int:
        """Persist one provider protocol trace and enforce per-model retention."""
        with self.session_scope() as session:
            row = AgentProviderTurn(
                session_id=session_id,
                run_id=run_id,
                provider=provider,
                model=model,
                anchor_user_message_id=int(anchor_user_message_id or 0),
                anchor_assistant_message_id=int(anchor_assistant_message_id or 0),
                messages_json=json.dumps(
                    messages or [], ensure_ascii=False, default=str
                ),
                contains_reasoning=bool(contains_reasoning),
                contains_tool_calls=bool(contains_tool_calls),
                contains_thinking_blocks=bool(contains_thinking_blocks),
                must_roundtrip=bool(must_roundtrip),
                estimated_tokens=int(estimated_tokens or 0),
            )
            session.add(row)
            session.flush()
            row_id = int(row.id)
            if row.must_roundtrip:
                self._trim_agent_provider_turns(
                    session=session,
                    session_id=session_id,
                    provider=provider,
                    model=model,
                    keep=PROVIDER_TRACE_RETENTION_LIMIT,
                )
            return row_id

    def get_agent_provider_turns(
        self,
        session_id: str,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        must_roundtrip_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return provider trace turns in chronological order."""
        with self.session_scope() as session:
            conditions = [AgentProviderTurn.session_id == session_id]
            if provider:
                conditions.append(AgentProviderTurn.provider == provider)
            if model:
                conditions.append(AgentProviderTurn.model == model)
            if must_roundtrip_only:
                conditions.append(AgentProviderTurn.must_roundtrip.is_(True))
            stmt = (
                select(AgentProviderTurn)
                .where(and_(*conditions))
                .order_by(AgentProviderTurn.created_at, AgentProviderTurn.id)
            )
            rows = session.execute(stmt).scalars().all()
            result = []
            for row in rows:
                try:
                    messages = json.loads(row.messages_json or "[]")
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Invalid provider trace messages_json skipped for session %s turn %s: %s",
                        row.session_id,
                        row.id,
                        exc,
                    )
                    messages = []
                result.append(
                    {
                        "id": row.id,
                        "session_id": row.session_id,
                        "run_id": row.run_id,
                        "provider": row.provider,
                        "model": row.model,
                        "anchor_user_message_id": row.anchor_user_message_id,
                        "anchor_assistant_message_id": row.anchor_assistant_message_id,
                        "messages": messages if isinstance(messages, list) else [],
                        "messages_json": row.messages_json,
                        "contains_reasoning": row.contains_reasoning,
                        "contains_tool_calls": row.contains_tool_calls,
                        "contains_thinking_blocks": row.contains_thinking_blocks,
                        "must_roundtrip": row.must_roundtrip,
                        "estimated_tokens": row.estimated_tokens,
                        "created_at": row.created_at,
                    }
                )
            return result

    def _trim_agent_provider_turns(
        self,
        *,
        session: Session,
        session_id: str,
        provider: str,
        model: str,
        keep: int,
    ) -> int:
        old_ids_stmt = (
            select(AgentProviderTurn.id)
            .where(
                and_(
                    AgentProviderTurn.session_id == session_id,
                    AgentProviderTurn.provider == provider,
                    AgentProviderTurn.model == model,
                    AgentProviderTurn.must_roundtrip.is_(True),
                )
            )
            .order_by(AgentProviderTurn.created_at.desc(), AgentProviderTurn.id.desc())
            .offset(max(0, int(keep)))
        )
        old_ids = list(session.execute(old_ids_stmt).scalars().all())
        if not old_ids:
            return 0
        result = session.execute(
            delete(AgentProviderTurn).where(AgentProviderTurn.id.in_(old_ids))
        )
        return int(getattr(result, "rowcount", None) or 0)

    def upsert_conversation_summary(
        self,
        session_id: str,
        summary: str,
        covered_message_id: int,
        source_message_count: int,
        estimated_tokens: int,
    ) -> None:
        """Create or update the rolling summary for a conversation session."""
        with self.session_scope() as session:
            now = datetime.now()
            values = {
                "session_id": session_id,
                "summary": summary,
                "covered_message_id": int(covered_message_id or 0),
                "source_message_count": int(source_message_count or 0),
                "estimated_tokens": int(estimated_tokens or 0),
                "updated_at": now,
            }
            stmt = sqlite_insert(ConversationSummary).values(**values)
            session.execute(
                stmt.on_conflict_do_update(
                    index_elements=["session_id"],
                    set_=values,
                )
            )

    def conversation_session_exists(self, session_id: str) -> bool:
        """Return True when at least one message exists for the given session."""
        with self.session_scope() as session:
            stmt = (
                select(ConversationMessage.id)
                .where(ConversationMessage.session_id == session_id)
                .limit(1)
            )
            return session.execute(stmt).scalar() is not None

    def get_chat_sessions(
        self,
        limit: int = 50,
        session_prefix: Optional[str] = None,
        extra_session_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        获取聊天会话列表（从 conversation_messages 聚合）

        Args:
            limit: Maximum number of sessions to return.
            session_prefix: If provided, only return sessions whose session_id
                starts with this prefix.  Used for per-user isolation (e.g.
                ``"telegram_12345"``).
            extra_session_ids: Optional exact session ids to include in
                addition to the scoped prefix.

        Returns:
            按最近活跃时间倒序的会话列表，每条包含 session_id, title, message_count, last_active
        """
        from sqlalchemy import func

        with self.session_scope() as session:
            normalized_prefix = None
            if session_prefix:
                normalized_prefix = (
                    session_prefix
                    if session_prefix.endswith(":")
                    else f"{session_prefix}:"
                )
            exact_ids = [sid for sid in (extra_session_ids or []) if sid]

            # 聚合每个 session 的消息数和最后活跃时间
            base = select(
                ConversationMessage.session_id,
                func.count(ConversationMessage.id).label("message_count"),
                func.min(ConversationMessage.created_at).label("created_at"),
                func.max(ConversationMessage.created_at).label("last_active"),
            )
            conditions = []
            if normalized_prefix:
                conditions.append(
                    ConversationMessage.session_id.startswith(normalized_prefix)
                )
            if exact_ids:
                conditions.append(ConversationMessage.session_id.in_(exact_ids))
            if conditions:
                base = base.where(or_(*conditions))
            stmt = (
                base.group_by(ConversationMessage.session_id)
                .order_by(desc(func.max(ConversationMessage.created_at)))
                .limit(limit)
            )
            rows = session.execute(stmt).all()

            results = []
            for row in rows:
                sid = row.session_id
                # 取该会话第一条 user 消息作为标题
                first_user_msg = session.execute(
                    select(ConversationMessage.content)
                    .where(
                        and_(
                            ConversationMessage.session_id == sid,
                            ConversationMessage.role == "user",
                        )
                    )
                    .order_by(ConversationMessage.created_at)
                    .limit(1)
                ).scalar()
                title = (first_user_msg or "新对话")[:60]

                results.append(
                    {
                        "session_id": sid,
                        "title": title,
                        "message_count": row.message_count,
                        "created_at": row.created_at.isoformat()
                        if row.created_at
                        else None,
                        "last_active": row.last_active.isoformat()
                        if row.last_active
                        else None,
                    }
                )
            return results

    def get_conversation_messages(
        self, session_id: str, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        获取单个会话的完整消息列表（用于前端恢复历史）
        """
        with self.session_scope() as session:
            stmt = (
                select(ConversationMessage)
                .where(ConversationMessage.session_id == session_id)
                .order_by(ConversationMessage.created_at)
                .limit(limit)
            )
            messages = session.execute(stmt).scalars().all()
            return [
                {
                    "id": str(msg.id),
                    "role": msg.role,
                    "content": msg.content,
                    "created_at": msg.created_at.isoformat()
                    if msg.created_at
                    else None,
                }
                for msg in messages
            ]

    def delete_conversation_session(self, session_id: str) -> int:
        """
        删除指定会话的所有消息

        Returns:
            删除的消息数
        """
        with self.session_scope() as session:
            session.execute(
                delete(AgentProviderTurn).where(
                    AgentProviderTurn.session_id == session_id
                )
            )
            session.execute(
                delete(ConversationSummary).where(
                    ConversationSummary.session_id == session_id
                )
            )
            result = session.execute(
                delete(ConversationMessage).where(
                    ConversationMessage.session_id == session_id
                )
            )
            return int(getattr(result, "rowcount", None) or 0)

    # ------------------------------------------------------------------
    # LLM usage tracking
    # ------------------------------------------------------------------

    def record_llm_usage(
        self,
        call_type: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        stock_code: Optional[str] = None,
        **telemetry: Any,
    ) -> None:
        """Append one LLM call record to llm_usage."""
        row_values: Dict[str, Any] = {
            "call_type": call_type,
            "model": model or "unknown",
            "stock_code": stock_code,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
        for column in _LLM_USAGE_TELEMETRY_COLUMN_SQL:
            row_values[column] = (
                None
                if column in _LLM_USAGE_DROPPED_FREE_TEXT_COLUMNS
                else telemetry.get(column)
            )
        row = LLMUsage(**row_values)
        with self.session_scope() as session:
            session.add(row)

    def get_llm_usage_summary(
        self,
        from_dt: datetime,
        to_dt: datetime,
    ) -> Dict[str, Any]:
        """Return aggregated token usage between from_dt and to_dt.

        Returns a dict with keys:
          total_calls, total_prompt_tokens, total_completion_tokens, total_tokens,
          by_call_type: list of {call_type, calls, prompt_tokens,
            completion_tokens, total_tokens},
          by_model: list of {model, calls, prompt_tokens, completion_tokens,
            total_tokens, max_total_tokens}
        """
        with self.session_scope() as session:
            base_filter = and_(
                LLMUsage.called_at >= from_dt,
                LLMUsage.called_at <= to_dt,
            )

            # Overall totals
            totals = session.execute(
                select(
                    func.count(LLMUsage.id).label("calls"),
                    func.coalesce(func.sum(LLMUsage.prompt_tokens), 0).label(
                        "prompt_tokens"
                    ),
                    func.coalesce(func.sum(LLMUsage.completion_tokens), 0).label(
                        "completion_tokens"
                    ),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("tokens"),
                ).where(base_filter)
            ).one()

            # Breakdown by call_type
            by_type_rows = session.execute(
                select(
                    LLMUsage.call_type,
                    func.count(LLMUsage.id).label("calls"),
                    func.coalesce(func.sum(LLMUsage.prompt_tokens), 0).label(
                        "prompt_tokens"
                    ),
                    func.coalesce(func.sum(LLMUsage.completion_tokens), 0).label(
                        "completion_tokens"
                    ),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("tokens"),
                )
                .where(base_filter)
                .group_by(LLMUsage.call_type)
                .order_by(desc(func.sum(LLMUsage.total_tokens)))
            ).all()

            # Breakdown by model
            by_model_rows = session.execute(
                select(
                    LLMUsage.model,
                    func.count(LLMUsage.id).label("calls"),
                    func.coalesce(func.sum(LLMUsage.prompt_tokens), 0).label(
                        "prompt_tokens"
                    ),
                    func.coalesce(func.sum(LLMUsage.completion_tokens), 0).label(
                        "completion_tokens"
                    ),
                    func.coalesce(func.sum(LLMUsage.total_tokens), 0).label("tokens"),
                    func.coalesce(func.max(LLMUsage.total_tokens), 0).label(
                        "max_total_tokens"
                    ),
                )
                .where(base_filter)
                .group_by(LLMUsage.model)
                .order_by(desc(func.sum(LLMUsage.total_tokens)))
            ).all()

        return {
            "total_calls": totals.calls,
            "total_prompt_tokens": totals.prompt_tokens,
            "total_completion_tokens": totals.completion_tokens,
            "total_tokens": totals.tokens,
            "by_call_type": [
                {
                    "call_type": r.call_type,
                    "calls": r.calls,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "total_tokens": r.tokens,
                }
                for r in by_type_rows
            ],
            "by_model": [
                {
                    "model": r.model,
                    "calls": r.calls,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "total_tokens": r.tokens,
                    "max_total_tokens": r.max_total_tokens,
                }
                for r in by_model_rows
            ],
        }

    def get_llm_usage_records(
        self,
        from_dt: datetime,
        to_dt: datetime,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return recent LLM usage audit rows between from_dt and to_dt.

        Each row contains id, call_type, model, stock_code, prompt_tokens,
        completion_tokens, total_tokens, and called_at. Results are ordered by
        newest call first, and limit is clamped to the public API range.
        """
        normalized_limit = max(1, min(int(limit or 50), 200))
        with self.session_scope() as session:
            rows = session.execute(
                select(
                    LLMUsage.id,
                    LLMUsage.call_type,
                    LLMUsage.model,
                    LLMUsage.stock_code,
                    LLMUsage.prompt_tokens,
                    LLMUsage.completion_tokens,
                    LLMUsage.total_tokens,
                    LLMUsage.called_at,
                )
                .where(
                    and_(
                        LLMUsage.called_at >= from_dt,
                        LLMUsage.called_at <= to_dt,
                    )
                )
                .order_by(desc(LLMUsage.called_at), desc(LLMUsage.id))
                .limit(normalized_limit)
            ).all()

        return [
            {
                "id": r.id,
                "call_type": r.call_type,
                "model": r.model,
                "stock_code": r.stock_code,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "total_tokens": r.total_tokens,
                "called_at": r.called_at,
            }
            for r in rows
        ]


# 便捷函数
def get_db() -> DatabaseManager:
    """获取数据库管理器实例的快捷方式"""
    return DatabaseManager.get_instance()


def persist_llm_usage(
    usage: Dict[str, Any],
    model: str,
    call_type: str,
    stock_code: Optional[str] = None,
) -> None:
    """Fire-and-forget: write one LLM call record to llm_usage. Never raises."""
    try:
        usage = usage or {}
        prompt_tokens = (
            _coerce_llm_usage_non_negative_int(usage.get("prompt_tokens")) or 0
        )
        completion_tokens = (
            _coerce_llm_usage_non_negative_int(usage.get("completion_tokens")) or 0
        )
        total_tokens = (
            _coerce_llm_usage_non_negative_int(usage.get("total_tokens")) or 0
        )
        telemetry = {
            column: usage.get(column) for column in _LLM_USAGE_TELEMETRY_COLUMN_SQL
        }
        for column in _LLM_USAGE_INTEGER_TELEMETRY_COLUMNS:
            telemetry[column] = _coerce_llm_usage_non_negative_int(
                telemetry.get(column)
            )
        telemetry["normalized_prompt_tokens"] = (
            telemetry.get("normalized_prompt_tokens")
            if telemetry.get("normalized_prompt_tokens") is not None
            else prompt_tokens
        )
        telemetry["normalized_completion_tokens"] = (
            telemetry.get("normalized_completion_tokens")
            if telemetry.get("normalized_completion_tokens") is not None
            else completion_tokens
        )
        telemetry["normalized_total_tokens"] = (
            telemetry.get("normalized_total_tokens")
            if telemetry.get("normalized_total_tokens") is not None
            else total_tokens
        )
        has_usage_payload = bool(usage.get("provider_usage_json")) or any(
            key in usage
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "normalized_prompt_tokens",
                "normalized_completion_tokens",
                "normalized_total_tokens",
            )
        )
        telemetry["cache_capability"] = usage.get("cache_capability") or "unknown"
        telemetry["cache_eligibility"] = usage.get("cache_eligibility") or "unknown"
        telemetry["cache_observation"] = usage.get("cache_observation") or (
            "no_usage" if not has_usage_payload else "unknown"
        )
        db = DatabaseManager.get_instance()
        db.record_llm_usage(
            call_type=call_type,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            stock_code=stock_code,
            **telemetry,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "[LLM usage] failed to persist usage record: %s", exc
        )


def _coerce_llm_usage_non_negative_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value < 0 or not value.is_integer():
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or not text.isdigit():
            return None
        return int(text)
    return None


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)

    db = get_db()

    print("=== 数据库测试 ===")
    print(f"数据库初始化成功")

    # 测试检查今日数据
    has_data = db.has_today_data("600519")
    print(f"茅台今日是否有数据: {has_data}")

    # 测试保存数据
    test_df = pd.DataFrame(
        {
            "date": [date_type.today()],
            "open": [1800.0],
            "high": [1850.0],
            "low": [1780.0],
            "close": [1820.0],
            "volume": [10000000],
            "amount": [18200000000],
            "pct_chg": [1.5],
            "ma5": [1810.0],
            "ma10": [1800.0],
            "ma20": [1790.0],
            "volume_ratio": [1.2],
        }
    )

    saved = db.save_daily_data(test_df, "600519", "TestSource")
    print(f"保存测试数据: {saved} 条")

    # 测试获取上下文
    context = db.get_analysis_context("600519")
    print(f"分析上下文: {context}")
