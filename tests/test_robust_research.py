import unittest

import numpy as np
import pandas as pd

from app.risk import Risk
from app.robust_research import Asset, CAPITAL, SYMBOLS, fill_size, market_regime


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


if __name__ == "__main__":
    unittest.main()
