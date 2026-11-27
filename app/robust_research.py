"""Pre-registered futures strategy falsification. This module never places orders."""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from math import floor, sqrt
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from .config import DAY, FEE, PRESETS, SLIP
from .market import BASELINE_RULES, MarketDataError, aggregate, archive_item, epoch, parse
from .risk import Risk

MINUTE = 60
SYMBOLS = ("ETH", "SOL", "BNB", "XRP")
CAPITAL = 50.0
PRESET = "BALANCED"
MIN_TRADES = 100
MIN_PF = 1.20
VOLUME_SHARE = 0.001
WARM, FIRST, HOLDOUT, END = map(epoch, ("2024-01-01", "2024-07-01", "2026-07-01", "2026-11-01"))
BOUNDS = tuple(map(epoch, ("2024-01-01", "2024-07-01", "2025-01-01", "2025-07-01", "2026-01-01", "2026-07-01")))
FIELDS = ["open", "high", "low", "close"]


@dataclass(frozen=True)
class Rule:
    family: str
    minutes: int
    parameter: float

    @property
    def name(self):
        return f"{self.family}-{self.minutes}m-{self.parameter:g}"

    @property
    def hold(self):
        return 1440 if self.family in ("trend", "ema") else 240


GRID = (
    *[Rule("trend", m, n) for m in (15, 60) for n in (16, 32)],
    *[Rule("revert", m, z) for m in (1, 5, 15) for z in (1.75, 2.25)],
)
BASELINES = (Rule("ema", 60, 48), Rule("plain-revert", 15, 2))


@dataclass
class Asset:
    bars: np.ndarray
    turnover60: np.ndarray
    funding: np.ndarray
    candles: pd.DataFrame


@dataclass
class Events:
    bar: np.ndarray
    asset: np.ndarray
    side: np.ndarray
    atr: np.ndarray
    strength: np.ndarray
    rule: Rule

    def between(self, left, right):
        return np.searchsorted(self.bar, (left, right))


def downloaded(cache, times):
    months = pd.period_range("2024-01", "2026-10", freq="M")
    plan = [(s, m, fund) for s in SYMBOLS for m in months for fund in (False, True)]
    parts = {(s, fund): [] for s in SYMBOLS for fund in (False, True)}
    with httpx.Client(timeout=25, limits=httpx.Limits(max_connections=12)) as client:
        def fetch(task):
            symbol, month, fund = task
            pair = symbol + "USDT"
            key = (f"fundingRate/{pair}/{pair}-fundingRate-{month}.zip" if fund
                   else f"klines/{pair}/1m/{pair}-1m-{month}.zip")
            body = archive_item(client, cache, key)
            if body is None:
                raise MarketDataError(f"Missing official archive: {key}")
            return symbol, fund, parse(body, fund, quote=not fund)

        with ThreadPoolExecutor(max_workers=12) as threads:
            for i, (s, fund, frame) in enumerate(threads.map(fetch, plan), 1):
                parts[s, fund].append(frame)
                if i % 34 == 0:
                    print(f"Verified {i}/{len(plan)} monthly archives", flush=True)
    final = {}
    for s in SYMBOLS:
        candles = pd.concat(parts.pop((s, False))).sort_index()
        if not np.array_equal(candles.index, times) or not np.isfinite(candles.values).all():
            raise MarketDataError(f"Incomplete or invalid 1m history for {s}")
        bars = np.ascontiguousarray(candles[FIELDS].to_numpy())
        o, high, low, c = bars.T
        if np.any((low <= 0) | (low > np.minimum(o, c)) | (high < np.maximum(o, c))
                  | (candles.quote_volume < 0)):
            raise MarketDataError(f"Invalid OHLC or quote turnover for {s}")
        fund = pd.concat(parts.pop((s, True))).sort_index()
        offsets = np.searchsorted(times, fund.index)
        if (fund.index.has_duplicates or not np.isfinite(fund.values).all() or not len(offsets)
                or np.any(offsets >= len(times)) or not np.array_equal(times[offsets], fund.index)
                or np.diff(np.r_[times[0], fund.index, times[-1]]).max() > DAY):
            raise MarketDataError(f"Incomplete or invalid funding history for {s}")
        charges = np.zeros(len(times))
        charges[offsets] = fund.rate.to_numpy() * o[offsets]
        final[s] = Asset(bars, candles.quote_volume.rolling(60).mean().shift().to_numpy(), charges, candles)
    return final


