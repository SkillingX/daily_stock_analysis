"""Generate and reopen a fresh fundamentals report via the running API.
Run: python scripts/smoke_fundamentals.py --base-url http://localhost:8000 --stock-code 300260
Requires a running local API and configured providers; consumes quota and saves one report.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from time import monotonic

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.v1.endpoints.fundamentals import FundamentalsGenerateResponse  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--stock-code', required=True)
    parser.add_argument('--stock-name')
    parser.add_argument('--output', type=Path, help='Optional private JSON evidence file')
    parser.add_argument('--timeout', type=float, default=330, help='HTTP wait in seconds (default: 330)')
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error('--timeout must be positive')
    started = monotonic()
    params = {'stock_code': args.stock_code, 'force_refresh': 'true'}
    if args.stock_name:
        params['stock_name'] = args.stock_name
    with httpx.Client(base_url=args.base_url.rstrip('/'), timeout=args.timeout, trust_env=False) as client:
        response = client.post('/api/v1/fundamentals/generate', params=params)
        response.raise_for_status()
        report = FundamentalsGenerateResponse.model_validate_json(response.content)
        if not report.report_id or report.status == 'failed':
            raise RuntimeError('Report was not saved')
        reopened = client.get('/api/v1/fundamentals/reports/' + report.report_id)
        reopened.raise_for_status()
        if reopened.json()['data']['markdown'] != report.markdown:
            raise RuntimeError('Reopened markdown differs from generated report')
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report.model_dump_json(indent=2))
        args.output.chmod(0o600)
    summary = {'report_id': report.report_id, 'status': report.status,
               'elapsed_seconds': round(monotonic() - started, 3), 'reopened': True,
               'dimensions': {key: value.get('status') for key, value in report.dims.items() if isinstance(value, dict)}}
    print(json.dumps(summary, ensure_ascii=False))
    # A readable partial report is useful evidence, but is not full acceptance.
    return 0 if report.status == 'success' else 2


if __name__ == '__main__':
    raise SystemExit(main())
