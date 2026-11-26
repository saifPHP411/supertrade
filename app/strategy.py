from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import BAR, BUDGETS, DAY, FEE, MAX_RISK, PRESETS, SLIP, YEAR


def ema(series, window):
    return series.ewm(span=window, adjust=False, min_periods=window).mean()


def atr(frame):
    previous = frame.close.shift()
    true_range = pd.concat([frame.high - frame.low, (frame.high - previous).abs(),
                            (frame.low - previous).abs()], axis=1).max(axis=1)
    return true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()


def rsi(close):
    change = close.diff()
    gain = change.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-change.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    return (100 * gain / (gain + loss)).mask((gain == 0) & (loss == 0), 50)


def closed_values(series, times):
    return series.reindex(times, method="ffill").to_numpy()


@dataclass
class Features:
    daily: dict
    four: dict
    risk_on: pd.Series = field(init=False)
    liquid: pd.DataFrame = field(init=False)
    atr: dict = field(init=False)
    rsi: dict = field(init=False)

    def __post_init__(self):
        btc = self.daily["BTC"].close
        fast, slow = ema(btc, 100), ema(btc, 200)
        self.risk_on = (btc > fast) & (fast > slow)
        volume = pd.DataFrame({symbol: data.quote_volume.rolling(30).mean() for symbol, data in self.daily.items()})
        self.liquid = volume.rank(axis=1, ascending=False, method="first").le(5)
        self.atr = {symbol: atr(frame) for symbol, frame in self.four.items()}
        self.rsi = {symbol: rsi(frame.close) for symbol, frame in self.four.items()}


@dataclass
class Signals:
    trend: dict
    reversion: dict
    atr: dict
    mean: dict
    close: dict
    high: dict
    regime: np.ndarray


def build_signals(features, times, trend, reversion):
    times = np.asarray(times, dtype=np.int64)
    four_boundary = times % BAR == 0
    day_boundary = times % DAY == 0
    regime = closed_values(features.risk_on, times) == True
    a_entries, b_entries, atrs, means, closes, highs = {}, {}, {}, {}, {}, {}
    for symbol, daily in features.daily.items():
        four = features.four[symbol]
        qualified = closed_values(features.liquid[symbol], times) == True
        previous_high = daily.high.shift().rolling(trend.breakout_days).max()
        a = ((daily.close > previous_high) & (daily.close.pct_change(30) > 0) &
             (daily.volume > trend.volume_ratio * daily.volume.shift().rolling(30).mean()))
        a_entries[symbol] = (day_boundary & regime & qualified &
                             (closed_values(a, times) == True)).astype(np.int8)
        means[symbol] = closed_values(four.close.rolling(reversion.mean_bars).mean(), times)
        closes[symbol] = closed_values(four.close, times)
        highs[symbol] = closed_values(four.high, times)
        atrs[symbol] = closed_values(features.atr[symbol], times)
        momentum = closed_values(features.rsi[symbol], times)
        band = reversion.deviation_atr * atrs[symbol]
        long = regime & (closes[symbol] < means[symbol] - band) & (momentum < reversion.rsi_low)
        short = ~regime & (closes[symbol] > means[symbol] + band) & (momentum > 100 - reversion.rsi_low)
        b_entries[symbol] = (four_boundary & qualified) * (long.astype(np.int8) - short.astype(np.int8))
    return Signals(a_entries, b_entries, atrs, means, closes, highs, regime)


def stop_gap(family, price, volatility, trend, reversion):
    if not np.isfinite(volatility) or volatility <= 0:
        return 0.0
    if family == "A":
        return min(price * 0.02, trend.trail_atr * volatility)
    return reversion.protection_atr * volatility


def trail_stop(stop, favorable, volatility, multiplier):
    if np.isfinite(volatility) and volatility > 0:
        return max(stop, favorable - multiplier * volatility)
    return stop


def exit_reason(family, side, held, risk_on, settled_close, mean, four_boundary):
    if family == "A" and not risk_on:
        return "BTC regime exit"
    if held >= (30 if family == "A" else 3) * DAY:
        return "time stop"
    if family == "B" and four_boundary and side * (settled_close - mean) >= 0:
        return "mean reversion"
    return None


def eligible(family, side, existing):
    positions = list(existing)
    if sum(direction == side for _, direction in positions) >= 3:
        return False
    return len(positions) < 3 or (family != "A" and all(f != "A" for f, _ in positions))


def prune_budget(stamps, now):
    return [stamp for stamp in stamps if stamp > now - YEAR]


def within_budget(family, now, entries, exits, active):
    limit = BUDGETS[family]
    return len(prune_budget(entries, now)) < limit and len(prune_budget(exits, now)) + active < limit


def size_position(preset, family, side, equity, gross, reserved, price, gap, scale, allocation=1.0):
    if equity <= 0 or price <= 0 or not 0 < gap < price:
        return 0.0
    limits = PRESETS[preset]
    budget = equity * min(MAX_RISK, limits.risk) * scale * allocation
    if family == "B" and side == -1:
        budget *= 0.5
    budget = min(budget, max(0.0, equity * limits.daily_loss - reserved))
    loss_per_unit = gap + (price + gap) * 2 * (FEE + SLIP)
    return max(0.0, min(budget / loss_per_unit, (equity * 0.98 - gross) / price))
