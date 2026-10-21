"""Replay a messages/tools JSON file through the configured Agent model, without executing tools.
Run: python scripts/probe_agent_model.py request.json --timeout 120
Add --scheduler to use the dedicated Scheduler model configuration.
Loads the normal ENV_FILE/.env and model configuration; makes a paid request, prints metadata only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import math
import os
from pathlib import Path
import sys
from time import monotonic
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class ProbeRequest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, validate_assignment=True)
    messages: list[dict[str, Any]] = Field(min_length=1)
    tools: list[dict[str, Any]] = Field(default_factory=list)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('request', type=Path)
    parser.add_argument('--timeout', type=float, default=120.0)
    parser.add_argument('--scheduler', action='store_true', help='Use the Scheduler model configuration')
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('timeout must be positive')
    started = monotonic()
    summary: dict[str, Any] = {}
    logging.disable(logging.CRITICAL)
    try:
        # Libraries can include credentials or response text in errors; emit only safe metadata.
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            from src.agent.llm_adapter import LLMToolAdapter
            from src.config import Config, get_config
            request = ProbeRequest.model_validate_json(args.request.read_text())
            messages = request.messages
            for message in messages:
                if message.get('tool_calls'):
                    message['tool_calls'] = [
                        {'id': tool['id'], 'name': tool['function']['name'],
                         'arguments': json.loads(tool['function']['arguments'])}
                        for tool in message['tool_calls']
                    ]
            config = Config.reload_for_scheduler() if args.scheduler else get_config()
            response = LLMToolAdapter(config).call_with_tools(messages, request.tools, timeout=args.timeout)
            summary['configured_reasoning_efforts'] = [
                entry['litellm_params'].get('reasoning_effort')
                for entry in config.llm_model_list
                if response.model in (entry.get('model_name'), entry['litellm_params'].get('model'))
            ]
            finish = response.raw.choices[0].finish_reason if response.raw else None
            summary.update(model=response.model, provider=response.provider, finish_reason=finish,
                           content_chars=len(response.content or ''), reasoning_chars=len(response.reasoning_content or ''),
                           tool_calls=len(response.tool_calls))
            try:
                summary['json_object'] = isinstance(json.loads(response.content or ''), dict)
            except ValueError:
                summary['json_object'] = False
            success = response.provider != 'error' and finish == 'stop' and summary['json_object']
    except Exception as exc:
        summary['error_type'] = type(exc).__name__
        success = False
    summary.update(success=bool(success), elapsed_seconds=round(monotonic() - started, 3))
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
