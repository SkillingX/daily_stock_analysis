"""Process-specific model routing through real loopback HTTP requests."""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest


@pytest.fixture
def model_server() -> Iterator[tuple[str, list[str], dict[str, bool]]]:
    paths: list[str] = []
    state = {"fail_scheduler": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            paths.append(self.path)
            failed = state["fail_scheduler"] and self.path.startswith("/scheduler/")
            if failed:
                payload = json.dumps({"error": {"message": "controlled rejection", "type": "authentication_error"}}).encode()
                status, content_type = 401, "application/json"
            else:
                def sse_event(delta: dict[str, Any] | None = None, finish: str | None = None, usage: dict[str, Any] | None = None) -> str:
                    chunk: dict[str, Any] = {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                                             "model": request["model"], "choices": []}
                    if usage is None:
                        chunk["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
                    else:
                        chunk["usage"] = usage
                    return f"data: {json.dumps(chunk)}\n\n"
                content = '{"status":"ok"}'
                events = [sse_event({"role": "assistant"})]
                events += [sse_event({"content": content[at:at+8]}) for at in range(0, len(content), 8)]
                events += [sse_event(finish="stop"),
                           sse_event(usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}),
                           "data: [DONE]\n\n"]
                payload = "".join(events).encode()
                status, content_type = 200, "text/event-stream"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", paths, state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)


def write_config(tmp_path: Path, base: str) -> Path:
    for role in ("api", "scheduler"):
        (tmp_path / f"{role}.yaml").write_text(
            f"model_list:\n  - model_name: openai/{role}-fixture\n"
            f"    litellm_params:\n      model: openai/{role}-fixture\n"
            f"      api_base: {base}/{role}/v1\n      api_key: os.environ/FIXTURE_KEY\n"
        )
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"LITELLM_CONFIG={tmp_path / 'api.yaml'}\n"
        f"SCHEDULER_LITELLM_CONFIG={tmp_path / 'scheduler.yaml'}\n"
        "LITELLM_MODEL=openai/api-fixture\nAGENT_LITELLM_MODEL=openai/api-fixture\n"
        "LITELLM_FALLBACK_MODELS=openai/api-fixture\nFIXTURE_KEY=fixture-secret\n"
        "STOCK_LIST=600519\n"
    )
    return env_file


