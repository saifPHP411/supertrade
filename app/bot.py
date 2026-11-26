from __future__ import annotations

import hashlib
import math
import os
import threading
import time
import uuid
from dataclasses import asdict

import ccxt
import httpx

from .config import BAR, DAY, FEE, MAX_RISK, PRESETS, SLIP, Deployment
from .exchange import Binance, ExchangeError
from .gate import live_gate
from .risk import Risk
from .strategy import (
    build_signals,
    eligible,
    exit_reason,
    prune_budget,
    size_position,
    stop_gap,
    trail_stop,
    within_budget,
)


class Bot:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.interrupt = threading.Event()
        self.exchange = None
        self.regime = None
        self.orders = []
        self.broker_positions = {}
        self.verified_at = None
        self.equity = None
        self.last_error = None
        self.alert_thread = None
        self.mode = store.get("mode", "paper")
        self.preset = store.get("edge_preset", "MAX GROWTH")
        if self.preset not in PRESETS:
            self.preset = "MAX GROWTH"
        self.capital = float(os.environ.get("CAPITAL_LIMIT", "50"))
        if not math.isfinite(self.capital) or self.capital < 10:
            raise ValueError("CAPITAL_LIMIT must be finite and at least 10 USDT")
        self.running = False
        self.healthy = False
        self.data = self.load_state(self.mode)
        self.worker = threading.Thread(target=self.loop, daemon=True, name="execution")

    def load_state(self, mode):
        return self.store.get(f"bot:{mode}", {"risk": None, "positions": {}, "anchor": None,
                                              "account": None, "killed": False, "flatten": False, "last_bar": 0})

    def save(self, trade=None):
        self.store.state(f"bot:{self.mode}", self.data, trade)

    def credentials(self, mode):
        return os.getenv(f"BINANCE_{mode.upper()}_KEY", ""), os.getenv(f"BINANCE_{mode.upper()}_SECRET", "")

    def account_id(self, mode):
        key, _ = self.credentials(mode)
        environment = os.getenv("BINANCE_PAPER_ENV", "demo") if mode == "paper" else "production"
        return hashlib.sha256(f"{mode}:{environment}:{key}".encode()).hexdigest()

    def plan(self):
        release = self.store.get("edge_release", {})
        record = release.get("plans", {}).get(self.preset)
        if record:
            plan = Deployment.model_validate(record)
            if plan.capital == self.capital:
                return plan
        return None

    def scope(self, mode):
        plan = self.plan()
        identifier = plan.fingerprint if plan else "edge-hunt-unqualified"
        return f"{self.account_id(mode)}:{identifier}:{self.capital:g}"

    def gate(self):
        result = live_gate(self.store, self.scope("paper"))
        qualified = self.plan() is not None
        result["checks"].insert(0, {"label": "Tournament survivor for this preset and allocation",
                                    "value": "Qualified" if qualified else "Pending", "target": "Qualified",
                                    "passed": qualified})
        result["passed"] &= qualified
        return result

    def event(self, level, message):
        self.store.log(level, message)
        if (level == "alert" and os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")
                and (self.alert_thread is None or not self.alert_thread.is_alive())):
            self.alert_thread = threading.Thread(target=self.telegram, args=(message,), daemon=True)
            self.alert_thread.start()

    def telegram(self, message):
        try:
            token = os.environ["TELEGRAM_BOT_TOKEN"]
            httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": message}, timeout=5).raise_for_status()
        except httpx.HTTPError:
            self.store.log("warning", "Telegram alert could not be delivered")

    def authorize(self, mode):
        if mode == "live" and (os.getenv("ENABLE_LIVE_TRADING") != "true" or not self.gate()["passed"]):
            raise ExchangeError("Live trading is locked by the paper gate or local opt-in")

    def connect(self, mode):
        self.authorize(mode)
        key, secret = self.credentials(mode)
        account_id = self.account_id(mode)
        if self.data["account"] and self.data["account"] != account_id:
            raise ExchangeError("This ledger belongs to a different exchange key or endpoint")
        adapter = Binance(mode, key, secret, os.getenv("BINANCE_PAPER_ENV", "demo"), lambda: self.authorize(mode))
        self.exchange = adapter

    def configure(self, preset):
        with self.lock:
            if self.running or self.data["positions"] or self.data["flatten"]:
                raise ExchangeError("Stop the engine and settle positions before changing the risk preset")
            self.preset = preset
            self.store.state("edge_preset", preset)
            self.regime = None
            self.equity = None
            self.healthy = False
            return self.snapshot()

    def start(self, mode):
        with self.lock:
            self.authorize(mode)
            plan = self.plan()
            if plan is None:
                raise ExchangeError("No qualifying tournament strategy for this risk preset and capital allocation")
            if self.running:
                if mode == self.mode:
                    return self.snapshot()
                raise ExchangeError("Stop the active engine before switching environments")
            if mode != self.mode and (self.data["positions"] or self.data["flatten"]):
                raise ExchangeError("Settle positions and outstanding flatten requests before switching modes")
            candidate = self.load_state(mode)
            if any(p.get("scope") != self.scope(mode) for p in candidate["positions"].values()):
                raise ExchangeError("Settle positions from the previous strategy before starting")
            if candidate["killed"] or (candidate["risk"] and candidate["risk"]["halted"]):
                raise ExchangeError("A latched risk stop requires a manual reset")
            old_mode, old_data, old_exchange = self.mode, self.data, self.exchange
            self.mode, self.data = mode, candidate
            try:
                self.connect(mode)
                self.exchange.verify_mode()
                self.refresh()
                unknown = set(self.broker_positions) - self.data["positions"].keys()
                unknown_orders = [order for order in self.orders if not order["client"].startswith("st-")]
                if unknown or unknown_orders:
                    raise ExchangeError("The account must be dedicated to Supertrade and free of unmanaged exposure")
                self.data["account"] = self.account_id(mode)
                self.save()
            except Exception:
                self.mode, self.data, self.exchange = old_mode, old_data, old_exchange
                self.equity, self.verified_at, self.orders, self.broker_positions = None, None, [], {}
                raise
            self.store.state("mode", mode)
            self.interrupt.clear()
            self.running = True
            self.healthy = False
            self.last_error = None
            self.regime = None
            self.event("info", f"{mode.upper()} engine started")
            return self.snapshot()

    def stop(self):
        self.interrupt.set()
        with self.lock:
            self.running = self.healthy = False
            self.event("info", "New entries disabled. Existing positions are still monitored.")
            return self.snapshot()

    def reset(self):
        with self.lock:
            if self.running or self.data["flatten"] or self.data["positions"]:
                raise ExchangeError("Stop the engine and verify a flat account before resetting")
            if not self.exchange:
                self.connect(self.mode)
            self.refresh()
            if self.broker_positions or self.orders:
                raise ExchangeError("Reset requires a verified flat account and zero open orders")
            stamp = int(time.time())
            prior = Risk(**self.data["risk"]) if self.data["risk"] else Risk(self.equity, self.equity, stamp // DAY)
            if not self.data["killed"] and not prior.halted:
                raise ExchangeError("There is no latched kill or drawdown stop to reset")
            if prior.halted:
                prior.peak = self.equity
                prior.halted = False
            prior.observe(stamp, self.equity, self.preset)
            self.data["risk"] = asdict(prior)
            self.data["killed"] = False
            self.save()
            self.event("info", "Manual drawdown and kill-switch reset recorded")
            return self.snapshot()

    def kill(self):
        self.interrupt.set()
        with self.lock:
            self.running = self.healthy = False
            self.data["killed"] = self.data["flatten"] = True
            self.save()
            self.event("alert", f"Kill switch latched for {self.mode.upper()}. Flatten requested.")
            try:
                self.flatten()
            except Exception as error:  # noqa: BLE001 — the latched flatten retries from the worker
                self.report_error(error)
            return self.snapshot()

    def refresh(self):
        wallet, positions = self.exchange.account()
        orders = self.exchange.orders()
        if self.data["anchor"] is None:
            self.data["anchor"] = wallet
            self.data["initial_budget"] = min(self.capital, wallet)
        # An oversized testnet wallet cannot inflate the configured strategy allocation.
        allocated = min(self.capital, self.data.get("initial_budget", self.data["anchor"]))
        self.equity = max(0.0, min(wallet, allocated + wallet - self.data["anchor"]))
        self.broker_positions, self.orders = positions, orders
        self.verified_at = int(time.time())

    def close_market(self, symbol, position):
        quantity = self.exchange.quantity(symbol, position["size"], position["mark"], entry=False)
        if not quantity:
            raise ExchangeError(f"Unable to express the remaining {symbol} position at exchange precision")
        self.exchange.market_order(symbol, -position["side"], quantity, "st-x-" + uuid.uuid4().hex[:24], reduce=True)

    def flatten(self):
        if not self.exchange:
            self.connect(self.mode)
        self.refresh()
        errors = []
        # Cancel working entry orders first. Leave native protection while reducing exposure.
        for order in self.orders:
            if not order["conditional"]:
                try:
                    self.exchange.cancel(order)
                except (ccxt.BaseError, ExchangeError) as error:
                    errors.append(error)
        for symbol, position in self.broker_positions.items():
            try:
                self.close_market(symbol, position)
            except (ccxt.BaseError, ExchangeError) as error:
                errors.append(error)
        self.refresh()
        for order in self.orders:
            if order["symbol"] not in self.broker_positions:
                try:
                    self.exchange.cancel(order)
                except (ccxt.BaseError, ExchangeError) as error:
                    errors.append(error)
        self.refresh()
        if self.broker_positions or self.orders:
            raise ExchangeError(f"Flatten pending: {len(self.broker_positions)} positions and {len(self.orders)} orders remain") from (
                errors[0] if errors else None)
        self.reconcile(int(time.time()))
        self.data["flatten"] = False
        self.save()
        self.event("info", "Exchange confirmed zero positions and zero open orders")

    def ensure_protection(self, symbol, managed, current):
        if current["side"] != managed["side"]:
            raise ExchangeError(f"{symbol} position direction differs from the order journal")
        managed["entry"] = current["entry"]
        managed["quantity"] = current["size"]
        fresh = managed.get("stop") is None
        if fresh:
            gap = min(managed["gap"], managed["entry"] * 0.02) if managed.get("family") == "A" else managed["gap"]
            managed["stop"] = managed["entry"] - managed["side"] * gap
            managed["favorable"] = managed["entry"]
        stop_tick = float(self.exchange.tick_price(symbol, managed["stop"], stop_side=managed["side"]))
        stop_gap = max(0.0, managed["side"] * (managed["entry"] - stop_tick))
        modeled_loss = current["size"] * (stop_gap + managed["entry"] * 2 * (FEE + SLIP))
        if modeled_loss > managed["risk_budget"] * (1 + 1e-9):
            self.close_market(symbol, current)
            raise ExchangeError(f"{symbol} position exceeded the modeled entry risk budget")
        if fresh:
            self.save()
            self.event("trade", f"{self.mode.upper()} {symbol} entry reconciled at {current['entry']:g}")

        matching = [order for order in self.orders if order["symbol"] == symbol and order["conditional"]
                    and order["kind"] == "STOP_MARKET" and order["client"].startswith("st-")]
        if not any(abs(order["price"] - stop_tick) < 1e-12 for order in matching):
            if managed["side"] * (current["mark"] - stop_tick) <= 0:
                self.close_market(symbol, current)
                return
            client = "st-p-" + uuid.uuid4().hex[:24]
            try:
                protected = self.exchange.protection(symbol, managed["side"], stop_tick, client)
                self.orders.append({"symbol": symbol, "kind": "STOP_MARKET", "side": "SELL" if managed["side"] == 1 else "BUY",
                                    "price": stop_tick, "id": str(protected["algoId"]), "client": client, "conditional": True})
            except (ccxt.BaseError, ExchangeError):
                if not matching:
                    self.close_market(symbol, current)
                raise
        for order in matching:
            if abs(order["price"] - stop_tick) >= 1e-12:
                self.exchange.cancel(order)
                self.orders.remove(order)

    def reconcile(self, stamp):
        risk = Risk(**self.data["risk"]) if self.data["risk"] else None
        for symbol, managed in list(self.data["positions"].items()):
            if symbol in self.broker_positions:
                self.ensure_protection(symbol, managed, self.broker_positions[symbol])
                continue
            order = self.exchange.find_entry(symbol, managed["id"])
            if not order:
                if stamp - managed["opened"] > 120 and not managed.get("entry"):
                    del self.data["positions"][symbol]
                    self.save()
                continue
            if order["status"] in ("NEW", "PARTIALLY_FILLED"):
                continue
            if not float(order["executedQty"]):
                del self.data["positions"][symbol]
                self.save()
                continue
            actual = self.exchange.outcome(symbol, int(order["time"]))
            if actual is None:
                continue
            trade = {**actual, "id": managed["id"], "symbol": symbol, "side": managed["side"],
                     "opened": managed["opened"], "mode": self.mode, "scope": managed["scope"]}
            family = managed.get("family")
            if family:
                trade["family"] = family
                ledger = self.budget(family)
                ledger["exits"] = prune_budget(ledger["exits"], stamp) + [trade["closed"]]
            if risk:
                risk.closed(trade["closed"], trade["pnl"])
                self.data["risk"] = asdict(risk)
                if risk.streak == 6:
                    self.event("alert", "Six consecutive net losses. New entries paused for two hours.")
            del self.data["positions"][symbol]
            self.save(trade)
            self.event("trade", f"{self.mode.upper()} {symbol} closed · net {trade['pnl']:+.4f} USDT")
        for order in self.orders:
            if order["conditional"] and order["client"].startswith("st-") and order["symbol"] not in self.data["positions"]:
                self.exchange.cancel(order)

    def budget(self, family):
        return self.data.setdefault("trade_budget", {}).setdefault(family, {"entries": [], "exits": []})

    def enter(self, stamp, data, risk, plan):
        allocation = 1 / len(plan.families)
        hard_cap = min(MAX_RISK, PRESETS[plan.preset].risk)
        for family in plan.families:
            candidates = data.trend if family == "A" else data.reversion
            ledger = self.budget(family)
            ledger["entries"] = prune_budget(ledger["entries"], stamp)
            for symbol, signal in candidates.items():
                side = int(signal[0])
                if not side:
                    continue
                if self.interrupt.is_set() or int(time.time()) - (stamp // BAR * BAR) > 90:
                    return
                managed = self.data["positions"]
                existing = [(p.get("family"), p["side"]) for p in managed.values()]
                active = sum(kind == family for kind, _ in existing)
                if symbol in managed or not eligible(family, side, existing):
                    continue
                if not within_budget(family, stamp, ledger["entries"], ledger["exits"], active):
                    continue
                # A pending entry may still fill. Do not reuse its capacity until reconciliation.
                if managed.keys() - self.broker_positions.keys():
                    return
                mark = self.exchange.mark(symbol)
                gap = stop_gap(family, mark, data.atr[symbol][0], plan.trend, plan.reversion)
                gross = sum(p["size"] * p["mark"] for p in self.broker_positions.values())
                reserved = sum(
                    p["size"] * (max(0.0, p["side"] * (p["mark"] - (managed[s].get("stop") or p["entry"])))
                                 + p["mark"] * (FEE + SLIP))
                    for s, p in self.broker_positions.items()
                )
                quantity = size_position(plan.preset, family, side, self.equity, gross, reserved,
                                         mark, gap, risk.scale(self.equity), allocation)
                rounded = self.exchange.quantity(symbol, quantity, mark)
                if not rounded:
                    self.event("info", f"{family} {symbol} skipped: exchange minimum or available risk budget")
                    continue
                self.exchange.prepare_symbol(symbol)
                if self.interrupt.is_set():
                    return
                client_id = "st-e-" + uuid.uuid4().hex[:24]
                managed[symbol] = {
                    "id": client_id, "family": family, "side": side, "opened": stamp, "gap": gap,
                    "scope": self.scope(self.mode),
                    "risk_budget": min(self.equity * hard_cap * allocation * risk.scale(self.equity)
                                       * (0.5 if family == "B" and side == -1 else 1),
                                       float(rounded) * (gap + (mark + gap) * 2 * (FEE + SLIP))),
                }
                ledger["entries"].append(stamp)
                self.save()
                self.exchange.market_order(symbol, side, rounded, client_id)
                self.refresh()
                if symbol not in self.broker_positions:
                    raise ExchangeError(f"{symbol} entry is awaiting exchange reconciliation")
                self.ensure_protection(symbol, managed[symbol], self.broker_positions[symbol])

    def tick(self):
        if not self.exchange:
            self.connect(self.mode)
        if self.data["flatten"]:
            self.flatten()
            return
        self.refresh()
        stamp = int(time.time())
        if set(self.broker_positions) - self.data["positions"].keys():
            self.running = False
            raise ExchangeError("Unmanaged exchange exposure found. Use the kill switch to flatten.")
        risk = Risk(**self.data["risk"]) if self.data["risk"] else Risk(self.equity, self.equity, stamp // DAY)
        was_daily, was_halted = risk.daily_locked, risk.halted
        risk.observe(stamp, self.equity, self.preset)
        self.data["risk"] = asdict(risk)
        self.save()
        if risk.halted:
            self.running = False
            if not was_halted:
                self.event("alert", "Maximum drawdown hit. Manual reset required.")
        if risk.daily_locked and not was_daily:
            self.event("alert", "Daily loss limit reached. Entries paused until the next UTC day.")
        if risk.daily_locked or risk.halted:
            for symbol, position in self.broker_positions.items():
                self.close_market(symbol, position)
            self.refresh()
        else:
            expired = []
            for symbol, position in self.broker_positions.items():
                managed = self.data["positions"][symbol]
                family = managed.get("family")
                if family and stamp - managed["opened"] >= (30 if family == "A" else 3) * DAY:
                    self.close_market(symbol, position)
                    expired.append(symbol)
            if expired:
                self.refresh()
        self.reconcile(stamp)
        risk = Risk(**self.data["risk"])
        plan = self.plan()
        current_bar = stamp // BAR * BAR
        new_bar = current_bar > self.data["last_bar"]
        if plan and (new_bar or self.regime is None) and (self.running or self.data["positions"]):
            features = self.exchange.signal_features(plan.universe, stamp, self.interrupt if self.running else None)
            data = build_signals(features, [current_bar], plan.trend, plan.reversion)
            self.regime = bool(data.regime[0])
            for symbol, position in self.broker_positions.items():
                managed = self.data["positions"][symbol]
                family = managed.get("family")
                if family not in plan.families:
                    continue
                reason = exit_reason(family, position["side"], stamp - managed["opened"], self.regime,
                                     data.close[symbol][0], data.mean[symbol][0], new_bar)
                if reason:
                    self.close_market(symbol, position)
                elif family == "A" and new_bar and managed.get("stop"):
                    if current_bar - BAR >= managed["opened"]:
                        managed["favorable"] = max(managed["favorable"], float(data.high[symbol][0]))
                    managed["stop"] = float(trail_stop(managed["stop"], managed["favorable"],
                                                       data.atr[symbol][0], plan.trend.trail_atr))
                    self.ensure_protection(symbol, managed, position)
            self.data["last_bar"] = current_bar
            self.save()
            if new_bar and self.running and risk.permitted(stamp) and int(time.time()) - current_bar <= 90:
                self.enter(stamp, data, risk, plan)
        self.store.sample(self.mode, self.scope(self.mode), stamp, self.equity,
                          self.running and self.regime is not None, continuous=self.healthy)
        self.healthy = self.running and self.regime is not None

    def report_error(self, error):
        self.healthy = False
        # Exchange exceptions can include signed URLs, so store only safe application messages.
        message = str(error) if isinstance(error, ExchangeError) else f"Binance request failed ({type(error).__name__})"
        if message != self.last_error:
            self.event("error", message)
        self.last_error = message

    def loop(self):
        while not self.stopping.is_set():
            with self.lock:
                if self.running or self.data["positions"] or self.data["flatten"]:
                    try:
                        self.tick()
                        self.last_error = None
                    except Exception as error:  # noqa: BLE001 — keep monitoring after recoverable failures
                        self.report_error(error)
            self.stopping.wait(12)

    def snapshot(self):
        with self.lock:
            stamp = int(time.time())
            risk = self.data["risk"]
            scope = self.scope(self.mode)
            marks = self.store.chart(self.mode, scope, stamp - 28 * DAY)
            pnl = {}
            for key, boundary in (("daily", stamp // DAY * DAY), ("weekly", stamp - 7 * DAY)):
                baseline = self.store.baseline(self.mode, scope, boundary)
                pnl[key] = self.equity - baseline if self.equity is not None and baseline is not None else None
            positions = [{"symbol": symbol, **position, "stop": self.data["positions"].get(symbol, {}).get("stop"),
                          "target": self.data["positions"].get(symbol, {}).get("target")}
                         for symbol, position in self.broker_positions.items()]
            gate = self.gate()
            plan = self.plan()
            release = self.store.get("edge_release", {})
            return {"mode": self.mode, "running": self.running, "equity": self.equity,
                    "strategy": {"preset": self.preset, "families": list(plan.families) if plan else []},
                    "edge_available": plan is not None,
                    "edge_status": release.get("status", {}).get(self.preset, "UNDER REVIEW"),
                    "positions": positions, "orders": self.orders, "verified_at": self.verified_at,
                    "regime": self.regime, "pnl": pnl, "curve": marks, "risk": risk, "killed": self.data["killed"],
                    "flatten_pending": self.data["flatten"], "error": self.last_error, "gate": gate,
                    "paper_configured": all(self.credentials("paper")),
                    "live_available": gate["passed"] and os.getenv("ENABLE_LIVE_TRADING") == "true"
                    and all(self.credentials("live"))}
