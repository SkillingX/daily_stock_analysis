#!/usr/bin/env python
"""Frozen-input baseline / re-run entrypoint for the issue #55 model-metric chain.

Records the model-call chain of the CURRENT code tree (``ROOT`` below) against
three frozen fact samples (sufficient / missing key facts / conflicting
news-vs-governance evidence), keeping the real orchestration, admission,
HTTP transport, parsing, dimension contracts and rule consumers — only the
external model endpoint and supplier payloads are replaced (``--provider
fixture``, the default) or left real (``--provider real``, the coordinator's
explicit opt-in for later old/new interleaving; never run by the builder).

Counting semantics (kept explicit, no generic observation layer):
- model transport: loopback server hits in fixture mode; the per-attempt
  ledger (``run.attempts``) is authoritative in both modes;
- supplier queries: logical handler calls per need identity vs. actual
  round-trips at the frozen supplier boundary (the #51 same-run single-flight
  and the cross-report caches stay real, so handler call counts are NOT
  claimed as duplicate supplier round-trips);
- durations: fixture fetches are local, so cold/hot *supplier* latency is
  unknown here; cache condition is recorded separately. No speedup claims.

Offline default is a dry run proving the metering chain only — it is not a
quality or performance acceptance. Sample facts are explicitly fixtures, not
online evidence. Output is measurement evidence: keep it under
``.claude/reviews/stage1-baseline/`` or another non-committed location.

Usage:
    python scripts/model_workflow_baseline.py --label new [--samples ...]
        [--runs 3] [--provider fixture|real] [--fixture FILE] [--dims a,b]
        [--hot] [--out DIR] [--cache-dir DIR] [--expect-commit SHA]
        [--expect-content SHA] [--timeout S] [--include-untracked a,b]

Freezing the "old" source — coordinator runs this ONLY after the main tree is
stable and reviewed; this live tree is NOT frozen yet:
    VENV/bin/python scripts/model_workflow_baseline.py \
        --make-snapshot /path/stage1-old-src [--include-untracked a,b]
    # Copies tracked files inside the explicit source set (SOURCE_DIRS /
    # SOURCE_ROOT_FILES below) plus explicitly approved untracked source
    # (APPROVED_UNTRACKED or --include-untracked) and writes
    # baseline-source-manifest.json with one sha256 per file. Anything else
    # in the working tree — .env, private configs, caches, DBs, logs — is
    # excluded by construction, never by blacklisting secrets. Prints the
    # base commit and the content identity to pin in later runs.

Replaying from a snapshot (no .git required):
    cd /path/stage1-old-src && VENV/bin/python scripts/model_workflow_baseline.py \
        --label old --expect-commit <printed-commit> \
        [--expect-content <printed-content-identity>] ...
    # Every run in a snapshot verifies the manifest first: a changed byte in
    # any listed file, a missing file, or an extra source file aborts before
    # any endpoint or research starts.

Real provider (explicit opt-in via --provider real; no extra env gate):
    - before any run it only checks that a channel/key is CONFIGURED for the
      effective primary model (presence check, explicitly NOT credential
      validation; credentials come only from the authorized process env or
      ENV_FILE — this entry never searches anywhere else);
    - if the real calls actually fail, the failures are recorded and the
      process exits 3: a written JSON file is never itself presented as
      successful real evidence.

Tool-contract correction (2026-10-27, from the first real comparison run):
    - the frozen realtime quote is a real UnifiedRealtimeQuote (the contract
      every consumer already codes against), not a partial SimpleNamespace
      that crashed analyze_trend with ``no attribute 'open_price'``;
    - the frozen observation point (fixture as_of) is carried through the
      whole run via history_loader's frozen-target-date ContextVar AND
      bounded date-subclass clocks installed for the two known external
      wall-clock consumers (analysis_tools' merge guard and explore_agents'
      business-prompt analysis date), so the real _merge_today_realtime_bar
      guard sees "today already stored" instead of appending a current-day
      bar to the declared frozen history, and the prompt clock matches the
      declared as_of.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import median
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHEMA = "model_workflow_baseline_v3"
SNAPSHOT_MANIFEST_SCHEMA = "model_workflow_baseline_snapshot_v1"
MANIFEST_NAME = "baseline-source-manifest.json"
SAMPLE_IDS = ("sufficient", "missing_key_facts", "conflicting_news_governance")
FIXTURE_REL = Path("tests/fixtures/model_workflow_baseline.json")
# Explicit source set for snapshots and content identity: tracked files under
# these top-level dirs (plus the root files below) plus APPROVED_UNTRACKED.
# Secrets (.env at root, ignored caches/DBs/logs) are outside this set by
# construction; git-untracked files outside the approvals are refused loudly.
SOURCE_DIRS: tuple[str, ...] = (
    "src", "data_provider", "api", "bot", "tests", "scripts", "data",
    "templates", "experts", "strategies", "chanlun", "skills", ".grok", ".foundry",
)
SOURCE_ROOT_FILES: tuple[str, ...] = (
    "main.py", "server.py", "pyproject.toml", "requirements.txt", "setup.cfg",
)
APPROVED_UNTRACKED: tuple[str, ...] = (
    "src/llm/attempt.py",
    "scripts/model_workflow_baseline.py",
    "tests/fixtures/model_workflow_baseline.json",
    "tests/test_model_attempt_ledger.py",
    "tests/test_report_usage_attribution.py",
)
# User-task markers of the researcher prompts served by the frozen model.
DIM_MARKERS: tuple[tuple[str, str], ...] = (
    ("技术面深度分析", "technical"),
    ("资金面深度分析", "capital"),
    ("业务画像", "business"),
    ("股权架构与高管背景", "ownership"),
)
_SECRET_MARKERS = ("key", "token", "secret", "auth", "password", "credential")
_SCALAR_CONTROL_KEYS = ("model", "temperature", "top_p", "presence_penalty", "frequency_penalty",
                        "max_tokens", "max_completion_tokens", "reasoning_effort", "effort", "thinking")

PROVIDER_STATE: dict[str, Any] = {}
_CURRENT_SAMPLE = {"id": SAMPLE_IDS[0]}
# supplier counting: logical handler calls vs frozen-boundary round-trips
LOGICAL_CALLS: dict[str, int] = {}
NATIVE_CALLS: dict[str, int] = {}
FIXTURE: dict[str, Any] = {}
FROZEN_SEARCH_BOUNDARY: dict[str, Any] = {}
# Frozen observation clock actually installed for the analysis tools, recorded
# into the run document as evidence of the pinned wall-clock seam.
FROZEN_CLOCK: dict[str, Any] = {}
# Post-assembly fact-verification result for the current run: the actual
# adopted ctx inputs (history/quote/fundamental/concepts) checked against the
# declared fixture facts. A mismatch raises before any model call.
FACT_ADOPTION: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# source content identity (git working tree or verified snapshot manifest)
# ---------------------------------------------------------------------------

def _git(args: list[str]) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout


def _git_paths(args: list[str]) -> list[str]:
    out = subprocess.run(["git", *args, "-z"], cwd=ROOT, capture_output=True, text=True)
    return [p for p in out.stdout.split("\0") if p]


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content_identity(files: dict[str, str]) -> str:
    material = "".join(f"{name}\0{digest}\n" for name, digest in sorted(files.items()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _is_included(rel: str) -> bool:
    top = rel.split("/", 1)[0]
    return top in SOURCE_DIRS or rel in SOURCE_ROOT_FILES


def source_file_set(extra_untracked: list[str]) -> dict[str, Any]:
    """Content map {relpath: sha256} of the source that would run, git mode."""
    tracked = {p for p in _git_paths(["ls-files"]) if _is_included(p)}
    untracked = {p for p in _git_paths(["ls-files", "--others", "--exclude-standard"])
                 if _is_included(p)}
    approved = set(APPROVED_UNTRACKED) | set(extra_untracked)
    chosen = tracked | (untracked & approved)
    files: dict[str, str] = {}
    deleted: list[str] = []
    for rel in sorted(chosen):
        path = ROOT / rel
        if not path.is_file():
            deleted.append(rel)
        else:
            files[rel] = _hash_file(path)
    return {
        "files": files,
        "unapproved_untracked": sorted(p for p in untracked if p not in approved),
        "deleted_tracked": deleted,
        "untracked_included": sorted(untracked & approved),
    }


def _walk_source_tree() -> set[str]:
    """Files present under the explicit source set (snapshot verification)."""
    found: set[str] = set()
    for top in SOURCE_DIRS:
        base = ROOT / top
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                found.add(path.relative_to(ROOT).as_posix())
    for name in SOURCE_ROOT_FILES:
        if (ROOT / name).is_file():
            found.add(name)
    return found


def verify_snapshot_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Every listed byte unchanged and no unlisted source file present."""
    files = manifest.get("files") or {}
    problems: list[str] = []
    for rel, digest in sorted(files.items()):
        path = ROOT / rel
        if not path.is_file():
            problems.append(f"missing: {rel}")
        elif _hash_file(path) != digest:
            problems.append(f"content changed: {rel}")
    problems.extend(f"unlisted source file: {rel}" for rel in sorted(_walk_source_tree() - set(files)))
    return {"ok": not problems, "problems": problems[:50], "problem_count": len(problems)}