def signal_frame(candles, rule):
    if rule.minutes == 1:
        frame = candles.set_axis(candles.index + MINUTE)
    else:
        frame = aggregate(candles, rule.minutes * MINUTE, base=MINUTE)
    c, h, low = frame.close, frame.high, frame.low
    last = c.shift()
    true_range = np.maximum(h - low, np.maximum(abs(h - last), abs(low - last)))
    atr = true_range.rolling(14).mean()
    ema = c.ewm(span=48, adjust=False, min_periods=48).mean()
    slope = ema - ema.shift(6)
    if rule.family == "trend":
        upper = h.shift().rolling(int(rule.parameter)).max()
        lower = low.shift().rolling(int(rule.parameter)).min()
        longs = (c > upper) & (last <= upper.shift()) & (slope > 0)
        shorts = (c < lower) & (last >= lower.shift()) & (slope < 0)
        strength = (c - ema).abs() / atr
    elif rule.family == "ema":
        longs, shorts = (c > ema) & (last <= ema.shift()), (c < ema) & (last >= ema.shift())
        strength = (c - ema).abs() / atr
    else:
        mean = c.rolling(24).mean()
        sd = c.rolling(24).std().replace(0, np.nan)
        z = (c - mean) / sd
        old = z.shift()
        longs = (old <= -rule.parameter) & (z > -rule.parameter) & (z < 0)
        shorts = (old >= rule.parameter) & (z < rule.parameter) & (z > 0)
        if rule.family == "revert":
            longs &= (slope.abs() <= 1.5 * atr) & (c >= ema - 2 * atr)
            shorts &= (slope.abs() <= 1.5 * atr) & (c <= ema + 2 * atr)
        strength = old.abs()
    side = longs.astype(np.int8) - shorts.astype(np.int8)
    selection = (side != 0) & (atr > 0) & np.isfinite(strength)
    return side.loc[selection], atr, strength.loc[selection]


def make_events(data, rule):
    rows = []
    for index, symbol in enumerate(SYMBOLS):
        asset = data[symbol]
        side, atr, strength = signal_frame(asset.candles, rule)
        offsets = (side.index.to_numpy() - WARM) // MINUTE
        rows.append(np.column_stack((offsets, np.full(len(offsets), index),
                                    side.to_numpy(), atr.loc[side.index].to_numpy(), strength.to_numpy())))
    samples = np.concatenate(rows)
    idx = np.lexsort((samples[:, 1], -samples[:, 4], samples[:, 0]))
    bar, asset, side, atr, strength = samples[idx].T
    return Events(bar.astype(np.int64), asset.astype(np.int8), side.astype(np.int8), atr, strength, rule)


@dataclass
class Trade:
    asset: int
    side: int
    opened: int
    closed: int
    pnl: float
    raw: float
    commission: float
    slippage: float
    funding: float
    turnover: float
    minutes: int


def max_dd(values):
    a = np.asarray(values, dtype=float)
    return float(np.max(1 - a / np.maximum.accumulate(a)))


def pf(gains, losses):
    return float(gains / losses) if losses else None


def stats(trades, daily, marked_drawdown, halted, size_skips):
    pnl = np.array([t.pnl for t in trades])
    wins, losses = pnl[pnl > 0], -pnl[pnl < 0]
    e = np.asarray(daily, dtype=float)
    returns = np.diff(e) / e[:-1]
    sd = returns.std(ddof=1) if len(returns) > 1 else 0
    ratio = sum(t.minutes for t in trades) / (len(returns) * 1440)
    fields = {field: float(sum(getattr(t, field) for t in trades))
              for field in ("raw", "commission", "slippage", "funding", "turnover")}
    return {
        "net_return": float(e[-1] / CAPITAL - 1), "without_costs_same_trades": fields["raw"] / CAPITAL,
        "sharpe": float(returns.mean() / sd * sqrt(365)) if sd > 0 else None,
        "profit_factor": pf(wins.sum(), losses.sum()),
        "max_drawdown": max(max_dd(e), float(marked_drawdown)), "halted": bool(halted),
        "trades": len(trades), "win_rate": float(len(wins) / len(trades)) if len(trades) else None,
        "average_win": float(wins.mean()) if len(wins) else None,
        "average_loss": float(losses.mean()) if len(losses) else None,
        "exposure": ratio, "turnover_x": fields["turnover"] / CAPITAL,
        "commission_usdt": fields["commission"], "slippage_usdt": fields["slippage"],
        "funding_pnl_usdt": fields["funding"], "size_skips": size_skips,
    }


