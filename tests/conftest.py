"""Every test is offline. Attempts to open a network connection fail."""
import asyncio
import dataclasses
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import socket
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from enum import IntEnum
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import core


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Network access forbidden in offline tests')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
    monkeypatch.setenv('APEX_ALLOW_LIVE_TRADING', 'I_UNDERSTAND_THE_RISK')
    monkeypatch.setenv('APEX_USE_DEMO', 'false')
    monkeypatch.setenv('APEX_ADOPT_ORPHANS', 'false')


SYMBOL = 'BTC/USDT:USDT'


class FakeExchange:
    """In-memory venue only; invented test IDs never reach any external API."""
    def __init__(self, hedged=False):
        self.options = {}
        self.hedge_mode = hedged
        self.live = {}
        self.algos = {}
        self.entries = {}
        self.created_algos = []
        self.market_calls = []
        self.cancelled_ids = []
        self.normal_orders = []
        self.fail_algo = {}
        self.fail_positions = False
        self.fail_query_algo = False
        self.fail_open_algos = False
        self.entry_fault = ''
        self.close_fault = ''
        self.keep_position_on_close = False
        self.fee = None
        self.mark = 100.0
        self.closed = False
        self.counter = 1000
        self._markets = {
            SYMBOL: {'id': 'BTCUSDT', 'symbol': SYMBOL, 'linear': True, 'swap': True,
                     'settle': 'USDT', 'contractSize': 1, 'active': True,
                     'limits': {'amount': {'min': .001, 'max': 100}, 'cost': {'min': 5}}},
        }

    async def load_markets(self):
        return self._markets

    def market(self, symbol):
        return self._markets[symbol]

    def price_to_precision(self, symbol, price):
        return format(Decimal(str(price)).quantize(Decimal('.1'), rounding=ROUND_HALF_UP), 'f')

    def amount_to_precision(self, symbol, amount):
        return format(Decimal(str(amount)).quantize(Decimal('.001'), rounding=ROUND_DOWN), 'f')

    async def fapiPrivateGetPositionSideDual(self, params):
        return {'dualSidePosition': self.hedge_mode}

    async def fapiPublicGetPremiumIndex(self, params):
        return {'symbol': params['symbol'], 'markPrice': str(self.mark)}

    async def fetch_positions(self, symbols=None):
        if self.fail_positions:
            raise RuntimeError('simulated positions timeout')
        result = []
        for (symbol, side), (qty, entry, leverage) in self.live.items():
            if symbols and symbol not in symbols:
                continue
            result.append({'symbol': symbol, 'side': side.lower(), 'contracts': qty,
                           'contractSize': 1, 'entryPrice': entry, 'leverage': leverage,
                           'info': {'positionSide': side if self.hedge_mode else 'BOTH',
                                    'positionAmt': str(qty if side == 'LONG' else -qty), 'entryPrice': str(entry)}})
        if not result:
            # Critical regression: contractSize=1 with contracts=0 is flat.
            return [{'symbol': SYMBOL, 'side': 'long', 'contracts': 0,
                     'contractSize': 1, 'entryPrice': 0, 'info': {'positionAmt': '0'}}]
        return result

    async def set_leverage(self, leverage, symbol):
        self.leverage = leverage

    async def fetch_ticker(self, symbol):
        return {'last': self.mark}

    async def fetch_time(self):
        return int(time.time() * 1000)

    async def fetch_balance(self):
        return {'USDT': {'total': 1000.0, 'free': 1000.0},
                'info': {'totalMarginBalance': '1000', 'availableBalance': '1000'}}

    async def fapiPrivateGetOpenOrders(self, params):
        return list(self.normal_orders)

    async def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = dict(params or {})
        self.market_calls.append((symbol, type, side, amount, price, params))
        assert type == 'market', 'SL/TP must use the raw Algo API, not this fake ordinary endpoint'
        is_close = params.get('newClientOrderId', '').startswith('qa-close-')
        direction = params['positionSide'] if params.get('positionSide') in ('LONG', 'SHORT') else ('LONG' if (side == 'buy') != is_close else 'SHORT')
        fault = self.close_fault if is_close else self.entry_fault
        if fault == 'timeout_not_accepted':
            raise RuntimeError('simulated lost request, acceptance unknown')
        self.counter += 1
        client_id = params['newClientOrderId']
        raw = {'symbol': self.market(symbol)['id'], 'orderId': self.counter,
               'clientOrderId': client_id, 'status': 'FILLED', 'avgPrice': str(self.mark),
               'executedQty': str(amount), 'side': side.upper(), 'positionSide': params.get('positionSide', 'BOTH')}
        self.entries[client_id] = raw
        if is_close:
            if not self.keep_position_on_close:
                self.live.pop((symbol, direction), None)
        else:
            self.live[(symbol, direction)] = (amount, self.mark, getattr(self, 'leverage', 10))
        if fault == 'timeout_accepted':
            raise RuntimeError('simulated lost ACK after fill')
        if fault == 'nonterminal':
            raw['status'] = 'NEW'
            return {'id': str(self.counter), 'status': 'open', 'filled': 0, 'fee': self.fee}
        return {'id': str(self.counter), 'status': 'closed', 'average': self.mark,
                'filled': amount, 'fee': self.fee}

    async def fapiPrivateGetOrder(self, params):
        client_id = params['origClientOrderId']
        if client_id not in self.entries:
            raise RuntimeError('simulated order identity not found')
        return dict(self.entries[client_id])

    async def fapiPrivatePostAlgoOrder(self, params):
        self.created_algos.append(dict(params))
        fault = self.fail_algo.get(params['type'], '')
        if fault == 'reject':
            raise RuntimeError('simulated -2021 invalid trigger')
        self.counter += 1
        row = dict(params)
        row.update(algoId=self.counter, orderType=params['type'], algoStatus='NEW', closePosition=True)
        if fault == 'wrong_side':
            row['side'] = 'BUY' if params['side'] == 'SELL' else 'SELL'
        if fault == 'wrong_trigger':
            row['triggerPrice'] = str(float(params['triggerPrice']) + .1)
        self.algos[str(self.counter)] = row
        if fault == 'timeout_accepted':
            raise RuntimeError('simulated lost algo ACK')
        return dict(row)

    async def fapiPrivateGetAlgoOrder(self, params):
        if self.fail_query_algo:
            raise RuntimeError('simulated algo query timeout')
        if 'algoId' in params:
            row = self.algos.get(str(params['algoId']))
        else:
            row = next((r for r in self.algos.values() if r['clientAlgoId'] == params['clientAlgoId']), None)
        if row is None:
            raise RuntimeError('simulated algo not found')
        return dict(row)

    async def fapiPrivateGetOpenAlgoOrders(self, params):
        if self.fail_open_algos:
            raise RuntimeError('simulated open algo query timeout')
        return [dict(row) for row in self.algos.values()
                if row['algoStatus'] == 'NEW' and row['symbol'] == params['symbol']]

    async def fapiPrivateDeleteAlgoOrder(self, params):
        if 'algoId' in params:
            row = self.algos.get(str(params['algoId']))
        else:
            row = next((r for r in self.algos.values() if r['clientAlgoId'] == params['clientAlgoId']), None)
        if row:
            row['algoStatus'] = 'CANCELED'
            self.cancelled_ids.append(str(row['algoId']))
        return {'code': 200}

    async def fapiPrivateGetUserTrades(self, params):
        return []

    async def close(self):
        self.closed = True


