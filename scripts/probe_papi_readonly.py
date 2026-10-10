"""Human-operated PAPI diagnostics. GET only; never an ARM/readiness receipt.

Credentials come only from operator-supplied environment variables. This tool
does not load .env, emit identifiers/keys, or validate order acceptance.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

SYMBOL = 'ETHUSDC'
PAPI = 'https://papi.binance.com'
READONLY_PATHS = frozenset({
    '/papi/v1/account', '/papi/v1/balance', '/papi/v1/um/accountConfig',
    '/papi/v1/um/positionSide/dual', '/papi/v1/um/symbolConfig',
    '/papi/v1/um/commissionRate', '/papi/v1/um/leverageBracket',
    '/papi/v1/um/positionRisk', '/papi/v1/cm/positionRisk',
    '/papi/v1/um/openOrders', '/papi/v1/um/algo/openAlgoOrders',
    '/papi/v1/cm/openOrders', '/papi/v1/cm/conditional/openOrders',
})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('PROBE_REDIRECT_REFUSED')


def _read_json(request: Request) -> Any:
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            return json.loads(response.read(1_048_576).decode('utf-8'))
    except HTTPError as error:
        # Never propagate signed URLs, header values, or arbitrary response text.
        raise RuntimeError(f'PROBE_HTTP_{error.code}') from None
    except Exception:
        raise RuntimeError('PROBE_RESPONSE_UNAVAILABLE') from None


def signed_get(path: str, params: dict, key: str, secret: str, offset_ms: int) -> Any:
    if path not in READONLY_PATHS:
        raise ValueError('PROBE_READONLY_PATH_REFUSED')
    values = dict(params)
    if any(k in values for k in ('signature', 'timestamp', 'recvWindow')):
        raise ValueError('PROBE_SIGNING_OVERRIDE_REFUSED')
    values.update(timestamp=int(time.time() * 1000) + offset_ms, recvWindow=5000)
    query = urlencode(sorted(values.items()))
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return _read_json(Request(f'{PAPI}{path}?{query}&signature={signature}',
                             headers={'X-MBX-APIKEY': key}, method='GET'))


def synchronize_clock() -> tuple[int, int]:
    before = time.time_ns() // 1_000_000
    data = _read_json(Request('https://fapi.binance.com/fapi/v1/time', method='GET'))
    after = time.time_ns() // 1_000_000
    if not isinstance(data, dict) or type(data.get('serverTime')) is not int or after < before:
        raise RuntimeError('PROBE_CLOCK_UNVERIFIED')
    return data['serverTime'] - (before + after) // 2, after - before


def number(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError('NUMBER_UNAVAILABLE') from None
    if not result.is_finite():
        raise ValueError('NUMBER_NONFINITE')
    return result


def collect_probe(get: Callable[[str, dict], Any], *, clock_offset_ms: int,
                  clock_rtt_ms: int, git_sha: str) -> dict:
    started = datetime.now(timezone.utc)
    checks: list[dict] = []
    report: dict[str, Any] = {
        'schema_version': 1, 'symbol': SYMBOL, 'git_sha': git_sha,
        'observed_at': started.isoformat(), 'scope': 'SIGNED_GET_DIAGNOSTICS_ONLY',
        'clock_offset_ms': clock_offset_ms, 'clock_rtt_ms': clock_rtt_ms,
        'protection_acceptance': 'NOT_RUN', 'protection_latency': 'NOT_RUN',
        'ready_for_pilot': False, 'checks': checks,
    }
    def check(name: str, operation: Callable[[], bool]):
        try:
            passed = operation() is True
            checks.append({'name': name, 'status': 'PASS' if passed else 'FAIL'})
        except Exception:
            checks.append({'name': name, 'status': 'UNKNOWN'})
    check('HOST_CLOCK', lambda: abs(clock_offset_ms) + clock_rtt_ms / 2 <= 1000
          and 0 <= clock_rtt_ms <= 1000)
    def account():
        data = get('/papi/v1/account', {})
        return data.get('accountStatus') == 'NORMAL' and all(
            0 < number(data[k]) <= 250 for k in ('accountEquity', 'actualEquity'))
    check('ACCOUNT_EQUITY_CAP', account)
    def config():
        data = get('/papi/v1/um/accountConfig', {})
        return data.get('canTrade') is True and data.get('multiAssetsMargin') is False
    check('ACCOUNT_CONFIG', config)
    check('ONE_WAY', lambda: get('/papi/v1/um/positionSide/dual', {}).get('dualSidePosition') is False)
    def leverage():
        data = get('/papi/v1/um/symbolConfig', {'symbol': SYMBOL})
        rows = [row for row in data if row.get('symbol') == SYMBOL]
        return len(rows) == 1 and 1 <= number(rows[0]['leverage']) <= 10
    check('SYMBOL_LEVERAGE', leverage)
    def commission():
        data = get('/papi/v1/um/commissionRate', {'symbol': SYMBOL})
        rates = {k: number(data[k]) for k in ('makerCommissionRate', 'takerCommissionRate')}
        report['commission_rates'] = {k: str(v) for k, v in rates.items()}
        return all(0 <= v < 1 for v in rates.values())
    check('COMMISSION_RATES', commission)
    def bracket():
        rows = get('/papi/v1/um/leverageBracket', {'symbol': SYMBOL})
        selected = [row for row in rows if row.get('symbol') == SYMBOL]
        mmr = number(selected[0]['brackets'][0]['maintMarginRatio']) if len(selected) == 1 else None
        report['tier1_mmr'] = str(mmr) if mmr is not None else None
        return mmr is not None and 0 < mmr < 1
    check('LEVERAGE_BRACKET', bracket)
    def collateral():
        rows = get('/papi/v1/balance', {})
        if not isinstance(rows, list) or not rows:
            return False
        usdc = [row for row in rows if row.get('asset') == 'USDC']
        if len(usdc) != 1 or not 0 < number(usdc[0]['totalWalletBalance']) <= 250:
            return False
        for row in rows:
            if row.get('asset') != 'USDC' and number(row['totalWalletBalance']) != 0:
                return False
            if any(number(row.get(k, '0')) != 0 for k in ('crossMarginBorrowed', 'crossMarginInterest',
                                                         'negativeBalance')):
                return False
        return True
    check('USDC_COLLATERAL_ONLY', collateral)
    for market in ('um', 'cm'):
        def positions(market=market):
            rows = get(f'/papi/v1/{market}/positionRisk', {})
            if market == 'um':
                report['position_risk_shape'] = ('EMPTY_LIST' if rows == [] else
                                                 'ROWS' if isinstance(rows, list) else 'UNKNOWN')
            return isinstance(rows, list) and all(number(row['positionAmt']) == 0 for row in rows)
        check(f'{market.upper()}_ALL_POSITIONS_FLAT', positions)
    for path in ('/papi/v1/um/openOrders', '/papi/v1/um/algo/openAlgoOrders',
                 '/papi/v1/cm/openOrders', '/papi/v1/cm/conditional/openOrders'):
        def orders(path=path):
            data = get(path, {})
            rows = data.get('orders') if isinstance(data, dict) else data
            return isinstance(rows, list) and len(rows) == 0
        check(path, orders)
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    report['diagnostics_pass'] = all(c['status'] == 'PASS' for c in checks)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operator-readonly-approved', action='store_true')
    parser.add_argument('--output', type=Path, default=Path('artifacts/papi-readonly-probe.json'))
    args = parser.parse_args()
    if not args.operator_readonly_approved:
        parser.error('Human authorization required: --operator-readonly-approved')
    key = os.environ.get('BINANCE_MAINNET_API_KEY', '')
    secret = os.environ.get('BINANCE_MAINNET_API_SECRET', '')
    if not key or not secret:
        parser.error('Operator must supply BINANCE_MAINNET_API_KEY/SECRET through environment')
    try:
        offset, rtt = synchronize_clock()
        environment = {k: v for k, v in os.environ.items() if k.upper() in
                       {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'}}
        environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_NO_REPLACE_OBJECTS='1', GIT_CONFIG_GLOBAL=os.devnull)
        sha = subprocess.run(['git', 'rev-parse', 'HEAD'], env=environment, capture_output=True,
                             text=True, timeout=10, check=True).stdout.strip()
        report = collect_probe(lambda p, v: signed_get(p, v, key, secret, offset),
                               clock_offset_ms=offset, clock_rtt_ms=rtt, git_sha=sha)
    except Exception:
        print('PROBE_UNAVAILABLE: no readiness claim; no signed URLs or credentials logged')
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2))
    print('Diagnostics only. PAPI order/protection acceptance remains NOT_RUN. NOT ARMED.')
    return 0 if report['diagnostics_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
