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


_AKSHARE_PAGINATION_SCRIPT = r'''
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

# 干净环境：合成配置，不加载真实凭据
os.environ.clear()
os.environ.update({
    "PATH": "/usr/bin:/bin",
    "ENV_FILE": "/dev/null",
    "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    "HOME": os.environ.get("HOME", "/tmp"),
})

ROOT = "__PROJECT_ROOT__"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

# 零 TCP guard：全程任何真实 TCP 连接尝试即失败
tcp_attempts = []
_orig_connect = socket.socket.connect


def _no_tcp(sock, address):
    tcp_attempts.append((sock.family, address))
    raise AssertionError(f"unexpected real TCP: {address}")


socket.socket.connect = _no_tcp

import akshare as ak
import requests
from akshare.utils import func

from src.patches.eastmoney_patch import eastmoney_patch, request_deadline
from src.services.sector_analysis_service import (
    BudgetExhausted,
    ReportBudget,
    _FETCH_EXECUTOR,
    _ak_with_retry,
    _bounded_ak,
)

# 关键：不预装 eastmoney_patch——worker 内安装是本修复的行为本身。
# 不做任何 SDK 源码 AST/inspect 结构依赖（CI AkShare 1.20.35 的 adapter
# 用 rename(columns=...)，本地 1.19.1 为位置赋列）；响应行按真实
# PreparedRequest URL query 的 fields 键集**数字升序**构造合法东财 clist
# f-key 行——1.19.1 位置赋列按该序配中文列名、1.20.35 按 f-key rename，
# 两版本语义映射（板块代码/名称/涨跌幅/上涨下跌家数）都有效且被逐值断言。

# 东财 clist f-key 合法值（可断言的关键映射用唯一可辨识值）
FIELD_VALUES = {
    "f1": 2, "f2": 1500.0, "f3": -1.9, "f4": -28.0, "f5": 1000,
    "f6": 3.5e9, "f7": 2.0, "f8": 1.12, "f9": 30.0, "f10": 4.0,
    "f11": 60.0, "f12": "BK0475", "f13": 0, "f14": "银行Ⅱ", "f15": 1520.0,
    "f16": 1490.0, "f17": 1505.0, "f18": 1495.0, "f20": 1.9e12,
    "f21": 1.8e12, "f22": 100.0, "f23": 12.0, "f24": 3.0, "f25": 2.0,
    "f26": 0, "f33": 800.0, "f62": 1.0e9, "f104": 3, "f105": 17,
    "f107": "BK0475", "f115": 3.3e9, "f124": 1700000000,
    "f128": "样本股", "f136": 5.5, "f140": "600036", "f141": 1.0,
    "f152": 2, "f207": 0, "f208": 0, "f209": 0, "f222": 0,
}

market_wires = []     # 有 fields 的市场 wire（含 dispatch 线程与剩余预算）
default_wires = []    # 无 fields 的无关请求 wire（timeout+线程语义）
worker_entries = [0]  # _ak_with_retry 实际进入 worker 的次数（worker 层入口）
sdk_entries = [0]     # SDK 调用实际发生次数（require_fetch 之后）
worker_thread = [None]  # 实际执行 worker 的线程 ident
fail_mode = [False]


def counted_worker(call, *args, **kwargs):
    """worker 层入口计数包装：调真实 _ak_with_retry（不 mock 掉执行），
    证明真实排队任务在过期后实际进入 worker（非取消冒充）。"""
    worker_entries[0] += 1
    worker_thread[0] = threading.get_ident()
    return _ak_with_retry(call, *args, **kwargs)


def terminal(self, request, **kwargs):
    q = parse_qs(urlparse(request.url).query)
    if "fields" not in q:
        # 无 fields 的无关请求（无 deadline）单独记录 timeout+线程语义
        default_wires.append((kwargs.get("timeout"), threading.get_ident()))
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        return response
    fields = q["fields"][0].split(",")
    # 按 f 键数字升序构造响应行（东财响应字段数值顺序）：
    # 1.19.1 位置赋列按该序配中文列名；1.20.35 按 f-key rename
    ordered = sorted(fields, key=lambda x: int(x[1:]))
    row = {f: FIELD_VALUES.get(f, 1) for f in ordered}
    market_wires.append({
        "remaining": round(active_budget[0].fetch_deadline - time.monotonic(), 4)
        if active_budget[0] is not None else None,
        "deadline_context": request_deadline.get(),
        "timeout": kwargs.get("timeout"),
        "thread": threading.get_ident(),
    })
    if fail_mode[0]:
        raise requests.ConnectionError("synthetic provider failure")
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(
        {"data": {"total": 2, "diff": [row]}}).encode()
    return response


def sdk_entry():
    """SDK 调用入口计数（require_fetch 通过后才会发生）。"""
    sdk_entries[0] += 1
    return ak.stock_board_industry_name_em()


def wait_future_done(fut, timeout=15.0):
    """等待真实 worker future 实际结束（有限且足够覆盖 SDK 内部重试退避），
    不以固定短 sleep 冒充完成；返回是否在时限内 done。"""
    done, pending = futures_wait([fut], timeout=timeout)
    return not pending


# ---- Case A：紧预算（root 反例形状，真实分页）→ 调用方放弃 →
# 等真实 worker future 实际结束 → deadline 后零新 wire ----
instrumented = []
real_submit = _FETCH_EXECUTOR.submit


def recording_submit(fn, *args, **kwargs):
    fut = real_submit(fn, *args, **kwargs)
    instrumented.append(fut)
    return fut


active_budget = [ReportBudget(1.0, finish_margin=0.5)]
with patch.object(requests.Session, "send", terminal), \
        patch.object(_FETCH_EXECUTOR, "submit", recording_submit), \
        patch.object(socket.socket, "connect", _no_tcp), \
        patch.object(func.random, "uniform", return_value=0.7):
    try:
        _bounded_ak(ak.stock_board_industry_name_em,
                    budget=active_budget[0], retries=0)
        caller_timeout = False
    except BudgetExhausted:
        caller_timeout = True
    # 等待真实 worker future 实际结束、完成检查与 wire 统计全部保持在
    # 同一 terminal mock 作用域内（不先撤 mock 再等）；guard 故障
    # （AssertionError）不得伪装成正常完成。
    worker_fut = instrumented[-1]
    worker_finished = wait_future_done(worker_fut, timeout=15.0)
    assert worker_finished, "等待上限内真实 SDK worker 未实际结束"
    worker_exc = worker_fut.exception()
    assert worker_exc is None or not isinstance(
        worker_exc, AssertionError), (
        f"worker 异常为 guard 故障（不得伪装正常完成）: {worker_exc!r}")
    wires_case_a = len(market_wires)

assert caller_timeout, "调用方未按预算放弃"
assert instrumented, "未捕获到真实 worker future"
assert wires_case_a == 1, (
    f"修复后 worker 实际结束后市场 wire 应恰为 1，实际 {wires_case_a}")
first = market_wires[0]
assert first["deadline_context"] == active_budget[0].fetch_deadline, (
    f"worker 内未绑定 fetch_deadline: {first['deadline_context']}")
# 首 wire timeout 钳制到 dispatch 时刻实际剩余（+0.05 跨函数计时容差；
# 形态随 SDK 版本：1.19.1 标量、1.20.35 默认 (10,60) tuple 逐项钳制）
tv = first["timeout"]
assert isinstance(tv, (int, float, tuple)), tv
timeout_values = tv if isinstance(tv, tuple) else (tv,)
assert timeout_values and all(
    isinstance(v, (int, float)) and v > 0 for v in timeout_values), tv
assert all(v <= first["remaining"] + 0.05 for v in timeout_values), (
    f"timeout {tv} 未钳到剩余预算 {first['remaining']}")

# ---- Case B：充足预算 → 真实分页两页合并成功（同步返回=worker 已结束）；
# 关键 f-key→中文列映射逐值断言（1.19.1 位置赋列 / 1.20.35 rename 都须
# 产出正确的板块代码/名称/涨跌幅/上涨下跌家数） ----
active_budget[0] = ReportBudget(5.0, finish_margin=0.5)
wires_b_start = len(market_wires)
with patch.object(requests.Session, "send", terminal), \
        patch.object(socket.socket, "connect", _no_tcp), \
        patch.object(func.random, "uniform", return_value=0.7):
    df = _bounded_ak(ak.stock_board_industry_name_em,
                     budget=active_budget[0], retries=0)
assert len(df) == 2, f"分页两页应合并 2 行，实际 {len(df)}"
wires_b = market_wires[wires_b_start:]
assert wires_b, "成功路径应有 wire"
assert all(w["deadline_context"] == active_budget[0].fetch_deadline
           for w in wires_b)
for w in wires_b:
    tv = w["timeout"]
    assert isinstance(tv, (int, float, tuple)), tv
    vals = tv if isinstance(tv, tuple) else (tv,)
    assert all(isinstance(v, (int, float)) and v > 0 for v in vals), tv
    assert all(v <= w["remaining"] + 0.05 for v in vals), (
        f"timeout {tv} 未钳到剩余预算 {w['remaining']}")
# P4 关键映射断言（位置赋列/rename 两版本都必须正确，不能只断 shape）
assert df["板块代码"].iloc[0] == "BK0475", df["板块代码"].iloc[0]
assert df["板块名称"].iloc[0] == "银行Ⅱ", df["板块名称"].iloc[0]
assert df["涨跌幅"].iloc[0] == -1.9, df["涨跌幅"].iloc[0]
assert df["上涨家数"].iloc[0] == 3, df["上涨家数"].iloc[0]
assert df["下跌家数"].iloc[0] == 17, df["下跌家数"].iloc[0]
worker_ctx_after = _FETCH_EXECUTOR.submit(
    lambda: request_deadline.get()).result(timeout=5)
assert worker_ctx_after is None, f"worker 上下文未清理: {worker_ctx_after}"

# ---- Case C：真实排队晚获得执行权（single-worker：任务一旦开始即不可
# 取消——bounded_call 放弃时的 fut.cancel() 会移除未开始的排队任务、伪装
# 成晚执行，root 反例）。worker 实际进入（入口计数证明）→ require_fetch
# 在 worker 内拒绝 → 零 wire；过期退出后同线程 ctx 清理 + timeout=7 ----
single = ThreadPoolExecutor(max_workers=1)
start_gate = threading.Event()
release_gate = threading.Event()
budget_c = ReportBudget(0.2, finish_margin=0.05)
wires_c_start = len(market_wires)
entries_before = worker_entries[0]


def queued_late_worker():
    # 真实排队形态：任务已开始（不可再被 cancel）→ 等待晚获执行权 →
    # 执行 production worker（此时预算已耗尽）
    start_gate.set()
    release_gate.wait(5)
    return counted_worker(sdk_entry, retry_budget=budget_c, retries=0)


fut_late = single.submit(queued_late_worker)
assert start_gate.wait(5), "worker 未实际开始执行排队任务"
assert worker_entries[0] == entries_before, (
    "worker 不应在等待阶段提前进入")
time.sleep(0.3)  # budget_c 到期（worker 已开始，取消不再可能）
release_gate.set()
late_exc = None
try:
    fut_late.result(timeout=30)
except BudgetExhausted as exc:
    late_exc = exc
except Exception as exc:  # noqa: BLE001 - 记录任何非预期异常形态
    late_exc = exc
assert fut_late.done(), "排队任务未实际完成"
assert worker_entries[0] == entries_before + 1, (
    f"worker 实际进入次数未增加（取消冒充）: {worker_entries[0]}")
late_worker_delta = worker_entries[0] - entries_before
late_sdk_count = sdk_entries[0]
assert late_sdk_count == 0, (
    f"过期后 require_fetch 必须在 SDK 调用前拒绝: {sdk_entries[0]}")
assert late_exc is not None, "worker 过期执行必须以预算异常失败"
assert isinstance(late_exc, BudgetExhausted), f"异常形态: {late_exc!r}"
# fut_late.result 同步返回后 worker 已实际结束，直接计数即可
late_wires_now = len(market_wires)
assert late_wires_now == wires_c_start, (
    f"真实排队晚获执行权不得产生 wire: {late_wires_now} > {wires_c_start}")
# 同线程紧随观察：worker 入口线程 == 观察线程（finally 属于原线程）且
# ctx 清理；观察任务在 worker 线程内实际发出无 ctx 请求（timeout=7），
# ctx=None 时 patch 保持调用方 timeout
with patch.object(requests.Session, "send", terminal):
    def _same_thread_probe():
        ident = threading.get_ident()
        try:
            requests.get("https://late-noctx.example.com/ping", timeout=7)
        except requests.RequestException:
            pass
        return ident, request_deadline.get()
    thread_c, ctx_c = single.submit(_same_thread_probe).result(timeout=5)
assert thread_c == worker_thread[0], (
    f"过期清理线程 {thread_c} != SDK 入口线程 {worker_thread[0]}")
assert ctx_c is None, f"过期退出后 worker 线程 ctx 未清理: {ctx_c}"
late_timeout_wires = [(t, ident) for t, ident in default_wires]
assert late_timeout_wires and all(
    t == 7 for t, _ in late_timeout_wires), (
    f"worker 线程内无 ctx 请求应保持调用方 timeout 7: {late_timeout_wires}")

# ---- Case D：同一 single-worker 两种退出（成功 / 独立供应商失败）——
# 每种紧随同线程观察 ctx 清理 + 无 ctx timeout=7 ----
wires_d_start = len(market_wires)
with patch.object(requests.Session, "send", terminal), \
        patch.object(socket.socket, "connect", _no_tcp), \
        patch.object(func.random, "uniform", return_value=0.7):
    # D1 成功退出
    budget_d = ReportBudget(5.0, finish_margin=0.5)
    fut = single.submit(counted_worker, sdk_entry,
                        retry_budget=budget_d, retries=0)
    fut.result(timeout=30)
    success_thread = market_wires[-1]["thread"]
    assert worker_entries[0] > 0 and sdk_entries[0] > 0  # 真实进入并完成 SDK
    def _d1_probe():
        ident = threading.get_ident()
        try:
            requests.get("https://d1-noctx.example.com/ping", timeout=7)
        except requests.RequestException:
            pass
        return ident, request_deadline.get()
    thread_d1, ctx_d1 = single.submit(_d1_probe).result(timeout=5)
    d1_wires = list(default_wires)
    assert thread_d1 == success_thread and ctx_d1 is None
    assert [t for t, _ in d1_wires] == [7, 7], (
        f"同线程无 ctx 请求应保持 timeout 7: {d1_wires}")
    # D2 独立供应商失败退出
    fail_mode[0] = True
    budget_d2 = ReportBudget(5.0, finish_margin=0.5)
    fut = single.submit(counted_worker, sdk_entry,
                        retry_budget=budget_d2, retries=0)
    provider_failed = False
    try:
        fut.result(timeout=30)
    except requests.ConnectionError:
        provider_failed = True
    failure_thread = market_wires[-1]["thread"]
    def _d2_probe():
        ident = threading.get_ident()
        try:
            requests.get("https://d2-noctx.example.com/ping", timeout=7)
        except requests.RequestException:
            pass
        return ident, request_deadline.get()
    thread_d2, ctx_d2 = single.submit(_d2_probe).result(timeout=5)
    fail_mode[0] = False
    d2_wires = list(default_wires)
    assert provider_failed, "合成供应商失败未传播"
    assert thread_d2 == failure_thread and ctx_d2 is None
    assert [t for t, _ in d2_wires] == [7, 7, 7], (
        f"同线程无 ctx 请求应保持 timeout 7: {d2_wires}")
single.shutdown(wait=True)


# ---- 零 TCP ----
assert not tcp_attempts, tcp_attempts
print(json.dumps({
    "caller_timeout": caller_timeout,
    "worker_finished_in_wait": worker_finished,
    "wires_case_a": wires_case_a,
    "first_timeout_clamped": all(
        v <= first["remaining"] + 0.05
        for v in (first["timeout"] if isinstance(first["timeout"], tuple)
                  else (first["timeout"],))),
    "success_rows": len(df),
    "field_mapping_correct": True,
    "wires_case_b": len(wires_b),
    "worker_ctx_after_success": worker_ctx_after,
    "late_worker_entered": late_worker_delta == 1,
    "late_sdk_call_suppressed": late_sdk_count == 0,
    "late_budget_exhausted": isinstance(late_exc, BudgetExhausted),
    "late_wires": late_wires_now - wires_c_start,
    "late_default_timeout_kept": all(t == 7 for t, _ in late_timeout_wires),
    "expired_same_thread_cleanup": thread_c == worker_thread[0],
    "success_same_thread_cleanup": thread_d1 == success_thread
    and ctx_d1 is None,
    "provider_failure_propagated": provider_failed,
    "failure_same_thread_cleanup": thread_d2 == failure_thread
    and ctx_d2 is None,
    "default_timeout_kept": [t for t, _ in d2_wires] == [7, 7, 7],
    "tcp_attempts": len(tcp_attempts),
}, ensure_ascii=False))
'''.replace("__PROJECT_ROOT__", str(PROJECT_ROOT))