def run_process(env_file: Path, script: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    env.update(ENV_FILE=str(env_file), LITELLM_LOCAL_MODEL_COST_MAP="True",
               NO_PROXY="127.0.0.1,localhost", DATABASE_PATH=str(env_file.parent / "db.sqlite"))
    result = subprocess.run(
        [sys.executable, "-c", "import main\nfrom src.config import Config, get_config\n" + script],
        env=env, capture_output=True, text=True, timeout=45,
    )
    assert "fixture-secret" not in result.stdout + result.stderr
    return result


def test_processes_route_and_scheduler_reload_keeps_its_model(
    tmp_path: Path, model_server: tuple[str, list[str], dict[str, bool]],
) -> None:
    base, paths, state = model_server
    env_file = write_config(tmp_path, base)
    for role in ("api", "scheduler"):
        script = f'''
from pathlib import Path
import os
from src.agent.llm_adapter import LLMToolAdapter
role = {role!r}
config = main._reload_runtime_config() if role == "scheduler" else get_config()
if role == "scheduler":
    path = Path(os.environ["ENV_FILE"])
    path.write_text(path.read_text().replace("STOCK_LIST=600519", "STOCK_LIST=300260"))
    config = main._reload_runtime_config()
    assert config.stock_list == ["300260"]
    assert config.litellm_fallback_models == []
assert config.litellm_model == f"openai/{{role}}-fixture"
assert config.agent_litellm_model == config.litellm_model
assert get_config() is config
response = LLMToolAdapter().call_text([{{"role":"user", "content":"hello"}}], timeout=10)
assert response.content == '{{"status":"ok"}}', response.content
'''
        result = run_process(env_file, script)
        assert result.returncode == 0, result.stdout + result.stderr
    assert paths == ["/api/v1/chat/completions", "/scheduler/v1/chat/completions"]

    state["fail_scheduler"] = True
    paths.clear()
    result = run_process(env_file, '''
from src.agent.llm_adapter import LLMToolAdapter
main._reload_runtime_config()
response = LLMToolAdapter().call_text([{"role":"user", "content":"hello"}], timeout=10)
assert response.provider == "error"
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert paths and all(path.startswith("/scheduler/") for path in paths)


@pytest.mark.parametrize("invalid", ["missing", "empty", "missing_key", "two_models"])
def test_invalid_scheduler_config_fails_before_provider_fallback(
    tmp_path: Path, model_server: tuple[str, list[str], dict[str, bool]], invalid: str,
) -> None:
    base, paths, _state = model_server
    env_file = write_config(tmp_path, base)
    profile = tmp_path / "scheduler.yaml"
    if invalid == "missing":
        profile.unlink()
    elif invalid == "empty":
        profile.write_text("model_list: []\n")
    elif invalid == "missing_key":
        profile.write_text(profile.read_text().replace("os.environ/FIXTURE_KEY", "os.environ/ABSENT_KEY"))
    else:
        profile.write_text(profile.read_text() + profile.read_text().split("model_list:\n")[1])
    result = run_process(env_file, '''
get_config()  # The API configuration remains valid.
try:
    main._reload_runtime_config()
except ValueError as exc:
    assert "SCHEDULER_LITELLM_CONFIG" in str(exc)
else:
    raise AssertionError("Invalid scheduler config must not use the API model")
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert paths == []


def test_shared_process_is_rejected_before_http_server_start(
    tmp_path: Path, model_server: tuple[str, list[str], dict[str, bool]],
) -> None:
    base, _paths, _state = model_server
    env_file = write_config(tmp_path, base)
    result = run_process(env_file, '''
import sys
from unittest.mock import patch
sys.argv = ["main.py", "--serve", "--schedule", "--no-run-immediately"]
with patch("main._setup_bootstrap_logging"), patch("main._setup_runtime_logging"), \
     patch("main.prepare_webui_frontend_assets") as frontend, \
     patch("src.scheduler.run_with_schedule"), patch("main.start_bot_stream_clients"), \
     patch("main.start_api_server") as start:
    assert main.main() == 1
    frontend.assert_not_called()
    start.assert_not_called()
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_scheduler_override_preserves_existing_config(
    tmp_path: Path, model_server: tuple[str, list[str], dict[str, bool]],
) -> None:
    base, _paths, _state = model_server
    env_file = write_config(tmp_path, base)
    env_file.write_text("\n".join(line for line in env_file.read_text().splitlines()
                                  if not line.startswith("SCHEDULER_LITELLM_CONFIG=")))
    result = run_process(env_file, '''
config = main._reload_runtime_config()
assert config.litellm_model == "openai/api-fixture"
assert config.litellm_fallback_models == ["openai/api-fixture"]
''')
    assert result.returncode == 0, result.stdout + result.stderr


def test_probe_cli_uses_dedicated_scheduler_model(
    tmp_path: Path, model_server: tuple[str, list[str], dict[str, bool]],
) -> None:
    base, paths, _state = model_server
    env_file = write_config(tmp_path, base)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"messages": [{"role": "user", "content": "hello"}]}))
    result = run_process(env_file, f'''
import sys
from scripts.probe_agent_model import main as probe
sys.argv = ["probe_agent_model.py", {str(request)!r}, "--scheduler", "--timeout", "10"]
assert probe() == 0
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert paths == ["/scheduler/v1/chat/completions"]


@pytest.mark.parametrize("valid", [True, False])
def test_scheduler_startup_loads_profile_before_registering_jobs(
    tmp_path: Path, model_server: tuple[str, list[str], dict[str, bool]], valid: bool,
) -> None:
    base, paths, _state = model_server
    env_file = write_config(tmp_path, base)
    if not valid:
        (tmp_path / "scheduler.yaml").unlink()
    result = run_process(env_file, f'''
import sys
from unittest.mock import patch
sys.argv = ["main.py", "--schedule", "--no-run-immediately"]
with patch("main._setup_bootstrap_logging"), patch("main._setup_runtime_logging"), \
     patch("src.scheduler.run_with_schedule") as run:
    assert main.main() == {0 if valid else 1}
    assert run.called == {valid!r}
    if {valid!r}:
        assert get_config().litellm_model == "openai/scheduler-fixture"
        assert run.call_args.kwargs["run_immediately"] is False
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert paths == []


def test_serve_only_ignores_scheduler_profile_and_schedule_enabled(
    tmp_path: Path, model_server: tuple[str, list[str], dict[str, bool]],
) -> None:
    base, paths, _state = model_server
    env_file = write_config(tmp_path, base)
    with env_file.open("a") as stream:
        stream.write("SCHEDULE_ENABLED=true\n")
    (tmp_path / "scheduler.yaml").unlink()
    result = run_process(env_file, '''
import sys
from unittest.mock import patch
sys.argv = ["main.py", "--serve-only"]
with patch("main._setup_bootstrap_logging"), patch("main._setup_runtime_logging"), \
     patch("main.prepare_webui_frontend_assets", return_value=True), \
     patch("main.start_api_server") as start, patch("main.start_bot_stream_clients"), \
     patch("main.time.sleep", side_effect=KeyboardInterrupt):
    assert main.main() == 0
    assert start.call_args.kwargs["config"].litellm_model == "openai/api-fixture"
''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert paths == []
