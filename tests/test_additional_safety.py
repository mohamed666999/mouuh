import asyncio
from types import SimpleNamespace

import pytest

from core import ApexPatchAlgoService, ApexPatchSafetyError, ApexPatchStateUnknown
from conftest import FakeExchange, SYMBOL
from test_execution import enter


def test_raw_ccxt_methods_exist_without_connecting():
    ccxtpro = pytest.importorskip('ccxt.pro')
    exchange = ccxtpro.binanceusdm()
    api = ApexPatchAlgoService(exchange)
    api.assert_capabilities()  # Introspection only; no markets/request.


def test_no_new_entry_exceeds_prospective_portfolio_risk(execution_factory):
    engine, exchange, database = execution_factory()
    database.rows.append({'id':99,'symbol':'ETH/USDT:USDT','side':'LONG','status':'OPEN',
                          'qty':1,'entry_price':200,'sl_price':50})  # Existing $150 at risk.
    success, _ = asyncio.run(enter(engine))
    assert not success and not exchange.market_calls


def test_margin_cap_applies_even_when_stop_is_very_tight(execution_factory):
    engine, exchange, _ = execution_factory()
    success, _ = asyncio.run(engine.execute_order(SYMBOL,'LONG',40,10,99.9,100.2,
                                                  reference_entry=100,risk_equity=1000))
    assert not success and not exchange.market_calls  # $400 margin > $300 cap.


def test_leverage_failure_does_not_create_unresolved_entry_journal(execution_factory):
    engine, exchange, database = execution_factory()
    async def failed(*args):
        raise RuntimeError('simulated leverage request rejected before entry')
    exchange.set_leverage = failed
    success, _ = asyncio.run(enter(engine))
    assert not success and not exchange.market_calls
    assert not any(event['event_type']=='APEX_ENTRY_INTENT' for event in database.events)


def test_flat_side_specific_leg_not_other_hedged_leg():
    exchange = FakeExchange(hedged=True)
    exchange.live[(SYMBOL,'SHORT')] = (1,100,10)
    api = ApexPatchAlgoService(exchange,read_delay=0)
    assert asyncio.run(api.position(SYMBOL,'LONG')) is None
    assert asyncio.run(api.position(SYMBOL,'SHORT')).quantity == 1


def test_open_algo_failure_not_interpreted_as_no_protection():
    exchange = FakeExchange()
    exchange.fail_open_algos = True
    api = ApexPatchAlgoService(exchange,read_delay=0)
    with pytest.raises(ApexPatchStateUnknown):
        asyncio.run(api.open_algos(SYMBOL))
