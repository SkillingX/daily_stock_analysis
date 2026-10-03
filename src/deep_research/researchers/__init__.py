# -*- coding: utf-8 -*-
"""研究员 Skill 体系：每位研究员 = SKILL.md（策略定义）+ 独立模块（代码）。

懒加载（PEP 562）：registry 导入研究员子模块，避免包初始化循环。
"""

from typing import Any


def __getattr__(name: str) -> Any:
    if name in ("REGISTRY", "RUNNER_NAMES", "researcher_dim_ids"):
        from . import registry

        return getattr(registry, name)
    raise AttributeError(name)


__all__ = ["REGISTRY", "RUNNER_NAMES", "researcher_dim_ids"]
