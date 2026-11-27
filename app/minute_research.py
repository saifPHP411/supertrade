"""Research-only one-minute pullback entries for Binance USD-M perpetuals."""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from math import floor
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from .config import DAY, FEE, PRESETS, SLIP
from .market import BASELINE_RULES, CANDLE_COLUMNS, MarketDataError, archive_item, epoch, parse
from .risk import Risk

MINUTE = 60
SYMBOLS = ("ETH", "SOL", "BNB", "XRP")
BOUNDARIES = tuple(map(epoch, (
    "2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01", "2026-01-01", "2026-07-01",
)))
CAPITAL = 50.0
PRESET = "BALANCED"
DAILY_LIMIT = 4
COOLDOWN = 15 * MINUTE


@dataclass(frozen=True)
class Variant:
    deviation: float
    hold_minutes: int

    @property
    def name(self):
        return f"z{self.deviation:g}-hold{self.hold_minutes}"


VARIANTS = tuple(Variant(z, minutes) for z in (1.5, 2.25) for minutes in (45, 90))


@dataclass
class Market:
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    atr: np.ndarray
    funding: np.ndarray
    signal: dict[float, np.ndarray]
    strength: np.ndarray


def from_frames(candles, funding, times):
    if not np.array_equal(candles.index, times):
        raise MarketDataError("Minute candles are missing, duplicated, or misaligned")
    o, h, low, close, volume = candles[CANDLE_COLUMNS].to_numpy().T
    if (not np.isfinite(candles.values).all() or np.any((low <= 0) | (low > np.minimum(o, close))
            | (h < np.maximum(o, close)) | (volume < 0))):
        raise MarketDataError("Invalid one-minute candles")
    index = np.searchsorted(times, funding.index)
    if (funding.index.has_duplicates or not np.isfinite(funding.values).all()
            or not len(index) or np.any(index >= len(times))
            or not np.array_equal(times[index], funding.index)
            or np.diff(np.r_[times[0], funding.index, times[-1]]).max() > DAY):
        raise MarketDataError("Funding events are missing, duplicated, or misaligned")
    fees = np.zeros(len(times))
    fees[index] = funding.rate.to_numpy() * o[index]

    price = candles.close
    fast = price.ewm(span=60, min_periods=60, adjust=False).mean()
    slow = price.ewm(span=240, min_periods=240, adjust=False).mean()
    up = (fast > slow) & (slow > slow.shift(60))
    down = (fast < slow) & (slow < slow.shift(60))
    mean = price.rolling(30).mean()
    deviation = price.rolling(30).std().replace(0, np.nan)
    z = (price - mean) / deviation
    previous = z.shift(1)
    signals = {}
    for threshold in sorted({variant.deviation for variant in VARIANTS}):
        long = up & (previous <= -threshold) & (z > -threshold) & (z < 0)
        short = down & (previous >= threshold) & (z < threshold) & (z > 0)
        signals[threshold] = long.to_numpy(dtype=np.int8) - short.to_numpy(dtype=np.int8)
    prev_close = np.r_[close[0], close[:-1]]
    true_range = np.maximum(h - low, np.maximum(abs(h - prev_close), abs(low - prev_close)))
    atr = pd.Series(true_range).rolling(30).mean().to_numpy()
    return Market(o, h, low, close, atr, fees, signals, abs(previous.to_numpy()))


def historical(cache, times):
    months = pd.period_range("2024-01", "2026-06", freq="M")
    tasks = [(symbol, month, funding) for symbol in SYMBOLS for month in months for funding in (False, True)]
    parts = {(symbol, funding): [] for symbol in SYMBOLS for funding in (False, True)}
    with httpx.Client(timeout=25, limits=httpx.Limits(max_connections=12)) as client:
        def fetch(task):
            symbol, month, funding = task
            pair = symbol + "USDT"
            if funding:
                key = f"fundingRate/{pair}/{pair}-fundingRate-{month}.zip"
            else:
                key = f"klines/{pair}/1m/{pair}-1m-{month}.zip"
            data = archive_item(client, cache, key)
            if data is None:
                raise MarketDataError(f"Missing Binance archive {key}")
            return symbol, funding, parse(data, funding)

        with ThreadPoolExecutor(max_workers=12) as workers:
            for count, (symbol, funding, frame) in enumerate(workers.map(fetch, tasks), 1):
                parts[symbol, funding].append(frame)
                if count % 30 == 0:
                    print(f"Verified and parsed {count}/{len(tasks)} archives", flush=True)
    result = {}
    for symbol in SYMBOLS:
        candles = pd.concat(parts.pop((symbol, False))).sort_index()
        funding = pd.concat(parts.pop((symbol, True))).sort_index()
        try:
            result[symbol] = from_frames(candles, funding, times)
        except MarketDataError as exc:
            raise MarketDataError(f"{symbol}USDT: {exc}") from exc
    return result