def fill_size(symbol, price, gap, equity, turnover, direction, risk, fee, slip):
    if not np.isfinite(turnover) or turnover <= 0:
        return 0
    step, min_qty, min_notional, max_qty = BASELINE_RULES[symbol]
    cap = equity * PRESETS[PRESET].risk * risk.scale(equity) * (0.5 if direction < 0 else 1.0)
    loss = gap + (price + gap) * (2 * fee + 2 * slip)
    limit = min(cap / loss, equity / (price * (1 + fee)), turnover * VOLUME_SHARE / price, max_qty)
    qty = floor(limit / step + 1e-10) * step
    return qty if qty >= min_qty and qty * price >= min_notional else 0


def evaluate(data, events, start, end, *, fee=FEE, slip=SLIP, delay=0, stop_scale=1., allowed=SYMBOLS, regime=None):
    left, right = (start - WARM) // MINUTE, (end - WARM) // MINUTE
    lo, hi = events.between(left, right)
    by_day = np.full((end - start) // DAY, np.nan)
    allowed_ids = {SYMBOLS.index(s) for s in allowed}
    risk = Risk(CAPITAL, CAPITAL, start // DAY)
    equity = CAPITAL
    trades = []
    skipped, next_bar, trade_day, today = 0, left, -1, 0
    price_arrays = [data[s].bars for s in SYMBOLS]
    for n in range(lo, hi):
        i, s, d = int(events.bar[n]) + delay, int(events.asset[n]), int(events.side[n])
        if risk.halted:
            break
        if i < next_bar or i >= right - 1 or s not in allowed_ids:
            continue
        stamp = WARM + i * MINUTE
        if regime is not None and not regime[i // 1440]:
            continue
        risk.observe(stamp, equity, PRESET)
        if trade_day != stamp // DAY:
            trade_day, today = stamp // DAY, 0
        if today >= 4 or not risk.permitted(stamp):
            continue
        symbol = SYMBOLS[s]
        asset = data[symbol]
        prices = price_arrays[s]
        arrival = float(prices[i, 0])
        entered = arrival * (1 + d * slip)
        gap = max(2.5 * events.atr[n], 0.004 * arrival) * stop_scale
        q = fill_size(symbol, entered, gap, equity, asset.turnover60[i], d, risk, fee, slip)
        if not q:
            skipped += 1
            continue
        today += 1
        stop, target = entered - d * gap, entered + d * 2 * gap
        deadline, funding = min(right - 1, i + events.rule.hold), 0.
        for j in range(i, deadline + 1):
            o, h, low, c = prices[j]
            now = WARM + j * MINUTE
            snap = (now - start) // DAY
            if j != i:
                funding -= d * q * asset.funding[j]
            if j == deadline:
                raw_exit = o
            elif d == 1 and low <= stop or d == -1 and h >= stop:
                raw_exit = min(o, stop) if d == 1 else max(o, stop)
            elif d == 1 and h >= target or d == -1 and low <= target:
                raw_exit = target
            else:
                mark = c * (1 - d * slip)
                marked = equity + d * q * (mark - entered) - q * (mark + entered) * fee + funding
                risk.observe(now + MINUTE - 1, marked, PRESET)
                by_day[snap] = marked
                if (risk.daily_locked or risk.halted) and j + 1 < deadline:
                    deadline = j + 1
                continue
            exited = raw_exit * (1 - d * slip)
            gross = d * q * (raw_exit - arrival)
            slip_bill = q * (arrival + raw_exit) * slip
            turnover = q * (entered + exited)
            commission = fee * turnover
            pnl = gross - slip_bill - commission + funding
            equity += pnl
            by_day[snap] = equity
            risk.observe(now, equity, PRESET)
            risk.closed(now, pnl)
            trades.append(Trade(s, d, stamp, now, pnl, gross, commission, slip_bill, funding,
                                turnover, max(1, j - i)))
            next_bar = j + 15
            break
    daily = pd.Series(np.r_[CAPITAL, by_day]).ffill().to_numpy()
    return stats(trades, daily, risk.worst_drawdown, risk.halted, skipped), trades, daily

def pooled(runs):
    all_trades = []
    daily = np.array([CAPITAL])
    worst, stopped, skipped = 0., False, 0
    for result, trades, equity in runs:
        all_trades.extend(trades)
        daily = np.r_[daily, equity[1:] * daily[-1] / CAPITAL]
        worst = max(worst, result["max_drawdown"])
        stopped |= result["halted"]
        skipped += result["size_skips"]
    result = stats(all_trades, daily, worst, stopped, skipped)
    result["gross_fixed_stake_return"] = sum(t.raw for t in all_trades) / CAPITAL
    result["net_fixed_stake_return"] = sum(t.pnl for t in all_trades) / CAPITAL
    result["without_costs_same_trades"] = float(np.prod([
        1 + item[0]["without_costs_same_trades"] for item in runs
    ]) - 1)
    return result, all_trades


def metrics_ok(result):
    return (result["trades"] >= MIN_TRADES and result["net_return"] > 0
            and (result["profit_factor"] or 0) >= MIN_PF and (result["sharpe"] or 0) > 0
            and not result["halted"] and result["max_drawdown"] < 0.20)


def stress_ok(result):
    return result["net_return"] > 0 and (result["profit_factor"] or 0) >= 1 and not result["halted"]


def score(result):
    sharpe = result["sharpe"]
    return (-99 if sharpe is None else sharpe, result["profit_factor"] or 0, result["net_return"])


def concentration(trades):
    if not trades or not any(t.pnl > 0 for t in trades):
        return {"positive_asset_share_max": None, "positive_month_share_max": None,
                "largest_five_wins_share": None, "passes": False}
    asset, month = {}, {}
    wins = []
    for t in trades:
        if t.pnl <= 0:
            continue
        m = pd.Timestamp(t.closed, unit="s").strftime("%Y-%m")
        asset[t.asset] = asset.get(t.asset, 0.) + t.pnl
        month[m] = month.get(m, 0.) + t.pnl
        wins.append(t.pnl)
    total = sum(wins)
    a, m, f = max(asset.values()) / total, max(month.values()) / total, sum(sorted(wins)[-5:]) / total
    return {"positive_asset_share_max": float(a), "positive_month_share_max": float(m),
            "largest_five_wins_share": float(f), "passes": bool(a <= 0.50 and m <= 0.35 and f <= 0.35)}


def causal_atr(data, rule):
    result = []
    for s in SYMBOLS:
        _, atr, _ = signal_frame(data[s].candles, rule)
        index = (atr.index.to_numpy() - WARM) // MINUTE
        expanded = np.repeat(atr.to_numpy(), rule.minutes)
        result.append((int(index[0]), expanded))
    return result


def random_events(source, causal, start, end, rng):
    left, right = (start - WARM) // MINUTE, (end - WARM) // MINUTE
    first, last = source.between(left, right)
    size = last - first
    times = rng.integers(left, right - 1, size)
    assets, side = source.asset[first:last], source.side[first:last]
    atr = np.zeros(size)
    for s, (offset, values) in enumerate(causal):
        mask = assets == s
        idx = times[mask] - offset
        atr[mask] = values[np.maximum(0, idx)]
        atr[mask & (times < offset)] = np.nan
    sort = np.argsort(times, kind="stable")
    idx = sort[np.isfinite(atr[sort]) & (atr[sort] > 0)]
    return Events(times[idx], assets[idx], side[idx], atr[idx], np.ones(len(idx)), source.rule)


def buy_hold(data, symbol, start, end):
    left, right = (start - WARM) // MINUTE, (end - WARM) // MINUTE
    asset = data[symbol]
    o = asset.bars[left:right, 0]
    original = float(o[0])
    entered = original * (1 + SLIP)
    step, min_qty, min_notional, max_qty = BASELINE_RULES[symbol]
    cap = min(CAPITAL / (1 + FEE), VOLUME_SHARE * asset.turnover60[left])
    q = min(max_qty, floor((cap / entered) / step) * step)
    if q < min_qty or q * entered < min_notional:
        return None
    fills = o * (1 - SLIP)
    funding = -q * np.cumsum(np.r_[0., asset.funding[left + 1:right]])
    equity = CAPITAL + q * (fills - entered) - FEE * q * (fills + entered) + funding
    bill = float(FEE * q * (fills[-1] + entered))
    drag = float(SLIP * q * (original + o[-1]))
    t = Trade(SYMBOLS.index(symbol), 1, start, end - MINUTE, float(equity[-1] - CAPITAL),
              float(q * (o[-1] - original)), bill, drag, float(funding[-1]),
              float(q * (fills[-1] + entered)), right - left)
    return stats([t], np.r_[CAPITAL, equity[1439::1440]], max_dd(np.r_[CAPITAL, equity]), False, 0)


def market_regime(data):
    basket = np.vstack([d.bars[1439::1440, 3] for d in data.values()])
    # A UTC day's basket return is available at the next day's open.
    closed = np.mean(basket[:, 20:] / basket[:, :-20] - 1, axis=0) > 0
    return np.r_[np.zeros(21, dtype=bool), closed[:-1]], 21


def perturb(rule, direction):
    parameter = (round(rule.parameter * (1 + direction * 0.20))
                 if rule.family == "trend" else round(rule.parameter + direction * 0.25, 2))
    return Rule(rule.family, rule.minutes, parameter)


def test(data):
    event_cache = {rule: make_events(data, rule) for rule in GRID}

    def hist(rule):
        if rule not in event_cache:
            event_cache[rule] = make_events(data, rule)
        return event_cache[rule]

    families = [(family, minutes) for family, minutes in dict.fromkeys(
        (r.family, r.minutes) for r in GRID
    )]
    wfruns, wf_rules, training, all_results = {}, {}, {}, {}
    for family, minutes in families:
        key = f"{family}-{minutes}m"
        rules = [r for r in GRID if (r.family, r.minutes) == (family, minutes)]
        results, selected, fits = [], [], []
        for fold in range(4):
            start, split, end = BOUNDS[fold:fold + 3]
            train = [(r, evaluate(data, hist(r), start, split)[0]) for r in rules]
            chosen, fit = max(train, key=lambda item: score(item[1]))
            selected.append(chosen)
            fits.append({r.name: m for r, m in train})
            results.append(evaluate(data, hist(chosen), split, end))
        wfruns[key], wf_rules[key], training[key] = results, selected, fits
        aggregate_result, _ = pooled(results)
        annotated = {"aggregate": aggregate_result, "test_folds": [one[0] for one in results],
                     "fold_rules": [r.name for r in selected], "training": fits}
        all_results[key] = annotated
        print(f"Walk-forward {key}: net {aggregate_result['net_return']:+.2%}, "
              f"PF {aggregate_result['profit_factor']}, trades {aggregate_result['trades']}", flush=True)

    def ranking(key):
        results = all_results[key]
        robust = min(score(fold)[0] for fold in results["test_folds"])
        return (metrics_ok(results["aggregate"]), robust, *score(results["aggregate"]))

    selected = max(all_results, key=ranking)
    chosen_rule = wf_rules[selected][-1]
    wf_windows = [(BOUNDS[i + 1], BOUNDS[i + 2]) for i in range(4)]

    def walk(rules=None, **stress):
        use = wf_rules[selected] if rules is None else rules
        return pooled([evaluate(data, hist(r), a, b, **stress)
                       for (a, b), r in zip(wf_windows, use)])[0]

    def both(rules=None, hold=None, **kwargs):
        return {"walk_forward": walk(rules, **kwargs),
                "holdout": evaluate(data, hist(chosen_rule if hold is None else hold), HOLDOUT, END, **kwargs)[0]}

    wf, wf_trades = pooled(wfruns[selected])
    oos, trades, _ = evaluate(data, hist(chosen_rule), HOLDOUT, END)
    stresses = {
        "double_slippage": both(slip=SLIP * 2),
        "higher_fee": both(fee=FEE * 1.5),
        "joint_costs": both(fee=FEE * 1.5, slip=SLIP * 2),
        "one_minute_delay": both(delay=1),
        "five_minute_delay": both(delay=5),
        "stop_minus_20pct": both(stop_scale=0.8),
        "stop_plus_20pct": both(stop_scale=1.2),
        "parameter_lower": both([perturb(r, -1) for r in wf_rules[selected]], perturb(chosen_rule, -1)),
        "parameter_higher": both([perturb(r, 1) for r in wf_rules[selected]], perturb(chosen_rule, 1)),
    }
    print("Completed holdout and execution stress tests", flush=True)
    per_asset = {s: both(allowed=(s,)) for s in SYMBOLS}
    drop_asset = {s: both(allowed=tuple(x for x in SYMBOLS if x != s)) for s in SYMBOLS}
    regime, warm = market_regime(data)
    regimes = {"positive_trailing_20_day_basket": both(regime=regime),
               "nonpositive_trailing_20_day_basket": both(regime=np.r_[np.zeros(warm, dtype=bool), ~regime[warm:]])}
    month = {}
    for t in trades:
        m = pd.Timestamp(t.closed, unit="s").strftime("%Y-%m")
        month[m] = month.get(m, 0.) + t.pnl
    benchmark = {r.family: both([r] * 4, r) for r in BASELINES}
    benchmark["buy_and_hold_futures"] = {
        s: {"walk_forward": buy_hold(data, s, FIRST, HOLDOUT), "holdout": buy_hold(data, s, HOLDOUT, END)}
        for s in SYMBOLS
    }
    used = set(wf_rules[selected])
    rand_atr = {r: causal_atr(data, r) for r in used}
    random_metrics = {"walk_forward": [], "holdout": []}
    for seed in range(12):
        rng = np.random.default_rng(20261127 + seed)
        jobs = [(start, end, rule) for (start, end), rule in zip(wf_windows, wf_rules[selected])]
        wf_r = pooled([evaluate(data, random_events(hist(rule), rand_atr[rule], start, end, rng), start, end)
                       for start, end, rule in jobs])[0]
        hold_r = evaluate(data, random_events(hist(chosen_rule), rand_atr[chosen_rule], HOLDOUT, END, rng),
                          HOLDOUT, END)[0]
        for side, metrics in (("walk_forward", wf_r), ("holdout", hold_r)):
            random_metrics[side].append(metrics)
    benchmark["matched_random_12_seeds"] = {
        side: {"median_net_return": float(np.median([v["net_return"] for v in values])), "runs": values}
        for side, values in random_metrics.items()
    }
    wf_con, held_con = concentration(wf_trades), concentration(trades)
    neighbors = [name for name in stresses if name.startswith(("parameter_", "stop_"))]
    gates = {
        "walk_forward": metrics_ok(wf), "holdout": metrics_ok(oos),
        "joint_costs": all(metrics_ok(v) for v in stresses["joint_costs"].values()),
        "concentration": wf_con["passes"] and held_con["passes"],
        "parameter_sensitivity": all(stress_ok(v) for name in neighbors for v in stresses[name].values()),
    }
    details = {
        "validation": wf, "holdout": oos, "holdout_rule": chosen_rule.name, "selected_diagnostic_family": selected,
        "ruling": "DEMO" if all(gates.values()) else "REJECT", "gates": gates, "all_families": all_results,
        "cost_and_delay_stress": stresses, "assets": per_asset, "leave_one_asset_out": drop_asset,
        "regimes": regimes, "concentration": {"walk_forward": wf_con, "holdout": held_con},
        "holdout_monthly_realized_pnl_usdt": {k: float(v) for k, v in month.items()},
        "benchmarks": benchmark,
    }
    return details


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("data/robust-archive"))
    parser.add_argument("--output", type=Path, default=Path("robust-research.json"))
    args = parser.parse_args()
    times = np.arange(WARM, END, MINUTE)
    data = downloaded(args.cache, times)
    details = test(data)
    details["sample"] = {"symbols": list(SYMBOLS), "bars_per_asset": len(times), "warm": WARM,
                         "validation_start": FIRST, "holdout_start": HOLDOUT, "end_exclusive": END}
    args.output.write_text(json.dumps(details, indent=2, allow_nan=False) + "\n")
    keep = {k: details[k] for k in ("sample", "validation", "holdout", "holdout_rule",
                                     "selected_diagnostic_family", "ruling", "gates", "concentration",
                                     "holdout_monthly_realized_pnl_usdt")}
    def compact(items):
        return {k: {side: [m["net_return"], m["profit_factor"], m["trades"], m["max_drawdown"]]
                       for side, m in v.items()} for k, v in items.items()}
    for k in ("cost_and_delay_stress", "assets", "regimes"):
        keep[k] = compact(details[k])
    keep["families"] = {k: {f: v["aggregate"][f] for f in ("net_return", "profit_factor", "sharpe", "trades", "max_drawdown")}
                        for k, v in details["all_families"].items()}
    keep["benchmarks"] = {
        k: {side: (m["median_net_return"] if k.startswith("matched") else m["net_return"]) for side, m in v.items()}
        for k, v in details["benchmarks"].items() if k != "buy_and_hold_futures"
    }
    keep["buy_hold"] = {s: {side: m["net_return"] if m else None for side, m in v.items()}
                        for s, v in details["benchmarks"]["buy_and_hold_futures"].items()}
    print("ROBUST_RESULT " + json.dumps(keep, allow_nan=False), flush=True)


if __name__ == "__main__":
    main()