def code_identity(expect_commit: str | None, expect_content: str | None,
                  extra_untracked: list[str]) -> dict[str, Any]:
    """Who actually ran: content-hashed source + module origin, not just HEAD."""
    manifest_path = ROOT / MANIFEST_NAME
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("schema") != SNAPSHOT_MANIFEST_SCHEMA:
            raise SystemExit(f"[baseline] unknown manifest schema at {manifest_path}: "
                             f"{manifest.get('schema')}")
        verification = verify_snapshot_manifest(manifest)
        if not verification["ok"]:
            raise SystemExit("[baseline] snapshot manifest mismatch; refusing to run:\n  "
                             + "\n  ".join(verification["problems"]))
        files = dict(manifest.get("files") or {})
        created_from = manifest.get("created_from") or {}
        identity: dict[str, Any] = {
            "mode": "snapshot(manifest content-verified)",
            "commit": created_from.get("git_commit") or "",
            "dirty_files": [],
            "untracked_files": created_from.get("untracked_included") or [],
            "files": files,
            "content_identity_sha256": _content_identity(files),
            "manifest_verified": True,
        }
    else:
        info = source_file_set(extra_untracked)
        if info["unapproved_untracked"]:
            raise SystemExit(
                "[baseline] untracked source files inside the explicit source set are not "
                "approved; approve them explicitly via --include-untracked or remove them:\n  "
                + "\n  ".join(info["unapproved_untracked"]))
        dirty = [line[3:].strip() for line in _git(["status", "--porcelain"]).splitlines() if line.strip()]
        identity = {
            "mode": "git(working-tree content)",
            "commit": _git(["rev-parse", "HEAD"]).strip(),
            "dirty_files": sorted(dirty),
            "untracked_files": info["untracked_included"],
            "deleted_tracked": info["deleted_tracked"],
            "files": info["files"],
            "content_identity_sha256": _content_identity(info["files"]),
            "manifest_verified": False,
        }

    import src.agent.deep_research.orchestrator as orchestrator
    import src.agent.llm_adapter as llm_adapter
    import src.config as config_module
    import src.llm.attempt as attempt
    import src.llm.concurrency as concurrency
    import src.llm.generation_params as generation_params
    modules = {
        "orchestrator": orchestrator.__file__,
        "llm_adapter": llm_adapter.__file__,
        "config": config_module.__file__,
        "attempt_ledger": attempt.__file__,
        "concurrency": concurrency.__file__,
        "generation_params": generation_params.__file__,
    }
    resolved = {k: Path(v).resolve() for k, v in modules.items()}
    identity.update({
        "expect_commit": expect_commit,
        "expect_commit_match": None if expect_commit is None else identity["commit"] == expect_commit,
        "expect_content": expect_content,
        "expect_content_match": None if expect_content is None
        else identity["content_identity_sha256"] == expect_content,
        "module_paths": {k: str(v) for k, v in resolved.items()},
        "import_tree_matches_root": not [p for p in resolved.values() if ROOT not in p.parents],
        "content_note": "identity covers the full content of every source file that can run; "
                        "one changed relevant byte changes content_identity_sha256",
        "python": sys.executable,
        "python_version": sys.version.split()[0],
    })
    return identity


