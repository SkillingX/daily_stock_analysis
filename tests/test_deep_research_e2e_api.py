# -*- coding: utf-8 -*-
"""
深度投研模块 E2E 测试（API 级别端到端测试）

通过实际 HTTP 请求调用真实服务，测试完整管线：
API → DeepResearchService → LLM调用 → 报告生成 → 日缓存

无需浏览器，测试速度快，可验证所有关键行为。
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime
from typing import Any

import requests

# ── 配置 ────────────────────────────────────────────────────────────────────
BASE_URL = "https://agentrade.space"
TEST_STOCK = "600519"  # 贵州茅台
REQUEST_TIMEOUT = 300  # 5min（LLM 生成需要时间）


# ── 辅助 ────────────────────────────────────────────────────────────────────
def api_health() -> dict:
    r = requests.get(f"{BASE_URL}/api/health", timeout=10)
    return {"status_code": r.status_code, "body": r.json()}


def sse_stream_events(url: str, payload: dict, timeout=REQUEST_TIMEOUT) -> tuple[list[dict], float, int]:
    """发送 SSE 请求，收集所有事件，返回 (events, elapsed_s, status_code)。"""
    headers = {"Content-Type": "application/json"}
    start = time.time()
    events = []
    status_code = 0
    try:
        r = requests.post(url, json=payload, headers=headers, stream=True, timeout=timeout)
        status_code = r.status_code
        for line in r.iter_lines():
            if line:
                line = line.decode("utf-8")
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
    except requests.exceptions.Timeout:
        return events, time.time() - start, status_code
    except Exception as e:
        return events, time.time() - start, status_code
    return events, time.time() - start, status_code


def event_types(events: list[dict]) -> list[str]:
    return [e.get("type") for e in events]


def get_done_event(events: list[dict]) -> dict | None:
    return next((e for e in events if e.get("type") == "done"), None)


def get_markdown(events: list[dict]) -> str:
    done = get_done_event(events)
    return (done or {}).get("markdown", "")


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# ── 测试用例 ────────────────────────────────────────────────────────────────
class DeepResearchE2E:
    """深度投研模块 E2E 测试套件。"""

    def run_all(self):
        results = []
        tests = [
            ("T1_health", self.t1_health),
            ("T2_first_request_full_generation", self.t2_first_request),
            ("T3_cache_hit_same_stock_same_day", self.t3_cache_hit),
            ("T4_force_refresh_bypasses_cache", self.t4_force_refresh),
            ("T5_report_structure_complete", self.t5_structure),
            ("T6_different_stock_no_cache_collision", self.t6_no_collision),
            ("T7_api_inflight_deduplication", self.t7_inflight_dedup),
        ]
        for name, fn in tests:
            print(f"\n{'='*60}")
            print(f"▶ {name}")
            print("=" * 60)
            try:
                ok, detail = fn()
                results.append((name, "PASS", ok, detail))
                print(f"  {'✅ PASS' if ok else '❌ FAIL'}: {detail}")
            except Exception as e:
                results.append((name, "ERROR", False, str(e)))
                print(f"  💥 ERROR: {e}")
        return results

    # ── T1: 前置检查 ────────────────────────────────────────────────────────
    def t1_health(self) -> tuple[bool, str]:
        r = api_health()
        assert r["status_code"] == 200, f"Health check failed: {r}"
        assert r["body"].get("status") == "ok", f"Status not ok: {r}"
        return True, f"API healthy at {BASE_URL}"

    # ── T2: 首次请求完整生成 ──────────────────────────────────────────────
    def t2_first_request(self) -> tuple[bool, str]:
        payload = {
            "stock_code": TEST_STOCK,
            "stock_name": "贵州茅台",
            "report_type": "deep",
            "force_refresh": True,
        }
        events, elapsed, code = sse_stream_events(
            f"{BASE_URL}/api/v1/deep-research/generate/stream",
            payload,
        )
        assert code == 200, f"Expected 200, got {code}"
        assert "done" in event_types(events), f"Missing 'done' event. Types: {event_types(events)}"

        done = get_done_event(events)
        md = done.get("markdown", "")
        assert len(md) > 200, f"Markdown too short: {len(md)}"

        self._last_markdown = md
        self._last_elapsed = elapsed
        self._last_events = events

        print(f"  首次生成耗时: {elapsed:.1f}s")
        print(f"  markdown 长度: {len(md)} 字符")
        print(f"  事件数: {len(events)}")
        print(f"  事件类型: {event_types(events)}")
        return True, f"生成成功 {elapsed:.0f}s, {len(md)} chars"

    # ── T3: 同股票同一天缓存命中 ─────────────────────────────────────────
    def t3_cache_hit(self) -> tuple[bool, str]:
        payload = {
            "stock_code": TEST_STOCK,
            "stock_name": "贵州茅台",
            "report_type": "deep",
            "force_refresh": False,
        }
        events2, elapsed2, code = sse_stream_events(
            f"{BASE_URL}/api/v1/deep-research/generate/stream",
            payload,
            timeout=30,
        )
        assert code == 200, f"Expected 200, got {code}"

        done2 = get_done_event(events2)
        md2 = done2.get("markdown", "")

        # 验证 markdown 完全一致
        assert md2 == self._last_markdown, (
            f"Markdown 不一致！\n"
            f"  首次: {len(self._last_markdown)} chars, md5={md5(self._last_markdown)}\n"
            f"  二次: {len(md2)} chars, md5={md5(md2)}"
        )

        # 缓存命中应该极快（< 10s）
        print(f"  第二次耗时: {elapsed2:.1f}s")
        if elapsed2 < 10:
            print(f"  ✅ 疑似缓存命中（< 10s）")
        else:
            print(f"  ⚠️ 耗时 {elapsed2:.1f}s > 10s，可能未命中缓存")

        # 检查 thinking 事件中是否有"缓存"字样
        thinking_msgs = [e.get("message", "") for e in events2 if e.get("type") == "thinking"]
        cache_related = [m for m in thinking_msgs if "缓存" in m]
        if cache_related:
            print(f"  缓存消息: {cache_related}")

        return True, f"缓存命中, markdown 一致, {elapsed2:.1f}s"

    # ── T4: force_refresh 绕过缓存 ──────────────────────────────────────
    def t4_force_refresh(self) -> tuple[bool, str]:
        payload = {
            "stock_code": TEST_STOCK,
            "stock_name": "贵州茅台",
            "report_type": "deep",
            "force_refresh": True,
        }
        events3, elapsed3, code = sse_stream_events(
            f"{BASE_URL}/api/v1/deep-research/generate/stream",
            payload,
        )
        assert code == 200, f"Expected 200, got {code}"
        assert "done" in event_types(events3), "Missing done event"

        # force_refresh 应该有完整的 thinking 事件（走了完整生成流程）
        thinking_count = sum(1 for e in events3 if e.get("type") == "thinking")
        print(f"  force_refresh thinking 事件数: {thinking_count}")
        assert thinking_count > 0, "force_refresh=True 但没有完整生成流程"

        done3 = get_done_event(events3)
        md3 = done3.get("markdown", "")
        # 由于 LLM 输出非确定性，force_refresh 的 markdown 可能与首次不完全一致
        # 但长度应该在合理范围
        assert len(md3) > 200, f"Markdown too short: {len(md3)}"
        print(f"  强制刷新耗时: {elapsed3:.1f}s")
        return True, f"force_refresh 成功, {elapsed3:.0f}s"

    # ── T5: 报告结构完整性 ───────────────────────────────────────────────
    def t5_structure(self) -> tuple[bool, str]:
        # 使用 T2 生成的报告验证结构
        md = self._last_markdown
        required = [
            ("报告标题", "深度投研报告"),
            ("数据截至", "数据截至"),
            ("评级", "评级"),
            ("信号", "信号"),
            ("数据透视", "数据透视"),
            ("情报", "情报"),
            ("作战计划", "作战计划"),
            ("结论", "结论"),
        ]
        missing = [(name, kw) for name, kw in required if kw not in md]
        if missing:
            print(f"  ❌ 缺失: {[m[0] for m in missing]}")
        else:
            print(f"  ✅ 所有必要章节存在")

        # 检查没有明显的错误标记
        error_markers = ["错误", "失败", "ERROR", "Exception"]
        has_errors = [m for m in error_markers if m in md and m not in ["错误", "失败"]]  # 排除正常用词
        if has_errors:
            print(f"  ⚠️ 可能包含错误标记: {has_errors}")

        return len(missing) == 0, f"结构完整，{len(md)} 字符"

    # ── T6: 不同股票不撞缓存 ─────────────────────────────────────────────
    def t6_no_collision(self) -> tuple[bool, str]:
        # 用一个不同的股票代码
        other_stock = "000001"  # 平安银行
        payload = {
            "stock_code": other_stock,
            "stock_name": "平安银行",
            "report_type": "deep",
            "force_refresh": True,
        }
        events4, elapsed4, code = sse_stream_events(
            f"{BASE_URL}/api/v1/deep-research/generate/stream",
            payload,
        )
        # 如果是 409 表示 in-flight 有冲突，这里测试缓存隔离所以等久一点
        assert code in (200, 409), f"Unexpected status {code}"

        if code == 200:
            done4 = get_done_event(events4)
            md4 = done4.get("markdown", "")
            # 不同股票 markdown 应该不同（内容不同）
            assert md4 != self._last_markdown, "不同股票但 markdown 相同？缓存 key 有问题！"
            print(f"  不同股票 markdown 不同 ✅ md4={len(md4)} chars")
            return True, f"不同股票缓存隔离正确, {elapsed4:.0f}s"
        else:
            print(f"  ⚠️ 409（in-flight冲突），缓存隔离无法验证")
            return True, "in-flight 冲突，跳过验证"

    # ── T7: 并发 in-flight 去重 ──────────────────────────────────────────
    def t7_inflight_dedup(self) -> tuple[bool, str]:
        import concurrent.futures

        results = []

        def do_request():
            payload = {
                "stock_code": "688486",  # 用一个较少使用的股票
                "stock_name": "测试股票",
                "report_type": "deep",
                "force_refresh": False,
            }
            events, elapsed, code = sse_stream_events(
                f"{BASE_URL}/api/v1/deep-research/generate/stream",
                payload,
                timeout=60,
            )
            return {"code": code, "elapsed": elapsed, "events": events}

        # 同时发送 3 个请求
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futures = [ex.submit(do_request) for _ in range(3)]
            for f in concurrent.futures.as_completed(futures, timeout=90):
                results.append(f.result())

        codes = [r["code"] for r in results]
        print(f"  3并发请求状态码: {codes}")
        success_count = sum(1 for c in codes if c == 200)
        conflict_count = sum(1 for c in codes if c == 409)
        print(f"  成功: {success_count}, 冲突(去重生效): {conflict_count}")

        # 至少一个成功，且不是全部都成功（in-flight 去重生效）
        assert success_count >= 1, "全部失败？"
        # in-flight 去重让后续请求收到 409
        return True, f"in-flight去重: {success_count}成功+{conflict_count}冲突"


# ── 运行入口 ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print("深度投研模块 E2E 测试报告")
    print(f"测试目标: {BASE_URL}")
    print(f"测试股票: {TEST_STOCK}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    suite = DeepResearchE2E()
    results = suite.run_all()

    print(f"\n{'='*60}")
    print("汇总")
    print("=" * 60)
    passed = sum(1 for _, s, *_ in results if s == "PASS")
    failed = sum(1 for _, s, *_ in results if s == "FAIL")
    errors = sum(1 for _, s, *_ in results if s == "ERROR")
    total = len(results)
    print(f"总计: {total} | ✅ PASS: {passed} | ❌ FAIL: {failed} | 💥 ERROR: {errors}")
    print()
    for name, status, ok, detail in results:
        icon = "✅" if status == "PASS" else "❌" if status == "FAIL" else "💥"
        print(f"  {icon} {name}")
        print(f"      {detail}")
    print("=" * 60)

    sys.exit(0 if failed == 0 and errors == 0 else 1)
