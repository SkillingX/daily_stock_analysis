# -*- coding: utf-8 -*-
"""
Shared factory for building fully-configured AgentExecutor instances.

Centralises construction to eliminate boilerplate duplicated across
api/v1/endpoints/agent.py, bot/commands/chat.py, bot/commands/ask.py,
and src/core/pipeline.py.

Performance notes
-----------------
* ``ToolRegistry`` is built once and cached at module level — tool
  registrations are immutable after setup so the object is safe to share
  across every request.
* ``SkillManager`` is expensive to create (loads YAML files from disk).
  A prototype is built on first use and cheap ``deepcopy`` clones are
  returned for each request, preserving thread-safety (``activate()``
  mutates internal state).

Usage::

    from src.agent.factory import build_agent_executor

    executor = build_agent_executor(config, skills=["bull_trend", "shrink_pullback"])
    result   = executor.chat(message="...", session_id="...")
"""

import copy
import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from src.config import AGENT_MAX_STEPS_DEFAULT
from typing import cast  # added by mypy_codemod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level caches
# ---------------------------------------------------------------------------
_TOOL_REGISTRY = None
_SKILL_MANAGER_PROTOTYPE = None
# Sentinel used as initial value so None (i.e. no custom dir) compares as "changed"
# on the very first call, forcing a build rather than accidentally skipping it.
_SENTINEL = object()
# Track which custom_dir the prototype was built with so we can invalidate
# the cache if AGENT_SKILL_DIR changes at runtime (e.g. via config reload).
_SKILL_MANAGER_CUSTOM_DIR: object = _SENTINEL


@dataclass
class SkillPromptState:
    """Resolved skill activation + prompt fragments for analysis entrypoints."""

    skill_manager: object
    skills_to_activate: List[str]
    explicit_skill_selection: bool
    use_legacy_default_prompt: bool
    skill_instructions: str
    default_skill_policy: str
    technical_skill_policy: str


def _coerce_config_int(
    raw_value: object, default: int, *, field_name: str | None = None
) -> int:
    """Coerce optional numeric config values to int with a fallback default.

    This protects test doubles and incomplete config objects from propagating
    mock-like values (e.g., MagicMock attributes) into strict numeric paths.

    This function is side-effect free: it only returns a parsed int fallback value
    and intentionally never mutates source config attributes.
    """

    try:
        if isinstance(raw_value, bool):
            return int(default)
        return cast(int, int(raw_value))  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        if field_name:
            logger.warning(
                "[AgentFactory] Invalid value for %s: %r, fallback to default %s",
                field_name,
                raw_value,
                default,
            )
        return default


def _normalize_skill_ids(
    skill_ids: Optional[List[str]],
    *,
    available_skill_ids: set[str],
) -> tuple[List[str], List[str]]:
    """Return validated skill ids plus unknown ids, preserving input order."""
    normalized: List[str] = []
    unknown: List[str] = []

    for skill_id in skill_ids or []:
        if not isinstance(skill_id, str):
            continue
        cleaned = skill_id.strip()
        if not cleaned:
            continue
        if cleaned == "all":
            if "all" not in normalized:
                normalized.append("all")
            continue
        if cleaned in available_skill_ids:
            if cleaned not in normalized:
                normalized.append(cleaned)
            continue
        if cleaned not in unknown:
            unknown.append(cleaned)

    return normalized, unknown


def _resolve_selected_skill_ids(
    *,
    requested_skills: Optional[List[str]],
    configured_skills: Optional[List[str]],
    default_skills: List[str],
    available_skill_ids: set[str],
) -> tuple[List[str], bool]:
    """Resolve active skill ids and whether they came from a valid explicit selection."""
    selection_source = None
    raw_skill_ids = None
    if requested_skills is not None:
        selection_source = "request"
        raw_skill_ids = requested_skills
    elif configured_skills is not None:
        selection_source = "config"
        raw_skill_ids = configured_skills
    else:
        return list(default_skills), False

    selected_skill_ids, unknown_skill_ids = _normalize_skill_ids(
        raw_skill_ids,
        available_skill_ids=available_skill_ids,
    )
    if unknown_skill_ids:
        logger.warning(
            "[AgentFactory] Ignoring unknown %s skill ids: %s",
            selection_source,
            unknown_skill_ids,
        )
    if selected_skill_ids:
        return selected_skill_ids, True

    if raw_skill_ids:
        logger.warning(
            "[AgentFactory] No valid %s skills remain after validation; falling back to default skills: %s",
            selection_source,
            default_skills,
        )
    return list(default_skills), False


