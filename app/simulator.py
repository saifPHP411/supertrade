from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from .config import BAR, DAY, FEE, HOUR, SLIP
from .risk import Risk
from .strategy import (
    eligible,
    exit_reason,
    prune_budget,
    size_position,
    stop_gap,
    trail_stop,
    within_budget,
)


@dataclass
class Position:
    family: str
    side: int
    variant: int
    opened: int
    quantity: float
    raw_entry: float
    fill: float
    stop: float
    favorable: float
    equity_at_entry: float
    fee: float
    funding: float = 0.0


@dataclass
class Market:
    times: np.ndarray
    candles: dict
    funding: dict
    rules: dict

    @classmethod
    def build(cls, frames, funding, rules, start, end):
        times = np.arange(start, end, HOUR)
        candles = {s: frame.reindex(times)[["open", "high", "low", "close"]].to_numpy()
                   for s, frame in frames.items()}
        settles = defaultdict(list)
        for symbol, rates in funding.items():
            for stamp, rate in rates.rate.loc[start:end - 1].items():
                settles[int(stamp // HOUR * HOUR)].append((symbol, float(rate)))
        return cls(times, candles, settles, rules)


@dataclass
class Variant:
    trend: object
    reversion: object
    signals: object


@dataclass
class Simulation:
    times: np.ndarray
    equity: np.ndarray
    fees: np.ndarray
    slippage: np.ndarray
    funding: np.ndarray
    drawdowns: np.ndarray
    trades: list
    hard_stop: bool


def round_quantity(rules, unrounded, mark):
    step, minimum, min_cost, maximum = rules
    amount = np.floor(unrounded / step + 1e-10) * step
    return float(amount) if minimum <= amount <= maximum and amount * mark >= min_cost else 0.0


def simulate(market, variants, schedule, family_names, preset, start, end, capital):
    # schedule is chronological and changes only the parameters of new entries.
    left, right = np.searchsorted(market.times, (start, end))
    if right <= left:
        raise ValueError("Simulation requires hourly observations")
    positions, trades = {}, []
    entries, exits = defaultdict(list), defaultdict(list)
    cash = equity = float(capital)
    risk = Risk(capital, capital, start // DAY)
    marks = np.zeros((right - left + 1, 5))
    marks[0, 0] = capital
    fees = slip = funding = 0.0
    allocation = 1 / len(family_names)
    schedule_cursor = 0
    candidate_rows = {}
    for f in family_names:
        for index, variant in enumerate(variants):
            per_symbol = variant.signals.trend if f == "A" else variant.signals.reversion
            for symbol, series in per_symbol.items():
                for row in np.flatnonzero(series[left:right]) + left:
                    candidate_rows.setdefault((f, index, row), []).append((symbol, int(series[row])))

    def close(symbol, raw, stamp, bucket, reason):
        nonlocal cash, fees, slip
        p = positions.pop(symbol)
        fill = raw * (1 - p.side * SLIP)
        fee = fill * p.quantity * FEE
        fees += fee
        slip += raw * p.quantity * SLIP
        cash += p.side * (fill - p.fill) * p.quantity - fee
        gross_pnl = p.side * (raw - p.raw_entry) * p.quantity
        slippage = p.quantity * SLIP * (raw + p.raw_entry)
        net = gross_pnl - p.fee - fee - slippage + p.funding
        exits[p.family] = prune_budget(exits[p.family], stamp)
        exits[p.family].append(stamp)
        trades.append({
            "family": p.family, "symbol": symbol, "side": p.side, "opened": p.opened, "closed": stamp,
            "hour": bucket, "net": float(net), "gross": float(gross_pnl), "fees": float(p.fee + fee),
            "slippage": float(slippage), "funding": float(p.funding), "exposure_return": float(net / p.equity_at_entry),
            "reason": reason,
        })
        risk.closed(stamp, net)

    for row in range(left, right):
        stamp = int(market.times[row])
        if schedule_cursor + 1 < len(schedule) and stamp >= schedule[schedule_cursor + 1][0]:
            schedule_cursor += 1
        selection = schedule[schedule_cursor][1]
        for symbol, rate in market.funding.get(stamp, ()):
            if symbol in positions:
                p = positions[symbol]
                payment = -p.quantity * p.side * market.candles[symbol][row, 0] * rate
                p.funding += payment
                cash += payment
                funding += payment

        for symbol, p in list(positions.items()):
            opening = market.candles[symbol][row, 0]
            variant = variants[p.variant]
            data = variant.signals
            if p.side * (opening - p.stop) <= 0:
                close(symbol, opening, stamp, stamp, "stop gap")
                continue
            reason = exit_reason(p.family, p.side, stamp - p.opened, bool(data.regime[row]),
                                 data.close[symbol][row], data.mean[symbol][row], stamp % BAR == 0)
            if reason:
                close(symbol, opening, stamp, stamp, reason)
                continue
            if p.family == "A" and stamp % BAR == 0:
                # The completed bar's maximum is known at this opening, not inside the bar.
                if stamp - BAR >= p.opened:
                    p.favorable = max(p.favorable, data.high[symbol][row])
                p.stop = trail_stop(p.stop, p.favorable, data.atr[symbol][row], variant.trend.trail_atr)
                if opening <= p.stop:
                    close(symbol, opening, stamp, stamp, "trail crossed")

        equity = cash + sum(p.side * p.quantity * (market.candles[s][row, 0] - p.fill)
                            for s, p in positions.items())
        risk.observe(stamp, equity, preset)
        if risk.halted or risk.daily_locked:
            for symbol in list(positions):
                close(symbol, market.candles[symbol][row, 0], stamp, stamp, "equity stop")
            equity = cash
        open_dd = risk.drawdown(equity)
        if risk.permitted(stamp):
            for family in family_names:
                index = selection[family]
                variant = variants[index]
                for symbol, side in candidate_rows.get((family, index, row), ()):
                    if symbol in positions or not eligible(family, side, ((p.family, p.side) for p in positions.values())):
                        continue
                    active = sum(p.family == family for p in positions.values())
                    entries[family] = prune_budget(entries[family], stamp)
                    if not within_budget(family, stamp, entries[family], exits[family], active):
                        continue
                    mark = market.candles[symbol][row, 0]
                    fill = mark * (1 + side * SLIP)
                    gap = stop_gap(family, fill, variant.signals.atr[symbol][row], variant.trend, variant.reversion)
                    gross = sum(p.quantity * market.candles[s][row, 0] for s, p in positions.items())
                    reserved = sum(p.quantity * (max(0, p.side * (market.candles[s][row, 0] - p.stop))
                                                  + market.candles[s][row, 0] * (FEE + SLIP))
                                   for s, p in positions.items())
                    quantity = size_position(preset, family, side, equity, gross, reserved, max(mark, fill),
                                             gap, risk.scale(equity), allocation)
                    quantity = round_quantity(market.rules[symbol], quantity, mark)
                    if not quantity:
                        continue
                    fee = fill * quantity * FEE
                    positions[symbol] = Position(family, side, index, stamp, quantity, mark, fill,
                                                 fill - side * gap, fill, equity, fee)
                    entries[family].append(stamp)
                    fees += fee
                    slip += mark * quantity * SLIP
                    cash -= fee
                    equity -= fee + mark * quantity * SLIP

        for symbol, p in list(positions.items()):
            candle = market.candles[symbol][row]
            adverse = candle[2] if p.side == 1 else candle[1]
            if p.side * (adverse - p.stop) <= 0:
                close(symbol, p.stop, stamp + HOUR, stamp, "protective stop")
        equity = cash + sum(p.side * p.quantity * (market.candles[s][row, 3] - p.fill)
                            for s, p in positions.items())
        risk.observe(stamp, equity, preset)
        if risk.halted or risk.daily_locked or row == right - 1:
            for symbol in list(positions):
                close(symbol, market.candles[symbol][row, 3], stamp + HOUR, stamp,
                      "equity stop" if risk.halted or risk.daily_locked else "window end")
            equity = cash
            risk.observe(stamp, equity, preset)
        marks[row - left + 1] = [equity, fees, slip, funding, max(open_dd, risk.drawdown(equity))]

    times = np.append(market.times[left:right], end)
    return Simulation(times, *marks.T, trades, risk.halted)
