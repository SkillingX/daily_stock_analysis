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


def test_changelog_records_search_channel_retirement() -> None:
    changelog = (ROOT_DIR / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [", 2)[1]

    assert "SearXNG 公共实例自动发现" in unreleased
    assert "html_scrape 搜索回退" in unreleased