def _should_use_legacy_default_prompt(
    *,
    skills_to_activate: List[str],
    explicit_skill_selection: bool,
    skill_catalog: Sequence[object],
) -> bool:
    """Keep the legacy prompt only for the implicit built-in bull_trend fallback."""
    if explicit_skill_selection or skills_to_activate != ["bull_trend"]:
        return False

    bull_trend_skill = next(
        (
            skill
            for skill in skill_catalog
            if str(getattr(skill, "name", "")).strip() == "bull_trend"
        ),
        None,
    )
    return getattr(bull_trend_skill, "source", None) == "builtin"


def get_tool_registry():
    """Return a cached ToolRegistry (built once, shared across requests)."""
    global _TOOL_REGISTRY
    if _TOOL_REGISTRY is not None:
        return _TOOL_REGISTRY

    from src.agent.tools.registry import ToolRegistry
    from src.agent.tools.data_tools import ALL_DATA_TOOLS
    from src.agent.tools.analysis_tools import ALL_ANALYSIS_TOOLS
    from src.agent.tools.search_tools import ALL_SEARCH_TOOLS
    from src.agent.tools.market_tools import ALL_MARKET_TOOLS
    from src.agent.tools.backtest_tools import ALL_BACKTEST_TOOLS
    from src.agent.tools.knowledge_base_tools import (
        SEARCH_KNOWLEDGE_BASE_TOOL,
        LIST_KNOWLEDGE_DOCUMENTS_TOOL,
    )

    registry = ToolRegistry()
    for tool_fn in (
        ALL_DATA_TOOLS
        + ALL_ANALYSIS_TOOLS
        + ALL_SEARCH_TOOLS
        + ALL_MARKET_TOOLS
        + ALL_BACKTEST_TOOLS
    ):
        registry.register(tool_fn)

    # Register knowledge base tools
    registry.register(SEARCH_KNOWLEDGE_BASE_TOOL)
    registry.register(LIST_KNOWLEDGE_DOCUMENTS_TOOL)

    _TOOL_REGISTRY = registry
    logger.info(
        "[AgentFactory] ToolRegistry cached (%d tools)",
        len(registry._tools) if hasattr(registry, "_tools") else -1,
    )
    return _TOOL_REGISTRY


def get_skill_manager(config=None):
    """Return a deepcopy-clone of the cached SkillManager prototype.

    The prototype is initialised from disk on first call; subsequent calls
    return ``copy.deepcopy(prototype)`` which is ~10× faster than re-reading
    YAML files.  Each clone is independent so ``.activate()`` calls do not
    bleed between requests.

    Cache invalidation: if ``config.agent_skill_dir`` changes at runtime
    (e.g. via the web settings reload), the prototype is rebuilt automatically.
    """
    global _SKILL_MANAGER_PROTOTYPE, _SKILL_MANAGER_CUSTOM_DIR

    if config is None:
        from src.config import get_config

        config = get_config()

    current_custom_dir = getattr(config, "agent_skill_dir", None)
    if (
        _SKILL_MANAGER_PROTOTYPE is not None
        and current_custom_dir == _SKILL_MANAGER_CUSTOM_DIR
    ):
        return copy.deepcopy(_SKILL_MANAGER_PROTOTYPE)

    from src.agent.skills.base import SkillManager

    if _SKILL_MANAGER_PROTOTYPE is not None:
        logger.info(
            "[AgentFactory] SkillManager prototype invalidated (agent_skill_dir changed: %r -> %r)",
            _SKILL_MANAGER_CUSTOM_DIR,
            current_custom_dir,
        )

    skill_manager = SkillManager()
    skill_manager.load_builtin_skills()

    if current_custom_dir:
        try:
            skill_manager.load_custom_skills(current_custom_dir)
        except Exception as exc:
            logger.warning(
                "[AgentFactory] Failed to load custom skills from %s: %s",
                current_custom_dir,
                exc,
            )

    _SKILL_MANAGER_PROTOTYPE = skill_manager
    _SKILL_MANAGER_CUSTOM_DIR = current_custom_dir
    logger.info(
        "[AgentFactory] SkillManager prototype cached (%d skills)",
        len(skill_manager._skills),
    )
    return copy.deepcopy(_SKILL_MANAGER_PROTOTYPE)


