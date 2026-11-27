import unittest

import numpy as np
import pandas as pd

from app.risk import Risk
from app.robust_research import Asset, CAPITAL, SYMBOLS, DAY, MINUTE, WARM, Events, Rule, evaluate, fill_size, market_regime


class RobustResearchTests(unittest.TestCase):
    def test_capacity_and_contract_minimum(self):
        risk = Risk(CAPITAL, CAPITAL, 0)
        self.assertEqual(fill_size("XRP", 1.0, 0.004, 50, 4900, 1, risk, 0.001, 0.0005), 0)
        self.assertEqual(fill_size("XRP", 1.0, 0.004, 50, float("nan"), 1, risk, 0.001, 0.0005), 0)
        self.assertAlmostEqual(fill_size("XRP", 1.0, 0.004, 50, 9000, 1, risk, 0.001, 0.0005), 9)

    def test_regime_cannot_see_todays_close(self):
        n = 23 * 1440
        p = np.ones((n, 4))
        p[21 * 1440 - 1, 3] = 1.1
        fixture = {s: Asset(p, np.ones(n), np.zeros(n), pd.DataFrame()) for s in SYMBOLS}
        regime, warm = market_regime(fixture)
        self.assertEqual(warm, 21)
        self.assertEqual(len(regime), 23)
        self.assertFalse(regime[20])
        self.assertTrue(regime[21])

    def test_open_position_in_archive_gap_exits_adversely_on_reopen(self):
        prices = np.ones((DAY // MINUTE, 4))
        prices[2] = np.nan
        asset = Asset(prices, np.full(len(prices), 100_000.), np.zeros(len(prices)), pd.DataFrame())
        market = {s: asset for s in SYMBOLS}
        events = Events(np.array([1]), np.array([3]), np.array([1]), np.array([0.002]),
                        np.array([1.0]), Rule("trend", 60, 24))
        result, trades, _ = evaluate(market, events, WARM, WARM + DAY)
        self.assertEqual(result["trades"], 1)
        self.assertEqual(trades[0].closed, WARM + 3 * MINUTE)
        self.assertLess(trades[0].pnl, -0.30)


if __name__ == "__main__":
    unittest.main()