class FakeDB:
    def __init__(self):
        self.rows = []
        self.events = []
        self.fail_save = False
        self.db = self
        self.trades = self
        self.system_events = self
        self.signals = self

    async def save_trade(self, data):
        if self.fail_save:
            raise RuntimeError('simulated database write failure')
        result = dict(data)
        result.update(id=len(self.rows) + 1, status='OPEN', entry_price=data['entry'],
                      sl_price=data['sl'], tp_price=data['tp'], alpha_score=data['alpha'],
                      metadata=json.dumps(data.get('metadata') or {}),
                      sl_order_id=data.get('sl_order_id', ''), tp_order_id=data.get('tp_order_id', ''))
        self.rows.append(result)
        return result['id']

    async def get_open_positions(self):
        return [dict(row) for row in self.rows if row['status'] == 'OPEN']

    async def get_total_pnl(self):
        return sum(float(r.get('pnl_usdt') or 0) for r in self.rows if r['status'] == 'CLOSED')

    async def log_event(self, **kwargs):
        self.events.append({**kwargs, 'details': json.dumps(kwargs.get('details') or {})})
        return len(self.events)

    async def query(self, sql, params=()):
        return [dict(event) for event in self.events]

    async def execute(self, sql, params=()):
        assert sql.startswith('UPDATE trades SET ')
        trade_id = params[-1]
        row = next(r for r in self.rows if r['id'] == trade_id)
        fields = sql[len('UPDATE trades SET '):].split(' WHERE ')[0].split(', ')
        for field, value in zip(fields, params[:-1]):
            row[field.split(' = ')[0].strip()] = value
        return SimpleNamespace(rowcount=1)


