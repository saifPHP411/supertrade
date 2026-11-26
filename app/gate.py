from datetime import date

from .config import DAY, HARD_DRAWDOWN


def live_gate(store, scope):
    intervals, summary, samples = store.evidence(scope)
    complete = set()
    longest = 0
    for first, last in intervals:
        longest = max(longest, last - first)
        complete.update(range((first + DAY - 1) // DAY, last // DAY))
    epoch_ordinal = date(1970, 1, 1).toordinal()
    weekend = any((day + epoch_ordinal - 1) % 7 == 5 and day + 1 in complete for day in complete
                  if any(first <= day * DAY and last >= (day + 2) * DAY for first, last in intervals))
    peak = max_dd = 0.0
    for equity in samples:
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, 1 - equity / peak)
    expectancy = summary["expectancy"]
    checks = [
        {"label": "Complete UTC paper operation days", "value": len(complete), "target": 14, "passed": len(complete) >= 14},
        {"label": "Closed paper round trips", "value": summary["n"], "target": 20, "passed": summary["n"] >= 20},
        {"label": "Net expectancy (USDT per trade)", "value": expectancy, "target": "> 0", "passed": expectancy is not None and expectancy > 0},
        {"label": "Maximum observed paper drawdown", "value": f"{max_dd:.2%}" if samples else None, "target": "< 20%",
         "passed": bool(samples and max_dd < HARD_DRAWDOWN)},
        {"label": "Uninterrupted Saturday and Sunday (UTC)", "value": "Complete" if weekend else "Pending",
         "target": "Complete", "passed": weekend},
        {"label": "Longest uninterrupted session (hours)", "value": round(longest / 3600, 2), "target": 24,
         "passed": longest >= DAY},
    ]
    return {"passed": all(check["passed"] for check in checks), "checks": checks}
