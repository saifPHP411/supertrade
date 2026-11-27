from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from urllib.parse import urlparse

import ccxt
import numpy as np
import pandas as pd

from .config import BAR, DAY
from .market import CANDLE_COLUMNS
from .strategy import Features


class ExchangeError(RuntimeError):
    pass


class Binance:
    def __init__(self, mode, key, secret, paper_host, authorize):
        if not key or not secret:
            raise ExchangeError(f"Set BINANCE_{mode.upper()}_KEY and BINANCE_{mode.upper()}_SECRET in .env")
        self.mode = mode
        self.authorize = authorize
        self.exchange = ccxt.binanceusdm({
            "apiKey": key, "secret": secret, "enableRateLimit": True, "timeout": 8000,
            "options": {"fetchCurrencies": False, "defaultType": "future",
                        "fetchMarkets": {"types": ["linear"]}},
        })
        if mode == "paper":
            if paper_host == "demo":
                self.exchange.enable_demo_trading(True)
                expected = "demo-fapi.binance.com"
            elif paper_host == "testnet":
                self.exchange.set_sandbox_mode(True)
                expected = "testnet.binancefuture.com"
            else:
                raise ExchangeError("BINANCE_PAPER_ENV must be demo or testnet")
        else:
            expected = "fapi.binance.com"
        for group in ("fapiPublic", "fapiPrivate", "fapiPrivateV2", "fapiPrivateV3"):
            if urlparse(self.exchange.urls["api"][group]).hostname != expected:
                raise ExchangeError("Unexpected Binance Futures endpoint")
        # Honor standard HTTPS proxy and CA bundle settings for local installations.
        self.exchange.session.trust_env = True
        self.host = expected
        self.market_rules = {}
        self.history = {}

    def market(self, symbol):
        if not self.market_rules:
            markets = self.exchange.fapiPublicGetExchangeInfo()["symbols"]
            self.market_rules = {m["baseAsset"]: m for m in markets if m["contractType"] == "PERPETUAL"
                                 and m["quoteAsset"] == "USDT" and m["status"] == "TRADING"}
        if symbol not in self.market_rules:
            raise ExchangeError(f"{symbol}USDT is not an active perpetual on the selected endpoint")
        return self.market_rules[symbol]

    def write(self, method, params):
        self.authorize()
        return getattr(self.exchange, method)(params)

    def verify_mode(self):
        settings = self.exchange.fapiPrivateGetPositionSideDual()
        if settings.get("dualSidePosition") not in (False, "false"):
            raise ExchangeError("Use Binance Futures one-way position mode")

    def account(self):
        state = self.exchange.fapiPrivateV3GetAccount()
        if state.get("multiAssetsMargin") is True or state.get("multiAssetsMargin") == "true":
            raise ExchangeError("Use single-asset USDT margin mode")
        positions = {}
        for row in self.exchange.fapiPrivateV3GetPositionRisk():
            quantity = float(row["positionAmt"])
            if quantity:
                if row["positionSide"] != "BOTH" or not row["symbol"].endswith("USDT"):
                    raise ExchangeError("Use a dedicated USDT account in one-way position mode")
                positions[row["symbol"][:-4]] = {
                    "size": abs(quantity), "side": 1 if quantity > 0 else -1,
                    "entry": float(row["entryPrice"]), "mark": float(row["markPrice"]),
                    "pnl": float(row["unRealizedProfit"]),
                }
        usdt = next((asset for asset in state["assets"] if asset["asset"] == "USDT"), None)
        if not usdt:
            raise ExchangeError("The account does not expose a USDT margin balance")
        equity = float(usdt["marginBalance"])
        if equity < 0 or not np.isfinite(equity):
            raise ExchangeError("Invalid account equity")
        return equity, positions

    def orders(self):
        regular = self.exchange.fapiPrivateGetOpenOrders()
        conditional = self.exchange.fapiPrivateGetOpenAlgoOrders()
        return [
            {"symbol": row["symbol"][:-4], "kind": row.get("orderType", row.get("type")),
             "side": row["side"], "price": float(row.get("triggerPrice") or row.get("price") or 0),
             "id": str(row.get("algoId", row.get("orderId"))),
             "client": row.get("clientAlgoId", row.get("clientOrderId", "")), "conditional": algo}
            for source, algo in ((regular, False), (conditional, True)) for row in source
        ]

    def prepare_symbol(self, symbol):
        self.market(symbol)
        self.write("fapiPrivatePostLeverage", {"symbol": symbol + "USDT", "leverage": 1})

    def quantity(self, symbol, quantity, price, entry=True):
        market = self.market(symbol)
        filters = {f["filterType"]: f for f in market["filters"]}
        step = filters["MARKET_LOT_SIZE"]["stepSize"]
        if float(step) == 0:
            step = filters["LOT_SIZE"]["stepSize"]
        rounded = ccxt.decimal_to_precision(str(quantity), ccxt.TRUNCATE, step, ccxt.TICK_SIZE)
        min_notional = float(filters.get("MIN_NOTIONAL", {}).get("notional", 0))
        lot = filters["MARKET_LOT_SIZE"]
        if entry and (float(rounded) < float(lot["minQty"]) or float(rounded) * price < min_notional
                      or float(rounded) > float(lot["maxQty"])):
            return None
        return rounded if float(rounded) > 0 else None

    def tick_price(self, symbol, price, stop_side=0):
        tick = next(f["tickSize"] for f in self.market(symbol)["filters"] if f["filterType"] == "PRICE_FILTER")
        if stop_side:
            step = Decimal(tick)
            count = (Decimal(str(price)) / step).to_integral_value(
                rounding=ROUND_CEILING if stop_side == 1 else ROUND_FLOOR)
            return format(count * step, "f")
        return ccxt.decimal_to_precision(str(price), ccxt.ROUND, tick, ccxt.TICK_SIZE)

    def market_order(self, symbol, side, quantity, client_id, reduce=False):
        return self.write("fapiPrivatePostOrder", {
            "symbol": symbol + "USDT", "side": "BUY" if side == 1 else "SELL",
            "type": "MARKET", "quantity": quantity, "newClientOrderId": client_id,
            "reduceOnly": "true" if reduce else "false", "newOrderRespType": "RESULT",
        })

    def protection(self, symbol, side, price, client_id, target=False):
        return self.write("fapiPrivatePostAlgoOrder", {
            "symbol": symbol + "USDT", "side": "SELL" if side == 1 else "BUY",
            "algoType": "CONDITIONAL", "type": "TAKE_PROFIT_MARKET" if target else "STOP_MARKET",
            "triggerPrice": self.tick_price(symbol, price), "closePosition": "true",
            "workingType": "CONTRACT_PRICE", "clientAlgoId": client_id,
        })

    def cancel(self, order):
        params = {"symbol": order["symbol"] + "USDT"}
        if order["conditional"]:
            return self.write("fapiPrivateDeleteAlgoOrder", {**params, "algoId": order["id"]})
        return self.write("fapiPrivateDeleteOrder", {**params, "orderId": order["id"]})

    def outcome(self, symbol, opened):
        now = self.exchange.milliseconds()
        fills, funding = [], []
        window = 7 * DAY * 1000
        for start in range(opened, now + 1, window):
            parameters = {"symbol": symbol + "USDT", "startTime": start,
                          "endTime": min(start + window - 1, now), "limit": 1000}
            trades = self.exchange.fapiPrivateGetUserTrades(parameters)
            income = self.exchange.fapiPrivateGetIncome({**parameters, "incomeType": "FUNDING_FEE"})
            if len(trades) >= 1000 or len(income) >= 1000:
                raise ExchangeError("Exchange history limit reached before round trip reconciliation")
            fills.extend(trades)
            funding.extend(income)
        if any(row["commissionAsset"] != "USDT" and float(row["commission"]) != 0 for row in fills):
            raise ExchangeError("Round trip fees could not be reconciled in USDT")
        if any(row["asset"] != "USDT" for row in funding):
            raise ExchangeError("Funding is not denominated in USDT")
        if not fills or abs(sum(float(row["qty"]) * (1 if row["side"] == "BUY" else -1) for row in fills)) > 1e-8:
            return None
        fees = sum(float(row["commission"]) for row in fills)
        credit = sum(float(row["income"]) for row in funding)
        pnl = sum(float(row["realizedPnl"]) for row in fills) - fees + credit
        order_ids = {str(row["orderId"]) for row in fills}
        clients = {}
        for order_id in order_ids:
            order = self.exchange.fapiPrivateGetOrder({"symbol": symbol + "USDT", "orderId": order_id})
            clients[order_id] = order["clientOrderId"]
        evidence = [{"order_id": str(row["orderId"]), "trade_id": str(row["id"]),
                     "client_id": clients[str(row["orderId"])], "at": int(row["time"]),
                     "side": 1 if row["side"] == "BUY" else -1,
                     "price": float(row["price"]), "quantity": float(row["qty"]),
                     "fee": float(row["commission"])} for row in fills]
        return {"pnl": pnl, "fees": fees, "funding": credit,
                "closed": max(int(row["time"]) // 1000 for row in fills), "fills": evidence}

    def signal_features(self, symbols, stamp, interrupt=None):
        daily, four = {}, {}
        for symbol in symbols:
            if interrupt and interrupt.is_set():
                raise ExchangeError("Market data refresh interrupted by engine control")
            for interval, seconds, length, minimum, target in (
                ("1d", DAY, 425, 200, daily), ("4h", BAR, 240, 80, four),
            ):
                key = (symbol, interval)
                boundary = stamp // seconds * seconds
                frame = self.history.get(key)
                if frame is None or frame.index[-1] != boundary:
                    rows = self.exchange.fapiPublicGetKlines({
                        "symbol": symbol + "USDT", "interval": interval, "limit": length,
                    })
                    frame = pd.DataFrame(
                        [[*map(float, row[1:6]), float(row[7])] for row in rows],
                        columns=CANDLE_COLUMNS + ["quote_volume"], index=[int(row[0]) // 1000 + seconds for row in rows],
                    )
                    frame = frame.loc[frame.index <= stamp]
                    if (len(frame) < minimum or frame.index[-1] != boundary
                            or np.any(np.diff(frame.index[-minimum:]) != seconds)):
                        raise ExchangeError(f"Missing completed {interval} history for {symbol}")
                    self.history[key] = frame
                target[symbol] = frame
        return Features(daily, four)

    def find_entry(self, symbol, client_id):
        try:
            return self.exchange.fapiPrivateGetOrder({"symbol": symbol + "USDT", "origClientOrderId": client_id})
        except ccxt.OrderNotFound:
            return None

    def mark(self, symbol):
        row = self.exchange.fapiPublicGetPremiumIndex({"symbol": symbol + "USDT"})
        mark = float(row["markPrice"])
        if not np.isfinite(mark) or mark <= 0:
            raise ExchangeError(f"Missing {symbol} mark price")
        return mark


    def midpoint(self, symbol):
        q = self.exchange.fapiPublicGetTickerBookTicker({"symbol": symbol + "USDT"})
        bid, ask = float(q["bidPrice"]), float(q["askPrice"])
        if not (0 < bid <= ask and np.isfinite(ask)):
            raise ExchangeError("No valid Binance Demo order-book midpoint")
        return (bid + ask) / 2
