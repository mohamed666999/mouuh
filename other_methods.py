# Targeted method templates embedded into the original script, not imported.

class TrendIndicatorsPatch:
    @staticmethod
    def atr(highs, lows, closes, period=14):
        """Wilder ATR with an SMA seed; period=1 returns true range."""
        highs = np.asarray(highs, dtype=float)
        lows = np.asarray(lows, dtype=float)
        closes = np.asarray(closes, dtype=float)
        if period < 1 or int(period) != period:
            raise ValueError('ATR period must be a positive integer')
        period = int(period)
        if highs.ndim != 1 or lows.shape != highs.shape or closes.shape != highs.shape:
            raise ValueError('ATR needs equal-length one-dimensional arrays')
        if not (np.all(np.isfinite(highs)) and np.all(np.isfinite(lows)) and np.all(np.isfinite(closes))):
            raise ValueError('ATR received non-finite prices')
        if np.any(highs < lows) or np.any(closes <= 0):
            raise ValueError('ATR received invalid prices')
        result = np.full(len(closes), np.nan, dtype=float)
        if len(closes) == 0:
            return result
        previous = np.concatenate(([closes[0]], closes[:-1]))
        tr = np.maximum.reduce((highs - lows, np.abs(highs - previous), np.abs(lows - previous)))
        if len(closes) < period:
            return result
        result[period - 1] = np.mean(tr[:period])
        for i in range(period, len(closes)):
            result[i] = (result[i - 1] * (period - 1) + tr[i]) / period
        return result


class VolatilityFactorPatch:
    def calculate(self, highs, lows, closes, config):
        if len(closes) < 30:
            return 0.0, ''
        _, upper, lower = VolatilityIndicators.bollinger_bands(closes, 20, 2.0)
        width = float(upper[-1] - lower[-1])
        if not math.isfinite(width) or width <= Constants.DEFAULT_EPSILON:
            return 0.0, ''
        position = float((closes[-1] - lower[-1]) / width)
        if position < 0.1:
            score = min(self.max_score, max(0.0, self.max_score * (0.1 - position) / 0.1))
            return score, f'BB Lower Band Touch ({position:.2f})'
        if position > 0.9:
            score = min(self.max_score, max(0.0, self.max_score * (position - 0.9) / 0.1))
            return score, f'BB Upper Band Touch ({position:.2f})'
        return 0.0, ''


class AlphaEnginePatch:
    def _legacy_factor_weight(self, factor):
        # Shared by the directional numerator and its normalization denominator.
        flags = {
            'mean_reversion': self.config.enable_mean_reversion,
            'momentum': self.config.enable_momentum,
            'order_flow': self.config.enable_order_flow,
            'smart_money': self.config.enable_smart_money,
        }
        if not factor.enabled or not flags.get(factor.name, True):
            return 0.0
        weight = float(factor.weight)
        if not math.isfinite(weight) or weight < 0:
            raise ValueError('Legacy factor weight must be finite and nonnegative')
        return weight

    def _compute_legacy_max_score(self):
        # Liquidity is a quality metric, not a directional contribution in the
        # original numerator. Exclude it from the normalization denominator.
        total = sum(self._legacy_factor_weight(f) * float(f.max_score)
                    for f in self.factors
                    if f.name not in (self.CQMI_FACTOR_NAME, 'liquidity'))
        return max(total, Constants.DEFAULT_EPSILON)


class DatabaseSchemaPatch:
    @classmethod
    def apply_schema(cls, conn):
        # CREATE IF NOT EXISTS does not migrate existing tables. Do NOT replace
        # the version on every launch or re-run old ALTERs on a fresh schema.
        conn.executescript(cls.SCHEMA_SQL)
        conn.executescript(cls.INDEXES_SQL)
        conn.execute('INSERT OR IGNORE INTO schema_info (key, value) VALUES (?, ?)', ('version', '0'))
        conn.commit()

    @classmethod
    def run_migrations(cls, conn):
        row = conn.execute("SELECT value FROM schema_info WHERE key='version'").fetchone()
        current_version = int(row[0]) if row else 0
        if current_version > cls.SCHEMA_VERSION:
            raise ValueError('Database schema is newer than this patch; refusing downgrade')
        migrations = [
            ('trades', 'strategy_name', 'TEXT'),
            ('trades', 'exit_reason', 'TEXT'),
            ('signals', 'strategy', 'TEXT'),
            ('trades', 'sl_order_id', 'TEXT'),
            ('trades', 'tp_order_id', 'TEXT'),
            ('trades', 'protection_status', "TEXT DEFAULT 'NONE'"),
        ]
        applied = 0
        for table, column, declaration in migrations:
            existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
            if column not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {declaration}')
                applied += 1
        conn.execute('INSERT OR REPLACE INTO schema_info (key, value) VALUES (?, ?)',
                     ('version', str(cls.SCHEMA_VERSION)))
        conn.commit()
        return applied


