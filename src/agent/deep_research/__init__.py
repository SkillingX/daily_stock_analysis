# -*- coding: utf-8 -*-
"""深度投研双轨引擎 · 编排层（Agent 侧）。

- ``explore_agents``：S3 情报 / L4 产业链 两个探索型维度的 ReAct 封装；
- ``orchestrator``：波次调度 + 降级收集 + 护栏 + 模板成文。

需求文档：``docs/deep-research-dual-track-agent-requirements.md``。
"""

from .orchestrator import DualTrackResult, run_dual_track

__all__ = ["DualTrackResult", "run_dual_track"]