def make_snapshot(dest: Path, extra_untracked: list[str]) -> int:
    """Freeze the current tree's source into dest + a content manifest."""
    info = source_file_set(extra_untracked)
    if info["unapproved_untracked"]:
        print("[baseline] refusing to snapshot: untracked files inside the source set are "
              "not approved (approve via --include-untracked or remove them):",
              file=sys.stderr)
        print("  " + "\n  ".join(info["unapproved_untracked"]), file=sys.stderr)
        return 2
    if info["deleted_tracked"]:
        print(f"[baseline] refusing to snapshot: tracked source files deleted in the working "
              f"tree: {info['deleted_tracked']}", file=sys.stderr)
        return 2
    if dest.exists() and any(dest.iterdir()):
        print(f"[baseline] refusing to snapshot into non-empty directory {dest}", file=sys.stderr)
        return 2
    dest.mkdir(parents=True, exist_ok=True)
    for rel in sorted(info["files"]):
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    commit = _git(["rev-parse", "HEAD"]).strip()
    manifest = {
        "schema": SNAPSHOT_MANIFEST_SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "created_from": {
            "git_commit": commit,
            "dirty_files": [line[3:].strip() for line in _git(["status", "--porcelain"]).splitlines()
                            if line.strip()],
            "untracked_included": info["untracked_included"],
            "deleted_tracked": info["deleted_tracked"],
        },
        "python_version": sys.version.split()[0],
        "file_count": len(info["files"]),
        "files": info["files"],
        "content_identity_sha256": _content_identity(info["files"]),
        "notice": "snapshot of tracked source inside the explicit source set + approved "
                  "untracked source only; .env/private configs/caches/DBs/logs excluded by "
                  "construction. Verify with this script before trusting a replay.",
    }
    (dest / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[baseline] snapshot: {len(info['files'])} files -> {dest}")
    print(f"[baseline] base commit: {commit}")
    print(f"[baseline] content identity: {manifest['content_identity_sha256']}")
    print("[baseline] replay with: --label old "
          f"--expect-commit {commit} --expect-content {manifest['content_identity_sha256']}")
    return 0


# ---------------------------------------------------------------------------
# frozen loopback model (fixture mode only)
# ---------------------------------------------------------------------------

def _safe_request_controls(body: dict[str, Any]) -> dict[str, Any]:
    """Safe view of what a request actually carried: key names + control values only."""
    keys = sorted(str(k) for k in body)
    controls: dict[str, Any] = {}
    for key in keys:
        lowered = key.lower()
        if key == "messages":
            continue
        if any(marker in lowered for marker in _SECRET_MARKERS):
            controls[key] = "<redacted>"
        elif key in _SCALAR_CONTROL_KEYS:
            controls[key] = body.get(key)
    return {"body_keys": keys, "controls": controls}


def start_loopback_provider() -> tuple[str, dict[str, Any]]:
    """A local openai-protocol endpoint; responses come from the frozen fixture."""
    state: dict[str, Any] = {"count": 0, "active": 0, "peak": 0, "requests": []}

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            state["count"] += 1
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            try:
                text = ""
                body: dict[str, Any] = {}
                try:
                    body = json.loads(raw)
                    text = " ".join(str(m.get("content") or "") for m in body.get("messages", []))
                except (ValueError, AttributeError, TypeError):
                    pass
                sample = FIXTURE["samples"][_CURRENT_SAMPLE["id"]]
                dim = next((name for marker, name in DIM_MARKERS if marker in text), None)
                response = (sample.get("model_responses") or {}).get(dim) if dim else None
                if dim is not None:
                    # Wire-observed effective controls (never prompts or secrets).
                    state["requests"].append({"dim": dim, **_safe_request_controls(body)})
                if response is None:
                    # Never fabricate a generic JSON: an unmapped request must
                    # stay visible instead of degrading into unrelated content.
                    payload = {"error": {"message": f"baseline-fixture: unmapped model request (dim={dim})",
                                         "type": "invalid_request_error"}}
                    self.send_response(400)
                    encoded = json.dumps(payload, ensure_ascii=False).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                elif body.get("stream"):
                    # The same frozen business payload rendered as a legal
                    # SSE stream (content chunk, stop, usage tail, [DONE]).
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()

                    def sse(payload: dict[str, Any]) -> None:
                        self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode())
                        self.wfile.flush()

                    sse({"id": "baseline-fixture", "object": "chat.completion.chunk", "created": 0,
                         "model": "fixture", "choices": [{
                             "index": 0, "finish_reason": None,
                             "delta": {"role": "assistant",
                                       "content": json.dumps(response["content"], ensure_ascii=False)}}]})
                    sse({"id": "baseline-fixture", "object": "chat.completion.chunk", "created": 0,
                         "model": "fixture", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
                    sse({"id": "baseline-fixture", "object": "chat.completion.chunk", "created": 0,
                         "model": "fixture", "choices": [], "usage": dict(response["usage"])})
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                else:
                    payload = {
                        "id": "baseline-fixture", "object": "chat.completion", "created": 0,
                        "model": "fixture",
                        "choices": [{
                            "index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant",
                                        "content": json.dumps(response["content"], ensure_ascii=False)},
                        }],
                        "usage": dict(response["usage"]),
                    }
                    self.send_response(200)
                    encoded = json.dumps(payload, ensure_ascii=False).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
            finally:
                state["active"] -= 1

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/v1", state


# ---------------------------------------------------------------------------
# frozen supplier boundary (both modes: evidence must stay frozen for old/new)
# ---------------------------------------------------------------------------

def _count(bucket: dict[str, int], need: str) -> None:
    bucket[need] = bucket.get(need, 0) + 1


def _frozen_quote(code: str) -> Any:
    """Frozen realtime quote on the REAL quote contract (UnifiedRealtimeQuote).

    The previous partial SimpleNamespace crashed real consumers that read
    contract attributes (``Tool analyze_trend failed: SimpleNamespace object
    has no attribute open_price`` in the first real comparison run). Every
    UnifiedRealtimeQuote field exists here; the fixture-declared economics are
    price/change_pct/volume/turnover/name/source/currency and every unprovided
    optional (open/high/low/amount/turnover_rate/pre_close/valuations/...)
    stays None — nothing is fabricated.
    """
    from data_provider.realtime_types import UnifiedRealtimeQuote

    quote = FIXTURE["quote"]
    _count(NATIVE_CALLS, f"frozen|realtime_quote|{code}")
    typed: Any = UnifiedRealtimeQuote(
        code=str(FIXTURE["stock"]["code"]),
        name=str(quote["name"]),
        source=quote["source"],
        price=float(quote["price"]),
        change_pct=float(quote["change_pct"]),
        volume=int(quote["volume"]),
        currency=str(quote["currency"]),
    )
    # 成交额 projection semantics, kept exactly as the existing stage-zero
    # quote guard reads it (_safe_quote: ctx.quote["turnover"] via
    # getattr(quote, "turnover")). UnifiedRealtimeQuote itself carries 成交额
    # as `amount` and 换手率 as `turnover_rate`; the fixture declares neither
    # under those names, so amount/turnover_rate stay None and the declared
    # value rides the guard's own `turnover` projection attribute.
    typed.turnover = float(quote["turnover"])
    return typed


def install_frozen_suppliers() -> None:
    """Freeze the supplier boundary below the real handler/single-flight/cache layers.

    The seams are the actual outbound points (native Choice builders, adapter
    calls, daily/quote/name fetchers, iFinD research queries); everything
    above them — handlers, compaction, eligibility adoption, #51 single-flight,
    snapshot and dim caches — keeps running real code.
    """
    from src.agent.tools import data_tools

    stock = FIXTURE["stock"]

    manager = data_tools._get_fetcher_manager()

    def _sample() -> dict[str, Any]:
        return FIXTURE["samples"][_CURRENT_SAMPLE["id"]]

    def frozen_fundamental(code_arg: str, budget_seconds: float | None = None, run: Any = None) -> dict[str, Any]:
        _count(NATIVE_CALLS, f"choice|fundamental_full|{code_arg}")
        return json.loads(json.dumps(_sample()["fundamental_bundle"], ensure_ascii=False))

    def frozen_capital_series(code_arg: str, deadline: float | None = None) -> dict[str, Any]:
        _count(NATIVE_CALLS, f"choice|capital_series|{code_arg}")
        return json.loads(json.dumps(_sample()["capital_flow"], ensure_ascii=False))

    def frozen_sector(code_arg: str, include_stock: bool = True) -> dict[str, Any]:
        _count(NATIVE_CALLS, f"choice|capital_sector|{code_arg}")
        return json.loads(json.dumps(FIXTURE["sector_capital"], ensure_ascii=False))

    def frozen_daily(code_arg: str, days: int = 60, **_kw: Any) -> Any:
        import pandas as pd

        _count(NATIVE_CALLS, f"frozen|daily_history|{code_arg}")
        return pd.DataFrame(json.loads(json.dumps(FIXTURE["history"]["rows"]))), FIXTURE["history"]["source_family"]

    def frozen_ifind_research(code_arg: str, question: str, period: str | None = None, deadline: float | None = None) -> Any:
        _count(NATIVE_CALLS, f"ifind|research:{question}|{code_arg}")
        research = _sample()["fundamental_bundle"]["research"]
        key = "shareholder_details" if question == "holders" else "product_segments"
        return json.loads(json.dumps(research.get(key) or {"items": [], "errors": ["frozen-fixture: not_requested"]}))

    def frozen_concepts(code_arg: str, deadline: float) -> dict[str, Any]:
        _count(NATIVE_CALLS, f"ifind|concept_membership|{code_arg}")
        return json.loads(json.dumps(FIXTURE["concept_membership"], ensure_ascii=False))

    manager._build_choice_fundamental_context_uncached = frozen_fundamental
    manager._fundamental_adapter.get_capital_flow = frozen_sector
    manager.get_daily_data = frozen_daily
    manager.get_realtime_quote = _frozen_quote
    manager.get_stock_name = lambda code_arg, allow_realtime=True: stock["name"]
    manager.get_ifind_research_context = frozen_ifind_research
    # history_loader keeps its own DataFetcherManager singleton for the DB-miss
    # fallback; freeze the same seam there, or stage-zero / technical history
    # fallbacks reach a public supplier outside this frozen boundary
    # (baseline qualification correction 2026-10-27).
    from src.services import history_loader as history_loader_module

    history_loader_module._fetcher_singleton = manager
    import data_provider.capital_flow_provider as capital_flow_provider

    capital_flow_provider.get_main_inflow_cumulative = frozen_capital_series
    data_tools._concept_membership = frozen_concepts

    # Frozen search supplier boundary: SearchService's failover loop calls
    # provider.search(...) as its actual outbound point. The real tools,
    # eligibility (key presence), failover order, caching, ranking, recency
    # filtering and persistence keep running; every provider's outbound is
    # pinned to the sample's fixed news/governance evidence (both sides of a
    # conflict are always served together), and uncovered queries get an
    # explicit miss instead of any online fetch.
    from src.search_service import SearchResult, SearchResponse, get_search_service

    def _pinned_search_evidence() -> list[dict[str, Any]]:
        research = (_sample().get("fundamental_bundle") or {}).get("research") or {}
        return research.get("governance_evidence") or []

    def _search_query_covered(query: str) -> bool:
        text = str(query or "")
        terms = (stock["code"], stock["name"], "治理", "分红", "派息", "公告", "新闻")
        return any(term in text for term in terms)

    patched_providers: list[str] = []
    for provider in list(getattr(get_search_service(), "_providers", None) or []):
        patched_providers.append(provider.name)

        def frozen_provider_search(query: str, max_results: int = 5, days: int = 7,
                                   _name: str = provider.name, **_kw: Any) -> Any:
            evidence = _pinned_search_evidence()
            covered = _search_query_covered(query) and bool(evidence)
            _count(NATIVE_CALLS, f"search|{_name}|{'covered' if covered else 'uncovered'}")
            if not covered:
                return SearchResponse(
                    query=str(query), results=[], provider="frozen-search-boundary",
                    success=False,
                    error_message="frozen search boundary: no pinned evidence for "
                                  "this query/sample (explicit miss, no online fetch)",
                )
            return SearchResponse(
                query=str(query), provider="frozen-search-boundary", success=True,
                results=[SearchResult(
                    title=f"[{item.get('source_family')}] {item.get('subject')}",
                    snippet=str(item.get("claim") or ""),
                    url=str(item.get("id") or ""),
                    source=str(item.get("source") or ""),
                    published_date=str(item.get("published_at") or ""),
                ) for item in evidence],
            )

        provider.search = frozen_provider_search
    FROZEN_SEARCH_BOUNDARY.update({
        "providers_patched": patched_providers,
        "evidence_source": "sample.fundamental_bundle.research.governance_evidence "
                           "(inside the fact identity's sample component)",
        "covered_query_rule": "query mentions the stock code/name or 治理/分红/派息/公告/新闻",
        "uncovered": "explicit miss (success=False, nothing fabricated)",
        "note": "real tools/eligibility/failover/ranking/recency filter stay real; the "
                "recency filter may legitimately drop pinned items by wall-clock window",
    })

    # Logical counts at the handler boundary: the original handlers run real
    # code over the frozen suppliers; counts are need-identity, not claims of
    # duplicate supplier round-trips.
    original_stock_info = data_tools._handle_get_stock_info
    original_capital_flow = data_tools._handle_get_capital_flow

    def counted_stock_info(code_arg: str, **kw: Any) -> dict[str, Any]:
        _count(LOGICAL_CALLS, f"get_stock_info|{code_arg}")
        return original_stock_info(code_arg, **kw)

    def counted_capital_flow(code_arg: str) -> dict[str, Any]:
        _count(LOGICAL_CALLS, f"get_capital_flow|{code_arg}")
        return original_capital_flow(code_arg)

    data_tools._handle_get_stock_info = counted_stock_info
    data_tools._handle_get_capital_flow = counted_capital_flow

    # Frozen observation time: the wall-clock as_of would drift per run and
    # destroy fact identity across old/new runs. Two existing seams carry it:
    # (1) history_loader's frozen-target-date ContextVar covers the whole run
    # (set by run_one_sample; read by load_history_df in every tool thread via
    # copy_context), and (2) the analysis tools' external date —
    # _merge_today_realtime_bar compares the df's last bar against
    # date.today(); unpinned, a later real tool run treats the frozen daily
    # history as stale and appends a current-day bar, changing the declared
    # frozen evidence. Only analysis_tools' `date` symbol is replaced by a
    # real date subclass whose today() returns the fixture as_of (a bounded
    # test clock, no freezegun); the merge guards, load_history_df and
    # StockTrendAnalyzer keep running real code. The fixture history already
    # ends at the as_of session, so the real "today already stored" guard
    # keeps the declared bars unchanged.
    from datetime import date

    frozen_date = date.fromisoformat(str(FIXTURE["as_of"])[:10])

    class _FrozenClockDate(date):
        @classmethod
        def today(cls) -> date:
            return frozen_date

    import src.agent.tools.analysis_tools as analysis_tools

    analysis_tools.date = _FrozenClockDate
    # Known second external-wall-clock consumer (root confirmation 2026-10-27):
    # explore_agents._run_explore writes the business dim's analysis date into
    # its system prompt via date.today(); pinned to the same frozen day so the
    # declared as_of and the prompt clock cannot disagree. Elapsed-time timers
    # (time.monotonic) are untouched.
    import src.agent.deep_research.explore_agents as explore_agents

    explore_agents.date = _FrozenClockDate
    FROZEN_CLOCK.clear()
    FROZEN_CLOCK.update({
        "module_seams": ["src.agent.tools.analysis_tools.date",
                         "src.agent.deep_research.explore_agents.date"],
        "today": frozen_date.isoformat(),
        "as_of": FIXTURE["as_of"],
        "note": "bounded date-subclass clocks (no freezegun); real merge guards, "
                "load_history_df and StockTrendAnalyzer keep running",
    })

    import src.agent.deep_research.orchestrator as orchestrator
    from src.deep_research_dims.context import build_shared_context as real_build

    def pinned_shared_context(stock_code: str, stock_name: str, **kw: Any) -> Any:
        # The frozen-target-date token is held by run_one_sample for the whole
        # run, so real_build and every later tool thread already see it here.
        ctx = real_build(stock_code, stock_name, **kw)
        ctx.as_of = FIXTURE["as_of"]
        # Assembly-exit fact guard: the adopted ctx inputs must equal the
        # declared fixture facts, or the run is aborted before any model call
        # (constant model fixtures could otherwise conceal absent facts).
        record = _verify_adopted_facts(ctx)
        record["sample_id"] = _CURRENT_SAMPLE["id"]
        FACT_ADOPTION.clear()
        FACT_ADOPTION.update(record)
        if not record["ok"]:
            raise BaselineFactAdoptionError("; ".join(record["problems"]))
        return ctx

    orchestrator.build_shared_context = pinned_shared_context


def reset_between_samples() -> None:
    """Caches are keyed by stock code, not by frozen sample; isolate samples."""
    from src.agent.tools import data_tools

    manager = getattr(data_tools, "_fetcher_manager_singleton", None)
    if manager is not None and hasattr(manager, "_fundamental_cache"):
        manager._fundamental_cache.clear()
    import src.deep_research_dims.dim_cache as dim_cache

    cache_dir = getattr(dim_cache, "_CACHE_DIR", None)
    if cache_dir and Path(cache_dir).is_dir():
        for path in Path(cache_dir).glob(f"{FIXTURE['stock']['code']}*"):
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# fact identity / rule inputs
# ---------------------------------------------------------------------------

def _sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _ma20_from_rows() -> float:
    closes = [row["close"] for row in FIXTURE["history"]["rows"]]
    return sum(closes[-20:]) / 20.0


def fact_identity(sample_id: str, dims: list[str]) -> dict[str, Any]:
    """Hash EVERY frozen input this case consumes — common and per-sample alike."""
    sample = FIXTURE["samples"][sample_id]
    history_rows = FIXTURE["history"]["rows"]
    components = {
        "as_of": FIXTURE["as_of"],
        "stock": FIXTURE["stock"],
        "dims": dims,
        "quote": FIXTURE["quote"],
        "history": FIXTURE["history"],
        "sector_capital": FIXTURE["sector_capital"],
        "concept_membership": FIXTURE["concept_membership"],
        "sample": sample,
    }
    computed_ma20 = _ma20_from_rows()
    declared_ma20 = (FIXTURE["history"].get("derived") or {}).get("ma20")
    return {
        "sha256": _sha256(components),
        "component_sha256": {name: _sha256(value) for name, value in components.items()},
        "covers": "all consumed frozen inputs (quote/history/sector_capital/concept_membership/"
                  "as_of/stock/dims + the whole per-sample section, including the "
                  "governance_evidence served at the frozen search boundary); mutating any "
                  "one of them changes this identity",
        "as_of": FIXTURE["as_of"],
        "source_families": sorted({FIXTURE["history"]["source_family"], FIXTURE["quote"]["source"],
                                   "frozen-choice", "ifind"}),
        "history": {"rows": len(history_rows), "sha256": _sha256(history_rows),
                    "first": history_rows[0]["date"], "last": history_rows[-1]["date"]},
        "declared_vs_derived": {
            "fixture_declared": "history.derived only authored the frozen model_responses at "
                                "fixture-generation time; the run itself does not consume it",
            "derived_by_real_code": "rule layer and dims recompute indicators from the frozen "
                                    "history rows; see rule_inputs and public_result",
            "ma20_crosscheck": {"declared": declared_ma20, "computed_from_rows": round(computed_ma20, 6),
                                "match": declared_ma20 is not None
                                and abs(declared_ma20 - computed_ma20) < 1e-6},
        },
        "fact_note": sample["fact_note"],
    }


def rule_inputs(sample_id: str) -> dict[str, Any]:
    """The values the rule layer actually consumes (values, units, periods, eligibility)."""
    sample = FIXTURE["samples"][sample_id]
    bundle = sample["fundamental_bundle"]
    growth_meta = (bundle.get("growth") or {}).get("field_meta") or {}
    valuation_meta = (bundle.get("valuation") or {}).get("field_meta") or {}
    fields = {name: {k: record.get(k) for k in ("value", "unit", "caliber", "period", "rule_eligible")}
              for name, record in {**growth_meta, **valuation_meta}.items()}
    flow = sample["capital_flow"]
    research = bundle.get("research") or {}
    return {
        "quote": {k: FIXTURE["quote"][k] for k in ("price", "change_pct", "volume", "turnover")},
        "history": {"rows": len(FIXTURE["history"]["rows"]),
                    "last_close": FIXTURE["history"]["rows"][-1]["close"],
                    "ma20_computed_from_rows": round(_ma20_from_rows(), 6),
                    "source_family": FIXTURE["history"]["source_family"],
                    "fixture_declared_derived": FIXTURE["history"].get("derived")},
        "fundamental_fields": fields,
        "balance_sheet": ((bundle.get("growth") or {}).get("data") or {}).get("balance_sheet"),
        "capital_flow": {"main_net_inflow": flow.get("main_net_inflow"), "inflow_5d": flow.get("inflow_5d"),
                         "inflow_10d": flow.get("inflow_10d"), "unit": f'{flow.get("source_unit")} {flow.get("currency")}'.strip(),
                         "expected_date": flow.get("expected_date"),
                         "windows_complete": {k: v.get("complete") for k, v in (flow.get("coverage") or {}).items()},
                         "main_inflow_rule_eligible": ((flow.get("field_meta") or {}).get("main_inflow") or {}).get("rule_eligible")},
        "institution": {name: {"value": record.get("value"), "rule_eligible": record.get("rule_eligible")}
                        for name, record in ((bundle.get("institution") or {}).get("field_meta") or {}).items()},
        "research": {"as_of": research.get("as_of"), "company": sorted((research.get("company") or {}).keys()),
                     "holders_period": (research.get("holders") or {}).get("period"),
                     "governance_evidence_ids": [e.get("id") for e in research.get("governance_evidence") or []],
                     "product_segments_present": bool(research.get("product_segments")),
                     "shareholder_details_present": bool(research.get("shareholder_details"))},
    }


# ---------------------------------------------------------------------------
# effective model/request controls
# ---------------------------------------------------------------------------

def derive_effective_controls(config: Any) -> dict[str, Any]:
    """Effective generation controls via the adapter's own param helpers.

    Uses the same helpers the adapter applies per call, so nothing here is a
    second opinion about routing; no prompts, no keys, no private reasoning.
    In fixture mode the per-run wire observations are the authoritative view.
    """
    from src.agent.llm_adapter import resolve_fallback_litellm_wire_models
    from src.config import get_effective_agent_primary_model
    from src.llm.generation_params import apply_litellm_generation_params, apply_minimax_reasoning_params

    model = get_effective_agent_primary_model(config)
    model_list = getattr(config, "llm_model_list", []) or []
    wire_models = resolve_fallback_litellm_wire_models(model, model_list)
    effort = getattr(config, "minimax_reasoning_effort", None)
    kwargs: dict[str, Any] = apply_minimax_reasoning_params({}, wire_models, effort)
    kwargs = apply_litellm_generation_params(
        kwargs, model, float(getattr(config, "llm_temperature", 0.2)), model_list=model_list)
    return {
        "model": model,
        "wire_models": wire_models,
        "derived_request_controls": kwargs,
        "minimax_reasoning_effort_config": effort,
        "minimax_effort_applies_only_to": "minimax-m3.1-flash wire models; not sent otherwise",
        "derivation": "adapter's own generation-param helpers; real wire not introspected "
                      "(fixture runs record wire-observed controls per sample)",
    }


def real_provider_preflight(config: Any) -> tuple[bool, str]:
    """Presence check only — a configured channel/key, never credential validation."""
    from src.config import get_api_keys_for_model, get_configured_llm_models, get_effective_agent_primary_model

    model = get_effective_agent_primary_model(config)
    if not model:
        return False, "no effective primary model is configured in the allowed environment"
    channels = set(get_configured_llm_models(getattr(config, "llm_model_list", []) or []))
    if model not in channels and not get_api_keys_for_model(model, config):
        return False, (f"no channel and no API key is configured for effective primary model "
                       f"{model!r}; supply configuration via the authorized process env or "
                       f"ENV_FILE (this entry never searches for credentials)")
    return True, (f"model {model!r} resolves to a configured channel/key (presence check only, "
                  "not credential validation; actual call failures are recorded and fail the run)")


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------

def run_one_sample(sample_id: str, dims: list[str], timeout: float, force_refresh: bool, write_cache: bool) -> dict[str, Any]:
    from datetime import date

    from src.agent.llm_adapter import LLMToolAdapter
    from src.config import get_config
    from src.core.research_run import ResearchRun
    from src.llm import concurrency
    from src.llm.attempt import attempts_detail, summarize_attempts
    from src.services import history_loader

    _CURRENT_SAMPLE["id"] = sample_id
    concurrency._slots = None  # rebuild admission slots from effective config

    config = get_config()
    adapter = LLMToolAdapter(config)
    run = ResearchRun(timeout, run_id=uuid4().hex)
    from src.agent.deep_research.orchestrator import run_dual_track

    # Frozen observation point for the WHOLE run: history_loader's frozen
    # target date must reach not only stage-zero assembly but every later real
    # tool thread (analyze_trend → load_history_df), or the frozen daily
    # history is re-judged against today's real session.
    frozen_token = history_loader.set_frozen_target_date(
        date.fromisoformat(str(FIXTURE["as_of"])[:10]))
    provider_state = PROVIDER_STATE
    started = time.monotonic()
    try:
        with run.bind():
            result = run_dual_track(
                stock_code=FIXTURE["stock"]["code"], stock_name=FIXTURE["stock"]["name"],
                llm_adapter=adapter, explore_max_steps=8, dims_filter=set(dims),
                force_refresh=force_refresh, write_cache=write_cache, report_id=None,
            )
    finally:
        history_loader.reset_frozen_target_date(frozen_token)
    duration = time.monotonic() - started
    usage = summarize_attempts(run.attempts, origin_run_id=run.run_id)
    return {
        "sample_id": sample_id,
        "fact_identity": fact_identity(sample_id, dims),
        "rule_inputs": rule_inputs(sample_id),
        "run_id": run.run_id,
        "status": result.status,
        "degraded_dims": result.degraded_dims,
        "dims_status": {name: getattr(dim, "status", None) for name, dim in result.dims.items()},
        "cache_condition": ("hot(force_refresh=off,write_cache=on)" if not force_refresh and write_cache
                            else "cold(force_refresh=on,write_cache=off)"),
        "duration_seconds": round(duration, 3),
        "model_transport": {
            "loopback_requests": provider_state.get("count"),
            "loopback_peak_active": provider_state.get("peak"),
            "ledger_outbound": usage.get("outbound"),
            "ledger_transport_observed": usage.get("transport_observed"),
        },
        "effective_request_controls": {
            "wire_observed_loopback": list(provider_state.get("requests") or []) if provider_state else None,
            "note": "wire-observed safe controls (key names + temperature/effort/thinking values); "
                    "prompts and secret-named fields are never recorded",
        } if provider_state else {
            "wire_observed_loopback": None,
            "note": "real mode: see config.effective_controls (derived, wire not introspected)",
        },
        "supplier_queries": {
            "logical_handler_calls": dict(LOGICAL_CALLS),
            "frozen_boundary_roundtrips": dict(NATIVE_CALLS),
            "note": "logical handler counts are need identities, not duplicate-supplier claims; "
                    "the #51 single-flight and caches above the frozen boundary are real, and "
                    "native choice/ifind transport never runs in this entrypoint",
        },
        "fact_adoption_guard": _fact_adoption_guard(),
        "model_usage": usage,
        "attempts": attempts_detail(run.attempts),
        "public_result": result.dims_payload,
        "report_markdown_chars": len(result.markdown or ""),
    }


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

def _fact_adoption_guard() -> dict[str, Any]:
    """A run only counts as fixture evidence if the frozen facts were consumed.

    Constant model-fixture responses can conceal absent runtime facts; two
    gates must hold: the frozen daily-history counter at the real supplier
    seam, and the assembly-exit verification that the ctx the model actually
    consumes carries the declared facts (baseline qualification correction
    2026-10-27). A counter hit alone does not prove adoption.
    """
    code = str(FIXTURE["stock"]["code"])
    hits = NATIVE_CALLS.get(f"frozen|daily_history|{code}", 0)
    adopted = dict(FACT_ADOPTION)
    counter_ok = hits >= 1
    facts_ok = bool(adopted.get("ok"))
    return {
        "ok": counter_ok and facts_ok,
        "frozen_daily_history_hits": hits,
        "adopted_facts": adopted or None,
        "requirement": "at least one frozen daily-history round-trip per run AND "
                       "assembly-exit verification of the adopted facts",
        "note": "a zero count or a failed fact verification means the run did not "
                "consume the declared fixture facts and must not be quoted as "
                "old/new evidence",
    }


class BaselineFactAdoptionError(RuntimeError):
    """Adopted context facts do not match the declared frozen fixture facts."""


def _declared_fundamental(bundle: dict[str, Any]) -> dict[str, Any]:
    """Values the real stage-zero fundamental assembly derives from a bundle."""
    val = (bundle.get("valuation") or {}).get("data") or {}
    fin = (bundle.get("growth") or {}).get("data") or {}
    return {
        "pe_ttm": next((val[k] for k in ("pe_ratio", "pe_ttm") if val.get(k) is not None), None),
        "pb": val.get("pb_ratio"),
        "market_cap": next((val[k] for k in ("total_mv", "market_cap", "total_market_cap")
                            if val.get(k) is not None), None),
        "revenue": fin.get("revenue"), "net_profit": fin.get("net_profit"), "roe": fin.get("roe"),
        "revenue_growth": next((fin[k] for k in ("revenue_yoy", "revenue_growth")
                                if fin.get(k) is not None), None),
        "net_profit_yoy": fin.get("net_profit_yoy"), "gross_margin": fin.get("gross_margin"),
    }


def _verify_adopted_facts(ctx: Any) -> dict[str, Any]:
    """Verify the assembled SharedContext against the declared fixture facts.

    Runs at the real assembly exit, before any model call: the checks read the
    actual adopted ctx inputs (not the fixture identity), so a lying, missing,
    stale or swapped daily supplier is rejected with an explicit reason. The
    canonical history identity is computed from what ctx carries after real
    loading/trimming, not from re-hashing the fixture alone.
    """
    from datetime import date

    problems: list[str] = []
    checks: dict[str, Any] = {}
    code = str(FIXTURE["stock"]["code"])

    # Point-in-time the real loader enforces, by the product's own session rule.
    from src.services import history_loader

    required = history_loader.cache_session_date(
        code, target_date=date.fromisoformat(str(FIXTURE["as_of"])[:10])).isoformat()
    declared_rows = FIXTURE["history"]["rows"]
    rows = list(getattr(ctx, "history", None) or [])

    checks["required_session_date"] = required
    checks["history_rows_actual"] = len(rows)
    if len(rows) != len(declared_rows):
        problems.append(f"history rows {len(rows)} != declared {len(declared_rows)}")
    actual_last = str(rows[-1].get("date"))[:10] if rows else ""
    checks["history_last_date_actual"] = actual_last
    if actual_last != str(declared_rows[-1]["date"])[:10]:
        problems.append(f"history last bar {actual_last!r} != declared "
                        f"{str(declared_rows[-1]['date'])[:10]!r}")
    if actual_last != required:
        problems.append(f"adopted history ends {actual_last!r}, required session "
                        f"{required!r} (point-in-time not adopted)")

    actual_source = str(getattr(ctx, "history_source", "") or "")
    checks["history_source_actual"] = actual_source
    if actual_source != str(FIXTURE["history"]["source_family"]):
        problems.append(f"history source {actual_source!r} != frozen boundary "
                        f"{FIXTURE['history']['source_family']!r}")

    def _canon(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"date": str(r.get("date"))[:10], "open": float(r.get("open")),
                 "high": float(r.get("high")), "low": float(r.get("low")),
                 "close": float(r.get("close")), "volume": float(r.get("volume"))}
                for r in values]

    actual_canon = _canon(rows)
    declared_canon = _canon(declared_rows)
    checks["history_identity_sha256"] = hashlib.sha256(
        json.dumps(actual_canon, sort_keys=True).encode()).hexdigest()
    checks["declared_history_identity_sha256"] = hashlib.sha256(
        json.dumps(declared_canon, sort_keys=True).encode()).hexdigest()
    if actual_canon != declared_canon:
        where = next((i for i, (a, b) in enumerate(zip(actual_canon, declared_canon)) if a != b),
                     min(len(actual_canon), len(declared_canon)))
        problems.append(f"adopted daily rows differ from declared facts "
                        f"(first divergence at row {where})")
    checks["history_last_row_actual"] = dict(rows[-1]) if rows else None

    quote = dict(getattr(ctx, "quote", None) or {})
    fq = FIXTURE["quote"]
    quote_actual = {k: quote.get(k) for k in ("price", "change_pct", "volume", "turnover", "name")}
    checks["quote_actual"] = quote_actual
    for key in ("price", "change_pct", "volume", "turnover"):
        actual = quote.get(key)
        if actual is None or float(actual) != float(fq[key]):
            problems.append(f"quote.{key} {actual!r} != declared {fq[key]!r}")
    if str(quote.get("name") or "") != str(fq["name"]):
        problems.append(f"quote.name {quote.get('name')!r} != declared {fq['name']!r}")

    fundamental = dict(getattr(ctx, "fundamental", None) or {})
    keys = ("pe_ttm", "pb", "market_cap", "revenue", "roe", "revenue_growth")
    fundamental_actual = {k: fundamental.get(k) for k in keys}
    checks["fundamental_actual"] = fundamental_actual
    declared = _declared_fundamental(
        (FIXTURE["samples"][_CURRENT_SAMPLE["id"]].get("fundamental_bundle") or {}))
    for key, actual in fundamental_actual.items():
        expected = declared.get(key)
        if actual is None:
            if expected is not None:
                problems.append(f"fundamental.{key} absent but the fixture declares it")
        elif expected is None or float(actual) != float(expected):
            problems.append(f"fundamental.{key} {actual!r} != declared {expected!r}")

    concepts = dict(getattr(ctx, "concept_membership", None) or {})
    if concepts != FIXTURE["concept_membership"]:
        problems.append("concept membership differs from declared frozen membership")

    return {"ok": not problems, "problems": problems, "checks": checks, "stock_code": code}


