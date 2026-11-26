from __future__ import annotations

from dataclasses import dataclass

from .config import DAY, HARD_DRAWDOWN, PRESETS


@dataclass
class Risk:
    peak: float
    day_start: float
    day: int
    worst_drawdown: float = 0.0
    halted: bool = False
    daily_locked: bool = False
    streak: int = 0
    pause_until: int = 0

    def observe(self, time, equity, preset):
        if self.day != time // DAY:
            self.day = time // DAY
            self.day_start = equity
            self.daily_locked = False
        self.peak = max(self.peak, equity)
        drawdown = self.drawdown(equity)
        self.worst_drawdown = max(self.worst_drawdown, drawdown)
        self.halted |= bool(drawdown >= HARD_DRAWDOWN)
        self.daily_locked |= bool(self.day_start and equity <= self.day_start * (1 - PRESETS[preset].daily_loss))

    def drawdown(self, equity):
        return 1 - equity / self.peak if self.peak > 0 else 1.0

    def permitted(self, time):
        return not (self.halted or self.daily_locked or time < self.pause_until)

    def scale(self, equity):
        return 0.5 if self.drawdown(equity) >= 0.10 else 1.0

    def closed(self, time, pnl):
        self.streak = self.streak + 1 if pnl < 0 else 0
        if self.streak >= 6:
            self.pause_until = max(self.pause_until, time + 7200)
        elif self.streak >= 3:
            self.pause_until = max(self.pause_until, time + 1800)
