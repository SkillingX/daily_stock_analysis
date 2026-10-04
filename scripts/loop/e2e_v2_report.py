# -*- coding: utf-8 -*-
"""E2E：用 600519 已落盘 dims.json 跑真实 LLM 经理终稿 + v2 模板渲染（不写库）。

跑法：~/dsa-venv/bin/python scripts/loop/e2e_v2_report.py
产物：/tmp/e2e_v2_report.md（打印关键片段供验收）
"""

import json
import sys

sys.path.insert(0, ".")

from src.agent.deep_research.orchestrator import _generate_final_conclusion  # noqa: E402
from src.deep_research_dims.manager_writeup import generate_manager_writeup  # noqa: E402
from src.deep_research_dims.render import (  # noqa: E402
    build_view,
    render_markdown,
    split_dim_sections,
    validate_structure,
)
from src.schemas.deep_research_dims import DIM_IDS, GuardrailEvent, parse_dim  # noqa: E402
from src.services.deep_research_service import _get_dual_track_adapter  # noqa: E402

DIMS_PATH = "reports/deep_research/600519_202610030829_dims.json"
OUT_PATH = "/tmp/e2e_v2_report.md"


def main() -> None:
    payload = json.load(open(DIMS_PATH, encoding="utf-8"))
    dims = {
        dim_id: parse_dim(dim_id, p)
        for dim_id, p in payload["dimensions"].items()
        if dim_id in set(DIM_IDS)
    }
    events = [GuardrailEvent(**e) for e in payload.get("guardrail_events") or []]
    print(f"dims loaded: {len(dims)}, guardrails: {len(events)}")

    adapter = _get_dual_track_adapter()
    manager = generate_manager_writeup(adapter, "贵州茅台", "600519", dims, events)
    print("title_angle:", manager["title_angle"])
    print("summary len:", len(manager["executive_summary"]))
    print("titles:", len(manager["section_titles"]), "narratives:", sorted(manager["section_narratives"]))
    print("title samples:", dict(list(manager["section_titles"].items())[:3]))

    final_conclusion = _generate_final_conclusion(adapter, "贵州茅台", "600519", dims, events)
    view = build_view(
        "贵州茅台", "600519",
        "2026-10-03T08:29:48", ["筹码分布数据缺失"], dims, events,
        report_id="600519_e2e_v2test", final_conclusion=final_conclusion,
        manager=manager,
    )
    md = render_markdown(view)
    missing = validate_structure(md)
    sections = split_dim_sections(md)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(md)
    print("missing headings:", missing)
    print("split sections:", len(sections))
    print("checks:", {
        "no_json_bare": '{"' not in md,
        "no_pydantic_err": "Input should be" not in md and "validation error" not in md,
        "no_optimistic": "optimistic" not in md,
        "has_summary": "## 内容概括" in md,
        "has_opinion": "｜" in md,
    })


if __name__ == "__main__":
    main()