def resolve_skill_prompt_state(
    config=None, skills: Optional[List[str]] = None
) -> SkillPromptState:
    """Resolve active skills and prompt fragments for analyzer / agent entrypoints."""
    if config is None:
        from src.config import get_config

        config = get_config()

    from src.agent.skills.defaults import (
        get_default_active_skill_ids,
        get_default_technical_skill_policy,
        get_default_trading_skill_policy,
    )

    skill_manager = get_skill_manager(config)
    skill_catalog = list(skill_manager.list_skills())
    available_skill_ids = {
        str(getattr(skill, "name", "")).strip()
        for skill in skill_catalog
        if str(getattr(skill, "name", "")).strip()
    }
    configured_skills = getattr(config, "agent_skills", None)
    if configured_skills == []:
        configured_skills = None
    default_skills = get_default_active_skill_ids(
        skill_catalog,
        available_skill_ids=available_skill_ids or None,
    )
    skills_to_activate, explicit_skill_selection = _resolve_selected_skill_ids(
        requested_skills=skills,
        configured_skills=configured_skills,
        default_skills=default_skills,
        available_skill_ids=available_skill_ids,
    )

    use_legacy_default_prompt = _should_use_legacy_default_prompt(
        skills_to_activate=skills_to_activate,
        explicit_skill_selection=explicit_skill_selection,
        skill_catalog=skill_catalog,
    )

    skill_manager.activate(skills_to_activate)
    logger.info("[AgentFactory] Activated skills: %s", skills_to_activate)

    return SkillPromptState(
        skill_manager=skill_manager,
        skills_to_activate=skills_to_activate,
        explicit_skill_selection=explicit_skill_selection,
        use_legacy_default_prompt=use_legacy_default_prompt,
        skill_instructions=skill_manager.get_skill_instructions(),
        default_skill_policy=get_default_trading_skill_policy(
            explicit_skill_selection=not use_legacy_default_prompt,
        ),
        technical_skill_policy=get_default_technical_skill_policy(
            explicit_skill_selection=not use_legacy_default_prompt,
        ),
    )


def build_agent_executor(config=None, skills: Optional[List[str]] = None):
    """Build and return a configured AgentExecutor (or future orchestrator).

    When ``AGENT_ARCH=multi``, this returns an orchestrator that manages
    multiple specialised agents. Otherwise it returns the legacy single-agent
    executor.

    Args:
        config: Application config object.  When *None*, ``get_config()`` is
                called automatically.
        skills: Skill ids to activate.  When *None* falls back to
                ``config.agent_skills``; if that is also empty falls back to
                the central default skill set.

    Returns:
        A ready-to-call :class:`src.agent.executor.AgentExecutor` instance.
    """
    if config is None:
        from src.config import get_config

        config = get_config()

    arch = getattr(config, "agent_arch", "single")

    from src.agent.llm_adapter import LLMToolAdapter

    registry = get_tool_registry()
    prompt_state = resolve_skill_prompt_state(config, skills=skills)
    skill_manager = prompt_state.skill_manager
    logger.info(
        "[AgentFactory] Resolved skill prompt state: skills=%s (arch=%s, explicit=%s, legacy_default_prompt=%s)",
        prompt_state.skills_to_activate,
        arch,
        prompt_state.explicit_skill_selection,
        prompt_state.use_legacy_default_prompt,
    )

    llm_adapter = LLMToolAdapter(config)

    if arch == "multi":
        return _build_orchestrator(
            config,
            registry,
            llm_adapter,
            skill_manager,
            technical_skill_policy=prompt_state.technical_skill_policy,
        )

    from src.agent.executor import AgentExecutor

    # Intentionally do not mutate config routing fields here. We only coerce
    # execution params (max_steps/timeout_seconds) from config values; provider,
    # model, base URL and channel routes stay unchanged and are consumed by
    # downstream adapter logic as-is.
    return AgentExecutor(
        tool_registry=registry,
        llm_adapter=llm_adapter,
        skill_instructions=prompt_state.skill_instructions,
        default_skill_policy=prompt_state.default_skill_policy,
        use_legacy_default_prompt=prompt_state.use_legacy_default_prompt,
        max_steps=_coerce_config_int(
            getattr(config, "agent_max_steps", AGENT_MAX_STEPS_DEFAULT),
            AGENT_MAX_STEPS_DEFAULT,
            field_name="agent_max_steps",
        ),
        timeout_seconds=_coerce_config_int(
            getattr(config, "agent_orchestrator_timeout_s", 0),
            0,
            field_name="agent_orchestrator_timeout_s",
        ),
    )


