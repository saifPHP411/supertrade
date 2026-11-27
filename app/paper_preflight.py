"""Read-only Binance Demo authentication check for the hosted service."""

import logging
import os

from .exchange import Binance, ExchangeError

logger = logging.getLogger("uvicorn.error")


def verify_demo():
    if not os.getenv("RAILWAY_SERVICE_ID"):
        return
    key = os.getenv("BINANCE_PAPER_KEY")
    secret = os.getenv("BINANCE_PAPER_SECRET")
    if not key or not secret:
        logger.info("SUPERTRADE_PREFLIGHT WAITING_FOR_CREDENTIALS")
        return
    if os.getenv("ENABLE_LIVE_TRADING") != "false" or os.getenv("BINANCE_PAPER_ENV") != "demo":
        logger.warning("SUPERTRADE_PREFLIGHT FAILED execution_mode")
        return

    def reject_write():
        raise ExchangeError("Paper preflight is read-only")

    try:
        exchange = Binance("paper", key, secret, "demo", reject_write)
        exchange.verify_mode()
        _, positions = exchange.account()
        orders = exchange.orders()
    except Exception as exc:  # noqa: BLE001 — do not allow a signed request error into the runtime log
        detail = str(exc).lower()
        checks = (
            ("restricted_location", "unavailable from a restricted location" in detail or " 451 " in detail),
            ("key_ip_or_permissions", "-2015" in detail),
            ("invalid_signature", "-1022" in detail),
            ("timestamp", "-1021" in detail),
        )
        safe = next((label for label, matched in checks if matched), "unclassified")
        logger.warning("SUPERTRADE_PREFLIGHT FAILED %s %s", type(exc).__name__, safe)
    else:
        logger.info("SUPERTRADE_PREFLIGHT AUTHENTICATED positions=%s orders=%s live=false",
                    len(positions), len(orders))
