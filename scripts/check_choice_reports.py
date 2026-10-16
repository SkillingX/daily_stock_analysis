"""Generate, reread and download a report from an explicitly chosen local API.
Run with --base-url, --code, --kind and --output-dir (keep evidence out of git).
The report endpoints used here do not send notifications; no credentials are read.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, ProxyHandler, build_opener


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--code", required=True)
    parser.add_argument("--kind", choices=("fundamentals", "financial-analysis", "sector-analysis", "deep-research"), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--dims", nargs="+", help="Deep-research dimensions; omit for the full report")
    args = parser.parse_args()
    target = urlsplit(args.base_url)
    if target.scheme != "http" or target.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("This acceptance command requires an explicit local HTTP API")
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    if args.dims and args.kind != "deep-research":
        parser.error("--dims is only supported for deep-research")
    http = build_opener(ProxyHandler({}))
    base = f"{args.base_url.rstrip('/')}/api/v1/{args.kind}"
    started = time.monotonic()
    events = []
    if args.kind == "deep-research":
        request = Request(f"{base}/generate/stream", data=json.dumps({"stock_code": args.code, "force_refresh": True,
                          "dims": args.dims}).encode(), headers={"Content-Type": "application/json"}, method="POST")
        generated = {}
        with http.open(request, timeout=args.timeout) as response:
            for line in response:
                if not line.startswith(b"data:"):
                    continue
                event = json.loads(line[5:])
                observation = {key: event[key] for key in ("type", "run_id", "dim", "tool", "success", "status", "queue_seconds", "model_seconds") if key in event}
                observation["elapsed_seconds"] = round(time.monotonic() - started, 3)
                events.append(observation)
                if event.get("type") in {"accepted", "dim_done", "partial_ready", "done"}:
                    print(json.dumps(observation, ensure_ascii=False), flush=True)
                if event.get("type") == "error":
                    raise RuntimeError(event.get("message") or "Deep research failed")
                if event.get("type") == "done":
                    generated = event
    else:
        request = Request(f"{base}/generate?{urlencode({'stock_code': args.code, 'force_refresh': 'true'})}", data=b"", method="POST")
        with http.open(request, timeout=args.timeout) as response:
            generated = json.load(response)
    report_id = generated.get("report_id")
    if not report_id:
        raise RuntimeError("Report was not persisted")
    with http.open(f"{base}/reports/{report_id}", timeout=args.timeout) as response:
        reread = json.load(response)
    with http.open(f"{base}/reports/{report_id}/markdown?download=1", timeout=args.timeout) as response:
        downloaded = response.read().decode("utf-8")
    markdown = generated.get("markdown", reread["data"]["markdown"])
    if markdown != downloaded or markdown != reread["data"]["markdown"]:
        raise RuntimeError("Generation, reread and download disagree")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.output_dir / f"{args.kind}-{args.code}"
    prefix.with_suffix(".events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
    prefix.with_suffix(".json").write_text(json.dumps({"generated": generated, "reread": reread}, ensure_ascii=False, indent=2))
    prefix.with_suffix(".md").write_text(markdown)
    print(json.dumps({"kind": args.kind, "report_id": report_id, "status": generated.get("status"), "reread_download_match": True, "elapsed_seconds": round(time.monotonic() - started, 3)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
