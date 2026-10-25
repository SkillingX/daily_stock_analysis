from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def test_env_example_documents_searxng_retirement() -> None:
    """issue #47：配置示例不再提供 SearXNG 入口，只说明退休与残留兼容。"""
    env_example = (ROOT_DIR / ".env.example").read_text(encoding="utf-8")

    assert "SEARXNG_BASE_URLS=" not in env_example
    assert "SEARXNG_PUBLIC_INSTANCES_ENABLED=" not in env_example
    assert "SEARXNG_BASE_URLS" in env_example  # 残留变量名仍被点名说明不再生效


def test_daily_analysis_workflow_does_not_inject_searxng() -> None:
    """issue #47：每日分析工作流不再注入退休渠道。"""
    workflow = (
        ROOT_DIR / ".github" / "workflows" / "00-daily-analysis.yml"
    ).read_text(encoding="utf-8")

    assert "SEARXNG_BASE_URLS" not in workflow
    assert "SEARXNG_PUBLIC_INSTANCES_ENABLED" not in workflow


def test_full_guide_does_not_present_retired_search_as_active() -> None:
    """issue #47：完整指南不再把退休搜索渠道写成可用搜索配置。"""
    guide = (ROOT_DIR / "docs" / "full-guide.md").read_text(encoding="utf-8")

    assert "SEARXNG_PUBLIC_INSTANCES_ENABLED=true" not in guide
    assert "searx.space" not in guide
    assert "留空时默认自动发现公共实例" not in guide
    assert "自动发现公共实例" not in guide
    for banned in ("博查搜索", "Brave Search", "适合实时金融新闻"):
        assert banned not in guide, f"退休搜索渠道仍按可用配置展示: {banned}"
    for key in ("SERPAPI_API_KEYS", "BOCHA_API_KEYS", "BRAVE_API_KEYS", "SEARXNG_BASE_URLS", "SEARXNG_PUBLIC_INSTANCES_ENABLED"):
        for line in guide.splitlines():
            if key in line and line.lstrip().startswith("|"):
                assert "已停用" in line, f"{key} 仍按可用搜索配置展示: {line}"
    assert "一 Key 同时启用大模型和" not in guide
    assert "不触发请求" in guide


def test_changelog_records_search_channel_retirement() -> None:
    changelog = (ROOT_DIR / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [", 2)[1]

    assert "SearXNG 公共实例自动发现" in unreleased
    assert "html_scrape 搜索回退" in unreleased
