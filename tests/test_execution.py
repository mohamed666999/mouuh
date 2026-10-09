import asyncio
import json

import pytest

from core import ApexPatchStateUnknown
from conftest import FakeExchange, FakeDB, SYMBOL


def run(awaitable):
    return asyncio.run(awaitable)


def enter(engine, side='LONG', qty=1):
    sl, tp = (97.5, 105) if side == 'LONG' else (102.5, 95)
    return engine.execute_order(SYMBOL, side, qty, 10, sl, tp,
                                reference_entry=100, alpha_score=70,
                                risk_pct=.0025, risk_equity=1000)


@pytest.mark.parametrize('hedged,side', [(False,'LONG'),(False,'SHORT'),(True,'LONG'),(True,'SHORT')])
def test_real_flow_entry_then_one_sl_one_tp_and_single_saved_trade(execution_factory, hedged, side):
    engine, exchange, database = execution_factory(FakeExchange(hedged))
    success, order = run(enter(engine, side))
    assert success and not engine.halted
    assert len(exchange.market_calls) == 1
    assert [p['type'] for p in exchange.created_algos] == ['STOP_MARKET','TAKE_PROFIT_MARKET']
    assert len(database.rows) == 1
    saved = database.rows[0]
    assert saved['timeframe'] == '30m'
    assert saved['entry_price'] == order.avg_fill_price == 100
    assert saved['qty'] == order.filled_qty == 1
    assert saved['protection_status'] == order._protection['protection_status'] == 'PROTECTED'
    assert saved['sl_order_id'] and saved['tp_order_id'] and saved['order_id']
    assert not engine.order_manager.active_orders
    assert exchange.market_calls[0][5]['positionSide'] == (side if hedged else 'BOTH')
    assert isinstance(engine.get_execution_stats(), dict)


def test_fee_none_does_not_crash_after_entry(execution_factory):
    engine, exchange, database = execution_factory()
    exchange.fee = None
    success, order = run(enter(engine))
    assert success and order.fees == 0
    assert json.loads(database.rows[0]['metadata'])['entry_fees_confirmed'] is False
    assert len(exchange.created_algos) == 2


def test_entry_timeout_accepted_is_queried_not_resent(execution_factory):
    engine, exchange, database = execution_factory()
    exchange.entry_fault = 'timeout_accepted'
    success, order = run(enter(engine))
    assert success and order.filled_qty == 1
    assert len(exchange.market_calls) == 1
    assert len(exchange.created_algos) == 2


def test_entry_uncertainty_halts_no_repeat_entry_or_fake_protected_status(execution_factory):
    engine, exchange, database = execution_factory()
    exchange.entry_fault = 'timeout_not_accepted'
    success, order = run(enter(engine))
    assert not success and engine.halted
    assert len(exchange.market_calls) == 1 and not exchange.created_algos
    assert order._protection['protection_status'] == 'ENTRY_UNRESOLVED'
    assert any(event['event_type'] == 'APEX_ENTRY_INTENT' for event in database.events)
    assert not any(event['event_type'] == 'APEX_ENTRY_RESOLVED' for event in database.events)


def test_sl_failure_flattens_with_verification_and_halts(execution_factory):
    engine, exchange, database = execution_factory()
    exchange.fail_algo['STOP_MARKET'] = 'reject'
    success, order = run(enter(engine))
    assert not success and engine.halted and not exchange.live
    assert len(exchange.created_algos) == 1  # TP never placed without confirmed SL.
    assert len(exchange.market_calls) == 2  # One entry, one verified reduction.
    assert order._protection['protection_status'] == 'FLAT_VERIFIED'
    assert 'simulated -2021' in order.error


def test_tp_failure_keeps_verified_sl_and_blocks_further_entries(execution_factory):
    engine, exchange, database = execution_factory()
    exchange.fail_algo['TAKE_PROFIT_MARKET'] = 'reject'
    success, order = run(enter(engine))
    assert success and engine.halted
    assert exchange.live and len(exchange.market_calls) == 1
    assert order._protection['protection_status'] == 'SL_ONLY_NO_TP'
    assert database.rows[0]['protection_status'] == 'SL_ONLY_NO_TP'
    again, _ = run(enter(engine))
    assert not again and len(exchange.market_calls) == 1


def test_failed_database_save_after_fill_de_risks_not_silent_untracked_entry(execution_factory):
    engine, exchange, database = execution_factory()
    database.fail_save = True
    success, order = run(enter(engine))
    assert not success and engine.halted and not exchange.live
    assert len(exchange.market_calls) == 2 and not exchange.created_algos


