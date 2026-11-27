import unittest

import numpy as np

from app.minute_research import Market, Variant, candidates, simulate


class MinuteResearchTests(unittest.TestCase):
    def market(self, both=False):
        values = np.ones(8)
        signals = np.zeros(8, dtype=np.int8)
        signals[2] = 1
        highs = values.copy()
        lows = values.copy()
        highs[3] = 1.01
        if both:
            lows[3] = 0.99
        return {"XRP": Market(values, highs, lows, values, values * 0.001, values * 0,
                              {1.5: signals}, values)}

    def test_signal_executes_next_bar_and_never_uses_a_previous_fold_signal(self):
        market = self.market()
        variant = Variant(1.5, 45)
        np.testing.assert_array_equal(candidates(market, variant, 0, 8), [3])
        np.testing.assert_array_equal(candidates(market, variant, 3, 8), [])

    def test_stop_precedes_target_and_fees_are_charged_on_both_sides(self):
        times = 1_700_000_000 + np.arange(8) * 60
        variant = Variant(1.5, 45)
        schedule = [(times[0], times[-1] + 60, variant)]
        target = simulate(times, self.market(), schedule)
        stopped = simulate(times, self.market(both=True), schedule)
        self.assertEqual(stopped["trades"], 1)
        self.assertGreater(target["net_usdt"], 0)
        self.assertLess(stopped["net_usdt"], 0)
        self.assertAlmostEqual(stopped["commission_usdt"], stopped["executed_notional_usdt"] * 0.001)


if __name__ == "__main__":
    unittest.main()
