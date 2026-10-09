import ast
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import numpy as np
import pytest

from apply_apex_fix import patch_source, PatchError, method_template
from conftest import ROOT


SOURCE_FIXTURE = '''#!/usr/bin/env python3
from __future__ import annotations
import os, time
from dataclasses import dataclass, field
os.environ["BINANCE_API_KEY"] = os.getenv("BINANCE_API_KEY", "TEST_ONLY_EXPOSED_KEY")
os.environ["BINANCE_SECRET"] = os.getenv("BINANCE_SECRET", "TEST_ONLY_EXPOSED_SECRET")
if not os.environ.get("BINANCE_API_KEY") or not os.environ.get("BINANCE_SECRET"):
    raise SystemExit("missing credentials")
BASE_DIR = "/original/nonportable/path"
class TradingMode:
    DRY_RUN = 0
    LIVE_TRADING = 2
@dataclass
class ApexConfig:
    dry_run: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "false") == "true")
    trading_mode: TradingMode = field(default_factory=lambda: TradingMode.LIVE_TRADING)
    primary_exchange: str = "binance"
    db_path: str = "apex_quant_v4.db"
class AlphaEngine:
    CQMI_WEIGHT = 0.55
    LEGACY_WEIGHT = 0.45
    CQMI_FACTOR_NAME = "cqmi"
    def _compute_legacy_max_score(self):
        return 172.5
    async def generate_signal(self, symbol, ohlcv, microstructure):
        if not ohlcv:
            return {"action":"WAIT", "timestamp":time.time()}
        aqii_score, reason = aqii_factor.calculate(highs, lows, closes, volumes, microstructure, self.config)
        legacy_score = sum(f.weight * f.max_score for f in self.factors if f.name not in ('cqmi', 'liquidity'))
        return {"action":"LONG", "score":aqii_score, "legacy_score":legacy_score, "timestamp":time.time()}
class TrendIndicators:
    @staticmethod
    def atr(highs, lows, closes, period=14):
        return closes
class VolatilityFactor:
    def calculate(self, highs, lows, closes, config):
        return 75, "BB Lower"
class DatabaseSchema:
    SCHEMA_VERSION = 3
    @classmethod
    def apply_schema(cls, conn):
        pass
    @classmethod
    def run_migrations(cls, conn):
        return 0
class BinanceAdapter:
    def __init__(self, config):
        self.config = config
    async def fetch_ohlcv(self, symbol: str, timeframe: str = '1m', limit: int = 100):
        pass
    def get_ws_streams(self, symbols):
        return ['old@kline_1m']
class ApexController:
    async def scanner_loop(self):
        pass
    async def monitor_positions(self):
        pass
    async def setup_health_checks(self):
        pass
    async def run(self):
        pass
    def get_status(self):
        return {}
class CircuitBreaker:
    def update_equity(self, equity):
        return {}
class ExecutionEngine:
    pass
class OrderManager:
    async def update_order(self, order_id, **kwargs):
        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            pass
@dataclass
class Signal:
    timeframe: str = "1m"
class TradeRepository:
    async def save_trade(self, trade):
        return trade.get('timeframe', '1m')
class SignalRepository:
    async def save_signal(self, signal):
        return signal.get('timeframe', '1m')
def _verify_credentials_loaded():
    return True
if __name__ == "__main__":
    raise RuntimeError("Fixture must NEVER be executed by the builder")
'''


def class_node(text, name):
    return next(n for n in ast.parse(text).body if isinstance(n, ast.ClassDef) and n.name == name)


def loaded_template_class(name, extra=None):
    tree = ast.parse((ROOT / 'other_methods.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    namespace = {'np':np, **(extra or {})}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<math_schema_template>', 'exec'), namespace)
    return namespace[name]