@dataclass
class Position:
    symbol: str
    direction: int
    quantity: float
    entry: float
    stop: float
    target: float
    last_bar: int
    funding: float = 0.0


def size(symbol, equity, price, gap, risk, direction, fee, slippage):
    step, minimum, min_notional, maximum = BASELINE_RULES[symbol]
    risk_budget = equity * PRESETS[PRESET].risk * risk.scale(equity) * (0.5 if direction < 0 else 1.0)
    risk_per_unit = gap + (price + gap) * (2 * fee + slippage)
    limit = min(risk_budget / risk_per_unit, equity / (price * (1 + fee)), maximum)
    quantity = floor(limit / step + 1e-10) * step
    return quantity if quantity >= minimum and quantity * price >= min_notional else 0.0


def candidates(market, variant, left, right):
    offsets = slice(left, right - 1)
    result = np.flatnonzero(np.any([m.signal[variant.deviation][offsets] for m in market.values()], axis=0))
    return result + left + 1


def simulate(times, market, schedule, fee=FEE, slippage=SLIP):
    equity = CAPITAL
    units = []
    fees = gross = funding = volume = 0.0
    risk = Risk(equity, equity, int(schedule[0][0]) // DAY)
    last_close = 0
    last_trade_day, day_count = None, 0
    for start, end, variant in schedule:
        left, right = np.searchsorted(times, (start, end))
        signals = candidates(market, variant, left, right)
        pointer, position, bar = 0, None, left
        while bar < right:
            if position is None:
                if risk.halted:
                    break
                pointer = max(pointer, int(np.searchsorted(signals, bar)))
                if pointer == len(signals):
                    break
                bar = int(signals[pointer])
                pointer += 1
                stamp = int(times[bar])
                risk.observe(stamp, equity, PRESET)
                day = stamp // DAY
                if last_trade_day != day:
                    last_trade_day, day_count = day, 0
                if bar >= right - 1 or not risk.permitted(stamp) or stamp < last_close + COOLDOWN or day_count >= DAILY_LIMIT:
                    bar += 1
                    continue

                ranked = sorted(
                    (name for name, m in market.items() if m.signal[variant.deviation][bar - 1]),
                    key=lambda name: -market[name].strength[bar - 1],
                )
                for name in ranked:
                    m = market[name]
                    direction = int(m.signal[variant.deviation][bar - 1])
                    price = float(m.open[bar]) * (1 + direction * slippage)
                    gap = max(2.5 * m.atr[bar - 1], 0.003 * price)
                    quantity = size(name, equity, price, gap, risk, direction, fee, slippage)
                    if quantity:
                        position = Position(name, direction, quantity, price,
                                            price - direction * gap,
                                            price + direction * max(2 * gap, 0.0075 * price),
                                            min(right - 1, bar + variant.hold_minutes))
                        day_count += 1
                        break
                bar += position is None
                if position is None:
                    continue
                entry_bar = bar

            m, d, q = market[position.symbol], position.direction, position.quantity
            stamp = int(times[bar])
            if bar > entry_bar:
                position.funding -= d * q * m.funding[bar]
            # Orders triggered inside a candle pay the modeled adverse slippage. If
            # both barriers are crossed, process the stop first.
            exit_price = None
            if bar == position.last_bar:
                exit_price = m.open[bar]
            elif d == 1 and m.low[bar] <= position.stop:
                exit_price = min(position.stop, m.open[bar])
            elif d == -1 and m.high[bar] >= position.stop:
                exit_price = max(position.stop, m.open[bar])
            elif (d == 1 and m.high[bar] >= position.target) or (d == -1 and m.low[bar] <= position.target):
                exit_price = position.target
            else:
                mark = m.close[bar] * (1 - d * slippage)
                marked = equity + d * q * (mark - position.entry) - q * (mark + position.entry) * fee + position.funding
                risk.observe(stamp + MINUTE - 1, marked, PRESET)
                if (risk.halted or risk.daily_locked) and bar + 1 < right:
                    position.last_bar = min(position.last_bar, bar + 1)

            if exit_price is not None:
                filled = float(exit_price) * (1 - d * slippage)
                move = d * q * (filled - position.entry)
                bill = q * (filled + position.entry) * fee
                pnl = move - bill + position.funding
                gross += move
                fees += bill
                funding += position.funding
                volume += q * (filled + position.entry)
                equity += pnl
                units.append(pnl)
                risk.observe(stamp, equity, PRESET)
                risk.closed(stamp, pnl)
                position = None
                last_close = stamp
            bar += 1

    gains = sum(x for x in units if x > 0)
    losses = -sum(x for x in units if x < 0)
    return {
        "trades": len(units), "net_return": float(equity / CAPITAL - 1),
        "net_usdt": float(equity - CAPITAL), "gross_usdt_after_slippage_before_fee_and_funding": float(gross),
        "commission_usdt": float(fees), "funding_usdt": float(funding), "executed_notional_usdt": float(volume),
        "max_drawdown": float(risk.worst_drawdown), "halted": risk.halted,
        "profit_factor": float(gains / losses) if losses else None,
        "win_rate": float(sum(x > 0 for x in units) / len(units)) if units else None,
    }


def study(times, market):
    report = {
        "research_only": True,
        "venue": "Binance USD-M historical archive",
        "symbols": list(SYMBOLS), "capital_usdt": CAPITAL, "risk_preset": PRESET,
        "fee_per_side": FEE, "slippage_per_side": SLIP, "closed_signal_minutes": 1,
        "bars_per_asset": len(times), "folds": [],
    }
    schedule = []
    for fold in range(3):
        train_start, test_start, test_end = BOUNDARIES[fold:fold + 3]
        trained = [(variant, simulate(times, market, [(train_start, test_start, variant)])) for variant in VARIANTS]
        variant, metrics = max(trained, key=lambda pair: pair[1]["net_return"])
        period = (test_start, test_end, variant)
        schedule.append(period)
        out = simulate(times, market, [period])
        report["folds"].append({
            "train_start": train_start, "test_start": test_start, "test_end_exclusive": test_end,
            "selected": variant.name, "train": metrics, "test": out,
        })
        print(f"Closed fold {fold + 1}: {variant.name}, {out['trades']} trades, net {out['net_return']:+.2%}", flush=True)

    holdout = (BOUNDARIES[-2], BOUNDARIES[-1], schedule[-1][2])
    report["walk_forward"] = simulate(times, market, schedule)
    report["holdout"] = {"start": holdout[0], "end_exclusive": holdout[1],
                         "selected": holdout[2].name, **simulate(times, market, [holdout])}
    report["combined_oos"] = simulate(times, market, schedule + [holdout])
    report["higher_cost_oos"] = simulate(times, market, schedule + [holdout], fee=0.0015, slippage=0.001)
    scored = (report["walk_forward"], report["holdout"])
    report["verdict"] = ("MEETS RESEARCH THRESHOLD" if all(
        m["net_return"] > 0 and (m["profit_factor"] or 0) >= 1.2 for m in scored
    ) else "NO EDGE FOUND")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("data/minute-archive"))
    parser.add_argument("--output", type=Path, default=Path("minute-research.json"))
    args = parser.parse_args()
    times = np.arange(BOUNDARIES[0], BOUNDARIES[-1], MINUTE)
    report = study(times, historical(args.cache, times))
    output = json.dumps(report, indent=2, allow_nan=False)
    args.output.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
