# Futures strategy falsification protocol

Frozen before the run. Research only. The Railway bot and the live lock have no dependency on this module.

## Universe and evaluation

- Binance USD-M ETHUSDT, SOLUSDT, BNBUSDT, XRPUSDT perpetuals. This is the four-contract Supertrade research universe used previously, with all contracts checked for complete histories. There is no retrospectively ranked or expanding universe. Results are conditional on these four contracts and do not establish all-market robustness.
- Official checksum-verified monthly archives. Minute execution and 1m, 5m, 15m, or 1h closed-candle signals. UTC windows are half-open. Jan–Jun 2024 is the first training window. Four successive six-month walk-forward tests cover Jul 2024–Jun 2026. Each uses only the immediately preceding six months for parameter selection. Jul–Oct 2026 is the untouched holdout.
- Base costs are 0.10% taker fee and 0.05% adverse slippage on each side. The strategies use marketable orders only, so there are no assumed maker fills or rebates. Historical signed funding is included at minute-open trade prices as a proxy for the exchange funding mark. Historical order-book spreads and exact historical contract filters are unavailable. The filter snapshot already used by Supertrade is frozen.
- Starting equity is 50 USDT per validation fold and per holdout. The aggregate walk-forward equity compounds independent fold returns. Report each fold's drawdown and mark-to-market drawdown. A hard stop in any fold fails the risk gate. Sharpe uses UTC daily net equity returns and a square-root-of-365 scale, with zero risk-free rate.
- Report gross P&L for the same executed trades with commission, slippage and funding added back (signed funding credits are removed). This controls for trade-set changes. Exposure is time with a position. Turnover is two-sided traded notional divided by starting capital. The report includes total costs separately.

## Candidate rules

- **Trend continuation** (15m and 60m, prior 16 or 32 bars). Long on a first close above the prior rolling high with a rising EMA48 over six bars; short is the mirror. The previous close must have been inside the corresponding channel. The hypothesis is short-term underreaction followed by continuation.
- **Filtered reversion** (1m, 5m and 15m; entry deviation threshold 1.75 or 2.25). Use the close's z-score from a 24-close mean and sample standard deviation. Long when z recrosses its negative threshold from below while still below zero, the close is no more than two ATR below EMA48, and the absolute six-bar EMA48 change is at most 1.5 ATR. Short mirrors. The hypothesis is temporary price pressure unwinding in a flat local regime.
- ATR is the average true range of the last 14 completed signal bars. Entries use the next actionable 1m open. Rank simultaneous signals by absolute standardized strength and then fixed universe order. Allow one position total, at most four entries per UTC day and a 15-minute cooldown after exit.
- Stop distance is max(2.5 ATR, 0.40% of arrival price). Target distance is twice the stop distance; both are based on the executed entry. The time stop is 24h for trend and 4h for reversion. Stop takes priority when both levels are touched in a minute; gaps through the stop fill at the worse open. Positions close before a fold boundary. Mark ongoing positions against the minute close after estimated liquidation costs.
- Size at 1% of equity risk for longs and 0.5% for shorts, including estimated two-sided costs. Round down to the frozen contract lot step and enforce minimum size/notional. Cap gross notional at 1x current equity and 0.10% of the trailing 60 minutes' average per-minute quote turnover. Apply the Supertrade BALANCED daily lock, peak drawdown stop, drawdown-based size scaling and loss-streak pauses. Gaps can overshoot the stop.

## Selection and falsification

Train selection is by net Sharpe, then profit factor and net return. Compare walk-forward family/timeframe policies; prefer one that passes the acceptance gates, then the highest worst-fold Sharpe, then aggregate Sharpe and PF. The diagnostic leader's last train-selected rule is frozen for the holdout, which never selects parameters or families. A failed diagnostic leader is not promoted.

- In both the walk-forward and holdout require positive net return and Sharpe, PF at least 1.20, at least 100 closed trades, and drawdown strictly below 20%. Apply the same gate with 2x slippage and 1.5x fees together.
- Report each cost stress independently and together, entry delay of 1 and 5 minutes, threshold perturbations (trend lookback ±20% rounded, reversion z ±0.25) and stop distance ±20%. Require positive net results, no hard stop and PF at least 1.0 for each neighbor. Stress cases keep the selected settings except for the specified perturbation.
- Concentration gate uses total positive net trade P&L. Maximum shares are 50% for any one asset, 35% for one UTC month and 35% for the five largest wins. Report each asset alone and with each dropped. Regime splits use the sign of the equal-weight basket's trailing 20-day simple return known at the prior UTC close.
- Benchmarks use single-asset buy and hold for all four contracts at 1x or less, a 60m close crossing EMA48, a 15m unfiltered ±2 z recross, and 12 fixed-seed randomized entry schedules. The random schedules preserve the selected signal counts and signs per contract and use the same risk, causal ATR and exits. Buy and hold uses no bot risk stop.
- No parameter range or split is to be changed based on the holdout. If a functional defect prevents the test from running, correct it without changing the hypothesis or testing windows and document the fix.
