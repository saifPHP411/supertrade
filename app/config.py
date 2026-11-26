from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HOUR = 3600
BAR = 4 * HOUR
DAY = 86_400
YEAR = 365 * DAY
FEE = 0.001
SLIP = 0.0005
HARD_DRAWDOWN = 0.20
MAX_RISK = 0.02
ENGINE_VERSION = "2.0"
DEFAULT_UNIVERSE = ("BTC", "ETH", "SOL", "BNB", "XRP")
PresetName = Literal["SAFE", "BALANCED", "MAX GROWTH"]
Family = Literal["A", "B"]


@dataclass(frozen=True)
class Preset:
    risk: float
    daily_loss: float


PRESETS = {
    "SAFE": Preset(0.005, 0.02),
    "BALANCED": Preset(0.01, 0.03),
    "MAX GROWTH": Preset(MAX_RISK, 0.05),
}
BUDGETS = {"A": 60, "B": 120}


class Trend(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    breakout_days: int = Field(20, ge=10, le=80)
    volume_ratio: float = Field(1, ge=1, le=2, allow_inf_nan=False)
    trail_atr: float = Field(3, ge=1.5, le=5, allow_inf_nan=False)


class Reversion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mean_bars: int = Field(20, ge=12, le=60)
    deviation_atr: float = Field(2, ge=1.5, le=4, allow_inf_nan=False)
    rsi_low: int = Field(25, ge=10, le=35)
    protection_atr: float = Field(3.5, ge=2, le=6, allow_inf_nan=False)


class TournamentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start: date = date(2023, 7, 1)
    end: date = date(2026, 7, 1)
    capital: float = Field(50, ge=10, le=1_000_000, allow_inf_nan=False)
    universe: tuple[str, ...] = Field(DEFAULT_UNIVERSE, validate_default=True)
    trend: Trend = Trend()
    reversion: Reversion = Reversion()
    fee_per_side: float = Field(FEE, ge=0, le=0.05, allow_inf_nan=False)
    slippage_per_side: float = Field(SLIP, ge=0, le=0.05, allow_inf_nan=False)

    @field_validator("universe")
    @classmethod
    def validate_universe(cls, values):
        if not 5 <= len(values) <= 15 or len(values) != len(set(values)):
            raise ValueError("Enter 5–15 distinct candidate assets")
        if not {"BTC", "ETH"} <= set(values) or any(not re.fullmatch(r"[A-Z0-9]{2,15}", x) for x in values):
            raise ValueError("Use uppercase base asset codes and include BTC and ETH")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def validate_dates(self):
        months = (self.end.year - self.start.year) * 12 + self.end.month - self.start.month
        if self.start.day != 1 or self.end.day != 1 or not 30 <= months <= 60 or months % 6:
            raise ValueError("Use month boundaries and a 30–60 month window in six-month increments")
        if self.start < date(2021, 1, 1) or self.end > date.today().replace(day=1):
            raise ValueError("Use completed archive months beginning January 2021 or later")
        return self

    def evidence_spec(self):
        return self.model_dump(mode="json", exclude={"fee_per_side", "slippage_per_side"})

    @property
    def canonical(self):
        return self == BASELINE_SPEC


BASELINE_SPEC = TournamentSpec(universe=DEFAULT_UNIVERSE)


class Deployment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    preset: PresetName
    universe: tuple[str, ...]
    families: tuple[Family, ...]
    trend: Trend = Trend()
    reversion: Reversion = Reversion()
    evidence: str
    capital: float = Field(50, ge=10, le=1_000_000, allow_inf_nan=False)

    @property
    def fingerprint(self):
        doc = json.dumps([ENGINE_VERSION, self.model_dump(mode="json")], sort_keys=True)
        return hashlib.sha256(doc.encode()).hexdigest()
