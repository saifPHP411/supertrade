from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from .config import (
    BAR,
    BASELINE_SPEC,
    DAY,
    ENGINE_VERSION,
    FEE,
    HARD_DRAWDOWN,
    PRESETS,
    SLIP,
    YEAR,
    Deployment,
    Reversion,
    TournamentSpec,
    Trend,
)
from .market import aggregate, epoch, historical, market_rules, spot_daily
from .simulator import Market, Variant, simulate
from .strategy import Features, build_signals

STRESS = (("2021 Q1 trend", date(2021, 1, 1), date(2021, 4, 1)),
          ("2022 summer chop", date(2022, 6, 1), date(2022, 9, 1)))
FAMILIES = ("A", "B")


def variant_settings(spec):
    return [
        (spec.trend, spec.reversion),
        (Trend.model_validate({**spec.trend.model_dump(), "trail_atr": max(1.5, spec.trend.trail_atr - 1)}),
         Reversion.model_validate({**spec.reversion.model_dump(),
                                   "protection_atr": max(2, spec.reversion.protection_atr - 0.75)})),
        (Trend.model_validate({**spec.trend.model_dump(), "trail_atr": min(5, spec.trend.trail_atr + 1),
                               "volume_ratio": min(2, spec.trend.volume_ratio + 0.15)}),
         Reversion.model_validate({**spec.reversion.model_dump(), "protection_atr": min(6, spec.reversion.protection_atr + 0.75),
                                   "deviation_atr": min(4, spec.reversion.deviation_atr + 0.25)})),
    ]


def folds(start, end):
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    cursor = first + pd.DateOffset(months=12)
    result = []
    while cursor + pd.DateOffset(months=6) <= last:
        result.append((epoch(cursor - pd.DateOffset(months=12)), epoch(cursor), epoch(cursor + pd.DateOffset(months=6))))
        cursor += pd.DateOffset(months=6)
    return result


def summary(run, start=None, end=None, trades=None):
    start = int(run.times[0]) if start is None else start
    end = int(run.times[-1]) if end is None else end
    a, b = np.searchsorted(run.times, (start, end))
    ledger = run.trades if trades is None else trades
    selected = [t for t in ledger if start <= t["hour"] < end]
    initial = run.equity[a]
    denominator = initial if initial > 0 else None
    change = run.equity[b] - initial
    fees = float(run.fees[b] - run.fees[a])
    slippage = float(run.slippage[b] - run.slippage[a])
    funding = float(run.funding[b] - run.funding[a])
    gross = float(change + fees + slippage - funding)
    wins = [t["net"] for t in selected if t["net"] > 0]
    losses = [-t["net"] for t in selected if t["net"] < 0]
    curve = run.equity[a:b + 1]
    peak = np.maximum.accumulate(curve)
    window_drawdown = float(max(0, np.max(1 - np.divide(curve, peak, out=np.ones_like(curve), where=peak > 0))))
    if a == 0:
        window_drawdown = max(window_drawdown, float(max(run.drawdowns[a:b + 1])))
    rolling = []
    first = 0
    for last, trade in enumerate(selected):
        while selected[first]["closed"] <= trade["closed"] - YEAR:
            first += 1
        rolling.append(last - first + 1)
    return {
        "gross_return": gross / float(denominator) if denominator else None,
        "net_return": float(change / denominator) if denominator else None,
        "gross_pnl": gross, "net_pnl": float(change), "fees": fees, "slippage": slippage, "funding": funding,
        "fee_to_gross": fees / gross if gross > 0 else None,
        "profit_factor": sum(wins) / sum(losses) if losses else None, "pf_unbounded": bool(wins and not losses),
        "max_drawdown": window_drawdown,
        "trades": len(selected), "trades_year": len(selected) * YEAR / (end - start),
        "max_rolling_round_trips": max(rolling, default=0),
        "average_hold_days": float(np.mean([(t["closed"] - t["opened"]) / DAY for t in selected])) if selected else None,
        "hard_stop": run.hard_stop if b == len(run.times) - 1 else bool(np.max(run.drawdowns[:b + 1]) >= HARD_DRAWDOWN),
    }


def edge(metrics):
    return bool(metrics["net_return"] is not None and metrics["net_return"] > 0
                and (metrics["pf_unbounded"] or (metrics["profit_factor"] or 0) >= 1.2))


def factor(metrics):
    return float("inf") if metrics["pf_unbounded"] else metrics["profit_factor"]


