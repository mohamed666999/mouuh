# Embedded by apply_apex_fix.py AFTER the original Order/OrderManager classes.
# Requires the core.py block immediately above it, plus original script globals.

class ExecutionEngine:
    """30m patch: actual Binance Algo SL/TP, verified once at entry.

    Live requests require explicit opt-in. Startup reconciliation completes
    before scanning. Uncertainty latches new entries off, not position reads.
    Stops are not trailed, repriced, or recreated by the normal monitor.
    """

    def __init__(self, config=None, *, exchange=None, database=None, master_config=None):
        self.master_config = master_config or CFG
        self.config = config or self.master_config.execution
        self.database = database or db
        self.exchange = exchange if exchange is not None else ccxtpro.binanceusdm({
            'apiKey': self.master_config.binance_api_key,
            'secret': self.master_config.binance_secret,
            'enableRateLimit': True,
            'timeout': 10000,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True,
                'fetchCurrencies': False,
            },
        })
        self.demo = os.getenv('APEX_USE_DEMO', 'false').lower() in ('true', '1', 'yes')
        if self.demo and exchange is None:
            # Must be enabled before the first request. Use separate demo keys.
            self.exchange.enable_demo_trading(True)
        self.api = ApexPatchAlgoService(self.exchange)
        self.order_manager = OrderManager()
        self._entry_lock = asyncio.Lock()
        self._critical_tasks = set()
        self.halted = False
        self.halt_reason = ''
        self.startup_ready = False
        self.execution_stats = {
            'total_orders': 0, 'successful_orders': 0, 'failed_orders': 0,
            'total_slippage_bps': 0.0, 'avg_slippage_bps': 0.0, 'total_fees': 0.0,
        }

    def _live_permission(self):
        if self.master_config.dry_run:
            return
        if os.getenv('APEX_ALLOW_LIVE_TRADING') != 'I_UNDERSTAND_THE_RISK':
            raise ApexPatchSafetyError('Live/demo order placement requires APEX_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK')
        if not self.master_config.binance_api_key or not self.master_config.binance_secret:
            raise ApexPatchSafetyError('Live/demo credentials are missing')

    async def _event(self, kind, message, details=None, severity='INFO'):
        return await self.database.system_events.log_event(
            event_type=kind, severity=severity, message=message,
            source='execution_30m_patch', details=details or {},
        )

    async def latch_halt(self, reason):
        self.halted = True
        self.halt_reason = str(reason)
        exec_logger.critical('APEX ENTRY HALT | %s', self.halt_reason)
        try:
            await self._event('APEX_SAFETY_HALT', self.halt_reason, severity='CRITICAL')
        except Exception:
            exec_logger.exception('Unable to persist safety halt; manual inspection required')

    async def initialize(self):
        if self.master_config.dry_run:
            await self.exchange.load_markets()
        else:
            self._live_permission()
            await self.api.initialize()

    async def _update_trade(self, trade_id, **fields):
        allowed = {'sl_order_id', 'tp_order_id', 'protection_status', 'metadata',
                   'qty', 'sl_price', 'tp_price', 'risk_pct', 'fees_usdt', 'order_id'}
        if not fields or any(key not in allowed for key in fields):
            raise ApexPatchSafetyError('Invalid trade update fields')
        names = list(fields)
        await self.database.db.execute(
            'UPDATE trades SET ' + ', '.join(name + ' = ?' for name in names) + ' WHERE id = ?',
            (*[fields[name] for name in names], trade_id),
        )

    @staticmethod
    def _metadata(pos):
        metadata = pos.get('metadata') or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (ValueError, TypeError):
                raise ApexPatchSafetyError('Invalid stored trade metadata')
        if not isinstance(metadata, dict):
            raise ApexPatchSafetyError('Invalid trade metadata type')
        return dict(metadata)

    @staticmethod
    def _bracket(side, reference_entry, sl, tp):
        reference_entry = apex_patch_number(reference_entry, positive=True)
        sl = apex_patch_number(sl, positive=True)
        tp = apex_patch_number(tp, positive=True)
        if side == 'LONG' and not sl < reference_entry < tp:
            raise ApexPatchSafetyError('LONG requires SL < entry < TP')
        if side == 'SHORT' and not tp < reference_entry < sl:
            raise ApexPatchSafetyError('SHORT requires TP < entry < SL')
        if side not in ('LONG', 'SHORT'):
            raise ApexPatchSafetyError('Unknown direction')
        return reference_entry, sl, tp

    async def _new_trade(self, symbol, side, entry, qty, sl, tp, leverage,
                         alpha, risk_pct, metadata, order_id='', status='PENDING'):
        trade_id = await self.database.trades.save_trade({
            'symbol': symbol, 'side': side, 'entry': entry, 'qty': qty,
            'sl': sl, 'tp': tp, 'leverage': leverage, 'alpha': alpha,
            'risk_pct': risk_pct, 'timeframe': self.master_config.trading_timeframe,
            'exchange': 'binance_demo' if self.demo else 'binance',
            'order_id': order_id, 'protection_status': status, 'metadata': metadata,
        })
        return {
            'id': trade_id, 'symbol': symbol, 'side': side, 'entry_price': entry,
            'qty': qty, 'sl_price': sl, 'tp_price': tp, 'leverage': leverage,
            'risk_pct': risk_pct, 'sl_order_id': '', 'tp_order_id': '',
            'metadata': json.dumps(metadata), 'protection_status': status,
        }

    async def _protect_one(self, pos, leg, hedged):
        """Persist attempt identity BEFORE POST; reuse it for read-only recovery."""
        self._live_permission()
        metadata = self._metadata(pos)
        client_key = leg + '_client_id'
        attempt_key = leg + '_attempted'
        client_id = metadata.get(client_key)
        if not client_id:
            client_id = 'qa-' + leg + '-' + uuid.uuid4().hex[:24]
            metadata[client_key] = client_id
        order_type = 'STOP_MARKET' if leg == 'sl' else 'TAKE_PROFIT_MARKET'
        spec = self.api.spec(pos['symbol'], pos['side'], order_type,
                             pos[leg + '_price'], client_id, hedged)
        if metadata.get(attempt_key):
            # Prior process/request may have submitted this already. Never
            # blindly issue a second POST on a restart or a network timeout.
            row = await self.api.verify(spec, str(pos.get(leg + '_order_id') or ''))
            if row is None:
                raise ApexPatchProtectionUnconfirmed('Prior protection attempt is unresolved',
                                                    str(pos.get(leg + '_order_id') or ''), client_id)
        else:
            metadata[attempt_key] = True
            pos['metadata'] = json.dumps(metadata)
            await self._update_trade(pos['id'], metadata=pos['metadata'])
            row = await self.api.place_once(spec)
        algo_id = str(row['algoId'])
        pos[leg + '_order_id'] = algo_id
        await self._update_trade(pos['id'], **{leg + '_order_id': algo_id})
        exec_logger.info('%s VERIFIED | %s %s | algoId=%s | triggerPrice=%s | positionSide=%s',
                         leg.upper(), pos['symbol'], pos['side'], algo_id,
                         spec.trigger_price, spec.position_side)
        return algo_id

    async def cleanup_known_protection(self, pos):
        metadata = self._metadata(pos)
        for leg in ('sl', 'tp'):
            await self.api.cancel_known(
                pos['symbol'], algo_id=str(pos.get(leg + '_order_id') or ''),
                client_id=str(metadata.get(leg + '_client_id') or ''),
            )

    async def _de_risk_unprotected(self, symbol, side, pos=None):
        try:
            flattened = await self.api.flatten(symbol, side)
            if not flattened:
                await self.latch_halt('UNPROTECTED POSITION STILL OPEN: ' + symbol + ' ' + side)
                return False
            if pos:
                await self.cleanup_known_protection(pos)
                await self._update_trade(pos['id'], protection_status='FLAT_AFTER_PROTECTION_FAILURE')
            exec_logger.warning('FLAT VERIFIED after protection failure | %s %s', symbol, side)
            return True
        except Exception as exc:
            await self.latch_halt('Flatness/cleanup UNCONFIRMED for ' + symbol + ': ' + str(exc))
            return False

    async def execute_order(self, symbol, side, qty, leverage, sl, tp, *,
                            reference_entry=None, alpha_score=0.0, risk_pct=0.0,
                            risk_equity=None):
        task = asyncio.create_task(self._execute_order_impl(
            symbol, side, qty, leverage, sl, tp, reference_entry=reference_entry,
            alpha_score=alpha_score, risk_pct=risk_pct, risk_equity=risk_equity,
        ))
        self._critical_tasks.add(task)
        task.add_done_callback(self._critical_tasks.discard)
        return await asyncio.shield(task)

    async def _execute_order_impl(self, symbol, side, qty, leverage, sl, tp, *,
                                 reference_entry=None, alpha_score=0.0, risk_pct=0.0,
                                 risk_equity=None):
        order = Order(symbol=symbol, side='buy' if side == 'LONG' else 'sell',
                      order_type='market', quantity=qty, leverage=leverage)
        order._protection = {'sl_order_id': '', 'tp_order_id': '', 'protection_status': 'NONE'}
        pos = None
        entry_sent = False
        sl_confirmed = False
        async with self._entry_lock:
            try:
                if self.halted or not self.startup_ready:
                    raise ApexPatchSafetyError('Entries blocked: startup incomplete or safety halt: ' + self.halt_reason)
                reference_entry, sl, tp = self._bracket(side, reference_entry, sl, tp)
                qty = self.api.amount(symbol, qty)
                leverage = int(leverage)
                if leverage < 1 or leverage > self.master_config.risk.leverage_tiers.get(0.20, 50):
                    raise ApexPatchSafetyError('Invalid/unsupported leverage')
                market = self.api.market(symbol)
                minimum_cost = (market.get('limits') or {}).get('cost', {}).get('min')
                if minimum_cost is not None and qty * reference_entry < float(minimum_cost):
                    raise ApexPatchSafetyError('Notional below exchange minimum; position is not rounded up')
                await self.order_manager.submit_order(order)
                self.execution_stats['total_orders'] += 1
                if self.master_config.dry_run:
                    ticker = await self.exchange.fetch_ticker(symbol)
                    entry = apex_patch_number(ticker.get('last'), positive=True)
                    stop_distance, tp_distance = abs(reference_entry - sl), abs(tp - reference_entry)
                    sl = float(self.api.price(symbol, entry - stop_distance if side == 'LONG' else entry + stop_distance))
                    tp = float(self.api.price(symbol, entry + tp_distance if side == 'LONG' else entry - tp_distance))
                    self._bracket(side, entry, sl, tp)
                    pos = await self._new_trade(symbol, side, entry, qty, sl, tp, leverage,
                                                alpha_score, risk_pct, {'dry_run': True}, status='DRY_RUN')
                    order.avg_fill_price, order.filled_qty = entry, qty
                    order._protection['protection_status'] = 'DRY_RUN'
                else:
                    self._live_permission()
                    hedged = await self.api.hedged()
                    # Prevent adding to a manual/previous leg and prevent an old
                    # sibling stop from attaching to a newly opened position.
                    if await self.api.positions(symbol):
                        raise ApexPatchSafetyError('Existing live position on this symbol; no duplicate entry')
                    if await self.api.open_algos(symbol) or await self.api.normal_open_orders(symbol):
                        raise ApexPatchSafetyError('Existing orders on this symbol; inspect them before a new entry')
                    all_live = await self.api.positions()
                    if len(all_live) >= self.master_config.risk.max_open_positions:
                        raise ApexPatchSafetyError('Live position-count cap reached')
                    risk_equity = apex_patch_number(risk_equity, positive=True)
                    if qty * abs(reference_entry - sl) / risk_equity > self.master_config.risk.max_kelly_pct + 1e-9:
                        raise ApexPatchSafetyError('Pre-entry single-position risk cap exceeded')
                    if qty * reference_entry / leverage > risk_equity * self.master_config.risk.max_position_size_pct + 1e-9:
                        raise ApexPatchSafetyError('Pre-entry margin-per-position cap exceeded')
                    current_rows = await self.database.trades.get_open_positions()
                    local_keys = {(p['symbol'], p['side']) for p in current_rows}
                    if {(p.symbol, p.side) for p in all_live} - local_keys:
                        raise ApexPatchSafetyError('Untracked live exposure; new entries blocked')
                    current_risk = sum(float(p['qty']) * abs(float(p['entry_price']) - float(p['sl_price']))
                                       for p in current_rows)
                    if (current_risk + qty * abs(reference_entry - sl)) / risk_equity > self.master_config.risk.max_portfolio_risk_pct + 1e-9:
                        raise ApexPatchSafetyError('Prospective portfolio stop-risk cap exceeded')
                    mark = await self.api.mark_price(symbol)
                    self._bracket(side, mark, sl, tp)
                    if abs(mark - reference_entry) / reference_entry * 10000 > self.config.max_slippage_bps:
                        raise ApexPatchSafetyError('Mark drift exceeds pre-entry slippage limit')
                    entry_client_id = 'qa-entry-' + uuid.uuid4().hex[:24]
                    details = {
                        'client_id': entry_client_id, 'symbol': symbol, 'side': side,
                        'qty': qty, 'reference_entry': reference_entry, 'sl': sl, 'tp': tp,
                        'leverage': leverage, 'alpha': alpha_score, 'risk_pct': risk_pct,
                        'timeframe': self.master_config.trading_timeframe,
                    }
                    # The journal survives even if the market-order reply is lost.
                    await self.exchange.set_leverage(leverage, symbol)
                    await self._event('APEX_ENTRY_INTENT', 'Entry planned', details)
                    params = {'newClientOrderId': entry_client_id, 'newOrderRespType': 'RESULT',
                              'positionSide': side if hedged else 'BOTH'}
                    entry_sent = True
                    response = None
                    try:
                        response = await self.exchange.create_order(symbol, 'market', order.side, qty, None, params)
                    except Exception as exc:
                        exec_logger.error('Entry reply lost/rejected; querying the same client ID, never resubmitting: %s', exc)
                    raw = None
                    for attempt in range(self.api.read_attempts):
                        try:
                            raw = await self.api.query_entry(symbol, entry_client_id)
                            if str(raw.get('status', '')).upper() in ('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'):
                                break
                        except ApexPatchStateUnknown:
                            pass
                        if attempt + 1 < self.api.read_attempts:
                            await asyncio.sleep(self.api.read_delay)
                    if raw is None or str(raw.get('status', '')).upper() not in ('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'):
                        raise ApexPatchStateUnknown('Entry acceptance/final quantity unresolved; manual intervention required')
                    live = None
                    for attempt in range(self.api.read_attempts):
                        live = await self.api.position(symbol, side)
                        if live is not None:
                            break
                        if attempt + 1 < self.api.read_attempts:
                            await asyncio.sleep(self.api.read_delay)
                    if live is None:
                        await self._event('APEX_ENTRY_RESOLVED', 'Terminal entry, no live position', {'client_id': entry_client_id})
                        raise ApexPatchSafetyError('No live filled position after terminal entry response')
                    actual_qty, actual_entry = live.quantity, live.entry
                    if actual_qty > qty + max(1e-12, qty * 1e-9):
                        raise ApexPatchSafetyError('Live size exceeds requested size; concurrent account activity detected')
                    # Compute final protective levels once around the ACTUAL fill.
                    stop_distance, tp_distance = abs(reference_entry - sl), abs(tp - reference_entry)
                    sl = float(self.api.price(symbol, actual_entry - stop_distance if side == 'LONG' else actual_entry + stop_distance))
                    tp = float(self.api.price(symbol, actual_entry + tp_distance if side == 'LONG' else actual_entry - tp_distance))
                    self._bracket(side, await self.api.mark_price(symbol), sl, tp)
                    actual_risk = actual_qty * abs(actual_entry - sl)
                    if risk_equity is None:
                        raise ApexPatchSafetyError('No current equity supplied for post-fill risk verification')
                    risk_equity = apex_patch_number(risk_equity, positive=True)
                    if actual_risk / risk_equity > self.master_config.risk.max_kelly_pct + 1e-9:
                        raise ApexPatchSafetyError('Actual stop loss exceeds the configured single-position risk cap')
                    actual_risk_pct = actual_risk / risk_equity
                    metadata = {
                        'entry_client_id': entry_client_id, 'dry_run': False,
                        'sl_client_id': 'qa-sl-' + uuid.uuid4().hex[:24],
                        'tp_client_id': 'qa-tp-' + uuid.uuid4().hex[:24],
                        'sl_attempted': False, 'tp_attempted': False,
                        'reference_entry': reference_entry,
                        'entry_fees_confirmed': bool((response or {}).get('fee') and ((response or {}).get('fee') or {}).get('currency') == 'USDT'),
                    }
                    # Save BEFORE protection, not in the scanner after returning.
                    pos = await self._new_trade(symbol, side, actual_entry, actual_qty, sl, tp, leverage,
                                                alpha_score, actual_risk_pct, metadata,
                                                order_id=str(raw.get('orderId') or ''))
                    await self._event('APEX_ENTRY_RESOLVED', 'Live entry tracked in database', {'client_id': entry_client_id})
                    order.avg_fill_price, order.filled_qty = actual_entry, actual_qty
                    order.exchange_order_id = str(raw.get('orderId') or '')
                    fee = (response or {}).get('fee') or {}
                    order.fees = float(fee.get('cost') or 0)
                    await self._update_trade(pos['id'], fees_usdt=order.fees)
                    sl_id = await self._protect_one(pos, 'sl', hedged)
                    sl_confirmed = True
                    pos['sl_order_id'] = sl_id
                    status = 'PROTECTED'
                    try:
                        tp_id = await self._protect_one(pos, 'tp', hedged)
                        pos['tp_order_id'] = tp_id
                    except Exception as exc:
                        status = 'SL_ONLY_NO_TP'
                        # Record uncertain IDs too so a later cleanup is targeted.
                        if isinstance(exc, ApexPatchProtectionUnconfirmed) and exc.algo_id:
                            pos['tp_order_id'] = exc.algo_id
                            await self._update_trade(pos['id'], tp_order_id=exc.algo_id)
                        await self.latch_halt('TP not confirmed for ' + symbol + '; SL retained: ' + str(exc))
                    await self._update_trade(pos['id'], protection_status=status)
                    order._protection.update(sl_order_id=pos['sl_order_id'],
                                             tp_order_id=pos['tp_order_id'], protection_status=status)
                    # If SL fired during setup, cancel the sibling; do not label
                    # a flat position as an ongoing successful protected entry.
                    if await self.api.position(symbol, side) is None:
                        await self.cleanup_known_protection(pos)
                        raise ApexPatchSafetyError('Position closed while protection was being installed')
                order.status = OrderStatus.FILLED
                order.filled_at = time.time()
                order._protection.update(trade_id=pos['id'], sl=sl, tp=tp)
                await self.order_manager.update_order(
                    order.id, status=order.status, avg_fill_price=order.avg_fill_price,
                    filled_qty=order.filled_qty, exchange_order_id=order.exchange_order_id, fees=order.fees,
                )
                self.execution_stats['successful_orders'] += 1
                self.execution_stats['total_fees'] += order.fees
                trade_logger.info('ORDER_FILLED | %s', order.to_dict())
                return True, order
            except Exception as exc:
                if entry_sent:
                    await self.latch_halt('Post-entry failure for ' + symbol + ': ' + str(exc))
                    if not sl_confirmed:
                        if isinstance(exc, ApexPatchProtectionUnconfirmed) and pos:
                            pos['sl_order_id'] = exc.algo_id
                            try:
                                await self._update_trade(pos['id'], sl_order_id=exc.algo_id)
                            except Exception:
                                exec_logger.exception('Could not record uncertain SL ID')
                        flattened = await self._de_risk_unprotected(symbol, side, pos)
                        order._protection['protection_status'] = ('ENTRY_UNRESOLVED' if isinstance(exc, ApexPatchStateUnknown) and pos is None
                                                               else ('FLAT_VERIFIED' if flattened else 'UNPROTECTED_OR_UNKNOWN'))
                order.status = OrderStatus.ERROR
                order.error = str(exc)
                await self.order_manager.update_order(order.id, status=OrderStatus.ERROR, error=order.error)
                self.execution_stats['failed_orders'] += 1
                exec_logger.error('EXECUTION FAILED | %s %s | %s', symbol, side, order.error)
                return False, order

    def _covers(self, row, pos, leg, hedged, live_qty):
        """Accept a stored close-all order or a genuinely covering reduce order."""
        try:
            order_type = 'STOP_MARKET' if leg == 'sl' else 'TAKE_PROFIT_MARKET'
            correct = (
                str(row.get('algoStatus', '')).upper() == 'NEW'
                and row.get('symbol') == self.api.market(pos['symbol'])['id']
                and row.get('side') == ('SELL' if pos['side'] == 'LONG' else 'BUY')
                and row.get('positionSide') == (pos['side'] if hedged else 'BOTH')
                and row.get('orderType', row.get('type')) == order_type
                and row.get('workingType') == 'MARK_PRICE'
                and Decimal(str(row.get('triggerPrice'))) == Decimal(self.api.price(pos['symbol'], pos[leg + '_price']))
            )
            if not correct or not row.get('algoId'):
                return False
            if apex_patch_bool(row.get('closePosition')):
                return True
            return (apex_patch_number(row.get('quantity'), positive=True) >= live_qty
                    and (hedged or apex_patch_bool(row.get('reduceOnly'))))
        except (ApexPatchSafetyError, InvalidOperation, ValueError, TypeError):
            return False

    async def verify_startup_protection(self, pos):
        if self.master_config.dry_run:
            return True
        live = await self.api.position(pos['symbol'], pos['side'])
        if live is None:
            return True  # Monitor reconciles this from a successful snapshot.
        hedged = await self.api.hedged()
        rows = await self.api.open_algos(pos['symbol'])
        for leg in ('sl', 'tp'):
            known_id = str(pos.get(leg + '_order_id') or '')
            metadata = self._metadata(pos)
            matching = [row for row in rows
                        if self._covers(row, pos, leg, hedged, live.quantity)
                        and ((known_id and str(row.get('algoId')) == known_id)
                             or str(row.get('clientAlgoId', '')).startswith('qa-' + leg + '-'))]
            if len(matching) > 1:
                raise ApexPatchSafetyError('Duplicate protective orders require manual reconciliation')
            if matching:
                pos[leg + '_order_id'] = str(matching[0]['algoId'])
                await self._update_trade(pos['id'], **{leg + '_order_id': pos[leg + '_order_id']})
                continue  # Verified from Binance, never from cached PROTECTED.
            # Do not add another order next to a different/manual protection.
            same_leg = [row for row in rows
                        if row.get('side') == ('SELL' if pos['side'] == 'LONG' else 'BUY')
                        and row.get('positionSide') == (pos['side'] if hedged else 'BOTH')
                        and row.get('orderType', row.get('type')) == ('STOP_MARKET' if leg == 'sl' else 'TAKE_PROFIT_MARKET')]
            if same_leg:
                raise ApexPatchSafetyError('Conflicting existing protection; inspect Binance instead of creating duplicates')
            mark = await self.api.mark_price(pos['symbol'])
            self._bracket(pos['side'], mark, pos['sl_price'], pos['tp_price'])
            if metadata.get(leg + '_attempted'):
                # Reuse the prior identity for recovery; uncertain attempts
                # require human inspection rather than repeated submissions.
                await self._protect_one(pos, leg, hedged)
            else:
                await self._protect_one(pos, leg, hedged)
            rows = await self.api.open_algos(pos['symbol'])
        await self._update_trade(pos['id'], protection_status='PROTECTED')
        return True

    async def _adopt_position(self, live, intent=None):
        if intent:
            reference = float(intent['reference_entry'])
            stop_distance = abs(reference - float(intent['sl']))
            tp_distance = abs(float(intent['tp']) - reference)
            anchor = live.entry
        else:
            candles = await self.exchange.fetch_ohlcv(live.symbol, self.master_config.trading_timeframe, limit=101)
            candles = apex_patch_closed_candles(candles)
            if len(candles) < 50:
                raise ApexPatchSafetyError('Not enough completed 30m candles to adopt orphan')
            highs = np.asarray([c[2] for c in candles], dtype=float)
            lows = np.asarray([c[3] for c in candles], dtype=float)
            closes = np.asarray([c[4] for c in candles], dtype=float)
            atr = float(TrendIndicators.atr(highs, lows, closes, 14)[-1])
            if not math.isfinite(atr) or atr <= 0:
                raise ApexPatchSafetyError('Invalid orphan ATR')
            # Old entries may be far behind current price: protect around the
            # CURRENT mark, not a stale price that would instantly reject.
            anchor = await self.api.mark_price(live.symbol)
            stop_distance = atr * self.master_config.risk.default_stop_atr_multiple
            tp_distance = atr * self.master_config.risk.default_take_profit_atr_multiple
        sl = float(self.api.price(live.symbol, anchor - stop_distance if live.side == 'LONG' else anchor + stop_distance))
        tp = float(self.api.price(live.symbol, anchor + tp_distance if live.side == 'LONG' else anchor - tp_distance))
        self._bracket(live.side, await self.api.mark_price(live.symbol), sl, tp)
        metadata = {'orphan': True, 'dry_run': False, 'protection_anchor': anchor,
                    'entry_client_id': (intent or {}).get('client_id', ''),
                    'sl_attempted': False, 'tp_attempted': False}
        pos = await self._new_trade(live.symbol, live.side, live.entry, live.quantity,
                                   sl, tp, live.leverage, 0.0, 0.0, metadata)
        await self.verify_startup_protection(pos)
        return pos

    async def startup_checks(self):
        """Resolve pending entries, verify DB legs, then allow the scanner."""
        await self.initialize()
        if self.master_config.dry_run:
            self.startup_ready = True
            return
        pending = {}
        records = await self.database.db.query(
            "SELECT event_type, details FROM system_events WHERE event_type IN ('APEX_ENTRY_INTENT', 'APEX_ENTRY_RESOLVED') ORDER BY id"
        )
        for record in records:
            details = json.loads(record.get('details') or '{}')
            client_id = details.get('client_id')
            if not client_id:
                continue
            if record['event_type'] == 'APEX_ENTRY_INTENT':
                pending[client_id] = details
            else:
                pending.pop(client_id, None)
        positions = await self.api.positions()
        tracked = await self.database.trades.get_open_positions()
        seen = set()
        for pos in tracked:
            key = (pos['symbol'], pos['side'])
            if key in seen:
                await self.latch_halt('Duplicate local position rows: ' + str(key))
            seen.add(key)
        for client_id, intent in pending.items():
            try:
                raw = await self.api.query_entry(intent['symbol'], client_id)
                if str(raw.get('status', '')).upper() not in ('FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'):
                    raise ApexPatchStateUnknown('Prior entry is not terminal')
                live = next((p for p in positions if (p.symbol, p.side) == (intent['symbol'], intent['side'])), None)
                if live and (live.symbol, live.side) not in seen:
                    pos = await self._adopt_position(live, intent)
                    tracked.append(pos)
                    seen.add((live.symbol, live.side))
                await self._event('APEX_ENTRY_RESOLVED', 'Pending entry reconciled at startup', {'client_id': client_id})
            except Exception as exc:
                await self.latch_halt('Pending entry requires manual verification: ' + client_id + ': ' + str(exc))
        allow_orphans = os.getenv('APEX_ADOPT_ORPHANS', 'false').lower() in ('true', '1', 'yes')
        for live in positions:
            if (live.symbol, live.side) not in seen:
                if not allow_orphans:
                    await self.latch_halt('Untracked Binance position; protect manually or explicitly set APEX_ADOPT_ORPHANS=true: ' + live.symbol)
                else:
                    try:
                        pos = await self._adopt_position(live)
                        tracked.append(pos)
                        seen.add((live.symbol, live.side))
                    except Exception as exc:
                        await self.latch_halt('Orphan protection not confirmed: ' + str(exc))
        for pos in tracked:
            try:
                await self.verify_startup_protection(pos)
            except Exception as exc:
                await self.latch_halt('Startup protection not confirmed for ' + pos['symbol'] + ': ' + str(exc))
        self.startup_ready = True  # halted remains latched if ANY check failed.

    async def inspect_protection(self, pos, live):
        """Read-only runtime check. Missing/unknown SL blocks new entries."""
        hedged = await self.api.hedged()
        rows = await self.api.open_algos(pos['symbol'])
        for leg in ('sl', 'tp'):
            algo_id = str(pos.get(leg + '_order_id') or '')
            if not algo_id or not any(str(row.get('algoId')) == algo_id
                                    and self._covers(row, pos, leg, hedged, live.quantity) for row in rows):
                if await self.api.position(pos['symbol'], pos['side']) is None:
                    return True  # Trigger raced this read; next pass reconciles the flat leg.
                await self._update_trade(pos['id'], protection_status='PROTECTION_UNCONFIRMED')
                await self.latch_halt(leg.upper() + ' not confirmed for ' + pos['symbol'] + '; no automatic recreation')
                return False
        return True

    async def close_position(self, symbol, side, qty):
        if self.master_config.dry_run:
            return True
        self._live_permission()
        try:
            if not await self.api.flatten(symbol, side):
                await self.latch_halt('Position close not confirmed for ' + symbol)
                return False
            for pos in await self.database.trades.get_open_positions():
                if pos['symbol'] == symbol and pos['side'] == side:
                    await self.cleanup_known_protection(pos)
            return True
        except Exception as exc:
            await self.latch_halt('Close state UNKNOWN for ' + symbol + ': ' + str(exc))
            return False

    async def cancel_all_orders(self, symbol=None):
        raise ApexPatchSafetyError('Bulk account cancellation intentionally disabled; cancel explicitly owned algo IDs only')

    async def close(self):
        self.halted = True
        self.startup_ready = False
        pending = tuple(self._critical_tasks)
        if pending:
            exec_logger.warning('Waiting for %s submitted entry/protection task(s) before shutdown', len(pending))
            await asyncio.gather(*pending, return_exceptions=True)
        await self.exchange.close()

    def get_execution_stats(self):
        return {**self.execution_stats, 'halted': self.halted,
                'halt_reason': self.halt_reason, 'startup_ready': self.startup_ready}