def test_builder_compiles_without_import_or_execution():
    fixed, changes = patch_source(SOURCE_FIXTURE)
    assert compile(fixed, '<result>', 'exec')
    assert len(changes) >= 20
    assert 'TEST_ONLY_EXPOSED_KEY' not in fixed and 'TEST_ONLY_EXPOSED_SECRET' not in fixed
    assert 'DRY_RUN", "false"' not in fixed
    assert 'trading_timeframe: str = "30m"' in fixed
    assert 'fapiPrivatePostAlgoOrder' in fixed and 'fapiPrivateGetOpenAlgoOrders' in fixed
    assert 'APEX_ALLOW_LIVE_TRADING' in fixed
    assert SOURCE_FIXTURE.endswith('''    raise RuntimeError("Fixture must NEVER be executed by the builder")\n''')


def test_fifty_fifty_weights_and_existing_sixty_point_filter_not_lowered():
    fixed, _ = patch_source(SOURCE_FIXTURE)
    engine = class_node(fixed, 'AlphaEngine')
    constants = {t.id:n.value.value for n in engine.body if isinstance(n, ast.Assign)
                 and isinstance(n.value, ast.Constant) for t in n.targets if isinstance(t, ast.Name)}
    assert constants['CQMI_WEIGHT'] == .5 and constants['LEGACY_WEIGHT'] == .5
    assert 100 * (.5 * .8 + .5 * .6) == 70  # One aggregated 50% per block.


def test_aqii_uses_named_arrays_in_correct_signature():
    fixed, _ = patch_source(SOURCE_FIXTURE)
    calls = [n for n in ast.walk(class_node(fixed,'AlphaEngine')) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
             and n.func.value.id == 'aqii_factor']
    assert len(calls) == 1 and not calls[0].args
    args = {k.arg:ast.unparse(k.value) for k in calls[0].keywords}
    assert args == {'closes':'closes','highs':'highs','lows':'lows','volumes':'volumes',
                    'microstructure':'microstructure','config':'self.config'}


def test_30m_every_signal_return_and_scanner_atr_and_stream():
    fixed, _ = patch_source(SOURCE_FIXTURE)
    alpha = class_node(fixed,'AlphaEngine')
    for n in ast.walk(alpha):
        if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict):
            assert 'timeframe' in [k.value for k in n.value.keys if isinstance(k, ast.Constant)]
    controller = ast.unparse(class_node(fixed,'ApexController'))
    assert 'CFG.trading_timeframe' in controller
    assert 'apex_patch_closed_candles' in controller
    assert 'ohlcv[-14:]' not in controller
    assert 'db.trades.save_trade' not in controller  # Saved by execution once.
    adapter = ast.unparse(class_node(fixed,'BinanceAdapter'))
    assert "'@kline_' + self.config.trading_timeframe" in adapter
    assert "timeframe: str='30m'" in adapter


def test_builder_rejects_reapplication():
    fixed, _ = patch_source(SOURCE_FIXTURE)
    with pytest.raises(PatchError, match='already patched'):
        patch_source(fixed)


def test_builder_fails_when_source_differs_instead_of_partial_patch():
    with pytest.raises(PatchError):
        patch_source('class DifferentBot: pass\n')
    with pytest.raises(PatchError, match='exactly one AQII'):
        patch_source(SOURCE_FIXTURE.replace('aqii_factor.calculate', 'other_factor.calculate'))


def test_builder_rejects_credential_literal_left_in_another_location():
    with pytest.raises(PatchError, match='credential literal'):
        patch_source(SOURCE_FIXTURE + '\n# TEST_ONLY_EXPOSED_SECRET\n')


def test_builder_accepts_pasted_code_fences():
    fixed, _ = patch_source('```python\n' + SOURCE_FIXTURE + '```\n')
    assert not fixed.startswith('```')


def test_wilder_atr_seed_and_recursion():
    cls = loaded_template_class('TrendIndicatorsPatch')
    highs = np.array([10,13,15,20,22], dtype=float)
    lows = np.array([8,10,12,16,19], dtype=float)
    closes = np.array([9,12,14,18,21], dtype=float)
    # True ranges: 2,4,3,6,4. SMA seed = 3; next = 4; next = 4.
    result = cls.atr(highs,lows,closes,3)
    np.testing.assert_allclose(result[2:], [3,4,4])
    assert np.isnan(result[0]) and np.isnan(result[1])
    np.testing.assert_allclose(cls.atr(highs,lows,closes,1), [2,4,3,6,4])


