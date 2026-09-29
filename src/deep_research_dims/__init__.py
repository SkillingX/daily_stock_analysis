# -*- coding: utf-8 -*-
"""深度投研双轨引擎 · 维度后台分析代码包。

每个维度一套纯函数（可单测），LLM 仅按维度性质参与（探索/叙述/无）。
编排见 ``src/agent/deep_research/orchestrator.py``。
"""

from .context import SharedContext, build_shared_context

__all__ = ["SharedContext", "build_shared_context"]