def overfit(train, test):
    training, unseen = factor(train), factor(test)
    # No trades on either side gives no estimate to compare. A traded fold without wins has PF zero.
    return training is not None and unseen is not None and training > 2 * unseen


def curve(run):
    return [(int(t), float(v)) for t, v in zip(run.times, run.equity) if t % DAY == 0]


def monte_carlo(trades):
    if not trades:
        return {"trades": 0, "shuffles": 0, "ruin_probability": None, "drawdown_percentiles": None}
    returns = np.array([trade["exposure_return"] for trade in trades])
    rng = np.random.default_rng(91)
    orders = np.array([rng.permutation(returns) for _ in range(1000)])
    paths = np.c_[np.ones(1000), np.cumprod(np.maximum(0, 1 + orders), axis=1)]
    drawdowns = np.max(1 - paths / np.maximum.accumulate(paths, axis=1), axis=1)
    quantiles = [float(x) for x in np.percentile(drawdowns, (5, 50, 95))]
    return {"trades": len(trades), "shuffles": 1000, "ruin_probability": float(np.mean(drawdowns >= HARD_DRAWDOWN)),
            "drawdown_percentiles": quantiles}


def benchmark(frame, capital):
    start = int(frame.index[0])
    cost_basis = float(frame.open.iloc[0]) * (1 + SLIP) * (1 + FEE)
    daily = [(start, capital)] + [(int(stamp + DAY), float(mark / cost_basis * capital * (1 - SLIP) * (1 - FEE)))
                                  for stamp, mark in frame.close.items()]
    return {"return": daily[-1][1] / capital - 1, "curve": daily}


def deployable(correlation, metrics):
    survivors = tuple(f for f in FAMILIES if edge(metrics[f]))
    if len(survivors) == 2:
        if correlation is not None and correlation < 0.3:
            return survivors
        return (max(survivors, key=lambda f: (metrics[f]["net_return"], factor(metrics[f]),
                                               -metrics[f]["max_drawdown"])),)
    return survivors


def daily_correlation(a, b):
    left, right = (np.array([y for _, y in curve(run)]) for run in (a, b))
    l, r = np.diff(left) / left[:-1], np.diff(right) / right[:-1]
    return float(np.corrcoef(l, r)[0, 1]) if len(l) > 1 and np.std(l) > 0 and np.std(r) > 0 else None


def evidence_id(spec, preset, names, trend, reversion):
    payload = [ENGINE_VERSION, spec.model_dump(mode="json"), preset, names,
               trend.model_dump(mode="json"), reversion.model_dump(mode="json")]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def walk_forward(market, variants, windows, preset, family, capital):
    choices, records = [], []
    for train, test, end in windows:
        candidates = []
        for index in range(len(variants)):
            result = simulate(market, variants, [(train, {family: index})], (family,), preset, train, test, capital)
            candidates.append(summary(result))
        winner = max(range(len(candidates)), key=lambda i: (
            candidates[i]["trades"] >= 5, candidates[i]["net_return"] - 0.5 * candidates[i]["max_drawdown"],
            candidates[i]["trades"] > 0,
        ))
        choice = variants[winner]
        config = choice.trend if family == "A" else choice.reversion
        choices.append((test, {family: winner}))
        records.append({"train_start": train, "test_start": test, "test_end": end, "variant": winner,
                        "parameters": config.model_dump(), "train": candidates[winner], "train_candidates": candidates})
    start, end = windows[0][1], windows[-1][2]
    unseen = simulate(market, variants, choices, (family,), preset, start, end, capital)
    for record in records:
        record["test"] = summary(unseen, record["test_start"], record["test_end"])
        record["overfit"] = overfit(record["train"], record["test"])
    aggregate = summary(unseen)
    return {
        "aggregate": aggregate, "folds": records, "equity": curve(unseen),
        "edge": "EDGE CONFIRMED" if edge(aggregate) else "NO EDGE FOUND",
        "overfit": any(record["overfit"] for record in records),
        "monte_carlo": monte_carlo(unseen.trades), "trades": unseen.trades,
    }, choices, unseen