def test_new_entries_require_startup_gate(execution_factory):
    engine, exchange, _ = execution_factory()
    engine.startup_ready = False
    success, _ = run(enter(engine))
    assert not success and exchange.market_calls == []


def test_existing_symbol_does_not_get_another_entry(execution_factory):
    engine, exchange, _ = execution_factory()
    exchange.live[(SYMBOL, 'LONG')] = (1,100,10)
    success, _ = run(enter(engine))
    assert not success and exchange.market_calls == [] and not exchange.created_algos


def test_existing_sibling_or_manual_order_blocks_new_entry(execution_factory):
    engine, exchange, _ = execution_factory()
    exchange.normal_orders = [{'orderId':999, 'symbol':'BTCUSDT'}]
    success, _ = run(enter(engine))
    assert not success and not exchange.market_calls


def test_equity_required_before_entry_not_after_unprotected_fill(execution_factory):
    engine, exchange, _ = execution_factory()
    success, _ = run(engine.execute_order(SYMBOL,'LONG',1,10,97.5,105,reference_entry=100))
    assert not success and not exchange.market_calls


def test_pre_entry_single_position_loss_cap(execution_factory):
    engine, exchange, _ = execution_factory()
    success, _ = run(enter(engine, qty=25))  # 62.5 loss exceeds 5% of 1000.
    assert not success and not exchange.market_calls


def test_position_size_not_rounded_up_to_exchange_minimum(execution_factory):
    engine, exchange, _ = execution_factory()
    success, _ = run(enter(engine, qty=.0009))
    assert not success and not exchange.market_calls


def test_dry_run_has_no_private_orders(execution_factory):
    engine, exchange, database = execution_factory(dry_run=True)
    exchange.fail_positions = True  # Private position reads must not be needed.
    success, order = run(enter(engine))
    assert success
    assert not exchange.market_calls and not exchange.created_algos
    assert database.rows[0]['protection_status'] == 'DRY_RUN'
    assert order._protection['trade_id'] == 1


def test_explicit_order_permission_required_even_if_config_live(execution_factory, monkeypatch):
    monkeypatch.delenv('APEX_ALLOW_LIVE_TRADING')
    engine, exchange, _ = execution_factory()
    success, _ = run(enter(engine))
    assert not success and not exchange.market_calls


def test_startup_verifies_exchange_not_cached_protected_flag(execution_factory):
    engine, exchange, database = execution_factory()
    assert run(enter(engine))[0]
    sl_id = database.rows[0]['sl_order_id']
    exchange.algos[sl_id]['algoStatus'] = 'CANCELED'
    new_engine, _, _ = execution_factory(exchange, database)
    run(new_engine.startup_checks())
    assert new_engine.halted
    assert len(exchange.created_algos) == 2  # No blind restart duplicate.


def test_startup_is_idempotent_when_protection_still_exists(execution_factory):
    engine, exchange, database = execution_factory()
    assert run(enter(engine))[0]
    new_engine, _, _ = execution_factory(exchange, database)
    run(new_engine.startup_checks())
    assert new_engine.startup_ready and not new_engine.halted
    assert len(exchange.created_algos) == 2 and len(database.rows) == 1


def test_read_error_never_claims_unprotected_position_flat(execution_factory):
    engine, exchange, database = execution_factory()
    exchange.live[(SYMBOL, 'LONG')] = (1,100,10)
    exchange.fail_positions = True
    assert run(engine._de_risk_unprotected(SYMBOL, 'LONG')) is False
    assert engine.halted and exchange.live and not exchange.market_calls


def test_shutdown_finishes_submitted_protection_not_caller_cancellation(execution_factory):
    async def scenario():
        engine, exchange, database = execution_factory()
        reached = asyncio.Event()
        release = asyncio.Event()
        original = exchange.fapiPrivatePostAlgoOrder
        async def delayed(params):
            if params['type'] == 'STOP_MARKET':
                reached.set()
                await release.wait()
            return await original(params)
        exchange.fapiPrivatePostAlgoOrder = delayed
        caller = asyncio.create_task(enter(engine))
        await reached.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert engine._critical_tasks
        release.set()
        await engine.close()
        assert exchange.closed
        assert database.rows[0]['protection_status'] == 'PROTECTED'
        assert len(exchange.created_algos) == 2
    run(scenario())
