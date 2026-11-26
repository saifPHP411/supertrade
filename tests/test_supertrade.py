from __future__ import annotations

import gzip
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
from pydantic import ValidationError

from app.bot import Bot
from app.config import (
    BAR,
    BASELINE_SPEC,
    DAY,
    FEE,
    HOUR,
    SLIP,
    YEAR,
    Reversion,
    TournamentSpec,
    Trend,
)
from app.exchange import Binance, ExchangeError
from app.gate import live_gate
from app.market import aggregate, epoch
from app.research import deployable, edge, folds, monte_carlo, overfit, summary
from app.risk import Risk
from app.simulator import Market, Variant, round_quantity, simulate
from app.store import Store
from app.strategy import (
    Features,
    Signals,
    build_signals,
    eligible,
    exit_reason,
    size_position,
    stop_gap,
    within_budget,
)


def trading_metrics(net=0.15, pf=1.4, drawdown=0.1):
    return {"net_return": net, "profit_factor": pf, "pf_unbounded": False, "max_drawdown": drawdown}


class ResearchTests(unittest.TestCase):
    def test_incomplete_candle_is_excluded(self):
        frame = pd.DataFrame(np.arange(30).reshape(5, 6), index=np.arange(5) * HOUR,
                             columns=["open", "high", "low", "close", "volume", "quote_volume"])
        result = aggregate(frame, BAR)
        self.assertEqual(result.index.tolist(), [BAR])
        self.assertEqual(result.loc[BAR, "close"], frame.iloc[3]["close"])
        self.assertEqual(result.loc[BAR, "quote_volume"], frame.quote_volume.iloc[:4].sum())

    def test_daily_breakout_is_only_available_at_its_close(self):
        dates = np.arange(1, 252) * DAY
        closes = np.arange(len(dates)) + 100.0
        base = pd.DataFrame({"close": closes, "high": closes + 1, "volume": 10., "quote_volume": 10000.}, index=dates)
        base.loc[dates[-1], ["close", "volume"]] = [500., 100.]
        stamps = np.arange(1, dates[-1] // BAR + 1) * BAR
        raw = pd.DataFrame({"high": 501., "low": 490., "close": 500.}, index=stamps)
        features = Features({"BTC": base}, {"BTC": raw})
        signal = build_signals(features, [dates[-1] - BAR, dates[-1]], Trend(), Reversion())
        self.assertEqual(signal.trend["BTC"].tolist(), [0, 1])

    def test_liquidity_uses_quote_volume_and_caps_at_five(self):
        dates = np.arange(1, 252) * DAY
        prices = np.arange(len(dates)) + 100.0
        base = pd.DataFrame({"close": prices, "high": prices + 1, "volume": 10., "quote_volume": 100.}, index=dates)
        daily = {s: base.assign(quote_volume=float(i + 100)) for i, s in enumerate(["BTC", "ETH", "BNB", "XRP", "SOL", "ADA"])}
        features = Features(daily, {s: d.assign(low=d.close - 1) for s, d in daily.items()})
        self.assertEqual(int(sum(features.liquid.iloc[-1])), 5)
        self.assertFalse(features.liquid.iloc[-1]["BTC"])

    def test_mean_reversion_signals_are_regime_conditioned(self):
        dates = np.arange(1, 230) * DAY
        base = pd.DataFrame({"close": np.arange(len(dates)) + 100., "high": 330., "volume": 1.,
                             "quote_volume": 100.}, index=dates)
        stamps = np.arange(1, 40) * BAR + dates[-1]
        four = pd.DataFrame({"high": 101., "low": 99., "close": 100.}, index=stamps)
        copied = four.copy()
        four.loc[stamps[-2], "close"] = 90
        four.loc[stamps[-1], "close"] = 120
        features = Features({"BTC": base, "ETH": base}, {"BTC": copied, "ETH": four})
        features.risk_on = pd.Series([True, False], index=stamps[-2:])
        features.atr["ETH"] = pd.Series(1., index=stamps)
        features.rsi["ETH"] = pd.Series([20., 80.], index=stamps[-2:])
        signal = build_signals(features, [stamps[-2], stamps[-2] + HOUR, stamps[-1]], Trend(), Reversion())
        self.assertEqual(signal.reversion["ETH"].tolist(), [1, 0, -1])

    def test_trade_accounting_has_both_sides_costs_and_settled_short_funding(self):
        window = np.arange(6) * HOUR
        marks = np.array([[100, 100, 100, 100], [100, 101, 99, 100], [100, 101, 99, 100],
                          [100, 101, 99, 100], [95, 96, 94, 95], [95, 96, 94, 95]])
        market = Market(window, {"SOL": marks}, {HOUR: [("SOL", 0.001)]}, {"SOL": (0.001, 0.001, 5., 100.)})
        vol = np.full(6, 0.5)
        signals = Signals({"SOL": np.zeros(6)}, {"SOL": np.array([-1, 0, 0, 0, 0, 0])},
                          {"SOL": vol}, {"SOL": np.array([90., 90., 90., 90., 96., 96.])},
                          {"SOL": marks[:, 3]}, {"SOL": marks[:, 1]}, np.zeros(6, dtype=bool))
        run = simulate(market, [Variant(Trend(), Reversion(), signals)], [(0, {"B": 0})],
                       ("B",), "MAX GROWTH", 0, 6 * HOUR, 50)
        trade, = run.trades
        self.assertEqual(trade["reason"], "mean reversion")
        self.assertEqual(trade["closed"], BAR)
        quantity = trade["gross"] / 5
        self.assertAlmostEqual(trade["fees"], FEE * quantity * (100 * (1 - SLIP) + 95 * (1 + SLIP)))
        self.assertAlmostEqual(trade["slippage"], SLIP * quantity * (100 + 95))
        self.assertAlmostEqual(trade["funding"], quantity * 100 * 0.001)
        m = summary(run)
        self.assertAlmostEqual(m["net_pnl"], trade["net"])
        self.assertAlmostEqual(m["net_pnl"], m["gross_pnl"] - m["fees"] - m["slippage"] + m["funding"])

    def test_both_families_share_four_adjacent_folds(self):
        windows = folds(BASELINE_SPEC.start, BASELINE_SPEC.end)
        self.assertEqual(len(windows), 4)
        self.assertTrue(all(windows[i][2] == windows[i + 1][1] for i in range(3)))
        self.assertEqual((windows[-1][2] - windows[0][1]) // DAY, 730)

    def test_only_qualifying_unseen_metrics_select_deployment(self):
        self.assertFalse(edge(trading_metrics(net=0)))
        self.assertFalse(edge(trading_metrics(pf=1.19)))
        self.assertTrue(edge(trading_metrics(pf=1.2)))
        metrics = {"A": trading_metrics(), "B": trading_metrics(net=0.22)}
        self.assertEqual(deployable(0.29, metrics), ("A", "B"))
        self.assertEqual(deployable(0.30, metrics), ("B",))
        self.assertEqual(deployable(None, {"A": trading_metrics(net=-1), "B": trading_metrics(pf=1.19)}), ())

    def test_overfit_threshold_and_monte_carlo(self):
        self.assertTrue(overfit(trading_metrics(pf=2.1), trading_metrics(pf=1)))
        self.assertFalse(overfit(trading_metrics(pf=2), trading_metrics(pf=1)))
        result = monte_carlo([{"exposure_return": v} for v in (0.01, -0.22, 0.05)])
        self.assertEqual(result["shuffles"], 1000)
        self.assertEqual(result["ruin_probability"], 1.0)
        self.assertIsNone(monte_carlo([])["ruin_probability"])

    def test_search_spec_rejects_short_or_misaligned_windows(self):
        for end in (date(2025, 1, 1), date(2026, 5, 1)):
            with self.assertRaises(ValidationError):
                TournamentSpec(end=end)
        with self.assertRaises(ValidationError):
            TournamentSpec.model_validate({"risk_per_trade": 0.10})
        self.assertFalse(TournamentSpec(capital=100).canonical)

    def test_embedded_result_matches_the_official_spec_and_cost_identity(self):
        path = Path(__file__).resolve().parents[1] / "app/edge-baseline.json.gz"
        with gzip.open(path, "rt") as source:
            report = json.load(source)
        self.assertEqual(report["spec"], BASELINE_SPEC.model_dump(mode="json"))
        self.assertEqual(report["hourly_bars"], 17520)
        for preset in report["presets"].values():
            for family, result in preset["strategies"].items():
                agg = result["aggregate"]
                self.assertAlmostEqual(agg["net_pnl"], agg["gross_pnl"] - agg["fees"] - agg["slippage"] + agg["funding"])
                self.assertLessEqual(agg["max_rolling_round_trips"], 60 if family == "A" else 120)


class RiskTests(unittest.TestCase):
    def test_per_trade_budget_includes_cost_and_half_risk_rules(self):
        args = ("MAX GROWTH", "B", 1, 50, 0, 0, 50, 3, 1)
        full = size_position(*args)
        loss_per_unit = 3 + (50 + 3) * 2 * (FEE + SLIP)
        self.assertLessEqual(full * loss_per_unit, 1)
        self.assertAlmostEqual(size_position(*args, allocation=0.5), full / 2)
        self.assertAlmostEqual(size_position("MAX GROWTH", "B", -1, *args[3:]), full / 2)

    def test_budget_carries_active_round_trips_and_expires_after_one_year(self):
        stamp = 2 * YEAR
        self.assertFalse(within_budget("A", stamp, [stamp - 1] * 60, [], 0))
        self.assertTrue(within_budget("A", stamp, [stamp - YEAR] * 60, [], 0))
        self.assertFalse(within_budget("B", stamp, [], [stamp - 1] * 119, 1))
        self.assertTrue(within_budget("B", stamp, [], [stamp - 1] * 118, 1))

    def test_position_limits_apply_to_the_combined_plan(self):
        self.assertFalse(eligible("B", 1, [("B", 1)] * 3))
        self.assertTrue(eligible("B", -1, [("B", 1)] * 3))
        self.assertFalse(eligible("A", 1, [("B", -1)] * 3))
        self.assertFalse(eligible("B", -1, [("A", 1)] * 3))

    def test_market_minimums_and_step_size(self):
        self.assertEqual(round_quantity((.01, .01, 5, 100), .0999, 100.), .09)
        self.assertEqual(round_quantity((.001, .001, 50, 100), .000999, 49_000.), 0)

    def test_strategy_specific_protection_and_time_stops(self):
        self.assertAlmostEqual(stop_gap("A", 100, 4, Trend(), Reversion()), 2)
        self.assertEqual(exit_reason("A", 1, DAY, False, 100, 101, True), "BTC regime exit")
        self.assertEqual(exit_reason("A", 1, 30 * DAY, True, 100, 101, True), "time stop")
        self.assertEqual(exit_reason("B", 1, 3 * DAY, True, 100, 101, False), "time stop")
        self.assertIsNone(exit_reason("B", 1, DAY, True, 100, 99, False))

    def test_risk_latches_daily_and_drawdown_stops(self):
        risk = Risk(50, 50, 0)
        risk.observe(100, 47, "MAX GROWTH")
        self.assertFalse(risk.permitted(100))
        risk.observe(DAY, 47, "MAX GROWTH")
        self.assertTrue(risk.permitted(DAY))
        risk.observe(DAY + 100, 44.99, "MAX GROWTH")
        self.assertEqual(risk.scale(44.99), 0.5)
        risk.observe(DAY + 800, 39.99, "MAX GROWTH")
        risk.observe(2 * DAY, 51, "MAX GROWTH")
        self.assertTrue(risk.halted)
        self.assertFalse(risk.permitted(2 * DAY))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Store(Path(directory.name) / "test.sqlite3")

    def test_no_edge_paper_and_live_rejected_before_adapter_construction(self):
        bot = Bot(self.db)
        with patch.dict(os.environ, {"ENABLE_LIVE_TRADING": "true"}), patch("app.bot.Binance") as adapter:
            for mode in ("paper", "live"):
                with self.assertRaises(ExchangeError):
                    bot.start(mode)
            adapter.assert_not_called()

    def test_survivor_reaches_paper_but_not_live_adapter(self):
        plan = {"preset": "MAX GROWTH", "universe": list(BASELINE_SPEC.universe),
                "families": ["A"], "evidence": "fixture", "capital": 50}
        self.db.state("edge_release", {"plans": {"MAX GROWTH": plan}})
        with patch.dict(os.environ, {"ENABLE_LIVE_TRADING": "true", "CAPITAL_LIMIT": "50"}):
            bot = Bot(self.db)
            self.assertEqual(bot.plan().families, ("A",))
            with patch("app.bot.Binance") as adapter:
                adapter.return_value.account.return_value = (50, {})
                adapter.return_value.orders.return_value = []
                with self.assertRaises(ExchangeError):
                    bot.start("live")
                adapter.assert_not_called()
                self.assertTrue(bot.start("paper")["running"])
                adapter.assert_called_once()

    def test_every_signed_write_rechecks_live_lock(self):
        auth = Mock(side_effect=ExchangeError("locked"))
        adapter = Binance("live", "fixture-key", "fixture-secret", "demo", auth)
        adapter.exchange = Mock()
        with self.assertRaises(ExchangeError):
            adapter.market_order("BTC", 1, "0.001", "test")
        adapter.exchange.fapiPrivatePostOrder.assert_not_called()
        auth.assert_called_once()

    def test_demo_and_testnet_modes_route_to_distinct_hosts(self):
        for env, host in (("demo", "demo-fapi.binance.com"), ("testnet", "testnet.binancefuture.com")):
            adapter = Binance("paper", "fixture-key", "fixture-secret", env, lambda: None)
            self.assertTrue(adapter.exchange.urls["api"]["fapiPrivate"].startswith(f"https://{host}/"))

    def test_paper_gate_requires_14_days_20_trades_and_one_continuous_weekend(self):
        start = epoch(date(2025, 1, 1))
        def state(days=14, n=20, eq=50.5, fragmented=False):
            intervals = [(start + i * DAY, start + (i + 1) * DAY) for i in range(days)] if fragmented else [(start, start + days * DAY)]
            return SimpleNamespace(evidence=lambda _: (intervals, {"n": n, "expectancy": 0.1}, [50., 51., eq]))
        self.assertTrue(live_gate(state(), "account")["passed"])
        self.assertFalse(live_gate(state(days=13), "account")["passed"])
        self.assertFalse(live_gate(state(n=19), "account")["passed"])
        self.assertFalse(live_gate(state(eq=39.99), "account")["passed"])
        self.assertFalse(live_gate(state(fragmented=True), "account")["passed"])

    def test_heartbeat_gap_breaks_the_attested_session(self):
        for stamp, continuous in ((100, False), (120, True), (130, False), (150, True)):
            self.db.sample("paper", "scope", stamp, 50, True, continuous=continuous)
        self.assertEqual(self.db.evidence("scope")[0], [(100, 120), (130, 150)])

    def test_exploratory_report_never_replaces_official_execution_evidence(self):
        self.db.complete_tournament("explore", {"spec": {}, "qualification": False})
        self.assertIsNone(self.db.get("edge_release"))
        self.db.complete_tournament("official", {"spec": {}, "qualification": True,
                                                "presets": {"MAX GROWTH": {"deployment": None}}})
        self.db.complete_tournament("later", {"spec": {}, "qualification": True,
                                             "presets": {"MAX GROWTH": {"deployment": {"invalid": "never chosen"}}}})
        self.assertEqual(self.db.get("edge_release")["run_id"], "official")

    def test_old_execution_state_is_preserved_without_arming_v1(self):
        self.db.state("strategy", {"donchian": 20, "preset": "SAFE"})
        bot = Bot(self.db)
        self.assertEqual(bot.preset, "MAX GROWTH")
        self.assertIsNone(bot.plan())

    def test_kill_switch_latches_cancels_and_only_reduces_exposure(self):
        bot = Bot(self.db)
        bot.running = True
        regular = {"symbol": "BTC", "id": "1", "client": "st-e-1", "conditional": False}
        stop = {"symbol": "BTC", "id": "2", "client": "st-p-2", "conditional": True}
        adapter = Mock()
        adapter.account.side_effect = [(50, {"BTC": {"size": 0.001, "mark": 95, "side": -1}}), (50, {}), (50, {})]
        adapter.orders.side_effect = [[regular, stop], [stop], []]
        adapter.quantity.return_value = "0.001"
        bot.exchange = adapter
        result = bot.kill()
        self.assertTrue(result["killed"])
        self.assertFalse(result["running"])
        self.assertFalse(result["flatten_pending"])
        self.assertEqual(adapter.cancel.call_count, 2)
        self.assertEqual(adapter.market_order.call_args.args[:3], ("BTC", 1, "0.001"))
        self.assertTrue(adapter.market_order.call_args.kwargs["reduce"])
        self.assertTrue(Bot(self.db).data["killed"])


if __name__ == "__main__":
    unittest.main()