class TestOrderStatus(IntEnum):
    __test__ = False
    PENDING = 0
    SUBMITTED = 1
    FILLED = 3
    CANCELLED = 4
    REJECTED = 5
    EXPIRED = 6
    ERROR = 7


@dataclasses.dataclass
class TestOrder:
    __test__ = False
    symbol: str = ''
    side: str = ''
    order_type: str = 'market'
    quantity: float = 0
    leverage: int = 1
    id: str = dataclasses.field(default_factory=lambda: __import__('uuid').uuid4().hex)
    status: TestOrderStatus = TestOrderStatus.PENDING
    avg_fill_price: float = 0
    filled_qty: float = 0
    exchange_order_id: str = ''
    fees: float = 0
    error: str = ''
    filled_at: float = 0

    def to_dict(self):
        return dataclasses.asdict(self)


class TestOrderManager:
    __test__ = False
    def __init__(self):
        self.active_orders = {}

    async def submit_order(self, order):
        self.active_orders[order.id] = order

    async def update_order(self, order_id, **kwargs):
        if order_id in self.active_orders:
            order = self.active_orders[order_id]
            for name, value in kwargs.items():
                setattr(order, name, value)
            if order.status in (TestOrderStatus.FILLED, TestOrderStatus.ERROR):
                self.active_orders.pop(order_id)


def master_config(dry_run=False):
    return SimpleNamespace(
        dry_run=dry_run, trading_timeframe='30m', binance_api_key='OFFLINE_TEST_ONLY',
        binance_secret='OFFLINE_TEST_ONLY',
        execution=SimpleNamespace(max_slippage_bps=10),
        risk=SimpleNamespace(max_open_positions=6, max_kelly_pct=.05,
                             max_portfolio_risk_pct=.15, max_position_size_pct=.30, leverage_tiers={.02:5,.05:10,.20:50},
                             default_stop_atr_multiple=2.5, default_take_profit_atr_multiple=5.0),
    )


@pytest.fixture
def execution_factory():
    namespace = {name: value for name, value in vars(core).items() if not name.startswith('__')}
    namespace.update(os=os, json=json, np=np, math=math, asyncio=asyncio, time=time,
                     Order=TestOrder, OrderManager=TestOrderManager, OrderStatus=TestOrderStatus,
                     exec_logger=logging.getLogger('offline.exec'), trade_logger=logging.getLogger('offline.trades'))
    text = (ROOT / 'execution_replacement.py').read_text()
    exec(compile('from __future__ import annotations\n' + text, '<execution_patch>', 'exec'), namespace)
    cls = namespace['ExecutionEngine']

    def factory(exchange=None, database=None, dry_run=False):
        exchange = exchange or FakeExchange()
        database = database or FakeDB()
        instance = cls(exchange=exchange, database=database, master_config=master_config(dry_run))
        instance.api.read_delay = 0
        instance.startup_ready = True
        return instance, exchange, database
    return factory
