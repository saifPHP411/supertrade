import os
import unittest
from unittest.mock import patch

from app.paper_preflight import verify_demo


class PaperPreflightTest(unittest.TestCase):
    def test_only_reads_demo_when_live_flag_is_false(self):
        flags = {"RAILWAY_SERVICE_ID": "fixture", "BINANCE_PAPER_ENV": "demo",
                 "ENABLE_LIVE_TRADING": "false", "BINANCE_PAPER_KEY": "fixture-key",
                 "BINANCE_PAPER_SECRET": "fixture-secret"}
        with patch.dict(os.environ, flags), patch("app.paper_preflight.Binance") as factory:
            adapter = factory.return_value
            adapter.account.return_value = (50., {})
            adapter.orders.return_value = []
            with self.assertLogs("uvicorn.error", "INFO") as logs:
                verify_demo()
            self.assertIn("AUTHENTICATED positions=0 orders=0 live=false", logs.output[0])
            adapter.verify_mode.assert_called_once()
            adapter.market_order.assert_not_called()
            self.assertEqual(factory.call_args.args[0], "paper")
            os.environ["ENABLE_LIVE_TRADING"] = "true"
            with self.assertLogs("uvicorn.error", "WARNING"):
                verify_demo()
            factory.assert_called_once()
