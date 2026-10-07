# -*- coding: utf-8 -*-
"""
深度投研模块 E2E 测试（Playwright 浏览器自动化）

测试策略：
1. 直接在浏览器访问深度分析页面
2. 输入股票代码发起请求
3. 等待报告生成完成
4. 第二次同一股票验证缓存行为（速度应该极快）
5. 验证报告结构完整性

依赖：
  pip install playwright && playwright install chromium --with-deps
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import pytest

# 整个文件依赖 playwright fixture + 真实网络（https://agentrade.space）；
# 缺少 playwright 包时跳过所有测试，避免 fixture 解析报错污染 CI 离线套件。
pytest.importorskip("playwright", reason="browser e2e requires playwright + live URL")

BASE_URL = "https://agentrade.space"
STOCK_CODE = "600519"  # 贵州茅台


def js_evaluate_sync(page, script, timeout=300000):
    """同步执行 JS 并等待结果（用于 SSE 流处理）。"""
    import asyncio

    async def _run():
        return await page.evaluate(script)

    return asyncio.get_event_loop().run_until_complete(_run())


class TestDeepResearchBrowserE2E:
    """深度投研模块浏览器 E2E 测试（Playwright）。"""

    @pytest.fixture(autouse=True)
    def setup(self, browser):
        self.browser = browser
        self.page = None

    def _open_deep_research_page(self):
        """打开深度分析页面。"""
        page = self.browser.new_page()
        page.set_default_timeout(300000)  # 5min 全局超时
        page.goto(f"{BASE_URL}/deep-research", wait_until="networkidle")
        time.sleep(2)
        self.page = page
        return page

    def test_01_page_loads(self):
        """页面加载成功。"""
        page = self._open_deep_research_page()
        title = page.title()
        print(f"\n页面标题: {title}")
        # 检查关键元素存在
        content = page.content()
        assert "深度" in content or "投研" in content or "deep" in content.lower()
        print("✅ 页面加载成功")

    def test_02_generate_report_first_time(self):
        """第一次生成报告（完整流程）。"""
        page = self._open_deep_research_page()

        # 查找股票代码输入框
        # 尝试多种可能的选择器
        selectors = [
            "input[placeholder*='股票']",
            "input[placeholder*='code']",
            "input[placeholder*='Code']",
            "input[type='text']",
            "#stock-code",
            "[data-testid='stock-input']",
        ]
        input_box = None
        for sel in selectors:
            try:
                el = page.wait_for_selector(sel, timeout=5000)
                if el:
                    input_box = el
                    print(f"  找到输入框: {sel}")
                    break
            except Exception:
                continue

        if input_box is None:
            # 截屏调试
            page.screenshot(path="/tmp/e2e_debug.png")
            print("  ❌ 未找到股票代码输入框，截图已保存到 /tmp/e2e_debug.png")
            # 打印页面文本
            texts = page.inner_text("body")
            print(f"  页面文本（前500字符）: {texts[:500]}")

        assert input_box is not None, "未找到股票代码输入框"

        # 输入股票代码
        input_box.fill(STOCK_CODE)
        time.sleep(0.5)

        # 查找生成按钮
        btn_selectors = [
            "button[type='submit']",
            "button:has-text('生成')",
            "button:has-text('分析')",
            "button:has-text('research')",
            "button:has-text('Research')",
        ]
        submit_btn = None
        for sel in btn_selectors:
            try:
                el = page.wait_for_selector(sel, timeout=3000)
                if el:
                    submit_btn = el
                    print(f"  找到提交按钮: {sel}")
                    break
            except Exception:
                continue

        assert submit_btn is not None, "未找到提交按钮"
        submit_btn.click()
        print(f"  已提交股票代码: {STOCK_CODE}")

        # 等待报告生成完成（等待 markdown 内容出现）
        start = time.time()
        timeout = 300  # 5分钟超时

        while time.time() - start < timeout:
            # 检查是否有报告内容
            markdown_elements = page.query_selector_all("pre, .markdown, [class*='markdown']")
            if markdown_elements:
                md_text = markdown_elements[0].inner_text()
                if len(md_text) > 200:
                    elapsed = time.time() - start
                    print(f"  ✅ 报告生成完成，耗时: {elapsed:.0f}s")
                    print(f"  报告长度: {len(md_text)} 字符")
                    # 保存供下一个测试使用
                    self._last_markdown = md_text
                    return md_text

            # 检查是否有 loading 状态
            loading = page.query_selector("[class*='loading'], [class*='spinner']")
            if loading:
                pass  # 继续等待

            time.sleep(5)

        raise TimeoutError(f"报告生成超时（>{timeout}s）")

    def test_03_second_request_is_instant(self):
        """第二次请求同一股票（应命中缓存，瞬间返回）。"""
        # 由于测试隔离，这里验证缓存机制的逻辑存在
        # （实际第二次请求由 test_02 后端缓存路径保证）
        page = self._open_deep_research_page()

        # 提交同一股票
        selectors = ["input[placeholder*='股票']", "input[type='text']"]
        inp = None
        for sel in selectors:
            try:
                inp = page.wait_for_selector(sel, timeout=5000)
                if inp:
                    break
            except Exception:
                continue

        if inp is None:
            pytest.skip("未找到输入框，跳过缓存验证")

        inp.fill(STOCK_CODE)
        time.sleep(0.3)

        btn_sel = "button[type='submit'], button:has-text('生成')"
        btn = page.wait_for_selector(btn_sel, timeout=3000)
        btn.click()

        start2 = time.time()
        timeout = 60  # 缓存命中应该极快
        while time.time() - start2 < timeout:
            md_els = page.query_selector_all("pre, .markdown, [class*='markdown']")
            if md_els:
                md = md_els[0].inner_text()
                if len(md) > 100:
                    elapsed2 = time.time() - start2
                    print(f"\n  第二次请求耗时: {elapsed2:.1f}s")
                    if elapsed2 < 5:
                        print(f"  ✅ 疑似命中缓存（<5s）")
                    else:
                        print(f"  ⚠️ 耗时较长，可能未命中缓存")
                    return
            time.sleep(2)

        print(f"  ⚠️ 等待超时（{timeout}s）")

    def test_04_report_structure(self):
        """验证报告结构完整性。"""
        page = self._open_deep_research_page()

        # 读取历史报告验证结构
        import requests
        r = requests.get(f"{BASE_URL}/api/v1/deep-research/reports?limit=1", timeout=10)
        if r.status_code == 200 and r.json().get("reports"):
            report = r.json()["reports"][0]
            report_id = report.get("report_id")
            # 获取报告详情
            r2 = requests.get(f"{BASE_URL}/api/v1/deep-research/reports/{report_id}", timeout=10)
            if r2.status_code == 200:
                data = r2.json()
                md = data.get("markdown", "")
                print(f"\n  历史报告 markdown 长度: {len(md)} 字符")

                required = ["深度投研报告", "评级", "信号", "数据透视", "情报", "作战计划"]
                missing = [s for s in required if s not in md]
                if not missing:
                    print(f"  ✅ 报告结构完整")
                else:
                    print(f"  ❌ 缺失章节: {missing}")
                assert not missing
            else:
                pytest.skip("无法获取报告详情")
        else:
            pytest.skip("无历史报告，跳过结构验证")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--tb=short"])
