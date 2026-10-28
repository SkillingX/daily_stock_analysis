#!/usr/bin/env python3
"""Verify the LiteLLM tool-processing and MCP transport import chain.

Issue #58 deployment preflight, run after dependency installation and before
stopping the old service: ordinary tool declarations enter LiteLLM's lazy MCP
import path even when no MCP tools are passed, so a plain install with an old
MCP release passes ``pip check`` and health imports while the real tools path
fails at request time. A failing check here must abort the service switch with
the old service still running; there is no legacy-SDK compatibility path.
"""
from __future__ import annotations

import argparse
import json
from importlib import import_module, metadata
import sys

# (check name, module, required attribute). Mirrors the imports the production
# tools path performs: litellm.completion(tools=...) pulls both MCP handlers,
# and src/services/mcp_client.py pulls the current MCP transport.
CHECKS: tuple[tuple[str, str, str], ...] = (
    ("litellm_tools_mcp_handler", "litellm.responses.mcp.chat_completions_handler", "acompletion_with_mcp"),
    ("litellm_proxy_mcp_gateway", "litellm.responses.mcp.litellm_proxy_mcp_handler", "LiteLLM_Proxy_MCP_Handler"),
    ("mcp_client_session", "mcp", "ClientSession"),
    ("mcp_streamable_http_transport", "mcp.client.streamable_http", "streamable_http_client"),
    ("mcp_http_client_factory", "mcp.shared._httpx_utils", "create_mcp_http_client"),
)

_VERSION_DISTS: tuple[str, ...] = ("litellm", "mcp", "openai")


def preflight() -> dict[str, object]:
    """Run every import check; return a JSON-serialisable report."""
    checks: list[dict[str, object]] = []
    ok = True
    for name, module, attribute in CHECKS:
        try:
            imported = import_module(module)
            getattr(imported, attribute)
        except (Exception, BaseExceptionGroup) as exc:
            # A failed import is the finding itself — but a group carrying a
            # system-exit member is never an ordinary incompatibility: keep
            # the same fatal-signal contract as the runtime cancellation
            # handling (issue #58) and let it propagate. (Standalone script:
            # the fatal-member check is inlined, not imported from src/.)
            if isinstance(exc, BaseExceptionGroup) and exc.subgroup((KeyboardInterrupt, SystemExit)) is not None:
                raise
            ok = False
            checks.append({"check": name, "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        else:
            checks.append({"check": name, "status": "ok"})
    versions: dict[str, str | None] = {}
    for dist in _VERSION_DISTS:
        try:
            versions[dist] = metadata.version(dist)
        except Exception:  # noqa: BLE001 - reporting only, never a gate
            versions[dist] = None
    return {"status": "ok" if ok else "failed", "checks": checks, "versions": versions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print one JSON report line")
    args = parser.parse_args()
    report = preflight()
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        for check in report["checks"]:  # type: ignore[union-attr]
            status = check["status"]
            suffix = "" if status == "ok" else f" — {check['error']}"
            print(f"  [{status}] {check['check']}{suffix}")
        print(f"  versions: {report['versions']}")
        print(f"  preflight: {report['status']}")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
