"""Verify the configured model probe at the CLI-to-provider HTTP boundary.
Run: python -m pytest tests/test_agent_model_probe.py
Uses a loopback provider with dummy credentials; no paid requests.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Any


@pytest.mark.parametrize("effort, finish", [("high", "stop"), (None, "stop"), ("high", "length")])
def test_configured_reasoning_and_tool_history_reach_provider(tmp_path: Path, effort: str | None, finish: str) -> None:
    requests: list[dict[str, Any]] = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            payload = json.dumps({'id':'test','object':'chat.completion','created':0,'model':'glm-5.3','choices':[{'index':0,'finish_reason':finish,'message':{'role':'assistant','content':'{"narrative":"fixture"}'}}], 'usage':{'prompt_tokens':5,'completion_tokens':3,'total_tokens':8}}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args: Any) -> None:
            pass
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker = Thread(target=server.serve_forever,daemon=True)
    worker.start()
    config = tmp_path/'models.yaml'
    config.write_text(f'model_list:\n  - model_name: openai/glm-5.3\n    litellm_params:\n      model: openai/glm-5.3\n      api_base: http://127.0.0.1:{server.server_port}/v1\n      api_key: os.environ/PROBE_TEST_KEY\n' + (f'      reasoning_effort: {effort}\n' if effort else ''))
    request = tmp_path/'request.json'
    request.write_text(json.dumps({'messages':[{'role':'user','content':'Summarize available facts as JSON'},{'role':'assistant','content':None,'reasoning_content':'opaque protocol fixture','tool_calls':[{'id':'call-1','type':'function','function':{'name':'facts','arguments':'{}'}}]},{'role':'tool','tool_call_id':'call-1','content':'{"available":false}'}]}))
    env = {k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','TMPDIR')}
    env.update(ENV_FILE=str(tmp_path/'absent.env'),LITELLM_CONFIG=str(config),LITELLM_MODEL='openai/glm-5.3',PROBE_TEST_KEY='fixture-secret',LITELLM_LOCAL_MODEL_COST_MAP='True')
    try:
        result = subprocess.run([sys.executable,'scripts/probe_agent_model.py',str(request),'--timeout','10'],env=env,text=True,capture_output=True,timeout=30)
        assert result.returncode == (0 if finish == 'stop' else 1), result.stdout + result.stderr
        out = json.loads(result.stdout)
        assert out['finish_reason']==finish and out['json_object'] is True
        assert out['configured_reasoning_efforts']==[effort]
        assert len(requests)==1
        assert requests[0].get('reasoning_effort')==effort
        assert requests[0]['messages'][1]['reasoning_content']=='opaque protocol fixture'
        assert requests[0]['messages'][2]['tool_call_id']=='call-1'
        assert 'fixture-secret' not in result.stdout+result.stderr
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)
