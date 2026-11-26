from __future__ import annotations

import hashlib
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile

import httpx
import numpy as np
import pandas as pd

from .config import DAY, HOUR

ARCHIVES = (
    "https://data.binance.vision/data",
    "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision/data",
)
CANDLE_COLUMNS = ["open", "high", "low", "close", "volume"]


class MarketDataError(RuntimeError):
    pass


def epoch(value):
    return int(pd.Timestamp(value, tz="UTC").timestamp())


def archive_item(client, cache, key, market="futures/um"):
    if market == "spot":
        cache = cache / "spot"
    path = cache / key
    if path.exists():
        return path.read_bytes()
    for attempt in range(6):
        url = f"{ARCHIVES[attempt % len(ARCHIVES)]}/{market}/monthly/{key}"
        try:
            response = client.get(url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            content = response.content
            checksum = client.get(f"{url}.CHECKSUM")
            checksum.raise_for_status()
            if hashlib.sha256(content).hexdigest() != checksum.text.split()[0].lower():
                raise MarketDataError(f"Checksum mismatch for {key}")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
            tmp.write_bytes(content)
            tmp.replace(path)
            return content
        except httpx.HTTPError as error:
            if attempt == 5:
                raise MarketDataError(f"Could not download {key}: {error}") from error
            time.sleep(min(8, 2 ** attempt))


def parse(content, funding, *, quote=False):
    with ZipFile(io.BytesIO(content)) as archive, archive.open(archive.namelist()[0]) as source:
        columns = [0, 2] if funding else [0, 1, 2, 3, 4, 5, 7] if quote else list(range(6))
        raw = pd.read_csv(source, header=None, usecols=columns)
    raw = raw.apply(pd.to_numeric, errors="coerce").dropna()
    stamp = raw.iloc[:, 0].astype("int64")
    raw.index = np.where(stamp >= 10**14, stamp // 10**6, stamp // 1000)
    result = raw.iloc[:, 1:].astype("float64")
    result.columns = ["rate"] if funding else CANDLE_COLUMNS + (["quote_volume"] if quote else [])
    return result


def historical(cache: Path, symbols, start, end, warmup_days, progress=lambda *_: None, *, allow_unlisted=False):
    lower = start - warmup_days * DAY
    months = pd.period_range(pd.to_datetime(lower, unit="s"), pd.to_datetime(end - 1, unit="s"), freq="M")
    tasks = []
    for symbol in symbols:
        market = f"{symbol}USDT"
        for month in months:
            tasks.extend([
                (symbol, False, month, f"klines/{market}/1h/{market}-1h-{month}.zip"),
                (symbol, True, month, f"fundingRate/{market}/{market}-fundingRate-{month}.zip"),
            ])
    candles, funding = {s: [] for s in symbols}, {s: [] for s in symbols}
    with httpx.Client(timeout=20, follow_redirects=False, limits=httpx.Limits(max_connections=12)) as client:
        def download(task):
            symbol, is_funding, month, key = task
            content = archive_item(client, cache, key)
            if content is None:
                if not allow_unlisted and month.end_time.timestamp() >= start:
                    raise MarketDataError(f"Missing evaluation archive {key}")
                return symbol, is_funding, None
            return symbol, is_funding, parse(content, is_funding, quote=not is_funding)

        with ThreadPoolExecutor(max_workers=12) as workers:
            for index, (symbol, is_funding, frame) in enumerate(workers.map(download, tasks), 1):
                if frame is not None:
                    (funding if is_funding else candles)[symbol].append(frame)
                if index % 25 == 0 or index == len(tasks):
                    progress(index, len(tasks))

    def combine(frames, symbol, is_funding):
        if not frames:
            raise MarketDataError(f"No {'funding' if is_funding else 'candles'} for {symbol}")
        result = pd.concat(frames).sort_index()
        result = result.loc[lower:end - 1]
        if result.index.has_duplicates or not np.isfinite(result.values).all():
            raise MarketDataError(f"Invalid or duplicate observations for {symbol}")
        evaluated = result.loc[start:end - 1]
        if is_funding:
            gaps = np.diff(np.r_[start, evaluated.index.values, end])
            if not len(evaluated) or gaps.max() > DAY:
                raise MarketDataError(f"Missing funding observations for {symbol}")
        else:
            expected = np.arange(start, end, HOUR)
            if not np.array_equal(evaluated.index, expected):
                missing = np.setdiff1d(expected, evaluated.index)
                raise MarketDataError(f"Incomplete 1h evaluation for {symbol}: {len(missing)} missing candles")
            o, h, low, c, v = result[CANDLE_COLUMNS].to_numpy().T
            if np.any((low <= 0) | (h < np.maximum(o, c)) | (low > np.minimum(o, c)) | (v < 0)):
                raise MarketDataError(f"Invalid OHLCV for {symbol}")
        return result

    available = tuple(s for s in symbols if not allow_unlisted or s in ("BTC", "ETH")
                      or any((frame.index >= start).any() for frame in candles[s]))
    return (
        {s: combine(candles[s], s, False) for s in available},
        {s: combine(funding[s], s, True) for s in available},
    )


def aggregate(frame, seconds, base=HOUR):
    groups = frame.index // seconds * seconds
    aggregation = {"open": ("open", "first"), "high": ("high", "max"), "low": ("low", "min"),
                   "close": ("close", "last"), "volume": ("volume", "sum")}
    if "quote_volume" in frame:
        aggregation["quote_volume"] = ("quote_volume", "sum")
    grouped = frame.groupby(groups)
    result = grouped.agg(**aggregation)
    result = result.loc[grouped.size() == seconds // base]
    result.index += seconds
    return result


def spot_daily(cache, start, end):
    months = pd.period_range(pd.to_datetime(start, unit="s"), pd.to_datetime(end - 1, unit="s"), freq="M")
    tasks = [(symbol, month) for symbol in ("BTC", "ETH") for month in months]
    result = {"BTC": [], "ETH": []}
    with httpx.Client(timeout=20, limits=httpx.Limits(max_connections=12)) as client:
        def download(item):
            symbol, month = item
            pair = f"{symbol}USDT"
            key = f"klines/{pair}/1d/{pair}-1d-{month}.zip"
            content = archive_item(client, cache, key, market="spot")
            if content is None:
                raise MarketDataError(f"Missing spot archive {key}")
            return symbol, parse(content, funding=False)
        with ThreadPoolExecutor(max_workers=12) as workers:
            for symbol, data in workers.map(download, tasks):
                result[symbol].append(data)
    output = {symbol: pd.concat(parts).sort_index().loc[start:end - 1] for symbol, parts in result.items()}
    for symbol, frame in output.items():
        if not np.array_equal(frame.index, np.arange(start, end, DAY)) or (frame[["open", "close"]] <= 0).any().any():
            raise MarketDataError(f"Incomplete or invalid spot benchmark for {symbol}")
    return output


BASELINE_RULES = {"BTC": (0.001, 0.001, 50., 120.), "ETH": (0.001, 0.001, 20., 2000.),
                  "SOL": (0.01, 0.01, 5., 80000.), "BNB": (0.01, 0.01, 5., 2000.),
                  "XRP": (0.1, 0.1, 5., 2000000.)}


def market_rules(cache, symbols):
    # Freeze default rules so a future filter change cannot make the release run irreproducible.
    extra = set(symbols) - BASELINE_RULES.keys()
    rules = {symbol: BASELINE_RULES[symbol] for symbol in symbols if symbol in BASELINE_RULES}
    if extra:
        path = cache / "exchange-info.json"
        if path.is_file():
            data = json.loads(path.read_text())
        else:
            response = httpx.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=20)
            response.raise_for_status()
            data = response.json()
            cache.mkdir(parents=True, exist_ok=True)
            path.write_text(response.text)
        for market in data["symbols"]:
            symbol = market["baseAsset"]
            if symbol not in extra or market["quoteAsset"] != "USDT" or market["contractType"] != "PERPETUAL":
                continue
            filters = {f["filterType"]: f for f in market["filters"]}
            lot = filters["MARKET_LOT_SIZE"]
            step = float(lot["stepSize"]) or float(filters["LOT_SIZE"]["stepSize"])
            rules[symbol] = (step, float(lot["minQty"]), float(filters["MIN_NOTIONAL"]["notional"]), float(lot["maxQty"]))
        if extra - rules.keys():
            raise MarketDataError(f"Missing contract limits for {', '.join(sorted(extra - rules.keys()))}")
    return rules