def _build_orchestrator(
    config, registry, llm_adapter, skill_manager, *, technical_skill_policy: str = ""
):
    """Build and return an :class:`AgentOrchestrator` (multi-agent mode).

    The orchestrator presents the same ``run()`` / ``chat()`` interface as
    :class:`AgentExecutor` so callers need no changes.
    """
    from src.agent.orchestrator import AgentOrchestrator

    mode = getattr(config, "agent_orchestrator_mode", "standard")
    logger.info("[AgentFactory] Building AgentOrchestrator (mode=%s)", mode)

    return AgentOrchestrator(
        tool_registry=registry,
        llm_adapter=llm_adapter,
        skill_instructions=skill_manager.get_skill_instructions(),
        technical_skill_policy=technical_skill_policy,
        max_steps=_coerce_config_int(
            getattr(config, "agent_max_steps", AGENT_MAX_STEPS_DEFAULT),
            AGENT_MAX_STEPS_DEFAULT,
            field_name="agent_max_steps",
        ),
        mode=mode,
        skill_manager=skill_manager,
        config=config,
    )


# Keep legacy alias so any external callers using the old name still work.
build_executor = build_agent_executor


def build_zhengxi_executor(config=None):
    """构建郑希投研分析专属 Executor。

    与问股 :func:`build_agent_executor` 完全隔离：构造**独立** ToolRegistry
    实例（只注册郑希 3 个工具，不污染问股全局缓存的工具集），复用
    :class:`LLMToolAdapter` 多渠道基建，返回
    :class:`src.agent.zhengxi_executor.ZhengxiExecutor`。
    """
    if config is None:
        from src.config import get_config

        config = get_config()

    from src.agent.llm_adapter import LLMToolAdapter
    from src.agent.tools.registry import ToolRegistry
    from src.agent.tools.zhengxi_tools import ALL_ZHENGXI_TOOLS
    from src.agent.zhengxi_executor import ZhengxiExecutor

    registry = ToolRegistry()
    for tool in ALL_ZHENGXI_TOOLS:
        registry.register(tool)
    logger.info("[AgentFactory] Zhengxi ToolRegistry built (%d tools)", len(registry))

    llm_adapter = LLMToolAdapter(config)

    return ZhengxiExecutor(
        tool_registry=registry,
        llm_adapter=llm_adapter,
        max_steps=_coerce_config_int(
            getattr(config, "agent_max_steps", AGENT_MAX_STEPS_DEFAULT),
            AGENT_MAX_STEPS_DEFAULT,
            field_name="agent_max_steps",
        ),
        timeout_seconds=_coerce_config_int(
            getattr(config, "agent_orchestrator_timeout_s", 0),
            0,
            field_name="agent_orchestrator_timeout_s",
        ),
    )


