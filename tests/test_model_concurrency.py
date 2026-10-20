"""Application model entrypoints against a loopback provider, never paid APIs."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest


@pytest.fixture
def provider() -> Iterator[tuple[str, dict[str, int]]]:
    state = {"active": 0, "peak": 0, "count": 0, "streams": 0}
    lock = threading.Lock()

    class Provider(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(state.copy()).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["active"] += 1
                state["count"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                if request.get("stream"):
                    with lock:
                        state["streams"] += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                             "model": "fixture", "choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": None}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                    self.wfile.flush()
                    # Observe actual peer close; a first chunk alone is not end-of-stream.
                    self.connection.settimeout(0.05)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        try:
                            if not self.connection.recv(1, socket.MSG_PEEK):
                                break
                        except TimeoutError:
                            continue
                    return
                time.sleep(0.2)
                hold_deadline = time.monotonic() + state.get("hold_seconds", 8)
                while state.get("hold_models") and time.monotonic() < hold_deadline:
                    time.sleep(0.01)
                if state.get("research_mode") == 1 and any(message.get("role") == "tool" for message in request.get("messages", [])):
                    body = b'{"error":{"message":"controlled provider failure","type":"server_error"}}'
                    self.send_response(500)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if state.get("research_mode") == 2:
                    time.sleep(0.6)
                message: dict[str, Any] = {"role": "assistant", "content": '{"status":"ok"}'}
                if state.get("legacy_text"):
                    message["content"] = "# 样本研究\n\n本次资料不足，尚不能形成完整结论。"
                finish = "stop"
                if state.get("research_mode") == 1:
                    message = {"role": "assistant", "content": None, "tool_calls": [{"id": "search-1", "type": "function", "function": {"name": "search_comprehensive_intel", "arguments": '{"stock_code":"600519","stock_name":"样本"}'}}]}
                    finish = "tool_calls"
                body = json.dumps({
                    "id": "fixture", "object": "chat.completion", "created": 0,
                    "model": "fixture", "choices": [{"index": 0, "finish_reason": finish,
                    "message": message}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    state["active"] -= 1

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def run_application(tmp_path: Path, base: str, script: str, limit: str = "1", extra_env: dict[str, str] | None = None, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    config = tmp_path / "models.yaml"
    config.write_text(f"model_list:\n  - model_name: openai/fixture\n    litellm_params:\n      model: openai/fixture\n      api_base: {base}\n      api_key: fixture-key\n")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    env.update(ENV_FILE=str(tmp_path / "absent.env"), LITELLM_CONFIG=str(config),
               LITELLM_MODEL="openai/fixture", DEEP_RESEARCH_LLM_CONCURRENCY=limit,
               OPENAI_API_KEY="fixture-key", OPENAI_API_BASE=base, DATABASE_PATH=str(tmp_path / "db.sqlite"),
               LITELLM_LOCAL_MODEL_COST_MAP="True", NO_PROXY="127.0.0.1,localhost")
    env.update(extra_env or {})
    prelude = '''
import json, sys, time, urllib.request
from src.agent.llm_adapter import LLMToolAdapter
from src.services.system_config_service import SystemConfigService
base = sys.argv[1]
def agent(timeout=10):
    return LLMToolAdapter().call_completion([{"role":"user", "content":"hello"}], timeout=timeout)
def state():
    return json.load(urllib.request.urlopen(base + "/state"))
def wait_active():
    end = time.monotonic() + 10
    while state()["active"] == 0:
        assert time.monotonic() < end
        time.sleep(0.005)
'''
    return subprocess.run([sys.executable, "-c", prelude + script, base], env=env, capture_output=True, text=True, timeout=timeout)


@pytest.mark.parametrize("limit", [1, 2])
def test_business_modules_and_instances_share_actual_request_limit(tmp_path: Path, provider: tuple[str, dict[str, int]], limit: int) -> None:
    base, observed = provider
    result = run_application(tmp_path, base, '''
import concurrent.futures
from src.analyzer import GeminiAnalyzer
def run(i):
    if i == 1:
        result = SystemConfigService().test_llm_channel(name="fixture", protocol="openai", base_url=base, api_key="fixture-key", models=["fixture"])
        assert result["success"], result
    elif i == 3:
        assert GeminiAnalyzer().generate_text("hello")
    else:
        result = agent()
        assert result.provider != "error", result.content
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(run, range(4)))
''', str(limit))
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["count"] == 4
    assert observed["peak"] == limit


def test_stream_capability_closes_transport_before_next_request(tmp_path: Path, provider: tuple[str, dict[str, int]]) -> None:
    base, observed = provider
    result = run_application(tmp_path, base, '''
result = SystemConfigService().test_llm_channel(name="fixture", protocol="openai", base_url=base, api_key="fixture-key", models=["fixture"], capability_checks=["stream"])
assert result["success"], result
assert agent().provider != "error"
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["streams"] == 1
    assert observed["count"] == 3
    assert observed["peak"] == 1


def test_cancelled_async_wait_and_queue_timeout_do_not_send_requests(tmp_path: Path, provider: tuple[str, dict[str, int]]) -> None:
    base, observed = provider
    result = run_application(tmp_path, base, '''
import asyncio, concurrent.futures
from src.services.llm_subjective_service import LLMSubjectiveScoringService
async def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(agent)
        wait_active()
        pending = asyncio.create_task(LLMSubjectiveScoringService(model="openai/fixture").analyze_supply_chain("300260", "fixture"))
        await asyncio.sleep(0.025)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled wait returned success")
        timed = pool.submit(agent, 0.025)
        assert timed.result().provider == "error"
        assert first.result().provider != "error"
        await LLMSubjectiveScoringService(model="openai/fixture").analyze_supply_chain("300260", "fixture")
asyncio.run(main())
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["count"] == 2
    assert observed["peak"] == 1


def test_alphasift_bridge_and_router_do_not_bypass_or_double_acquire(tmp_path: Path, provider: tuple[str, dict[str, int]]) -> None:
    base, observed = provider
    result = run_application(tmp_path, base, '''
import concurrent.futures
import litellm
from litellm import Router
from src.config import get_config
from src.services.alphasift_service import _alphasift_litellm_headers
config = get_config()
def foreign(i):
    with _alphasift_litellm_headers(config):
        if i:
            router = Router(model_list=config.llm_model_list, num_retries=0)
            response = router.completion(model="openai/fixture", messages=[{"role":"user","content":"hi"}], timeout=5)
        else:
            response = litellm.completion(model="openai/fixture", api_base=base, api_key="fixture-key", messages=[{"role":"user","content":"hi"}], timeout=5)
        assert response.choices[0].message.content
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    assert pool.submit(agent).result().provider != "error"
    list(pool.map(foreign, [0,1]))
    # Our regular routed entrypoint now traverses the installed foreign bridge.
    assert pool.submit(agent).result().provider != "error"
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed["count"] == 4
    assert observed["peak"] == 1


@pytest.mark.parametrize("limit,expected", [("", 3), ("2", 2), ("0", None), ("-1", None), ("bad", None), ("1.5", None)])
def test_startup_validates_model_limit(tmp_path: Path, limit: str, expected: int | None) -> None:
    result = run_application(tmp_path, "http://127.0.0.1:1/v1", f'from src.config import get_config\nassert get_config().deep_research_llm_concurrency == {expected!r}\n', limit)
    if expected is None:
        assert result.returncode != 0
        assert "must be a positive integer" in result.stderr
    else:
        assert result.returncode == 0, result.stdout + result.stderr


def test_stream_response_timeout_caps_actual_transport_read(tmp_path: Path, provider: tuple[str, dict[str, int]]) -> None:
    """单次响应上限与调用者预算都作用于真实传输读取，排队等待只消耗剩余预算。

    单次上限被 config 钳制到最小 10 秒，因此 hold 13 秒：cap 生效时读取约 10 秒
    超时，未生效时会等完 hold 后成功返回。排队段：holder 出站约 10 秒后失败，
    waiter 名额等待后只剩剩余预算，出站读取被削减而非重置为原始 timeout。
    """
    base, observed = provider
    observed["hold_models"] = True
    observed["hold_seconds"] = 13
    try:
        result = run_application(tmp_path, base, """
