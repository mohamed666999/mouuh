import asyncio
from dataclasses import replace
import math

import pytest

from core import (
    ApexPatchAlgoService, ApexPatchAlgoSpec, ApexPatchSafetyError,
    ApexPatchStateUnknown, ApexPatchProtectionUnconfirmed,
    apex_patch_bool, apex_patch_number, apex_patch_closed_candles,
)
from conftest import FakeExchange, SYMBOL


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.mark.parametrize('hedged,side,expected_position_side,expected_close_side', [
    (False, 'LONG', 'BOTH', 'SELL'), (False, 'SHORT', 'BOTH', 'BUY'),
    (True, 'LONG', 'LONG', 'SELL'), (True, 'SHORT', 'SHORT', 'BUY'),
])
def test_payload_mode_and_no_conflicting_params(hedged, side, expected_position_side, expected_close_side):
    api = ApexPatchAlgoService(FakeExchange(hedged), read_delay=0)
    spec = api.spec(SYMBOL, side, 'STOP_MARKET', 97.54, 'qa-sl-offline', hedged)
    payload = spec.payload()
    assert payload['positionSide'] == expected_position_side
    assert payload['side'] == expected_close_side
    assert payload['triggerPrice'] == '97.5'
    assert payload['algoType'] == 'CONDITIONAL'
    assert payload['workingType'] == 'MARK_PRICE'
    assert payload['closePosition'] == 'true'
    assert 'quantity' not in payload and 'reduceOnly' not in payload and 'stopPrice' not in payload


@pytest.mark.parametrize('position_side,close_side', [('LONG','BUY'),('SHORT','SELL'),('INVALID','BUY')])
def test_invalid_closing_direction_rejected(position_side, close_side):
    spec = ApexPatchAlgoSpec('BTCUSDT', close_side, position_side, 'STOP_MARKET', '97.5', 'qa-sl-offline')
    with pytest.raises(ApexPatchSafetyError):
        spec.payload()


def test_creation_lost_ack_is_recovered_without_second_post():
    exchange = FakeExchange()
    exchange.fail_algo['STOP_MARKET'] = 'timeout_accepted'
    api = ApexPatchAlgoService(exchange, read_delay=0)
    spec = api.spec(SYMBOL, 'LONG', 'STOP_MARKET', 97.5, 'qa-sl-offline', False)
    row = run(api.place_once(spec))
    assert str(row['algoId'])
    assert len(exchange.created_algos) == 1


def test_query_failure_recovers_from_open_algo_endpoint():
    exchange = FakeExchange()
    exchange.fail_query_algo = True
    api = ApexPatchAlgoService(exchange, read_delay=0)
    row = run(api.place_once(api.spec(SYMBOL, 'LONG', 'STOP_MARKET', 97.5, 'qa-sl-offline', False)))
    assert row['algoStatus'] == 'NEW'
    assert len(exchange.created_algos) == 1


@pytest.mark.parametrize('fault', ['reject', 'wrong_side', 'wrong_trigger'])
def test_failed_or_mismatched_protection_never_verified(fault):
    exchange = FakeExchange()
    exchange.fail_algo['STOP_MARKET'] = fault
    api = ApexPatchAlgoService(exchange, read_delay=0)
    with pytest.raises(ApexPatchProtectionUnconfirmed):
        run(api.place_once(api.spec(SYMBOL, 'LONG', 'STOP_MARKET', 97.5, 'qa-sl-offline', False)))
    assert len(exchange.created_algos) == 1


def test_new_order_response_alone_is_not_verification():
    exchange = FakeExchange()
    exchange.fail_query_algo = exchange.fail_open_algos = True
    api = ApexPatchAlgoService(exchange, read_delay=0)
    with pytest.raises(ApexPatchProtectionUnconfirmed):
        run(api.place_once(api.spec(SYMBOL, 'LONG', 'STOP_MARKET', 97.5, 'qa-sl-offline', False)))


@pytest.mark.parametrize('change', [
    {'algoStatus':'CANCELED'}, {'algoStatus':'EXPIRED'}, {'algoStatus':'REJECTED'},
    {'positionSide':'SHORT'}, {'side':'BUY'}, {'closePosition':False},
    {'symbol':'ETHUSDT'}, {'workingType':'CONTRACT_PRICE'}, {'triggerPrice':'97.6'},
    {'clientAlgoId':'some-other-order'}, {'orderType':'TAKE_PROFIT_MARKET'},
])
def test_verification_checks_all_protective_fields(change):
    exchange = FakeExchange()
    api = ApexPatchAlgoService(exchange, read_delay=0)
    spec = api.spec(SYMBOL, 'LONG', 'STOP_MARKET', 97.5, 'qa-sl-offline', False)
    row = run(api.place_once(spec))
    assert spec.matches(row, str(row['algoId']))
    assert not spec.matches({**row, **change}, str(row['algoId']))