def build_supply_chain_executor(config=None):
    """构建供应链分析（Serenity 方法）专属 Executor。

    与问股/郑希的关键差异：
    - **工具集**：复用问股 ``get_tool_registry()`` 的 18 个工具（行情/基本面/新闻/技术）
      + 注册 1 个供应链打分工具，全部装入**独立 ToolRegistry 实例**（复制问股工具，
      不在单例上 register，避免污染问股）。
    - **长任务**：``max_steps=40`` / ``wall_clock=1200s``（9 步深度调研 5–15 分钟），
      硬编码不读 ``config.agent_max_steps``（方案 A），问股/郑希仍用默认 10，互不影响。
    """
    if config is None:
        from src.config import get_config

        config = get_config()

    from src.agent.llm_adapter import LLMToolAdapter
    from src.agent.tools.registry import ToolRegistry
    from src.agent.tools.supply_chain_tools import ALL_SUPPLY_CHAIN_TOOLS
    from src.agent.supply_chain_executor import SupplyChainExecutor

    # 复用问股工具集（新建 registry 实例，复制工具，不污染全局单例）+ 注册供应链工具
    source_registry = get_tool_registry()
    registry = ToolRegistry()
    for tool in source_registry.list_tools():
        registry.register(tool)
    for tool in ALL_SUPPLY_CHAIN_TOOLS:
        registry.register(tool)
    logger.info(
        "[AgentFactory] SupplyChain ToolRegistry built (%d tools = %d wengu + %d supply_chain)",
        len(registry.list_tools()),
        len(source_registry.list_tools()),
        len(ALL_SUPPLY_CHAIN_TOOLS),
    )

    llm_adapter = LLMToolAdapter(config)

    return SupplyChainExecutor(
        tool_registry=registry,
        llm_adapter=llm_adapter,
        max_steps=40,
        timeout_seconds=1200.0,
    )


# 深度投研所需的精确工具集（去掉 backtest_tools + 冗余分析工具，减少 LLM 决策噪音）。
# 对应「消息面+产业链主导」新八章（docs/deep-research-chain-news-logic-plan.md）：
# 消息面(search_stock_news/search_comprehensive_intel + get_market_indices) +
# 产业链(get_sector_rankings + search_comprehensive_intel) +
# 财务估值 / 技术面 +
# 财务估值(get_stock_info) +
# 技术面(analyze_trend/get_capital_flow) +
# 通用(行情/K线/新闻)。
_DEEP_RESEARCH_TOOL_NAMES = {
    "get_realtime_quote",
    "get_daily_history",
    "get_stock_info",
    "get_capital_flow",
    "analyze_trend",
    "search_stock_news",
    "search_comprehensive_intel",
    "get_market_indices",
    "get_sector_rankings",
}


def build_deep_research_executor(config=None):
    """构建深度投研报告专属 Executor（消息面+产业链主导框架）。

    与问股/供应链的差异：
    - **工具集**：从问股 ``get_tool_registry()`` 精确筛选 11 个工具（去掉 backtest_tools
      与冗余分析工具 calculate_ma/get_volume_analysis/analyze_pattern，降低 LLM 决策噪音），
      装入**独立 ToolRegistry 实例**（复制不污染问股单例）。
      9 个工具对应新八章：消息面 / 产业链 / 财务估值 / 技术面 + 通用行情
      （板块归属公开双源校验已随 issue #45 退役）。
    - **长任务**：``max_steps=30`` / ``wall_clock=1200s``（八章穿透 + 报告生成 2–5 分钟），
      硬编码不读 ``config.agent_max_steps``，与问股(10)/供应链(40)互不影响。
    - 返回 :class:`src.agent.deep_research_executor.DeepResearchExecutor`。
    """
    if config is None:
        from src.config import get_config

        config = get_config()

    from src.agent.deep_research_executor import DeepResearchExecutor
    from src.agent.llm_adapter import LLMToolAdapter
    from src.agent.tools.registry import ToolRegistry

    source_registry = get_tool_registry()
    registry = ToolRegistry()
    registered = 0
    for tool in source_registry.list_tools():
        if tool.name in _DEEP_RESEARCH_TOOL_NAMES:
            registry.register(tool)
            registered += 1

    missing = _DEEP_RESEARCH_TOOL_NAMES - set(registry.list_names())
    if missing:
        logger.warning("[AgentFactory] DeepResearch 缺失工具: %s", sorted(missing))
    logger.info(
        "[AgentFactory] DeepResearch ToolRegistry built (%d/%d tools)",
        registered,
        len(_DEEP_RESEARCH_TOOL_NAMES),
    )

    llm_adapter = LLMToolAdapter(config)

    return DeepResearchExecutor(
        tool_registry=registry,
        llm_adapter=llm_adapter,
        max_steps=30,
        timeout_seconds=1200.0,
    )