def run_tournament(spec, cache, progress=lambda *_: None):
    begin = time.perf_counter()
    windows = folds(spec.start, spec.end)
    start, end = windows[0][1], windows[-1][2]
    settings = variant_settings(spec)
    rules = market_rules(cache, spec.universe)

    def load(left, right, name, *, stress_window=False):
        hourly, funding = historical(cache, spec.universe, left, right, 420,
                                     lambda done, total: progress(f"{name} · {done}/{total} archive files"),
                                     allow_unlisted=stress_window)
        market = Market.build(hourly, funding, rules, left, right)
        features = Features({s: aggregate(frame, DAY) for s, frame in hourly.items()},
                            {s: aggregate(frame, BAR) for s, frame in hourly.items()})
        variants = [Variant(a, b, build_signals(features, market.times, a, b)) for a, b in settings]
        return market, variants

    market, variants = load(epoch(spec.start), end, "History")
    stress = []
    for name, lower, upper in STRESS:
        left, right = epoch(lower), epoch(upper)
        asset_data = load(left, right, name, stress_window=True)
        spot = spot_daily(cache, left, right)
        stress.append((name, left, right, *asset_data,
                       {s: benchmark(frame, spec.capital)["return"] for s, frame in spot.items()}))
    benchmarks = {s: benchmark(frame, spec.capital) for s, frame in spot_daily(cache, start, end).items()}

    reports = {}
    for preset in PRESETS:
        results, schedules, sims, metrics = {}, {}, {}, {}
        for family in FAMILIES:
            progress(f"{preset} · testing strategy {family}")
            report, schedules[family], sims[family] = walk_forward(market, variants, windows, preset, family, spec.capital)
            selected = schedules[family][-1][1][family]
            report["stress"] = []
            for name, left, right, segment, parameters, reference in stress:
                simulation = simulate(segment, parameters, [(left, {family: selected})], (family,), preset,
                                      left, right, spec.capital)
                report["stress"].append({"name": name, "start": left, "end": right,
                                         "metrics": summary(simulation), "benchmarks": reference,
                                         "universe": list(segment.candles)})
            metrics[family] = report["aggregate"]
            results[family] = report

        correlation = daily_correlation(sims["A"], sims["B"])
        names = deployable(correlation, metrics)
        a, b = (schedules[f][-1][1][f] for f in FAMILIES)
        portfolio = None
        if len(names) == 2:
            joint = [(stamp, {**parameters, **schedules["B"][i][1]})
                     for i, (stamp, parameters) in enumerate(schedules["A"])]
            run = simulate(market, variants, joint, names, preset, start, end, spec.capital)
            portfolio = {"aggregate": summary(run), "equity": curve(run), "monte_carlo": monte_carlo(run.trades),
                         "trades_by_family": {f: sum(t["family"] == f for t in run.trades) for f in names}}
        deployment = None
        if names and spec.canonical:
            trend, reversion = variants[a].trend, variants[b].reversion
            deployment = Deployment(preset=preset, universe=spec.universe, families=names,
                                    trend=trend, reversion=reversion, capital=spec.capital,
                                    evidence=evidence_id(spec, preset, names, trend, reversion)).model_dump(mode="json")
        reports[preset] = {"strategies": results, "selected": names, "daily_correlation": correlation,
                           "deployment": deployment, "portfolio": portfolio}
    return {
        "kind": f"tournament-{ENGINE_VERSION}", "spec": spec.model_dump(mode="json"), "qualification": spec.canonical,
        "oos_start": start, "oos_end": end, "hourly_bars": (end - start) // 3600,
        "fee_per_side": FEE, "slippage_per_side": SLIP, "contract_filters": rules,
        "seconds": time.perf_counter() - begin, "presets": reports, "benchmarks": benchmarks,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Binance Edge Hunt tournament")
    parser.add_argument("--start", type=date.fromisoformat, default=BASELINE_SPEC.start)
    parser.add_argument("--end", type=date.fromisoformat, default=BASELINE_SPEC.end)
    parser.add_argument("--capital", type=float, default=BASELINE_SPEC.capital)
    parser.add_argument("--cache", type=Path, default=Path("data/archive"))
    parser.add_argument("--output", type=Path, default=Path("data/tournament.json"))
    args = parser.parse_args()
    result = run_tournament(TournamentSpec(start=args.start, end=args.end, capital=args.capital), args.cache,
                            lambda note: print(note, flush=True))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, allow_nan=False))
    print(f"Completed in {result['seconds']:.2f} seconds")
    for preset, report in result["presets"].items():
        for family, row in report["strategies"].items():
            print(preset, family, row["edge"], row["aggregate"])