def install_environment(base_url: str | None, tmp: Path, cache_dir: Path) -> dict[str, Any]:
    from src.config import Config, get_config
    from src.storage import DatabaseManager

    overrides: dict[str, Any] = {}
    if base_url is not None:
        config = Config(
            data_source_profile="choice",
            deep_research_cross_validate=False,
            enable_ifind_mcp=False,
            deep_research_llm_concurrency=1,
            llm_temperature=0.3,
            llm_model_list=[{"model_name": "openai/fixture",
                             "litellm_params": {"model": "openai/fixture", "api_base": base_url,
                                                "api_key": "fixture-key"}}],
        )
        config.litellm_model = "openai/fixture"
        config.agent_litellm_model = "openai/fixture"
        Config._instance = config
        os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
        overrides = {"data_source_profile": "choice", "deep_research_cross_validate": False,
                     "enable_ifind_mcp": False, "deep_research_llm_concurrency": 1,
                     "llm_temperature": 0.3, "litellm_model": "openai/fixture"}
    config = get_config()

    # Own sandbox: DB singleton isolated first, then constructed; dim/snapshot
    # caches redirected so nothing touches shared or production locations.
    # DATABASE_PATH keeps the usage HMAC secret beside the sandbox DB instead
    # of creating data/.llm_usage_hmac_secret inside the (snapshotted) tree.
    os.environ["DATABASE_PATH"] = str(tmp)
    DatabaseManager.reset_instance()
    DatabaseManager(f"sqlite:///{tmp / 'baseline.sqlite'}")
    import src.deep_research_dims.dim_cache as dim_cache

    dim_cache._CACHE_DIR = str(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    install_frozen_suppliers()

    return {
        "provider_mode": "fixture(loopback)" if base_url is not None else "real(existing routing)",
        "model": None,
        "temperature_config": getattr(config, "llm_temperature", None),
        "concurrency": getattr(config, "deep_research_llm_concurrency", None),
        "effective_controls": derive_effective_controls(config),
        "frozen_search_boundary": dict(FROZEN_SEARCH_BOUNDARY),
        "frozen_observation_clock": dict(FROZEN_CLOCK),
        "quote_contract": "UnifiedRealtimeQuote at the frozen supplier seam; fixture-declared "
                          "price/change_pct/volume/turnover/name/source/currency only, unprovided "
                          "optionals None (成交额 rides the existing guard's `turnover` projection; "
                          "amount/turnover_rate stay None)",
        "fixture_mode_overrides": overrides,
        "cache_dir": str(cache_dir),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"runs must be an integer, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"runs must be >= 1, got {value}")
    return value


def _positive_finite_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"timeout must be a number, got {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"timeout must be a finite positive number of seconds, got {text!r}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", default=",".join(SAMPLE_IDS), help=f"comma-separated from {SAMPLE_IDS}")
    parser.add_argument("--runs", type=_positive_int, default=3, help="repeat each sample N times for median evidence")
    parser.add_argument("--label", default=None, choices=("old", "new"),
                        help="caller-declared pipeline label; verify against code_identity in the output")
    parser.add_argument("--expect-commit", default=None,
                        help="abort unless the base commit equals this (git HEAD or snapshot manifest)")
    parser.add_argument("--expect-content", default=None,
                        help="abort unless the source content identity sha256 equals this")
    parser.add_argument("--provider", choices=("fixture", "real"), default="fixture")
    parser.add_argument("--fixture", default=str(ROOT / FIXTURE_REL))
    parser.add_argument("--dims", default=None, help="comma-separated dims (default: fixture dims)")
    parser.add_argument("--hot", action="store_true", help="hot cache condition (force_refresh off, write_cache on)")
    parser.add_argument("--out", default=str(ROOT / ".claude/reviews/stage1-baseline"))
    parser.add_argument("--cache-dir", default=None,
                        help="persistent dim/snapshot cache dir (default: per-invocation temp)")
    parser.add_argument("--timeout", type=_positive_finite_float, default=None,
                        help="per-run research budget seconds (finite positive)")
    parser.add_argument("--make-snapshot", default=None, metavar="DIR",
                        help="freeze source + content manifest into DIR and exit (no runs)")
    parser.add_argument("--include-untracked", default=None, metavar="PATHS",
                        help="comma-separated extra untracked paths explicitly approved "
                             "into the source set (snapshot/identity)")
    args = parser.parse_args()

    extra_untracked = [p.strip() for p in (args.include_untracked or "").split(",") if p.strip()]

    if args.make_snapshot:
        dest = Path(args.make_snapshot)
        dest.mkdir(parents=True, exist_ok=True)
        return make_snapshot(dest, extra_untracked)

    if not args.label:
        parser.error("--label is required for measurement runs")

    # ---- validation: refuse bad arguments before any endpoint or research ----
    fixture_path = Path(args.fixture) if Path(args.fixture).is_absolute() else ROOT / args.fixture
    if not fixture_path.is_file():
        print(f"[baseline] fixture not found: {fixture_path}", file=sys.stderr)
        return 2
    fixture_bytes = fixture_path.read_bytes()
    global FIXTURE
    FIXTURE = json.loads(fixture_bytes)
    if FIXTURE.get("schema") != "model_workflow_baseline_fixture_v2":
        print(f"[baseline] unexpected fixture schema: {FIXTURE.get('schema')}", file=sys.stderr)
        return 2
    samples = [s.strip() for s in args.samples.split(",") if s.strip()]
    unknown = [s for s in samples if s not in FIXTURE["samples"]]
    if not samples or unknown:
        print(f"[baseline] samples must be a non-empty subset of {sorted(FIXTURE['samples'])}; "
              f"got {args.samples!r}", file=sys.stderr)
        return 2
    dims = list(FIXTURE["dims"]) if args.dims is None \
        else [d.strip() for d in args.dims.split(",") if d.strip()]
    unknown_dims = [d for d in dims if d not in FIXTURE["dims"]]
    if not dims or unknown_dims:
        print(f"[baseline] dims must be a non-empty subset of {FIXTURE['dims']}; got {args.dims!r}",
              file=sys.stderr)
        return 2
    force_refresh = not args.hot
    write_cache = bool(args.hot)

    # ---- provenance gates: content identity + module origin ----
    try:
        identity = code_identity(args.expect_commit, args.expect_content, extra_untracked)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if identity["expect_commit_match"] is False:
        print(f"[baseline] base commit {identity['commit']} != --expect-commit {args.expect_commit}; "
              "refusing to record this as the frozen source", file=sys.stderr)
        return 2
    if identity["expect_content_match"] is False:
        print(f"[baseline] content identity {identity['content_identity_sha256']} != "
              f"--expect-content {args.expect_content}; source bytes differ from the pinned tree",
              file=sys.stderr)
        return 2
    if not identity["import_tree_matches_root"]:
        print(f"[baseline] imports resolved outside this tree: {identity['module_paths']}", file=sys.stderr)
        return 2

    base_url: str | None = None
    if args.provider == "fixture":
        base_url, provider_state = start_loopback_provider()
        global PROVIDER_STATE
        PROVIDER_STATE = provider_state

    out_dir = Path(args.out) if Path(args.out).is_absolute() else ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="dsa-baseline-") as tmp_name:
        tmp = Path(tmp_name)
        cache_dir = Path(args.cache_dir) if args.cache_dir else tmp / "dim_cache"
        config_block = install_environment(base_url, tmp, cache_dir)
        config_block["dims"] = dims
        config_block["runs_per_sample"] = args.runs
        from src.config import get_config

        config_block["model"] = config_block["effective_controls"]["model"]
        timeout = args.timeout if args.timeout is not None \
            else float(get_config().deep_research_timeout_seconds)

        if args.provider == "real":
            ok, message = real_provider_preflight(get_config())
            if not ok:
                print(f"[baseline] real provider refused: {message}", file=sys.stderr)
                return 2
            config_block["real_preflight"] = message

        for sample_id in samples:
            reset_between_samples()
            for index in range(args.runs):
                LOGICAL_CALLS.clear()
                NATIVE_CALLS.clear()
                FACT_ADOPTION.clear()
                if PROVIDER_STATE:
                    PROVIDER_STATE["count"] = 0
                    PROVIDER_STATE["peak"] = 0
                    PROVIDER_STATE["requests"] = []
                record = run_one_sample(sample_id, dims, timeout, force_refresh, write_cache)
                record["run_index"] = index + 1
                records.append(record)
                guard = record.get("fact_adoption_guard") or {}
                guard_note = "" if guard.get("ok") else \
                    "  FACT-GUARD FAILED: frozen daily history was never consumed; run is not valid evidence"
                print(f"[baseline] label={args.label} {sample_id} run {index + 1}: status={record['status']} "
                      f"degraded={record['degraded_dims']} outbound={record['model_transport']['ledger_outbound']} "
                      f"wait={record['model_usage']['model_wait_seconds']}s "
                      f"exec={record['model_usage']['model_exec_seconds']}s{guard_note}", flush=True)

    def _series(sample_id: str, key_path: tuple[str, ...]) -> list[float]:
        values = []
        for record in records:
            if record["sample_id"] != sample_id:
                continue
            node: Any = record
            for key in key_path:
                node = (node or {}).get(key)
            if isinstance(node, (int, float)):
                values.append(float(node))
        return values

    medians = {
        sample_id: {
            "duration_seconds": median(_series(sample_id, ("duration_seconds",))) if _series(sample_id, ("duration_seconds",)) else None,
            "model_wait_seconds": median(_series(sample_id, ("model_usage", "model_wait_seconds"))) or 0.0,
            "model_exec_seconds": median(_series(sample_id, ("model_usage", "model_exec_seconds"))) or 0.0,
            "model_outbound": median(_series(sample_id, ("model_usage", "outbound"))) or 0.0,
        }
        for sample_id in samples
    }

    exit_code = 0
    real_outcome: dict[str, Any] | None = None
    failed_guard = [f"{r['sample_id']}#run{r['run_index']}" for r in records
                    if not (r.get("fact_adoption_guard") or {}).get("ok")]
    if failed_guard:
        exit_code = 4
        print(f"[baseline] fact-adoption guard failed for {len(failed_guard)} run(s): "
              f"{failed_guard}; these runs must not be quoted as old/new evidence (exit 4)",
              file=sys.stderr)
    if args.provider == "real":
        ok_attempts = sum((r["model_usage"].get("by_status") or {}).get("ok", 0) for r in records)
        transport_attempts = sum(r["model_usage"].get("transport_observed") or 0 for r in records)
        real_outcome = {
            "ok_attempts": ok_attempts,
            "transport_observed_attempts": transport_attempts,
            "outcome": "transport_observed" if ok_attempts > 0 else "failed",
        }
        if ok_attempts == 0:
            print(f"[baseline] real provider run produced no successful model transport "
                  f"(ok={ok_attempts}); recorded as failed, exit 3", file=sys.stderr)
            exit_code = 3

    document = {
        "schema": SCHEMA,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "label": args.label,
        "label_note": "label is caller-declared; the code that ran is proven by code_identity "
                      "(per-file content hashes + aggregate content identity + module paths)",
        "code_identity": identity,
        "fixture": {"path": str(fixture_path), "sha256": hashlib.sha256(fixture_bytes).hexdigest(),
                    "fixture_notice": FIXTURE.get("fixture_notice")},
        "config": config_block,
        "real_mode_outcome": real_outcome,
        "fact_adoption_guard_failed_runs": failed_guard,
        "evidence_class": "frozen-input (fixture suppliers; supplier latency NOT real cold/hot evidence) — "
                          "distinct from any future online end-to-end evidence",
        "samples": records,
        "medians": medians,
        "unverified": ["quality acceptance (needs human review of public_result)",
                       "real-model performance (this dry run only proves the metering chain)",
                       "old-source freeze (coordinator snapshots the tree only after it is "
                       "stable and reviewed; this live tree is not frozen)"],
    }
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"baseline-{args.label}-{stamp}.json"
    out_path.write_text(json.dumps(document, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[baseline] written {out_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