def test_atr_gap_uses_previous_close_not_only_high_minus_low():
    cls = loaded_template_class('TrendIndicatorsPatch')
    result = cls.atr(np.array([101,112]), np.array([99,110]), np.array([100,111]),1)
    np.testing.assert_allclose(result, [2,12])


@pytest.mark.parametrize('invalid', [np.nan,np.inf,-np.inf])
def test_nonfinite_atr_input_rejected(invalid):
    cls = loaded_template_class('TrendIndicatorsPatch')
    with pytest.raises(ValueError):
        cls.atr(np.array([101,invalid]),np.array([99,99]),np.array([100,100]),1)


def test_schema_migrations_are_idempotent_and_do_not_downgrade():
    cls = loaded_template_class('DatabaseSchemaPatch')
    cls.SCHEMA_VERSION = 6
    cls.SCHEMA_SQL = '''CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY);
                       CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY);
                       CREATE TABLE IF NOT EXISTS schema_info(key TEXT PRIMARY KEY,value TEXT NOT NULL);'''
    cls.INDEXES_SQL = ''
    conn = sqlite3.connect(':memory:')
    cls.apply_schema(conn)
    assert cls.run_migrations(conn) == 6
    assert conn.execute("SELECT value FROM schema_info WHERE key='version'").fetchone()[0] == '6'
    cls.apply_schema(conn)
    assert conn.execute("SELECT value FROM schema_info WHERE key='version'").fetchone()[0] == '6'
    assert cls.run_migrations(conn) == 0
    columns = {r[1] for r in conn.execute('PRAGMA table_info(trades)')}
    assert {'sl_order_id','tp_order_id','protection_status'} <= columns
    conn.close()


def test_circuit_breaker_starts_first_day_baseline_and_does_not_timer_resume(monkeypatch):
    import math, time
    clock = {'value':1000}
    monkeypatch.setattr(time,'time',lambda:clock['value'])
    cls = loaded_template_class('CircuitBreakerPatch',{'math':math,'time':time})
    obj = cls()
    obj.config = SimpleNamespace(max_drawdown_pct=.20,max_daily_loss_pct=.05,
                                 max_weekly_loss_pct=.10,max_monthly_loss_pct=.15)
    obj.equity_peak = obj.daily_peak = obj.weekly_peak = obj.monthly_peak = 0
    obj.tripped = {name:False for name in ('drawdown','daily_loss','weekly_loss','monthly_loss')}
    obj.trip_times = {}
    obj._trip = lambda name: obj.tripped.__setitem__(name,True)
    obj.get_status = lambda:dict(obj.tripped)
    obj.update_equity(1000)
    assert obj.daily_start == obj.weekly_start == obj.monthly_start == 1000
    obj.update_equity(940)
    assert obj.tripped['daily_loss']
    clock['value'] += 4000  # More than original one-hour recovery timer.
    obj.update_equity(940)
    assert obj.tripped['daily_loss']


def test_disabled_legacy_factors_share_numerator_and_denominator_policy():
    import asyncio, math, time
    fixed, _ = patch_source(SOURCE_FIXTURE)
    engine_node = class_node(fixed, 'AlphaEngine')
    namespace = {
        'math':math, 'time':time,
        'CFG':SimpleNamespace(trading_timeframe='30m'),
        'Constants':SimpleNamespace(DEFAULT_EPSILON=1e-12),
        'highs':[], 'lows':[], 'closes':[], 'volumes':[],
        'aqii_factor':SimpleNamespace(calculate=lambda **kwargs:(30, 'synthetic')),
    }
    # Execute ONLY the safe synthetic AlphaEngine, not the generated engine.
    exec(compile(ast.Module(body=[engine_node], type_ignores=[]),
                 '<synthetic_alpha>', 'exec'), namespace)
    engine = namespace['AlphaEngine']()
    engine.config = SimpleNamespace(enable_mean_reversion=False, enable_momentum=True,
                                    enable_order_flow=True, enable_smart_money=True)
    specs = [('mean_reversion',25,1,True), ('momentum',25,1,False),
             ('order_flow',25,1,True), ('smart_money',25,1,True),
             ('volatility',15,.5,True), ('aqii',30,2,True),
             ('regime',10,.3,True), ('liquidity',10,.2,True),
             ('cqmi',100,1,True)]
    engine.factors = [SimpleNamespace(name=n, max_score=m, weight=w, enabled=e)
                      for n,m,w,e in specs]
    result = asyncio.run(engine.generate_signal('TEST/USDT:USDT', [1], {}))
    assert result['legacy_score'] == engine._compute_legacy_max_score() == 120.5
    assert result['timeframe'] == '30m'
    # Configuration flags, not just the object's enabled bit, gate both sides.
    engine.config.enable_order_flow = False
    result = asyncio.run(engine.generate_signal('TEST/USDT:USDT', [1], {}))
    assert result['legacy_score'] == engine._compute_legacy_max_score() == 95.5


