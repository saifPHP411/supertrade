# Lower-turnover futures research protocol

Fixed before testing. All tests are research-only. No changes to Railway, the strategy release, or order execution.

## Hypotheses and settings

Price continuation after a high-volume breakout or a pullback inside an existing trend might yield enough movement to pay two sides of fees and slippage. The prior 1m–1h study lost its gross edge to turnover. This study keeps minute execution but reduces signal frequency and increases the minimum target distance. The four contracts are ETH, SOL, BNB, and XRP Binance USD-M perpetuals. The same instruments and frozen contract filters apply throughout.

- Test 1h and 4h breakout policies with a 24- or 48-bar channel. A long signal is the first close above the prior high, a rising 48-bar EMA over six bars, and volume at least 1.15 times the average of the previous 24 bars. Short mirrors.
- Test 1h and 4h trend pullbacks with a deviation threshold of 1.50 or 2.00. A long signal is a recross above the negative threshold on the 24-close z-score while z stays negative, the six-bar EMA48 slope is positive, and the close is at or above EMA48 minus one ATR. Short mirrors.
- Enter at the next actionable minute open, ranking same-time signals by standardized strength. Use the average true range of the last 14 completed signal bars. Stop at max(3 ATR, 0.80% of arrival price), target 2.5 times the stop distance. Time limits are 3d and 7d for 1h and 4h trend and 2d and 5d for 1h and 4h pullbacks. Allow one position at a time, two daily entries and two hours after any exit.
- Risk: 50 USDT, 1x notional ceiling, 1% long and 0.5% short estimated per-trade risk including costs, 0.1% of trailing 60-minute average per-minute quote volume, and rounded market quantities. Reuse BALANCED daily stop, 20% hard drawdown stop, peak scaling, and loss-streak cooldowns. Gaps can overshoot the stop.

## Frozen research windows and acceptance

Train Jan–Jun 2021 and validate Jul–Dec 2021, then roll over Jan–Jun 2022 and Jul–Dec 2022 using exactly the preceding six-month training window in each fold. Score settings on training data via net daily Sharpe then PF. Rank family/timeframe policies first by acceptance on walk-forward, then worst-fold Sharpe, aggregate Sharpe and PF. Freeze the resulting rule before scoring Jan–Jun 2023, a period outside the prior experiment's reported validation. The same fixed rule separately tests Jul 2024–Oct 2026 for recent replication, a period already examined for the previous experiment. Recent replication is a stress check rather than an independent discovery holdout. All periods derive from checksum-verified, complete UTC minute and funding archives, including enough prior candles to initialize signals.

Use 0.10% taker and 0.05% adverse slippage per side, plus observed funding with minute-open price as a funding-mark proxy. No maker assumption. Repeat separate and joint fee/slippage stress (1.5x and 2x), 1m/5m delay, ATR stop ±20%, lookbacks ±20% or deviation ±0.25. Report single asset, leave-one-out, and prior 20-day basket regimes. Benchmark long buy/hold, EMA crossover, plain reversion and 12 matched-random seeds.

To qualify for Demo consideration, walk-forward, untouched holdout and recent replication each require positive net return and Sharpe, PF >= 1.20, >=100 closed trades and <20% drawdown. The cost stress must also pass. Range neighborhoods must show positive net, profitable PF and no stop failure. Holdout gains must not depend on one asset (50% max), one month (35%) or five winners (35%). No promotion from the research workflow. No retuning on any holdout.
