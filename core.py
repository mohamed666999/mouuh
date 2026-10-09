"""Offline-testable Binance USD-M conditional-order support.

No exchange is created here. No request is made at import time.
The patch builder embeds this file into the user's original script.
Only standard USD-M linear USDT perpetuals with contractSize=1 are supported;
portfolio-margin, inverse, spot and coin-margined accounts are not supported.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class ApexPatchSafetyError(RuntimeError):
    """The operation cannot be performed safely."""


class ApexPatchStateUnknown(ApexPatchSafetyError):
    """A failed read is NOT evidence that an order/position is absent."""


class ApexPatchProtectionUnconfirmed(ApexPatchSafetyError):
    def __init__(self, message: str, algo_id: str = "", client_id: str = ""):
        super().__init__(message)
        self.algo_id = algo_id
        self.client_id = client_id


def apex_patch_bool(value: Any) -> bool:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    raise ApexPatchStateUnknown("Unexpected Boolean in Binance response")


def apex_patch_number(value: Any, *, positive: bool = False) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ApexPatchStateUnknown("Missing/invalid numeric field") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ApexPatchSafetyError("Non-finite or non-positive numeric field")
    return number


def apex_patch_closed_candles(candles, timeframe_ms=1_800_000, now_ms=None):
    """Use completed, sorted, unique, contiguous 30-minute candles only."""
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    result = []
    previous = None
    for candle in candles:
        if not isinstance(candle, (list, tuple)) or len(candle) < 6:
            raise ApexPatchSafetyError("Malformed OHLCV candle")
        timestamp = int(candle[0])
        values = [apex_patch_number(v) for v in candle[1:6]]
        opening, high, low, closing, volume = values
        if min(opening, high, low, closing) <= 0 or volume < 0:
            raise ApexPatchSafetyError("Invalid OHLCV prices/volume")
        if high < max(opening, low, closing) or low > min(opening, high, closing):
            raise ApexPatchSafetyError("Inconsistent OHLCV high/low")
        if previous is not None and timestamp <= previous:
            raise ApexPatchSafetyError("Unsorted or duplicate candles")
        previous = timestamp
        if timestamp + timeframe_ms <= now_ms:
            if result and timestamp - int(result[-1][0]) != timeframe_ms:
                raise ApexPatchSafetyError("Gap in completed OHLCV candles")
            result.append([timestamp, *values])
    return result


@dataclass(frozen=True)
class ApexPatchLivePosition:
    symbol: str
    side: str
    quantity: float
    entry: float
    leverage: int


@dataclass(frozen=True)
class ApexPatchAlgoSpec:
    symbol: str                    # Binance market id, e.g. BTCUSDT
    close_side: str                # BUY or SELL
    position_side: str             # BOTH / LONG / SHORT
    order_type: str                # STOP_MARKET / TAKE_PROFIT_MARKET
    trigger_price: str             # Already rounded with CCXT market precision
    client_id: str

    def payload(self):
        if self.close_side not in {"BUY", "SELL"}:
            raise ApexPatchSafetyError("Invalid closing side")
        if self.position_side not in {"BOTH", "LONG", "SHORT"}:
            raise ApexPatchSafetyError("Invalid position side")
        if self.order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
            raise ApexPatchSafetyError("Unsupported protective order type")
        if self.position_side == "LONG" and self.close_side != "SELL":
            raise ApexPatchSafetyError("LONG must close with SELL")
        if self.position_side == "SHORT" and self.close_side != "BUY":
            raise ApexPatchSafetyError("SHORT must close with BUY")
        apex_patch_number(self.trigger_price, positive=True)
        if not self.client_id or len(self.client_id) > 36:
            raise ApexPatchSafetyError("Invalid clientAlgoId")
        return {
            "algoType": "CONDITIONAL",
            "symbol": self.symbol,
            "side": self.close_side,
            "positionSide": self.position_side,
            "type": self.order_type,
            "triggerPrice": self.trigger_price,
            "workingType": "MARK_PRICE",
            "closePosition": "true",
            # No quantity; no reduceOnly: both conflict with closePosition=true.
            "clientAlgoId": self.client_id,
        }

    def matches(self, row: dict, algo_id="") -> bool:
        """Exact identity/side/trigger/close-all verification, not price tolerance."""
        try:
            if not isinstance(row, dict) or not row.get("algoId"):
                return False
            if algo_id and str(row["algoId"]) != str(algo_id):
                return False
            return (
                row.get("clientAlgoId") == self.client_id
                and row.get("symbol") == self.symbol
                and str(row.get("side", "")).upper() == self.close_side
                and str(row.get("positionSide", "")).upper() == self.position_side
                and str(row.get("orderType", row.get("type", ""))).upper() == self.order_type
                and str(row.get("algoStatus", "")).upper() == "NEW"
                and str(row.get("workingType", "")).upper() == "MARK_PRICE"
                and apex_patch_bool(row.get("closePosition"))
                and Decimal(str(row.get("triggerPrice"))) == Decimal(self.trigger_price)
            )
        except (ApexPatchSafetyError, InvalidOperation, ValueError, TypeError):
            return False


class ApexPatchAlgoService:
    """Explicit /fapi/v1/algoOrder access through CCXT's signing/rate limiter.

    Creation is attempted ONCE with a stable clientAlgoId. Bounded retries are
    read-only. An uncertain request never becomes a blind duplicate POST.
    """

    REQUIRED_METHODS = (
        "fapiPrivatePostAlgoOrder", "fapiPrivateGetAlgoOrder",
        "fapiPrivateGetOpenAlgoOrders", "fapiPrivateDeleteAlgoOrder",
        "fapiPrivateGetPositionSideDual", "fapiPrivateGetOrder",
        "fapiPrivateGetOpenOrders", "fapiPublicGetPremiumIndex", "fapiPrivateGetUserTrades",
    )

    def __init__(self, exchange, read_attempts=3, read_delay=0.35):
        self.exchange = exchange
        self.read_attempts = max(1, int(read_attempts))
        self.read_delay = max(0.0, float(read_delay))

    def assert_capabilities(self):
        missing = [name for name in self.REQUIRED_METHODS
                   if not callable(getattr(self.exchange, name, None))]
        if missing:
            raise ApexPatchSafetyError("Update CCXT; missing raw Algo API methods: " + ", ".join(missing))
        if getattr(self.exchange, "options", {}).get("portfolioMargin"):
            raise ApexPatchSafetyError("Portfolio-margin accounts are not supported by this patch")

    async def initialize(self):
        self.assert_capabilities()
        await self.exchange.load_markets()
        # No position-mode mutation. Failure to read the mode blocks execution.
        return await self.hedged()

    def market(self, symbol):
        market = self.exchange.market(symbol)
        if not (market.get("linear") and market.get("swap")
                and market.get("settle") == "USDT"
                and apex_patch_number(market.get("contractSize"), positive=True) == 1):
            raise ApexPatchSafetyError("Only linear USDT perpetuals with contractSize=1 are supported")
        if market.get("active") is False:
            raise ApexPatchSafetyError("Inactive futures market")
        return market

    def price(self, symbol, value):
        self.market(symbol)
        apex_patch_number(value, positive=True)
        price = str(self.exchange.price_to_precision(symbol, value))
        apex_patch_number(price, positive=True)
        return price

    def amount(self, symbol, value):
        market = self.market(symbol)
        requested = apex_patch_number(value, positive=True)
        rounded = apex_patch_number(self.exchange.amount_to_precision(symbol, requested), positive=True)
        if rounded > requested + max(1e-12, requested * 1e-12):
            raise ApexPatchSafetyError("Amount precision unexpectedly rounded upward")
        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        if amount_limits.get("min") is not None and rounded < float(amount_limits["min"]):
            raise ApexPatchSafetyError("Quantity is below exchange minimum; do not round up")
        if amount_limits.get("max") is not None and rounded > float(amount_limits["max"]):
            raise ApexPatchSafetyError("Quantity exceeds exchange maximum")
        return rounded

    async def hedged(self):
        try:
            response = await self.exchange.fapiPrivateGetPositionSideDual({})
            return apex_patch_bool(response.get("dualSidePosition"))
        except ApexPatchSafetyError:
            raise
        except Exception as exc:
            raise ApexPatchStateUnknown("Cannot read Binance position mode") from exc

    async def mark_price(self, symbol):
        market_id = self.market(symbol)["id"]
        try:
            response = await self.exchange.fapiPublicGetPremiumIndex({"symbol": market_id})
            if not isinstance(response, dict) or response.get("symbol") != market_id:
                raise ApexPatchStateUnknown("Unexpected mark-price response")
            return apex_patch_number(response.get("markPrice"), positive=True)
        except ApexPatchSafetyError:
            raise
        except Exception as exc:
            raise ApexPatchStateUnknown("Cannot read current mark price") from exc

    @staticmethod
    def parse_positions(rows):
        if not isinstance(rows, list):
            raise ApexPatchStateUnknown("Unexpected positions response")
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ApexPatchStateUnknown("Malformed position")
            info = row.get("info") or {}
            # contractSize is the multiplier, NEVER a fallback for position qty.
            raw_qty = row.get("contracts")
            if raw_qty is None:
                raw_qty = info.get("positionAmt")
            quantity = abs(apex_patch_number(raw_qty))
            if quantity == 0:
                continue
            symbol = row.get("symbol")
            if not symbol:
                raise ApexPatchStateUnknown("Position has no unified symbol")
            raw_side = str(row.get("side") or "").upper()
            if raw_side in {"LONG", "SHORT"}:
                side = raw_side
            elif info.get("positionSide") in {"LONG", "SHORT"}:
                side = info["positionSide"]
            elif info.get("positionAmt") is not None:
                signed = apex_patch_number(info["positionAmt"])
                if signed == 0:
                    raise ApexPatchStateUnknown("Conflicting position quantity fields")
                side = "LONG" if signed > 0 else "SHORT"
            else:
                raise ApexPatchStateUnknown("Unknown position side")
            contract_size = row.get("contractSize")
            if contract_size is not None and apex_patch_number(contract_size, positive=True) != 1:
                raise ApexPatchSafetyError("Unsupported position contract multiplier")
            entry = apex_patch_number(row.get("entryPrice") or info.get("entryPrice"), positive=True)
            raw_leverage = row.get("leverage") or info.get("leverage") or 1
            leverage = int(apex_patch_number(raw_leverage, positive=True))
            key = (symbol, side)
            if key in seen:
                raise ApexPatchStateUnknown("Duplicate live position leg")
            seen.add(key)
            result.append(ApexPatchLivePosition(symbol, side, quantity, entry, leverage))
        return result

    async def positions(self, symbol=None):
        try:
            rows = await self.exchange.fetch_positions([symbol] if symbol else None)
            return self.parse_positions(rows)
        except ApexPatchSafetyError:
            raise
        except Exception as exc:
            raise ApexPatchStateUnknown("Cannot read positions; flatness is UNKNOWN") from exc

    async def position(self, symbol, side):
        for position in await self.positions(symbol):
            if position.symbol == symbol and position.side == side:
                return position
        return None  # Only after a successful snapshot.

    async def open_algos(self, symbol):
        try:
            response = await self.exchange.fapiPrivateGetOpenAlgoOrders({"symbol": self.market(symbol)["id"]})
            if isinstance(response, dict) and isinstance(response.get("orders"), list):
                response = response["orders"]
            if not isinstance(response, list) or any(not isinstance(row, dict) for row in response):
                raise ApexPatchStateUnknown("Unexpected openAlgoOrders response")
            return response
        except ApexPatchSafetyError:
            raise
        except Exception as exc:
            raise ApexPatchStateUnknown("Cannot read conditional orders") from exc

    async def normal_open_orders(self, symbol):
        try:
            response = await self.exchange.fapiPrivateGetOpenOrders({"symbol": self.market(symbol)["id"]})
            if not isinstance(response, list):
                raise ApexPatchStateUnknown("Unexpected openOrders response")
            return response
        except ApexPatchSafetyError:
            raise
        except Exception as exc:
            raise ApexPatchStateUnknown("Cannot read ordinary open orders") from exc

    async def query_algo(self, *, algo_id="", client_id=""):
        if not algo_id and not client_id:
            raise ApexPatchSafetyError("No actual order identity supplied")
        params = {"algoId": str(algo_id)} if algo_id else {"clientAlgoId": client_id}
        try:
            row = await self.exchange.fapiPrivateGetAlgoOrder(params)
            if not isinstance(row, dict) or not row.get("algoId"):
                raise ApexPatchStateUnknown("Malformed algo-order response")
            return row
        except ApexPatchSafetyError:
            raise
        except Exception as exc:
            raise ApexPatchStateUnknown("Algo status query failed") from exc

    async def verify(self, spec, algo_id=""):
        """Either independent GET or open-algo GET must confirm the exact spec."""
        for attempt in range(self.read_attempts):
            try:
                row = await self.query_algo(algo_id=algo_id, client_id=spec.client_id)
                if spec.matches(row, algo_id):
                    return row
            except ApexPatchStateUnknown:
                pass
            try:
                rows = await self.open_algos_by_id(spec.symbol)
                matches = [row for row in rows if spec.matches(row, algo_id)]
                if len(matches) == 1:
                    return matches[0]
            except ApexPatchStateUnknown:
                pass
            if attempt + 1 < self.read_attempts:
                await asyncio.sleep(self.read_delay)
        return None

    async def open_algos_by_id(self, market_id):
        # Unlike open_algos(), this accepts the already validated Binance id.
        try:
            rows = await self.exchange.fapiPrivateGetOpenAlgoOrders({"symbol": market_id})
            if isinstance(rows, dict) and isinstance(rows.get("orders"), list):
                rows = rows["orders"]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ApexPatchStateUnknown("Unexpected open-algo response")
            return rows
        except ApexPatchSafetyError:
            raise
        except Exception as exc:
            raise ApexPatchStateUnknown("Open-algo query failed") from exc

    def spec(self, symbol, side, order_type, trigger, client_id, hedged):
        if side not in {"LONG", "SHORT"}:
            raise ApexPatchSafetyError("Invalid position direction")
        return ApexPatchAlgoSpec(
            self.market(symbol)["id"], "SELL" if side == "LONG" else "BUY",
            side if hedged else "BOTH", order_type,
            self.price(symbol, trigger), client_id,
        )

    async def place_once(self, spec):
        payload = spec.payload()
        algo_id = ""
        create_error = ""
        try:
            response = await self.exchange.fapiPrivatePostAlgoOrder(payload)
            if isinstance(response, dict):
                algo_id = str(response.get("algoId") or "")
        except Exception as exc:
            # Response may have been lost AFTER Binance accepted the order.
            # Do not POST a second order. Recover using the SAME clientAlgoId.
            create_error = type(exc).__name__ + ': ' + str(exc)
        row = await self.verify(spec, algo_id)
        if row is None:
            raise ApexPatchProtectionUnconfirmed(
                "Conditional protection not independently confirmed; creation is not retried"
                + ("; create error=" + create_error if create_error else ""),
                algo_id, spec.client_id,
            )
        return row

    async def cancel_known(self, symbol, *, algo_id="", client_id=""):
        """Cancel only an explicitly owned ID; never cancel_all_orders(symbol)."""
        if not algo_id and not client_id:
            return
        expected_symbol = self.market(symbol)["id"]
        params = {"algoId": str(algo_id)} if algo_id else {"clientAlgoId": client_id}
        try:
            row = await self.query_algo(algo_id=algo_id, client_id=client_id)
        except ApexPatchStateUnknown as exc:
            # Absence from a successful open-order snapshot permits cleanup to
            # finish. A failed snapshot still raises: it is not proof of absence.
            rows = await self.open_algos(symbol)
            matches = [r for r in rows if (algo_id and str(r.get("algoId")) == str(algo_id))
                       or (client_id and r.get("clientAlgoId") == client_id)]
            if not matches:
                return
            if len(matches) != 1:
                raise ApexPatchStateUnknown("Ambiguous protective identity") from exc
            row = matches[0]
        if row.get("symbol") != expected_symbol:
            raise ApexPatchSafetyError("Refusing to cancel another market's order")
        if str(row.get("algoStatus", "")).upper() != "NEW":
            return
        await self.exchange.fapiPrivateDeleteAlgoOrder(params)
        rows = await self.open_algos(symbol)
        if any(str(r.get("algoId")) == str(row["algoId"]) for r in rows):
            raise ApexPatchStateUnknown("Cancellation not confirmed")

    async def query_entry(self, symbol, client_id):
        try:
            row = await self.exchange.fapiPrivateGetOrder({
                "symbol": self.market(symbol)["id"], "origClientOrderId": client_id,
            })
            if not isinstance(row, dict) or row.get("clientOrderId") != client_id:
                raise ApexPatchStateUnknown("Unexpected entry-order identity")
            return row
        except ApexPatchSafetyError:
            raise
        except Exception as exc:
            raise ApexPatchStateUnknown("Entry acceptance/fill status is UNKNOWN") from exc

    async def flatten(self, symbol, side):
        """Bounded close attempts using LIVE qty. A read failure never means flat.

        No retransmission of an uncertain close request. A retry is allowed
        only when its preceding close has a confirmed terminal order status.
        """
        hedged = await self.hedged()
        for _ in range(3):
            position = await self.position(symbol, side)
            if position is None:
                return True
            client_id = "qa-close-" + uuid.uuid4().hex[:24]
            params = {"newClientOrderId": client_id, "newOrderRespType": "RESULT"}
            if hedged:
                params["positionSide"] = side
            else:
                params["positionSide"] = "BOTH"
                params["reduceOnly"] = True
            response = None
            try:
                response = await self.exchange.create_order(
                    symbol, "market", "sell" if side == "LONG" else "buy",
                    self.amount(symbol, position.quantity), None, params,
                )
            except Exception:
                # Resolve the same client ID before considering any retry.
                response = None
            await asyncio.sleep(self.read_delay)
            if await self.position(symbol, side) is None:
                # A successful current snapshot is required, but an uncertain
                # close must also be terminal before new entries are allowed.
                if isinstance(response, dict) and response.get("status") in {"closed", "canceled", "rejected", "expired"}:
                    return True
                raw = await self.query_entry(symbol, client_id)
                if str(raw.get("status", "")).upper() in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
                    return True
                raise ApexPatchStateUnknown("Close request may still be active")
            raw = await self.query_entry(symbol, client_id)
            if str(raw.get("status", "")).upper() not in {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}:
                raise ApexPatchStateUnknown("Close is not terminal; no duplicate close sent")
        return False
