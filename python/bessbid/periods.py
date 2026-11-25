"""Trading-period arithmetic. All times are naive Singapore time (SGT, UTC+8, no DST).

Period ``k`` (1..48) of trading date ``d`` starts at ``d 00:00 + (k-1) * 30 min``.
"""

from __future__ import annotations

import datetime as dt
from typing import NamedTuple

import pandas as pd

__all__ = [
    "PERIOD",
    "PERIODS_PER_DAY",
    "Slot",
    "parse_month",
    "month_days",
    "month_slots",
    "period_start",
    "period_of",
]

PERIOD = pd.Timedelta(minutes=30)
PERIODS_PER_DAY = 48


class Slot(NamedTuple):
    """One trading period: its trading date, period number (1..48) and start time."""

    date: dt.date
    period: int
    start: pd.Timestamp

    @property
    def end(self) -> pd.Timestamp:
        return self.start + PERIOD


def parse_month(month: str) -> tuple[int, int]:
    """'2026-11' -> (2026, 11)."""
    try:
        y, m = month.strip().split("-")
        y, m = int(y), int(m)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"month must look like 'YYYY-MM', got {month!r}") from e
    if not 1 <= m <= 12:
        raise ValueError(f"month out of range: {month!r}")
    return y, m


def month_days(month: str) -> list[dt.date]:
    y, m = parse_month(month)
    first = dt.date(y, m, 1)
    nxt = dt.date(y + (m == 12), m % 12 + 1, 1)
    return [first + dt.timedelta(days=i) for i in range((nxt - first).days)]


def period_start(date: dt.date | pd.Timestamp, period: int) -> pd.Timestamp:
    if not 1 <= int(period) <= PERIODS_PER_DAY:
        raise ValueError(f"period must be 1..48, got {period}")
    return pd.Timestamp(date).normalize() + (int(period) - 1) * PERIOD


def period_of(ts: pd.Timestamp) -> int:
    ts = pd.Timestamp(ts)
    if ts.second or ts.microsecond or ts.minute not in (0, 30):
        raise ValueError(f"{ts} is not on the 30-minute grid")
    return ts.hour * 2 + ts.minute // 30 + 1


def month_slots(month: str) -> list[Slot]:
    """All trading periods of a month in submission order."""
    return [
        Slot(d, k, period_start(d, k)) for d in month_days(month) for k in range(1, PERIODS_PER_DAY + 1)
    ]