def test_akshare_internal_pagination_respects_fetch_deadline(tmp_path) -> None:
    """fresh subprocess：真实已安装 AkShare 内部分页在统一 worker 内服从
    同一 fetch 截止（PR84 复审 P2 修复回归；跨本地 1.19.1 与 CI 1.20.35
    两版本——响应按真实 PreparedRequest URL 的 fields 构造东财 clist
    f-key 行，无 SDK 源码 AST/inspect 依赖）。

    - worker 内幂等安装 eastmoney_patch(budget_only=True) + bind
      fetch_deadline（root 反例：第一页 deadline=None，调用方放弃后 SDK
      分页 sleep 又发出截止后新 wire——修复后第二页派发在
      patched_request 被拒绝、零新 send wire）
    - 首 wire timeout 钳制到 dispatch 时刻实际剩余预算
    - 成功/供应商失败/截止异常三类退出：finally 属于执行 SDK 的原线程
      （single-worker 同线程证明），worker 后续任务零残留
    - 真实排队晚获得执行权（占满共享池→预算内提交→worker 实际执行时
      已过期）：require_fetch 拒绝，零 wire
    - 无上下文无关 requests 保持调用方 timeout=7
    - 只隔离 Session.send 终端（真实 SDK 分页/重试逻辑全链），零真实 TCP
    """
    import subprocess

    script_path = tmp_path / "akshare_pagination_deadline_probe.py"
    script_path.write_text(_AKSHARE_PAGINATION_SCRIPT, encoding="utf-8")

    env = dict(os.environ)
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    env["ENV_FILE"] = "/dev/null"
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
    assert '"caller_timeout": true' in proc.stdout
    assert '"worker_finished_in_wait": true' in proc.stdout
    assert '"wires_case_a": 1' in proc.stdout
    assert '"first_timeout_clamped": true' in proc.stdout
    assert '"success_rows": 2' in proc.stdout
    assert '"field_mapping_correct": true' in proc.stdout
    assert '"worker_ctx_after_success": null' in proc.stdout
    assert '"late_worker_entered": true' in proc.stdout
    assert '"late_budget_exhausted": true' in proc.stdout
    assert '"late_wires": 0' in proc.stdout
    assert '"expired_same_thread_cleanup": true' in proc.stdout
    assert '"late_default_timeout_kept": true' in proc.stdout
    assert '"success_same_thread_cleanup": true' in proc.stdout
    assert '"provider_failure_propagated": true' in proc.stdout
    assert '"failure_same_thread_cleanup": true' in proc.stdout
    assert '"default_timeout_kept": true' in proc.stdout
    assert '"tcp_attempts": 0' in proc.stdout
