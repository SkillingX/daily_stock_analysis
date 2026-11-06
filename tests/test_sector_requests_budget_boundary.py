# -*- coding: utf-8 -*-
"""绝对 requests 预算装到真实 wire——fresh subprocess × 真实 sector 公共入口。

在**全新进程**中验证（pytest 进程可能已由其他测试安装 patch，测试顺序不能
作为覆盖证据）；与旧版（手动 eastmoney_patch + 直接 provider.search + 绝对
时钟比较）的区别：预算从 **sector 公共 POST 入口**触发真实安装，timeout
对照的是 **wire 发生时刻的剩余预算**（deadline − t_wire + 小开销容差），
不是绝对 deadline：

- 初始负控制：fresh process 中 ``eastmoney_patch`` 从未安装（排除其他模块
  预装）；fixture/provider 初始化同样不得安装
- 真实 ``MiniMaxSearchProvider``（BaseProvider/Tenacity 重试真实）+ 真实
  公开 ``POST /api/v1/sector-analysis/generate``（身份/日历/准入/存档真实），
  唯一隔离点是终端 ``Session.send``：
  - 第一根 wire 在 fetch_deadline 内发生且 timeout ≤ deadline−t_wire+0.02s
    （跨函数计时误差容差；绝对预算装到真实 wire）
  - 终端失败后 Tenacity 想重试，第二次尝试在 deadline 后被已安装的
    patched_request 挡下（requests.Timeout）→ **绝对零后续 wire**
  - API 返回（200 partial 存档）后延迟 2s 复查仍零迟到 wire
  - 同 worker contexts 归零、共享 executor 无 deadline/research 泄漏
- ``request_deadline`` 重置后，无上下文的无关请求保持调用方默认 timeout=7
  （patch 不改全局默认行为）
- 零 TCP guard：除被替换的 send 外全程零真实 TCP 尝试（失败也断言）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_SUBPROCESS_SCRIPT = r'''
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

# 干净环境：合成配置，不加载真实凭据；本地 LiteLLM 价格表
os.environ.clear()
os.environ.update({
    "PATH": "/usr/bin:/bin",
    "ENV_FILE": "/dev/null",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "LITELLM_MODE": "PRODUCTION",
    "HOME": os.environ.get("HOME", "/tmp"),
})

ROOT = "__PROJECT_ROOT__"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# ---- 零 TCP guard：全程任何真实 TCP 连接尝试即失败（失败也断言） ----
tcp_attempts = []
_orig_connect = socket.socket.connect


def _guarded_connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6):
        tcp_attempts.append(repr(address))
        raise AssertionError("zero-TCP guard: " + repr(address))
    return _orig_connect(sock, address)


socket.socket.connect = _guarded_connect

from src.patches.eastmoney_patch import (  # noqa: E402
    _patch_sign, eastmoney_patch, request_deadline,
)

# ---- 1) fresh-process 负控制：初始未安装（其他模块不得预装） ----
assert not _patch_sign.is_patched(), (
    "fresh process must start with patch uninstalled; test order leak")

import tests.conftest  # noqa: E402,F401  确认 conftest 不预装 requests 预算
assert not _patch_sign.is_patched(), "conftest import must not install patch"

from tests.test_sector_research_api_v2 import (  # noqa: E402
    SectorResearchApiV3TestCase, _board_table,
)
from src.core import trading_calendar as cal  # noqa: E402
from src.core.research_run import current_research  # noqa: E402
from src.search_service import MiniMaxSearchProvider, get_search_service  # noqa: E402
from src.services import sector_analysis_service as svc  # noqa: E402

assert not _patch_sign.is_patched(), "test module import must not install patch"


class _Fixed(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001 - datetime.now 签名
        v = cls(2026, 11, 6, 18, 0, 0)
        return v.replace(tzinfo=tz) if tz else v


case = SectorResearchApiV3TestCase(methodName="runTest")
case.setUp()
wires = []
try:
    def _terminal_send(self, request, **kwargs):  # noqa: ANN001 - Session 签名
        wires.append({
            "at": time.monotonic(),
            "timeout": kwargs.get("timeout"),
            "deadline": request_deadline.get(),
        })
        raise requests.ConnectionError("synthetic terminal failure")

    with patch.object(svc, "datetime", _Fixed), \
            patch.object(cal, "datetime", _Fixed):
        bh, bm = case._real_window_hist()
        case._mock_vendors(
            industry="白酒Ⅱ",
            boards=[("白酒Ⅱ", "BK1277"), ("电池", "BK1033")],
            cons=["600519", "000568"],
            board_table=_board_table([("白酒Ⅱ", "BK1277")]),
            board_hist=bh, bench_hist=bm,
        )
        search = get_search_service()
        search._providers = [MiniMaxSearchProvider(["synthetic-placeholder-key"])]
        # 真实公共入口总预算 1s（覆盖 env 无法进 setUp，显式重置单例）
        os.environ["SECTOR_ANALYSIS_REPORT_TIMEOUT_SECONDS"] = "1"
        from src.config import Config

        Config.reset_instance()
        # fixture/真实 provider 初始化仍不得安装 requests 预算
        assert not _patch_sign.is_patched(), (
            "fixture/provider init must not install patch")
        import requests  # noqa: E402  在 patch 断言后导入并替换终端

        started = time.monotonic()
        with patch.object(requests.Session, "send", _terminal_send):
            response = case.client.post(
                "/api/v1/sector-analysis/generate"
                "?stock_code=600519&stock_name=贵州茅台"
            )
            returned = time.monotonic()
            wire_count_at_return = len(wires)
            time.sleep(2.0)  # API 返回后延迟复查窗口
        assert response.status_code == 200, response.text
        analysis = response.json()["analysis"]
        rid = response.json()["report_id"]
        assert analysis["assessment"]["completeness"] == "partial", analysis
        # 搜索失败 → 无已证实催化 → 综合不执行（不虚构）
        assert analysis["sector_catalysts"]["items"] == [], analysis
        assert analysis["sector_synthesis"]["status"] == "no_qualified_facts"
        # partial 正常存档
        assert case.client.get(
            f"/api/v1/sector-analysis/reports/{rid}").status_code == 200

    # ---- 2) 绝对预算装到真实 wire（remaining 时钟，非绝对 deadline） ----
    assert _patch_sign.is_patched(), "sector transport must install the patch"
    assert len(wires) == wire_count_at_return == 1, wires
    first = wires[0]
    assert first["deadline"] is not None, "patch must bind request_deadline"
    assert first["at"] < first["deadline"], "first wire must be in budget"
    remaining_at_wire = first["deadline"] - first["at"]
    assert first["timeout"] is not None and first["timeout"] > 0
    assert first["timeout"] <= remaining_at_wire + 0.02, (
        f"first wire timeout must be clamped to remaining-at-wire "
        f"(+20ms tolerance): timeout={{first['timeout']}} vs "
        f"remaining={{remaining_at_wire}}")
    # ---- 3) deadline 后与 API 返回后绝对零迟到 wire ----
    assert all(w["at"] < w["deadline"] for w in wires), wires
    # ---- 4) worker 上下文清理（共享 executor 无泄漏） ----
    assert request_deadline.get() is None and current_research.get() is None
    clean = svc._FETCH_EXECUTOR.submit(
        lambda: (request_deadline.get(), current_research.get())).result(3)
    assert clean == (None, None), clean
    # ---- 5) 无上下文请求保持调用方默认 timeout=7（不改全局默认） ----
    default_wires = []

    def _default_probe_send(self, request, **kwargs):  # noqa: ANN001
        default_wires.append(kwargs.get("timeout"))
        raise requests.ConnectionError("default check")

    with patch.object(requests.Session, "send", _default_probe_send):
        try:
            requests.get("https://default-check.example.com/ping", timeout=7)
        except requests.RequestException:
            pass
    assert default_wires == [7], (
        f"no-context request must keep caller default timeout, "
        f"got {default_wires}")
    # ---- 6) 零 TCP：全程无真实连接尝试（失败也断言） ----
    assert not tcp_attempts, tcp_attempts
    print(json.dumps({
        "first_wire_timeout": first["timeout"],
        "remaining_at_wire": remaining_at_wire,
        "wire_count": len(wires),
        "late_wire_count": len(wires) - wire_count_at_return,
        "worker_context_clean": list(clean),
        "default_timeout_kept": default_wires[0],
        "tcp_attempts": len(tcp_attempts),
        "elapsed": returned - started,
    }, ensure_ascii=False))
finally:
    case.tearDown()
    case.doCleanups()
'''.replace("__PROJECT_ROOT__", str(PROJECT_ROOT))


def test_fresh_process_sector_public_entry_requests_boundary(tmp_path) -> None:
    """fresh subprocess：初始未装 → 公共入口触发安装 → wire 钳到 remaining
    → deadline 后/API 返回后零 wire → 默认与上下文语义不变。"""
    import subprocess

    script_path = tmp_path / "sector_entry_budget_probe.py"
    script_path.write_text(_SUBPROCESS_SCRIPT, encoding="utf-8")

    env = dict(os.environ)
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env["ENV_FILE"] = "/dev/null"  # 合成配置：不加载真实凭据
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    proc = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"subprocess failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    assert '"late_wire_count": 0' in proc.stdout
    assert '"default_timeout_kept": 7' in proc.stdout
    assert '"tcp_attempts": 0' in proc.stdout
    assert '"worker_context_clean": [null, null]' in proc.stdout