import concurrent.futures, time
from src.core.research_run import ResearchRun
run = ResearchRun(60)
with run.bind():
    start = time.monotonic()
    capped = agent(timeout=30)
    capped_elapsed = time.monotonic() - start
    assert capped.provider == "error", capped.content
    assert 8 < capped_elapsed < 13, capped_elapsed
    start = time.monotonic()
    short = agent(timeout=0.5)
    short_elapsed = time.monotonic() - start
    assert short.provider == "error", short.content
    assert short_elapsed < 2, short_elapsed
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(agent, 11)
        # 此前超时请求的服务端 handler 仍挂起，active 不会归零；用请求计数确认
        # holder（第 3 个请求）真正出站后，主线程请求才必然在名额上排队。
        deadline = time.monotonic() + 10
        while state()["count"] < 3:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        start = time.monotonic()
        queued_result = agent(timeout=12)   # 主线程在名额上排队，出站只剩剩余预算
        queued_elapsed = time.monotonic() - start
    assert holder.result().provider == "error"
    assert queued_result.provider == "error"
    assert queued_elapsed < 17, queued_elapsed
""", extra_env={"LLM_STREAM_RESPONSE_TIMEOUT": "10"}, timeout=90)
    finally:
        observed["hold_models"] = False
        observed.pop("hold_seconds", None)
    assert result.returncode == 0, result.stdout + result.stderr
    # 四个请求都真实到达传输层；服务端 handler 在客户端读超时后仍挂起至 hold
    # 结束，peak 反映的是残留连接而非名额并发，此处不作断言。
    assert observed["count"] == 4