def build_chanlun_executor(config=None):
    """构建缠论分析专属 Executor。

    与问股/郑希/供应链的关键差异：
    - **工具集**：只注册 1 个缠论分析工具 ``analyze_chanlun``
    - **短任务**：``max_steps=10`` / ``wall_clock=180s``
    """
    if config is None:
        from src.config import get_config

        config = get_config()

    from src.agent.llm_adapter import LLMToolAdapter
    from src.agent.tools.registry import ToolRegistry
    from src.agent.tools.chanlun_tools import ALL_CHANLUN_TOOLS
    from src.agent.chanlun_executor import ChanlunExecutor

    registry = ToolRegistry()
    for tool in ALL_CHANLUN_TOOLS:
        registry.register(tool)
    logger.info(
        "[AgentFactory] Chanlun ToolRegistry built (1 tool: analyze_chanlun)",
    )

    llm_adapter = LLMToolAdapter(config)

    return ChanlunExecutor(
        tool_registry=registry,
        llm_adapter=llm_adapter,
        max_steps=10,
        timeout_seconds=180.0,
    )


# 政策与公告双维度排雷所需的精确工具集（α 公告扫描 + β 政策分析 共享）。
# 严格对齐 executor 三角色 system prompt 引用的工具，去掉 backtest/技术博弈类工具，
# 降低 LLM 在「政策/公告/基本面」语境下的决策噪音。Ω 的 score_policy_minesweeper
# 单独通过 ALL_POLICY_MINESWEEPER_TOOLS 注册（见 build_policy_minesweeper_executor）。
_POLICY_MINESWEEPER_TOOL_NAMES = {
    "search_stock_news",  # α：公司公告/新闻
    "search_comprehensive_intel",  # α/β：综合情报（含政策）
    "get_stock_info",  # α/β：基本面（盈利/估值影响）
    "get_realtime_quote",  # α：行情（价格敏感度）
    "get_sector_rankings",  # β：板块/产业（政策暴露度）
}


def build_policy_minesweeper_executor(config=None):
    """构建政策与公告双维度排雷专属 Executor（α/β 并行 → Ω 综合）。

    与深度投研/供应链的差异：
    - **工具集**：从问股 ``get_tool_registry()`` 精确筛选 5 个工具（公告/新闻 + 综合情报 +
      基本面 + 行情 + 板块，覆盖 α 公告扫描与 β 政策分析），装入**独立 ToolRegistry 实例**
      （复制不污染问股单例），再注册 1 个排雷打分工具 ``score_policy_minesweeper``（Ω 用）。
    - **三 Agent 边界**：``max_steps_ab=10`` / ``max_steps_omega=6``；``wall_ab=300s`` /
      ``wall_omega=240s``（α/β 并行 + Ω 串行，总时长约 9 分钟，低于 SSE 960s 超时），
      硬编码在 executor 默认值，不读 ``config.agent_max_steps``，与问股/郑希/供应链互不影响。
    - 返回 :class:`src.agent.policy_minesweeper_executor.PolicyMinesweeperExecutor`。
    """
    if config is None:
        from src.config import get_config

        config = get_config()

    from src.agent.llm_adapter import LLMToolAdapter
    from src.agent.policy_minesweeper_executor import PolicyMinesweeperExecutor
    from src.agent.tools.policy_minesweeper_tools import ALL_POLICY_MINESWEEPER_TOOLS
    from src.agent.tools.registry import ToolRegistry

    source_registry = get_tool_registry()
    registry = ToolRegistry()
    registered = 0
    for tool in source_registry.list_tools():
        if tool.name in _POLICY_MINESWEEPER_TOOL_NAMES:
            registry.register(tool)
            registered += 1

    for tool in ALL_POLICY_MINESWEEPER_TOOLS:
        registry.register(tool)

    missing = _POLICY_MINESWEEPER_TOOL_NAMES - set(registry.list_names())
    if missing:
        logger.warning("[AgentFactory] PolicyMinesweeper 缺失工具: %s", sorted(missing))
    logger.info(
        "[AgentFactory] PolicyMinesweeper ToolRegistry built (%d wengu + %d score = %d tools)",
        registered,
        len(ALL_POLICY_MINESWEEPER_TOOLS),
        len(registry.list_tools()),
    )

    llm_adapter = LLMToolAdapter(config)

    return PolicyMinesweeperExecutor(
        tool_registry=registry,
        llm_adapter=llm_adapter,
    )