def test_contract_size_is_not_live_quantity():
    api = ApexPatchAlgoService(FakeExchange(), read_delay=0)
    assert run(api.positions()) == []
    assert run(api.position(SYMBOL, 'LONG')) is None


def test_failed_position_read_is_unknown_not_zero():
    exchange = FakeExchange()
    exchange.fail_positions = True
    api = ApexPatchAlgoService(exchange, read_delay=0)
    with pytest.raises(ApexPatchStateUnknown):
        run(api.position(SYMBOL, 'LONG'))
    with pytest.raises(ApexPatchStateUnknown):
        run(api.flatten(SYMBOL, 'LONG'))
    assert exchange.market_calls == []


@pytest.mark.parametrize('hedged,side', [(False,'LONG'),(False,'SHORT'),(True,'LONG'),(True,'SHORT')])
def test_flatten_uses_live_qty_and_correct_mode(hedged, side):
    exchange = FakeExchange(hedged)
    exchange.live[(SYMBOL, side)] = (.432, 100.0, 10)
    api = ApexPatchAlgoService(exchange, read_delay=0)
    assert run(api.flatten(SYMBOL, side))
    assert len(exchange.market_calls) == 1
    call = exchange.market_calls[0]
    assert call[3] == .432
    assert call[2] == ('sell' if side == 'LONG' else 'buy')
    assert call[5]['positionSide'] == (side if hedged else 'BOTH')
    assert ('reduceOnly' in call[5]) == (not hedged)
    assert not exchange.live


def test_uncertain_close_is_not_blindly_resent():
    exchange = FakeExchange()
    exchange.live[(SYMBOL, 'LONG')] = (1, 100, 10)
    exchange.close_fault = 'timeout_not_accepted'
    api = ApexPatchAlgoService(exchange, read_delay=0)
    with pytest.raises(ApexPatchStateUnknown):
        run(api.flatten(SYMBOL, 'LONG'))
    assert len(exchange.market_calls) == 1


def test_bounded_close_attempts_do_not_claim_still_open_position_flat():
    exchange = FakeExchange()
    exchange.live[(SYMBOL, 'LONG')] = (1, 100, 10)
    exchange.keep_position_on_close = True
    api = ApexPatchAlgoService(exchange, read_delay=0)
    assert run(api.flatten(SYMBOL, 'LONG')) is False
    assert len(exchange.market_calls) == 3


def test_cleanup_cancels_owned_id_not_other_orders():
    exchange = FakeExchange()
    api = ApexPatchAlgoService(exchange, read_delay=0)
    first = run(api.place_once(api.spec(SYMBOL, 'LONG', 'STOP_MARKET', 97.5, 'qa-sl-first', False)))
    second = run(api.place_once(api.spec(SYMBOL, 'LONG', 'TAKE_PROFIT_MARKET', 105, 'manual-other', False)))
    run(api.cancel_known(SYMBOL, algo_id=str(first['algoId'])))
    assert exchange.cancelled_ids == [str(first['algoId'])]
    assert exchange.algos[str(second['algoId'])]['algoStatus'] == 'NEW'


@pytest.mark.parametrize('value', [None, 'nan', float('inf'), float('-inf')])
def test_invalid_numbers_rejected(value):
    with pytest.raises(ApexPatchSafetyError):
        apex_patch_number(value)


@pytest.mark.parametrize('value,expected', [(True, True), (False, False), ('true',True),('false',False)])
def test_boolean_string_false_is_not_truthy(value, expected):
    assert apex_patch_bool(value) is expected


@pytest.mark.parametrize('value', ['False','0',0,None])
def test_unknown_boolean_response_fails_closed(value):
    with pytest.raises(ApexPatchStateUnknown):
        apex_patch_bool(value)


def test_amount_is_truncated_and_not_rounded_up_for_minimum():
    api = ApexPatchAlgoService(FakeExchange())
    assert api.amount(SYMBOL, 1.23499) == 1.234
    with pytest.raises(ApexPatchSafetyError):
        api.amount(SYMBOL, .0009)


def test_only_completed_30m_candles_are_used():
    candles = [[0,100,102,98,101,20], [1_800_000,101,103,99,102,20],
               [3_600_000,102,104,100,103,20]]
    result = apex_patch_closed_candles(candles, now_ms=4_000_000)
    assert [c[0] for c in result] == [0, 1_800_000]


@pytest.mark.parametrize('timestamps', [[0,0], [1_800_000,0], [0,3_600_000]])
def test_duplicate_unsorted_or_gapped_candles_rejected(timestamps):
    candles = [[t,100,102,98,101,20] for t in timestamps]
    with pytest.raises(ApexPatchSafetyError):
        apex_patch_closed_candles(candles, now_ms=10_000_000)
