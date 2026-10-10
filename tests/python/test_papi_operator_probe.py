from decimal import Decimal

from scripts.probe_papi_readonly import collect_probe, signed_get


def samples():
    return {
        '/papi/v1/account': {'accountStatus': 'NORMAL', 'accountEquity': '100', 'actualEquity': '100'},
        '/papi/v1/um/accountConfig': {'canTrade': True, 'dualSidePosition': False, 'multiAssetsMargin': False},
        '/papi/v1/um/positionSide/dual': {'dualSidePosition': False},
        '/papi/v1/um/symbolConfig': [{'symbol': 'ETHUSDC', 'leverage': 5}],
        '/papi/v1/um/commissionRate': {'takerCommissionRate': '0.0005', 'makerCommissionRate': '0.0002'},
        '/papi/v1/um/leverageBracket': [{'symbol': 'ETHUSDC', 'brackets': [{'maintMarginRatio': '0.004'}]}],
        '/papi/v1/balance': [{'asset': 'USDC', 'totalWalletBalance': '100'}],
        '/papi/v1/um/positionRisk': [],
        '/papi/v1/cm/positionRisk': [],
        '/papi/v1/um/openOrders': [],
        '/papi/v1/um/algo/openAlgoOrders': [],
        '/papi/v1/cm/openOrders': [],
        '/papi/v1/cm/conditional/openOrders': [],
    }


def probe(data):
    calls = []
    def get(path, params):
        calls.append((path, params))
        return data[path]
    result = collect_probe(get, clock_offset_ms=0, clock_rtt_ms=20, git_sha='a' * 40)
    return result, calls


def test_flat_empty_position_uses_symbol_config_and_never_claims_order_acceptance():
    result, calls = probe(samples())
    assert result['diagnostics_pass'] is True
    assert result['ready_for_pilot'] is False
    assert result['protection_acceptance'] == 'NOT_RUN'
    assert result['position_risk_shape'] == 'EMPTY_LIST'
    assert dict(calls)['/papi/v1/um/positionRisk'] == {}


def test_non_eth_exposure_and_hidden_orders_block_flat_diagnostics():
    data = samples()
    data['/papi/v1/um/positionRisk'] = [{'symbol': 'BTCUSDT', 'positionAmt': '0.1'}]
    data['/papi/v1/um/algo/openAlgoOrders'] = [{'symbol': 'BTCUSDT', 'clientAlgoId': 'sensitive-id'}]
    result, _ = probe(data)
    assert result['diagnostics_pass'] is False
    assert 'sensitive-id' not in str(result)


def test_missing_cantrade_nan_equity_or_clock_skew_is_not_pass():
    for change in ('permission', 'equity', 'clock'):
        data = samples()
        if change == 'permission':
            data['/papi/v1/um/accountConfig'].pop('canTrade')
        if change == 'equity':
            data['/papi/v1/account']['accountEquity'] = 'NaN'
        result = collect_probe(lambda p, _: data[p], clock_offset_ms=2000 if change == 'clock' else 0,
                               clock_rtt_ms=20, git_sha='a' * 40)
        assert result['diagnostics_pass'] is False


def test_signed_get_refuses_routes_outside_readonly_allowlist_before_network():
    import pytest
    with pytest.raises(ValueError, match='READONLY_PATH'):
        signed_get('/papi/v1/um/order', {}, 'never-print-key', 'never-print-secret', 0)


def test_missing_or_non_usdc_collateral_and_nonfinite_position_block():
    for value in ([], [{'asset': 'BTC', 'totalWalletBalance': '1'}]):
        data = samples()
        data['/papi/v1/balance'] = value
        result, _ = probe(data)
        assert result['diagnostics_pass'] is False
    data = samples()
    data['/papi/v1/um/positionRisk'] = [{'symbol': 'ETHUSDC', 'positionAmt': str(Decimal('NaN'))}]
    result, _ = probe(data)
    assert result['diagnostics_pass'] is False