class BinanceAdapterPatch:
    def __init__(self, config):
        self.name = ExchangeName.BINANCE
        self.config = config
        self.exchange = ccxtpro.binanceusdm({
            'apiKey': config.binance_api_key, 'secret': config.binance_secret,
            'enableRateLimit': True,
            'options': {'defaultType': 'future', 'adjustForTimeDifference': True, 'fetchCurrencies': False},
        })
        demo = os.getenv('APEX_USE_DEMO', 'false').lower() in ('true', '1', 'yes')
        if demo:
            self.exchange.enable_demo_trading(True)
        self.ws_base_url = 'wss://demo-fstream.binance.com' if demo else 'wss://fstream.binance.com'
        self.rate_limit_remaining = 0  # Do not pretend to know live remaining weight.
        self.last_request_time = 0.0

    def get_ws_streams(self, symbols):
        streams = []
        for symbol in symbols:
            base, quote = symbol.split(':')[0].split('/')
            market_id = (base + quote).lower()
            streams.extend((market_id + '@aggTrade', market_id + '@depth20@100ms',
                            market_id + '@kline_' + self.config.trading_timeframe,
                            market_id + '@markPrice'))
        return streams


class CircuitBreakerPatch:
    def update_equity(self, equity):
        # Initialize first-period baselines immediately. Keep loss breakers
        # latched while their thresholds still apply, even after a timer.
        if not math.isfinite(equity) or equity < 0:
            raise ValueError('Invalid equity')
        now = time.time()
        if not getattr(self, '_baselines_initialized', False):
            self.daily_start = self.weekly_start = self.monthly_start = equity
            self.last_reset = {'daily': now, 'weekly': now, 'monthly': now}
            self._baselines_initialized = True
        for period, seconds in (('daily', 86400), ('weekly', 604800), ('monthly', 2592000)):
            if now - self.last_reset[period] >= seconds:
                setattr(self, period + '_start', equity)
                setattr(self, period + '_peak', equity)
                self.last_reset[period] = now
                breaker = period + '_loss'
                self.tripped[breaker] = False
                self.trip_times.pop(breaker, None)
        self.equity_peak = max(self.equity_peak, equity)
        for period in ('daily', 'weekly', 'monthly'):
            setattr(self, period + '_peak', max(getattr(self, period + '_peak'), equity))
        if self.equity_peak > 0 and (self.equity_peak - equity) / self.equity_peak > self.config.max_drawdown_pct:
            self._trip('drawdown')
        for period, threshold in (('daily', self.config.max_daily_loss_pct),
                                  ('weekly', self.config.max_weekly_loss_pct),
                                  ('monthly', self.config.max_monthly_loss_pct)):
            start = getattr(self, period + '_start')
            if start > 0 and (equity - start) / start < -threshold:
                self._trip(period + '_loss')
        # Drawdown reset is manual; time passing does not erase its loss.
        return self.get_status()


def _verify_credentials_loaded():
    valid, errors = CFG.validate_all()
    if not valid:
        print('Configuration invalid: ' + '; '.join(errors), file=sys.stderr)
        return False
    if CFG.trading_timeframe != '30m':
        print('This patch is fixed to the requested 30m timeframe.', file=sys.stderr)
        return False
    if not HAS_SCIPY:
        print('CQMI requires SciPy. Install the included requirements.', file=sys.stderr)
        return False
    if CFG.primary_exchange != ExchangeName.BINANCE:
        print('This execution patch supports Binance USD-M only.', file=sys.stderr)
        return False
    if CFG.dry_run:
        print('DRY RUN: no exchange entry / SL / TP / close orders will be sent.')
        return True
    if os.getenv('APEX_ALLOW_LIVE_TRADING') != 'I_UNDERSTAND_THE_RISK':
        print('Orders disabled: explicit APEX_ALLOW_LIVE_TRADING opt-in is missing.', file=sys.stderr)
        return False
    if not CFG.binance_api_key or not CFG.binance_secret:
        print('Binance credentials missing; use environment variables or an uncommitted .env.', file=sys.stderr)
        return False
    print('Credentials present (not displayed). Independently verify permissions, IP restrictions and demo/live environment.')
    return True