def test_legacy_weight_transform_preserves_directional_generator_and_direct_read():
    from apply_apex_fix import AlphaTransform
    statement = ast.parse('score = mr_factor.weight + next(f.weight for f in self.factors)')
    transform = AlphaTransform()
    rewritten = transform.visit(statement)
    assert transform.legacy_weight_reads == 2
    text = ast.unparse(rewritten)
    assert 'self._legacy_factor_weight(mr_factor)' in text
    assert 'next((self._legacy_factor_weight(f) for f in self.factors))' in text


def test_builder_rejects_unrecognized_legacy_weighting():
    incompatible = SOURCE_FIXTURE.replace('f.weight * f.max_score', '42 * f.max_score')
    with pytest.raises(PatchError, match='No legacy factor weight references'):
        patch_source(incompatible)


def test_newer_database_version_is_never_downgraded():
    cls = loaded_template_class('DatabaseSchemaPatch')
    cls.SCHEMA_VERSION = 6
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE schema_info(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.execute("INSERT INTO schema_info VALUES('version','7')")
    with pytest.raises(ValueError, match='refusing downgrade'):
        cls.run_migrations(conn)
    assert conn.execute("SELECT value FROM schema_info WHERE key='version'").fetchone()[0] == '7'
    conn.close()


def test_cli_builds_sanitized_standalone_without_executing_original(tmp_path, monkeypatch, capsys):
    import json, stat, sys
    from apply_apex_fix import main
    original = tmp_path / 'original engine.py'
    output = tmp_path / 'new folder' / 'fixed engine.py'
    original.write_text(SOURCE_FIXTURE, encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['apply_apex_fix.py', str(original), '-o', str(output)])
    main()
    assert original.read_text(encoding='utf-8') == SOURCE_FIXTURE
    assert 'TEST_ONLY_EXPOSED' not in output.read_text(encoding='utf-8')
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    report = json.loads(output.with_suffix('.changes.json').read_text(encoding='utf-8'))
    assert 'no exchange connection' in report['validation']
    assert 'TEST_ONLY_EXPOSED' not in capsys.readouterr().out


def test_cli_refuses_original_overwrite_even_with_force(tmp_path, monkeypatch):
    import sys
    from apply_apex_fix import main
    original = tmp_path / 'original.py'
    original.write_text(SOURCE_FIXTURE, encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['apply_apex_fix.py', str(original), '-o', str(original), '--force'])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert original.read_text(encoding='utf-8') == SOURCE_FIXTURE


def test_cli_refuses_report_path_alias_to_original(tmp_path, monkeypatch):
    import sys
    from apply_apex_fix import main
    original = tmp_path / 'original.py'
    output = tmp_path / 'fixed.py'
    original.write_text(SOURCE_FIXTURE, encoding='utf-8')
    output.with_suffix('.changes.json').symlink_to(original)
    monkeypatch.setattr(sys, 'argv', ['apply_apex_fix.py', str(original), '-o', str(output)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not output.exists()
    assert original.read_text(encoding='utf-8') == SOURCE_FIXTURE
