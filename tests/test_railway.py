from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from app import main
from app.bot import Bot
from app.config import BASELINE_SPEC, TournamentSpec
from app.exchange import ExchangeError
from app.store import Store


class RailwayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / "state.sqlite3")
        environ = patch.dict(os.environ, {"RAILWAY_SERVICE_ID": "sandbox", "BINANCE_PAPER_ENV": "demo",
                                          "SUPERTRADE_RESUME_PAPER": "true", "CAPITAL_LIMIT": "50"})
        environ.start()
        self.addCleanup(environ.stop)
        plan = {"preset": "MAX GROWTH", "universe": list(BASELINE_SPEC.universe),
                "families": ["A"], "evidence": "fixture", "capital": 50}
        self.store.state("edge_release", {"plans": {"MAX GROWTH": plan}})

    def recovered_bot(self, positions, orders, broker):
        old = Bot(self.store)
        old.data.update(positions=positions, account=old.account_id("paper"),
                        last_bar=14400, resume=old.scope("paper"), broker_snapshot={"at": 123})
        old.save()
        bot = Bot(Store(self.store.path))
        bot.exchange = broker
        with patch.object(bot, "credentials", wraps=bot.credentials) as credentials:
            credentials.side_effect = lambda _: ("fixture-key", "fixture-secret")
            bot.data["resume"] = bot.scope("paper")
            bot.recover()
        self.assertTrue(bot.recovered)
        self.assertEqual(bot.data["last_bar"], 14400)
        self.assertEqual(bot.orders, orders)
        self.assertNotEqual(bot.data["broker_snapshot"]["at"], 123)
        return bot

    def test_restart_reconciles_live_open_position_without_replaying_entry(self):
        position = {"side": 1, "entry": 140.0, "mark": 141., "size": 0.01, "pnl": 0.01}
        journal = {"id": "st-e-example", "stop": 137.2, "side": 1, "risk_budget": 1.}
        stop_order = {"symbol": "SOL", "id": "existing", "price": 137.2, "client": "st-p-example",
                      "conditional": True, "kind": "STOP_MARKET"}
        broker = Mock()
        broker.account.return_value = (50., {"SOL": position})
        broker.orders.return_value = [stop_order]
        broker.tick_price.return_value = 137.2
        bot = self.recovered_bot({"SOL": journal}, [stop_order], broker)
        self.assertEqual(bot.broker_positions, {"SOL": position})
        broker.market_order.assert_not_called()
        broker.protection.assert_not_called()

    def test_restart_reconciles_closed_order_without_reentering_or_double_booking(self):
        journal = {"id": "st-e-example", "side": 1, "opened": 100, "scope": "verification"}
        broker = Mock()
        broker.account.return_value = (50., {})
        broker.orders.return_value = []
        broker.find_entry.return_value = {"status": "FILLED", "executedQty": "0.01", "time": 101000}
        broker.outcome.return_value = {"pnl": 0.2, "fees": 0.01, "funding": 0., "closed": 150, "fills": []}
        bot = self.recovered_bot({"SOL": journal}, [], broker)
        bot.reconcile(200)
        self.assertEqual(bot.data["positions"], {})
        self.assertEqual(len(self.store.paper_trades("verification")), 1)
        broker.market_order.assert_not_called()

    def test_restart_does_not_resend_reduce_only_close_while_broker_state_catches_up(self):
        bot = Bot(self.store)
        bot.data["close_intents"] = {"SOL": {"id": "st-x-existing", "at": 150, "size": 1.0}}
        bot.exchange = Mock()
        bot.exchange.quantity.return_value = "1"
        bot.exchange.find_entry.return_value = {"status": "FILLED"}
        bot.close_market("SOL", {"size": 1., "mark": 140., "side": 1})
        bot.exchange.market_order.assert_not_called()

    def test_railway_live_lock_remains_even_if_gate_and_opt_in_are_true(self):
        with patch.dict(os.environ, {"ENABLE_LIVE_TRADING": "true"}):
            bot = Bot(self.store)
            with patch.object(bot, "gate", return_value={"passed": True}), self.assertRaises(ExchangeError):
                bot.authorize("live")

    def test_verification_cannot_supply_official_gate_trades_or_uptime(self):
        with patch.dict(os.environ, {"SUPERTRADE_PAPER_PHASE": "verification"}):
            bot = Bot(self.store)
            self.assertNotEqual(bot.scope("paper"), bot.scope("paper", official=True))

    def test_nondefault_costs_are_exploratory(self):
        spec = TournamentSpec(slippage_per_side=0.001)
        self.assertFalse(spec.canonical)

    def test_hosted_dashboard_requires_auth_and_rejects_cross_origin_writes(self):
        password = "fixture-password-for-dashboard-test"
        header = "Basic " + base64.b64encode(f"supertrade:{password}".encode()).decode()
        with (patch.object(main, "ROOT", self.root / "api"), patch.object(main, "RAILWAY", True),
              patch.object(main, "PUBLIC_DOMAIN", "localhost"), patch.object(main, "DASHBOARD_PASSWORD", password),
              TestClient(main.app, base_url="https://localhost") as client):
            self.assertEqual(client.get("/health").json(), {"status": "ok"})
            self.assertEqual(client.get("/api/status").status_code, 401)
            self.assertEqual(client.get("/api/status", headers={"Authorization": header}).status_code, 200)
            denied = client.post("/api/bot/start", json={"mode": "paper"},
                                 headers={"Authorization": header, "Origin": "https://example.org"})
            self.assertEqual(denied.status_code, 403)
