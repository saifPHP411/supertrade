# Supertrade · Edge Hunt Edition

Supertrade is a local Binance USDⓈ-M perpetuals research and execution terminal. It includes a fee-aware strategy tournament, parameter search, run history, and Binance Demo/Testnet with a server-enforced live lock. All software and data access is free.

## Start locally

Install Docker Engine and the Docker Compose v2 plugin (2.24 or later). From the repository directory:

```sh
docker compose up --build
```

Open [Supertrade](http://127.0.0.1:8080). The app binds to loopback and persists the data cache, run history, account journal, and gate evidence in a Docker volume. The verified baseline is included and appears on the Tournament page at startup. Historical research requires no API credentials.

For development without Docker, use Python 3.11+ and Node 24:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
npm --prefix web ci
npm --prefix web run build
python -m uvicorn app.main:app --host 127.0.0.1 --port 8080
```

To rerun the baseline and write the full JSON report:

```sh
python -m app.research
```

The report is written to `data/tournament.json`. The CLI accepts `--start`, `--end`, `--capital`, `--cache`, and `--output`. Strategy Lab also lets you adjust both strategies' parameters and the set of candidate assets. Changes produce an exploratory report. Reusing an already inspected period for further search does not create fresh out-of-sample evidence.

## Research rules

Both families share the same data, simulated fill prices, allocation, contract rules, and walk-forward dates.

- **A · Slow trend.** The latest completed BTC daily candle must close above EMA(100), with EMA(100) above EMA(200). Long entries require a 20-day Donchian breakout on the completed daily close, positive 30-day ROC, and daily volume above the previous 30-day average. Stops use a 4h ATR(14) trail and a 2% initial hard stop. A closed BTC risk-off signal or 30 elapsed days closes the position. Its 5–30 day average holding period is a research target.
- **B · Volatility mean reversion.** The default 4h entry requires a close more than 2 ATR(14) below the 20-bar mean and RSI(14) below 25 in risk-on, or the mirrored conditions above the mean and RSI above 75 in risk-off. Risk-off shorts receive half the position risk. Positions exit when a completed 4h candle reaches the then-current mean or after three elapsed days. An ATR protective stop bounds modeled position risk.
- **Universe and order size.** The app ranks eligible assets daily by the sum of 30 days of USDT quote volume and selects the top five. The default candidates are BTC, ETH, SOL, BNB, and XRP. The model applies actual market lot, notional, and quantity filters. The default filter snapshot is frozen for reproducibility. It reflects the observed Binance USDⓈ-M filters at development time. At an initial 50 USDT and a 98% gross exposure limit the default BTC minimum prevents a BTC entry. BTC still determines the regime and appears as a spot benchmark.
- **Trade budgets.** A has a rolling 365-day cap of 60 round trips. B has a cap of 120. The simulator and broker engine reserve capacity for currently open or pending round trips. Neither the parameter search nor the risk preset can raise those caps. A holds at most three positions total and all plans cap same-direction positions at three.
- **Data and fills.** The loader checks Binance's monthly archive checksums and rejects missing 1h evaluation bars and missing funding coverage. It aggregates complete 1h bars into 4h and daily bars and gives indicators up to 420 warm-up days. A signal fills at the first hourly open after its source bars close. Resting stops use 1h highs/lows with adverse opening gaps. Slippage is 0.05% and taker fees are 0.10% on each side. Perpetual funding applies to longs and shorts at the recorded settlement, using the settlement-hour open as the price proxy. The model does not replay order-book depth or liquidation.
- **Costs and benchmarks.** Gross P&L is the price movement on the same fills and sizes before friction. Net P&L is gross less fees and slippage plus signed funding. The UI shows fees divided by positive gross P&L. Unlevered BTC and ETH buy-and-hold use actual Binance spot archive candles over the unseen period with the same modeled round-trip execution cost. The five-candidate universe is fixed before each run and can carry survivorship bias.
- **Walk-forward.** Each fold trains for 12 months and evaluates the next six months. The training-only grid compares the base parameters, tighter protection, and wider protection with stricter entry confirmation. It ranks first by whether a candidate produced five training trades and then by net return minus half its maximum drawdown. Unseen equity, positions, drawdown locks, and trade budgets carry continuously through the test folds. Held positions keep their entry parameters. The final test fold's trained settings become the paper plan only if the aggregate qualifies. An OVERFIT flag marks any fold where the selected training profit factor exceeds unseen profit factor by more than 2×.
- **Verdicts and portfolios.** The unseen aggregate must have net return above zero and PF of at least 1.20. When both qualify, correlation below 0.30 selects both and simulates the joint account at half risk each. Otherwise the positive strategy with higher net return is selected. The released baseline supplies the execution plan. Exploratory reruns cannot replace it, even with a higher score.
- **Stress and Monte Carlo.** Stress windows cover January–March 2021 and June–August 2022 with the final selected settings. A custom asset with no listing during a stress window is absent from that segment. Monte Carlo runs 1,000 reshuffles of the closed-trade order, estimates the probability of a 20% peak-to-trough drawdown, and reports the drawdown distribution. It does not model open-trade heat. The historical equity drawdown can therefore exceed the shuffled estimate.

## Verified baseline

Source data covers July 2023 through June 2026. Four contiguous unseen folds cover **July 2024 through June 2026** (17,520 execution candles per asset). The warm-cache tournament took about 12 seconds. BTC spot buy-and-hold returned **−6.89%** and ETH returned **−54.41%** on the same unseen dates after modeled costs.

| Preset | Strategy | Net return | Profit factor | Max drawdown | Round trips |
|---|---|---:|---:|---:|---:|
| SAFE | A | +18.55% | 2.50 | 10.41% | 36 |
| SAFE | B | +0.00% | — | 0.00% | 0 |
| BALANCED | A | +18.57% | 2.16 | 14.70% | 36 |
| BALANCED | B | +2.85% | 1.43 | 3.83% | 29 |
| MAX GROWTH | A | +36.39% | 3.26 | 20.10% | 17 |
| MAX GROWTH | B | +2.23% | 1.09 | 9.35% | 53 |

BALANCED's correlation was approximately −0.0025. Its joint half-risk simulation returned **+12.38%** with **11.06%** drawdown and **35** fills (34 from A and one from B). B therefore offered limited realized diversification at this allocation. A averaged 2.43–3.12 days across presets, below the 5–30 day target. MAX GROWTH A triggered the drawdown stop in its second fold and placed zero trades in the final two folds. Past backtests do not guarantee future results.

## Paper execution and live lock

1. Use Binance Futures Demo credentials from a dedicated single-asset USDT account in one-way position mode. For legacy Futures Testnet credentials, set `BINANCE_PAPER_ENV=testnet`. Use trade-only exchange keys. Keep account transfers and outside trading out of the paper evidence window.
2. Copy `.env.example` to `.env` and set `BINANCE_PAPER_KEY` and `BINANCE_PAPER_SECRET` locally. Leave `CAPITAL_LIMIT=50` for the qualified baseline. Demo balances larger than this use the 50 USDT internal allocation.
3. Open **Bot Control**, choose a qualified preset, and start **Paper**. The same worker runs Demo/Testnet or live through verified CCXT endpoints. Authenticated activity persists. No process restart automatically enables new entries. Existing v1 journals and run history are preserved. v1 evidence does not qualify v2, and existing exposure must be reconciled before switching.
4. **Go-live gate:** the chosen plan needs a qualifying tournament result, 14 complete UTC paper-operating days, at least 20 closed paper round trips, positive expectancy after exchange fees and funding, maximum observed drawdown below 20%, and an uninterrupted UTC Saturday and Sunday. Evidence is bound to the exact strategy, paper account, and allocation. Manual stops, restarts, or broker outages break unattended intervals.
5. Live mode additionally requires separate `BINANCE_LIVE_KEY` / `BINANCE_LIVE_SECRET` and `ENABLE_LIVE_TRADING=true`. The server checks qualification before connecting and before every signed live write. Choose Live explicitly in Bot Control after the checklist passes.

MAX GROWTH defaults to 2% risk per trade, BALANCED uses 1%, and SAFE uses 0.5%. Daily equity losses of 5% / 3% / 2% respectively close exposure and disable entries until the next UTC day. At 10% peak drawdown new size halves. A 20% drawdown halts and requires a manual reset from a verified flat account. Exchange stops reduce exposure, although a gap can incur losses beyond the modeled threshold.

Stop disables new entries and continues managing open positions. **Kill switch** disables the engine, cancels orders, sends reduce-only position closes, and verifies that the exchange reports no positions or orders. Failure leaves the request pending for retries. Optional Telegram alerts use `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. The loopback deployment trusts local OS and database access.

## Verify

```sh
python -m unittest discover -s tests -v
npm --prefix web ci
npm --prefix web run build
```

Tests cover causal candle aggregation, cross-strategy costs and funding, risk and trade budgets, qualification, paper and live routing, and a broker-mocked kill switch. An authenticated Testnet session requires Binance keys.

## Software and licenses

The backend's full Python pin set lives in `requirements.txt`. The frontend's full npm pin set, including platform-specific packages, is recorded in `web/package-lock.json`. Docker runs the open-source Docker Engine and Compose plugin (Apache-2.0), Python (PSF-2.0), Node.js (MIT and bundled notices), and Debian base packages (their respective licenses). The benchmark uses public Binance data under the [Binance Public Data terms](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md). Lightweight Charts is by [TradingView](https://www.tradingview.com/), Apache-2.0 with attribution.

### Python packages

| License | Packages |
|---|---|
| Apache-2.0 | aiosignal, frozenlist, multidict, propcache, requests, tzdata, yarl |
| Apache-2.0 AND MIT | aiohttp |
| Apache-2.0 OR BSD-3-Clause | cryptography |
| BSD-3-Clause | click, httpcore, httpx, idna, pandas, pycparser, python-dateutil, starlette, uvicorn |
| BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | numpy |
| MIT | aiodns, annotated-doc, annotated-types, anyio, attrs, ccxt, charset-normalizer, fastapi, h11, pycares, pydantic, pydantic-core, pytz, setuptools, socksio, typing-inspection, urllib3 |
| MIT OR Apache-2.0 | coincurve |
| MIT-0 | cffi |
| MPL-2.0 | certifi |
| PSF-2.0 | aiohappyeyeballs, typing-extensions |

### npm packages

Platform-specific binaries sharing a license are grouped by package prefix.

| License | Packages |
|---|---|
| Apache-2.0 | `baseline-browser-mapping`, `detect-libc`, `lightweight-charts` |
| BSD-3-Clause | `source-map-js` |
| BlueOak-1.0.0 | `graceful-fs` |
| CC-BY-4.0 | `caniuse-lite` |
| ISC | `electron-to-chromium`, `lru-cache`, `lucide-react`, `picocolors`, `semver`, `yallist` |
| MIT | `@babel/code-frame`, `@babel/compat-data`, `@babel/core`, `@babel/generator`, `@babel/helper-compilation-targets`, `@babel/helper-globals`, `@babel/helper-module-imports`, `@babel/helper-module-transforms`, `@babel/helper-plugin-utils`, `@babel/helper-string-parser`, `@babel/helper-validator-identifier`, `@babel/helper-validator-option`, `@babel/helpers`, `@babel/parser`, `@babel/plugin-transform-react-jsx-self`, `@babel/plugin-transform-react-jsx-source`, `@babel/template`, `@babel/traverse`, `@babel/types`, `@esbuild/*`, `@jridgewell/gen-mapping`, `@jridgewell/remapping`, `@jridgewell/resolve-uri`, `@jridgewell/sourcemap-codec`, `@jridgewell/trace-mapping`, `@rolldown/pluginutils`, `@rollup/rollup-*`, `@tailwindcss/node`, `@tailwindcss/oxide`, `@tailwindcss/oxide-*`, `@tailwindcss/vite`, `@types/babel__core`, `@types/babel__generator`, `@types/babel__template`, `@types/babel__traverse`, `@types/estree`, `@vitejs/plugin-react`, `browserslist`, `convert-source-map`, `debug`, `enhanced-resolve`, `esbuild`, `escalade`, `fancy-canvas`, `fdir`, `fsevents`, `gensync`, `jiti`, `js-tokens`, `jsesc`, `json5`, `magic-string`, `ms`, `nanoid`, `node-releases`, `picomatch`, `postcss`, `react`, `react-dom`, `react-refresh`, `rollup`, `scheduler`, `tailwindcss`, `tapable`, `tinyglobby`, `update-browserslist-db`, `vite` |
| MPL-2.0 | `lightningcss`, `lightningcss-*` |

## Running on Railway

Create a private project and a single service connected to this GitHub repository. Attach a persistent volume at `/data`. The Dockerfile builds the frontend and serves it with FastAPI. The entry point binds to `0.0.0.0:$PORT`, checks for the mounted volume, and drops privileges before starting the server. Set the service startup healthcheck to `/health`, allow 120 seconds, keep one replica, and disable Serverless where the plan permits it. The volume stores the SQLite journal, positions and order snapshots, trade and verification records, and archive cache.

| Variable | Value |
|---|---|
| `BINANCE_PAPER_KEY` | Binance Futures Demo API key, entered in Railway Variables |
| `BINANCE_PAPER_SECRET` | Binance Futures Demo API secret, entered in Railway Variables |
| `BINANCE_PAPER_ENV` | `demo` |
| `ENABLE_LIVE_TRADING` | `false` |
| `SUPERTRADE_PAPER_ONLY` | `true` |
| `SUPERTRADE_RESUME_PAPER` | `true` |
| `SUPERTRADE_PAPER_PHASE` | `verification` for the first 24 hours, then `gate` after verifying and flattening |
| `SUPERTRADE_DASHBOARD_PASSWORD` | Unique random password of 24 or more characters. Browser username is `supertrade`. |
| `CAPITAL_LIMIT` | `50` for the qualified baseline |
| `SUPERTRADE_DATA` | `/data` |
| `RAILWAY_RUN_UID` | `0` so the launcher can prepare the volume before dropping privileges |

Create keys from the Account menu → **API Management** → **Create API** inside [Binance Futures Demo](https://demo.binance.com/en/futures). Binance gates Demo access by region and account eligibility. Use a dedicated USDⓈ-M single-asset USDT account in one-way mode. The bot sets leverage to 1×. Add the secrets only in Railway Variables. The Railway runtime independently rejects live orders even if the environment flag is changed.

An explicitly started paper engine keeps its recovery intent in SQLite. On restart it verifies the account and position mode, refreshes broker positions and orders, and reconciles fills by their original client order IDs before resuming. The consumed signal bar is persisted before entry submission. An ambiguous entry is looked up at the exchange and is never replayed for the same bar. Close attempts use reduce-only orders and wait for authenticated confirmation of remaining exposure before retrying. A manual stop, kill switch, or drawdown stop clears recovery intent. API failures back off from 24 seconds to two minutes. Restarts interrupt unattended coverage.

The protected `GET /api/paper/evidence` returns a fresh authenticated account snapshot, verified uptime intervals, kill-switch counts and matched exchange order/fill IDs with side, size, price and fee. For bot-submitted market orders, slippage uses the signed distance from a captured pre-order book midpoint and is weighted by reference notional. Native protective orders without a matched reference are counted in fill coverage and excluded from the slippage estimate. The fee rate uses actual commission over executed notional. Report the sample size and treat Demo estimates as specific to Demo liquidity. If measured slippage exceeds 0.05% by at least 0.02 percentage points on a representative sample, rerun all presets with `python -m app.research --fee <decimal> --slippage <decimal>`. This rerun is exploratory and does not replace the qualified baseline. The verification phase contributes no days or trades to the official gate.

Railway Free is $0/month with $1 of monthly resources and serverless sleep. Hobby has a $5/month minimum, and Pro a $20/month minimum, both with usage charges above included credits. Hobby can run continuously when Serverless is disabled and sufficient compute budget is available. A compute hard limit can stop services mid-run. Check the workspace plan, remaining credit and spend cap before starting. A volume-backed redeploy has brief downtime. Free/Trial support On Failure with up to 10 restarts. Paid tiers can use Always if desired. See [Railway pricing](https://docs.railway.com/pricing/plans), [Serverless](https://docs.railway.com/deployments/serverless), and [compute limits](https://docs.railway.com/pricing/cost-control).
