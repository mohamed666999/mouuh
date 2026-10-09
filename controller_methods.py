# Method templates; the patcher replaces only these methods in ApexController.

class ApexControllerPatch:
    async def _refresh_equity(self):
        if CFG.dry_run:
            realized = await db.trades.get_total_pnl()
            equity = CFG.backtest.initial_capital + realized
            free = equity
        else:
            balance = await self.execution_engine.exchange.fetch_balance()
            info = balance.get('info') or {}
            quote = balance.get('USDT') or {}
            raw_equity = info.get('totalMarginBalance') if isinstance(info, dict) else None
            if raw_equity is None:
                # Require an explicit quote balance, never keep stale equity.
                raw_equity = quote.get('total')
            raw_free = info.get('availableBalance') if isinstance(info, dict) else None
            if raw_free is None:
                raw_free = quote.get('free')
            equity = apex_patch_number(raw_equity)
            free = apex_patch_number(raw_free)
            if equity < 0 or free < 0:
                raise ApexPatchSafetyError('Negative equity/available balance')
        self.risk_manager.update_equity(equity)
        self._available_margin = free
        return equity, free

    async def scanner_loop(self):
        logger.info('30m scanner | CQMI 50%% / other directional factors 50%% | closed candles only')
        last_bar = {}
        while self.is_running:
            try:
                if not self.execution_engine.startup_ready or self.execution_engine.halted:
                    await asyncio.sleep(30)
                    continue
                equity, free = await self._refresh_equity()
                if equity <= 0:
                    await asyncio.sleep(30)
                    continue
                for symbol in CFG.watchlist:
                    if not self.is_running or self.execution_engine.halted:
                        break
                    try:
                        await asyncio.sleep(0.25)
                        candles = await self.execution_engine.exchange.fetch_ohlcv(
                            symbol, CFG.trading_timeframe, limit=CFG.alpha.signal_lookback_candles + 1,
                        )
                        candles = apex_patch_closed_candles(candles)[-CFG.alpha.signal_lookback_candles:]
                        if len(candles) < max(60, CFG.alpha.min_candles_for_analysis):
                            continue
                        bar = int(candles[-1][0])
                        if last_bar.get(symbol) == bar:
                            continue
                        engine = self.engines[symbol]
                        book = engine.order_book.get_snapshot(1)
                        recent_trades = engine.trade_processor.get_recent_trades(50)
                        now = time.time()
                        # Empty/stale microstructure is unavailable, not neutral.
                        if (not book.bids or not book.asks or book.spread() <= 0
                                or now - engine.order_book.last_update_ts > 15
                                or len(recent_trades) < 50
                                or now - recent_trades[-1].timestamp > 15):
                            continue
                        last_bar[symbol] = bar
                        generated = await self.alpha_engine.generate_signal(symbol, candles, engine)
                        generated['timeframe'] = CFG.trading_timeframe
                        if generated['action'] not in ('LONG', 'SHORT'):
                            continue
                        if generated['score'] < CFG.alpha.min_alpha_score_to_execute:
                            continue
                        signal_id = await db.signals.save_signal(generated)
                        # REFRESH per candidate, not once per entire watchlist.
                        open_positions = await db.trades.get_open_positions()
                        self.risk_manager.open_positions_count = len(open_positions)
                        if any(p['symbol'] == symbol for p in open_positions):
                            continue
                        if not self.risk_manager.check_portfolio_risk(open_positions):
                            continue
                        equity, free = await self._refresh_equity()
                        if equity <= 0 or self.execution_engine.halted:
                            continue
                        entry = float(candles[-1][4])
                        highs = np.asarray([c[2] for c in candles], dtype=float)
                        lows = np.asarray([c[3] for c in candles], dtype=float)
                        closes = np.asarray([c[4] for c in candles], dtype=float)
                        # Full history plus Wilder smoothing, not a fresh 14-bar EMA.
                        atr = float(TrendIndicators.atr(highs, lows, closes, 14)[-1])
                        if not math.isfinite(atr) or atr <= 0:
                            continue
                        atr = max(atr, entry * 0.0001)
                        side = generated['action']
                        sl = entry - atr * CFG.risk.default_stop_atr_multiple if side == 'LONG' else entry + atr * CFG.risk.default_stop_atr_multiple
                        tp = entry + atr * CFG.risk.default_take_profit_atr_multiple if side == 'LONG' else entry - atr * CFG.risk.default_take_profit_atr_multiple
                        self.execution_engine._bracket(side, entry, sl, tp)
                        reward_risk = abs(tp - entry) / abs(entry - sl)
                        if reward_risk + 1e-9 < CFG.risk.min_risk_reward_ratio:
                            continue
                        qty, risk_pct, leverage = self.risk_manager.calculate_position_size(generated['score'], entry, sl)
                        qty = self.execution_engine.api.amount(symbol, qty)
                        risk_pct = qty * abs(entry - sl) / equity
                        if qty * entry < CFG.risk.min_position_size_usdt:
                            continue
                        passed, reason = self.risk_manager.check_position_risk(qty, entry, sl, leverage)
                        if not passed:
                            logger.warning('Risk block | %s | %s', symbol, reason)
                            continue
                        # Recalculate existing risk in cash against CURRENT equity.
                        existing_risk = sum(float(p['qty']) * abs(float(p['entry_price']) - float(p['sl_price']))
                                            for p in open_positions) / equity
                        if existing_risk + risk_pct > CFG.risk.max_portfolio_risk_pct + 1e-9:
                            continue
                        if qty * entry / leverage > free * 0.95:
                            continue
                        if not self.is_running:
                            break
                        success, order = await self.execution_engine.execute_order(
                            symbol, side, qty, leverage, sl, tp,
                            reference_entry=entry, alpha_score=generated['score'],
                            risk_pct=risk_pct, risk_equity=equity,
                        )
                        if success:
                            # Execution saved actual fill and IDs BEFORE returning.
                            # Saving here a second time would create duplicate rows.
                            await db.signals.mark_signal_executed(signal_id)
                            GLOBAL_STATE.total_orders_executed += 1
                            GLOBAL_STATE.total_signals_generated += 1
                            self.metrics_collector.increment('orders_executed')
                            self.signal_stats[side] += 1
                            protection = order._protection
                            await self.notification_manager.send_trade_alert({
                                'symbol': symbol, 'side': side, 'qty': order.filled_qty,
                                'entry': order.avg_fill_price, 'sl': protection['sl'], 'tp': protection['tp'],
                                'leverage': leverage, 'alpha_score': generated['score'],
                            })
                        else:
                            logger.error('Entry blocked/failed | %s | %s', symbol, order.error)
                        if self.execution_engine.halted:
                            await self.notification_manager.send_alert(
                                self.execution_engine.halt_reason, 'Protection / execution halt',
                                'critical', 'execution_safety',
                            )
                    except Exception:
                        logger.exception('30m scan failed for %s', symbol)
                await asyncio.sleep(30)
            except Exception as exc:
                logger.exception('Scanner cannot obtain reliable equity/state')
                if not CFG.dry_run:
                    await self.execution_engine.latch_halt('Scanner state unavailable: ' + str(exc))
                await asyncio.sleep(30)

    async def _verified_exit_data(self, pos):
        """Use real triggered-order fills; otherwise report unknown PnL.

        This is a trade PnL excluding funding. It is NOT full account PnL.
        Never substitute the latest ticker for a historical fill price.
        """
        orders = []
        for leg in ('sl', 'tp'):
            algo_id = str(pos.get(leg + '_order_id') or '')
            if not algo_id:
                continue
            try:
                row = await self.execution_engine.api.query_algo(algo_id=algo_id)
                actual_id = str(row.get('actualOrderId') or '')
                if actual_id and actual_id != '0':
                    orders.append(actual_id)
            except ApexPatchStateUnknown:
                pass
        if not orders:
            return None, None, None, None, 'CLOSED_ON_BINANCE_PNL_UNVERIFIED'
        rows = []
        for order_id in set(orders):
            fetched = await self.execution_engine.exchange.fapiPrivateGetUserTrades({
                'symbol': self.execution_engine.api.market(pos['symbol'])['id'],
                'orderId': order_id, 'limit': 1000,
            })
            if not isinstance(fetched, list) or len(fetched) >= 1000:
                return None, None, None, None, 'CLOSED_ON_BINANCE_PNL_UNVERIFIED'
            rows.extend(r for r in fetched if str(r.get('orderId')) == order_id)
        if not rows or any(r.get('commissionAsset') != 'USDT' for r in rows):
            return None, None, None, None, 'CLOSED_ON_BINANCE_PNL_UNVERIFIED'
        total_qty = sum(apex_patch_number(r.get('qty'), positive=True) for r in rows)
        expected = float(pos['qty'])
        if abs(total_qty - expected) > max(1e-10, expected * 1e-8):
            return None, None, None, None, 'CLOSED_ON_BINANCE_PNL_UNVERIFIED'
        exit_price = sum(float(r['price']) * float(r['qty']) for r in rows) / total_qty
        exit_fees = sum(float(r['commission']) for r in rows)
        if not self.execution_engine._metadata(pos).get('entry_fees_confirmed'):
            return exit_price, None, None, None, 'EXIT_FILL_KNOWN_TOTAL_PNL_UNVERIFIED'
        entry_fees = float(pos.get('fees_usdt') or 0)
        pnl = sum(float(r['realizedPnl']) for r in rows) - entry_fees - exit_fees
        pnl_pct = pnl / (float(pos['entry_price']) * expected)
        return exit_price, pnl, pnl_pct, entry_fees + exit_fees, 'BINANCE_FILL_CONFIRMED_EXCL_FUNDING'

    async def monitor_positions(self):
        logger.info('Monitor: reconciliation + read-only protection verification; no trailing')
        while self.is_running:
            try:
                # Do not mistake an in-flight entry for a manual orphan.
                if self.execution_engine._entry_lock.locked():
                    await asyncio.sleep(1)
                    continue
                async with self.execution_engine._entry_lock:
                    open_positions = await db.trades.get_open_positions()
                    self.risk_manager.open_positions_count = len(open_positions)
                    live_map = {}
                    if not CFG.dry_run:
                        live_map = {(p.symbol, p.side): p for p in await self.execution_engine.api.positions()}
                        local_keys = {(p['symbol'], p['side']) for p in open_positions}
                        if set(live_map) - local_keys:
                            await self.execution_engine.latch_halt('Untracked live leg detected; restart reconciliation/manual inspection required')
                    for pos in open_positions:
                        symbol, side = pos['symbol'], pos['side']
                        if CFG.dry_run:
                            ticker = await self.execution_engine.exchange.fetch_ticker(symbol)
                            current = apex_patch_number(ticker.get('last'), positive=True)
                            hit_sl = current <= pos['sl_price'] if side == 'LONG' else current >= pos['sl_price']
                            hit_tp = current >= pos['tp_price'] if side == 'LONG' else current <= pos['tp_price']
                            if hit_sl or hit_tp:
                                pnl = (current - pos['entry_price']) * pos['qty'] * (1 if side == 'LONG' else -1)
                                await db.trades.close_trade(pos['id'], current, pnl,
                                                           pnl / (pos['entry_price'] * pos['qty']),
                                                           'DRY_RUN_TICK_SIMULATION')
                            continue
                        live = live_map.get((symbol, side))
                        if live is None:
                            # Repeat the side-specific successful read before cleanup
                            # so a stale snapshot does not remove live protection.
                            if await self.execution_engine.api.position(symbol, side) is not None:
                                continue
                            exit_price, pnl, pnl_pct, fees, reason = await self._verified_exit_data(pos)
                            await self.execution_engine.cleanup_known_protection(pos)
                            await db.db.execute(
                                "UPDATE trades SET status='CLOSED', closed_at=?, exit_price=?, pnl_usdt=?, pnl_pct=?, fees_usdt=?, exit_reason=? WHERE id=?",
                                (time.time(), exit_price, pnl, pnl_pct, fees, reason, pos['id']),
                            )
                            logger.info('Reconciled %s %s | %s | PnL=%s', symbol, side, reason, pnl)
                        else:
                            await self.execution_engine.inspect_protection(pos, live)
                            if abs(live.quantity - float(pos['qty'])) > max(1e-10, live.quantity * 1e-8):
                                await self.execution_engine.latch_halt('Live/local size changed for ' + symbol + '; inspect account activity')
                    await self._refresh_equity()
            except Exception as exc:
                logger.exception('Position/balance state unavailable; no local flatness assumed')
                if not CFG.dry_run:
                    await self.execution_engine.latch_halt('Monitor state UNKNOWN: ' + str(exc))
            await asyncio.sleep(10)

    async def setup_health_checks(self):
        async def db_health():
            try:
                await db.db.query('SELECT 1')
                return True
            except Exception:
                return False
        async def ws_health():
            now = time.time()
            return any(now - engine.order_book.last_update_ts < 15
                       and engine.order_book.bids and engine.order_book.asks
                       for engine in self.engines.values())
        async def exchange_health():
            try:
                await self.execution_engine.exchange.fetch_time()
                return not self.execution_engine.halted
            except Exception:
                return False
        self.health_checker.register_component('database', db_health)
        self.health_checker.register_component('websocket', ws_health)
        self.health_checker.register_component('exchange', exchange_health)

    async def run(self):
        self.is_running = True
        tasks = []
        try:
            await self.setup_signal_handlers()
            await self.setup_health_checks()
            logger.info('APEX PATCH | 30m | CQMI=50%% OTHER=50%% | %s',
                        'DRY RUN' if CFG.dry_run else ('DEMO ORDERS' if self.execution_engine.demo else 'LIVE ORDERS'))
            # This MUST finish before the scanner starts. No startup race.
            await self.execution_engine.startup_checks()
            await self._refresh_equity()
            GLOBAL_STATE.mark_ready('controller')
            GLOBAL_STATE.mark_ready('startup_reconciliation')
            if CFG.monitoring.enable_fastapi:
                self.dashboard.metrics_collector = self.metrics_collector
                self.dashboard.setup()
                await self.dashboard.start()
            tasks = [
                asyncio.create_task(self.stream_market_data()),
                asyncio.create_task(self.scanner_loop()),
                asyncio.create_task(self.monitor_positions()),
                asyncio.create_task(self.heartbeat_loop()),
                asyncio.create_task(self.cleanup_loop()),
                asyncio.create_task(self.health_checker.run_periodic_checks()),
            ]
            await GLOBAL_STATE.shutdown_event.wait()
        finally:
            self.is_running = False
            self.health_checker.stop()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            # execute_order shields a submitted entry's critical section from
            # caller cancellation. Finish it before closing exchange/database.
            await self.execution_engine.close()
            await self.exchange_manager.close_all()
            await self.notification_manager.close()
            await self.dashboard.stop()
            await db.close()
            await apex_logger.shutdown()
            logger.info('APEX patch shutdown complete')
