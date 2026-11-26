"""Bid structures and builders.

A :class:`PeriodBid` holds the four offers of one trading period (Energy, Primary Reserve,
Contingency Reserve, Regulation). Each offer is a tuple of :class:`Band` (price, MW).
Quantities are per band, not cumulative. Energy MW is negative for charging and positive for
discharging (RC383 s3.1.2); AS MW is positive.

The builders normalise every offer the same way:

* prices are rounded to whole cents;
* MW is rounded *toward zero* to ``cfg.mw_resolution``, so rounding can never push an offer
  over the capacity limit; bands that round to 0 MW are dropped;
* bands with equal price and the same sign are merged; bands are sorted by price;
* charge prices must be below discharge prices, prices must lie within the configured bounds,
  and the capacity rule (``cfg.capacity_rule``) must hold, else :class:`BidError`.

:func:`to_table` turns a list of PeriodBids into the canonical bid table used by
:mod:`bessbid.validate` and :mod:`bessbid.writer`.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from .config import Config
from .periods import PERIODS_PER_DAY, period_start

__all__ = [
    "PRODUCTS",
    "AS_PRODUCTS",
    "TABLE_COLUMNS",
    "Band",
    "BidRow",
    "PeriodBid",
    "BidError",
    "round_mw",
    "round_price",
    "normalize_bands",
    "offered_capacity",
    "check_capacity",
    "make_period_bid",
    "regulation_only",
    "energy_fixed",
    "energy_curve",
    "to_table",
    "from_table",
]

#: Product labels exactly as the template's 'Product' column spells them, in row order.
PRODUCTS = ("Energy", "Primary Reserve", "Contingency Reserve", "Regulation")
AS_PRODUCTS = PRODUCTS[1:]
N_BANDS = 10
TABLE_COLUMNS = ["trading_date", "trading_period", "product"] + [
    f"{k}_{i}" for i in range(1, N_BANDS + 1) for k in ("price", "quantity")
]


class BidError(ValueError):
    """A bid breaks a template or market rule."""


@dataclass(frozen=True)
class Band:
    price: float
    mw: float


@dataclass(frozen=True)
class BidRow:
    """One row of the 'Bid Submission' sheet."""

    date: dt.date
    period: int
    product: str
    bands: tuple[Band, ...] = ()


@dataclass(frozen=True)
class PeriodBid:
    date: dt.date
    period: int
    energy: tuple[Band, ...] = ()
    primary: tuple[Band, ...] = ()
    contingency: tuple[Band, ...] = ()
    regulation: tuple[Band, ...] = ()
    #: Free-form note kept out of the workbook (e.g. which strategy layer set it).
    tag: str = field(default="", compare=False)

    @property
    def start(self) -> pd.Timestamp:
        return period_start(self.date, self.period)

    def offers(self) -> dict[str, tuple[Band, ...]]:
        return dict(zip(PRODUCTS, (self.energy, self.primary, self.contingency, self.regulation)))

    def rows(self) -> list[BidRow]:
        return [BidRow(self.date, self.period, p, b) for p, b in self.offers().items()]

    @property
    def charge_mw(self) -> float:
        return -sum(b.mw for b in self.energy if b.mw < 0)

    @property
    def discharge_mw(self) -> float:
        return sum(b.mw for b in self.energy if b.mw > 0)

    @property
    def as_mw(self) -> float:
        return sum(b.mw for p in (self.primary, self.contingency, self.regulation) for b in p)


# --------------------------------------------------------------------------------------
# Rounding
# --------------------------------------------------------------------------------------


def round_price(p: float) -> float:
    """Round to whole cents (half away from zero)."""
    if not math.isfinite(p):
        raise BidError(f"price must be finite, got {p}")
    c = math.floor(abs(p) * 100 + 0.5 + 1e-9)
    return math.copysign(c / 100, p) if c else 0.0


def round_mw(x: float, resolution: float = 0.1) -> float:
    """Round toward zero to a multiple of ``resolution`` (exact decimal result)."""
    if not math.isfinite(x):
        raise BidError(f"MW must be finite, got {x}")
    units = math.floor(abs(x) / resolution + 1e-9)
    decimals = max(0, -math.floor(math.log10(resolution) + 1e-12)) + 1
    v = round(units * resolution, decimals)
    return math.copysign(v, x) if v else 0.0


# --------------------------------------------------------------------------------------
# Normalisation and checks
# --------------------------------------------------------------------------------------


def _as_bands(x) -> list[Band]:
    out = []
    for b in x or ():
        if isinstance(b, Band):
            out.append(b)
        else:
            p, q = b
            out.append(Band(float(p), float(q)))
    return out


def normalize_bands(bands, cfg: Config, product: str) -> tuple[Band, ...]:
    """Round, merge equal-price bands, sort by price, and check sign/price/count rules."""
    is_energy = product == "Energy"
    merged: dict[tuple[float, int], float] = {}
    for b in _as_bands(bands):
        p = round_price(b.price)
        if not math.isfinite(b.mw):
            raise BidError(f"{product}: MW must be finite")
        if not is_energy and b.mw < 0:
            raise BidError(f"{product}: AS quantities must be >= 0, got {b.mw}")
        if not cfg.offer_price_min <= p <= cfg.offer_price_max:
            raise BidError(f"{product}: price {p} outside [{cfg.offer_price_min}, {cfg.offer_price_max}]")
        sgn = 1 if b.mw > 0 else -1
        merged[(p, sgn)] = merged.get((p, sgn), 0.0) + b.mw
    out = []
    for (p, _), q in sorted(merged.items()):
        q = round_mw(q, cfg.mw_resolution)
        if q != 0:
            out.append(Band(p, q))
    prices = [b.price for b in out]
    if len(set(prices)) != len(prices):
        raise BidError(f"{product}: a charge band and a discharge band share price {prices}")
    if is_energy:
        ch = [b.price for b in out if b.mw < 0]
        ds = [b.price for b in out if b.mw > 0]
        if ch and ds and max(ch) >= min(ds):
            raise BidError(f"{product}: charge prices {ch} must all be below discharge prices {ds}")
    limit = cfg.max_energy_bands if is_energy else cfg.max_as_bands
    if len(out) > limit:
        raise BidError(f"{product}: {len(out)} bands, at most {limit} allowed")
    return tuple(out)


def offered_capacity(pb: PeriodBid, rule: str = "strict") -> float:
    """Combined MW offered in a period under a capacity-rule reading."""
    if rule == "strict":
        return sum(abs(b.mw) for b in pb.energy) + pb.as_mw
    if rule == "max_side":
        return max(pb.charge_mw, pb.discharge_mw) + pb.as_mw
    raise ValueError(f"unknown capacity rule {rule!r}")


def check_capacity(pb: PeriodBid, cfg: Config) -> None:
    """Eq. (12) of ``docs/formulation.md``: offered MW within the rating (per ``capacity_rule``)."""
    tot = offered_capacity(pb, cfg.capacity_rule)
    if tot > cfg.power_mw + 1e-9:
        raise BidError(
            f"{pb.date} P{pb.period}: offered {tot:.3f} MW > {cfg.power_mw} MW ({cfg.capacity_rule} rule)"
        )
    for name, offer in pb.offers().items():
        s = sum(abs(b.mw) for b in offer)
        if s > cfg.power_mw + 1e-9:
            raise BidError(f"{pb.date} P{pb.period}: {name} offers {s} MW > {cfg.power_mw} MW")


def _date(d) -> dt.date:
    if isinstance(d, pd.Timestamp):
        return d.date()
    if isinstance(d, dt.datetime):
        return d.date()
    if isinstance(d, dt.date):
        return d
    return pd.Timestamp(d).date()


# --------------------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------------------


def make_period_bid(
    date,
    period: int,
    cfg: Config,
    energy=(),
    primary=(),
    contingency=(),
    regulation=(),
    clip_as: bool = False,
    tag: str = "",
) -> PeriodBid:
    """General builder. With ``clip_as=True`` AS quantities are cut (Regulation first, then
    Contingency, then Primary; highest-priced bands first) until the capacity rule holds;
    otherwise a breach raises :class:`BidError`."""
    if not 1 <= int(period) <= PERIODS_PER_DAY:
        raise BidError(f"period must be 1..{PERIODS_PER_DAY}, got {period}")
    d = _date(date)
    e = normalize_bands(energy, cfg, "Energy")
    p = normalize_bands(primary, cfg, "Primary Reserve")
    c = normalize_bands(contingency, cfg, "Contingency Reserve")
    r = normalize_bands(regulation, cfg, "Regulation")
    pb = PeriodBid(d, int(period), e, p, c, r, tag)
    if clip_as:
        excess = offered_capacity(pb, cfg.capacity_rule) - cfg.power_mw
        if excess > 1e-9:
            new = {"regulation": list(r), "contingency": list(c), "primary": list(p)}
            for prod in ("regulation", "contingency", "primary"):
                bands = new[prod]
                for i in range(len(bands) - 1, -1, -1):
                    if excess <= 1e-9:
                        break
                    cut = min(bands[i].mw, excess)
                    q = bands[i].mw - cut
                    # round down again so the cut is never smaller than needed
                    q = round_mw(q, cfg.mw_resolution)
                    excess -= bands[i].mw - q
                    bands[i] = Band(bands[i].price, q)
                new[prod] = [b for b in bands if b.mw > 0]
            pb = PeriodBid(d, int(period), e, tuple(new["primary"]), tuple(new["contingency"]),
                           tuple(new["regulation"]), tag)
    check_capacity(pb, cfg)
    return pb


def regulation_only(date, period: int, cfg: Config | None = None, mw: float | None = None,
                    price: float | None = None, tag: str = "regulation_only") -> PeriodBid:
    """``mw`` (default the full rating) of Regulation at ``price`` (default the AS floor,
    0.01); no Energy, Primary or Contingency offer (those rows stay blank)."""
    cfg = cfg or Config()
    mw = cfg.power_mw if mw is None else mw
    price = cfg.as_floor_price if price is None else price
    return make_period_bid(date, period, cfg, regulation=[(price, mw)] if mw else (), tag=tag)


def energy_fixed(date, period: int, mw: float, cfg: Config | None = None,
                 regulation_mw: float | str | None = "fill", tag: str = "energy_fixed") -> PeriodBid:
    """Always-clearing energy offer of ``mw`` (> 0 discharge at the floor price, < 0 charge
    at the cap price), plus Regulation at the AS floor price. ``regulation_mw='fill'`` uses
    the headroom left by the capacity rule; a number sets it; None or 0 offers none."""
    cfg = cfg or Config()
    e = []
    if mw > 0:
        e = [(cfg.discharge_floor_price, mw)]
    elif mw < 0:
        e = [(cfg.charge_cap_price, mw)]
    e_mw = abs(round_mw(mw, cfg.mw_resolution))
    if regulation_mw == "fill":
        reg = max(0.0, cfg.power_mw - e_mw)
    else:
        reg = float(regulation_mw or 0.0)
    r = [(cfg.as_floor_price, reg)] if reg > 0 else []
    return make_period_bid(date, period, cfg, energy=e, regulation=r, tag=tag)


def energy_curve(date, period: int, cfg: Config | None = None,
                 charge: Sequence[tuple[float, float]] = (),
                 discharge: Sequence[tuple[float, float]] = (),
                 primary=(), contingency=(), regulation=(), clip_as: bool = False,
                 tag: str = "energy_curve") -> PeriodBid:
    """Multi-band builder. ``charge`` and ``discharge`` take (price, MW) with MW given as a
    positive size; charge bands are written as negative MW."""
    cfg = cfg or Config()
    e = [(p, -abs(q)) for p, q in charge] + [(p, abs(q)) for p, q in discharge]
    return make_period_bid(date, period, cfg, energy=e, primary=primary, contingency=contingency,
                           regulation=regulation, clip_as=clip_as, tag=tag)


# --------------------------------------------------------------------------------------
# Table conversion
# --------------------------------------------------------------------------------------


def to_table(bids: Iterable[PeriodBid]) -> pd.DataFrame:
    """Canonical bid table: one row per (date, period, product), in submission order.

    Columns: ``trading_date`` (datetime64, midnight), ``trading_period`` (int),
    ``product`` (str), ``price_k``/``quantity_k`` for k = 1..10 (float, NaN when blank).
    """
    recs = []
    for pb in sorted(bids, key=lambda b: (b.date, b.period)):
        for row in pb.rows():
            rec = [pd.Timestamp(row.date), row.period, row.product]
            for i in range(N_BANDS):
                if i < len(row.bands):
                    rec += [row.bands[i].price, row.bands[i].mw]
                else:
                    rec += [np.nan, np.nan]
            recs.append(rec)
    df = pd.DataFrame(recs, columns=TABLE_COLUMNS)
    df["trading_date"] = pd.to_datetime(df["trading_date"]).astype("datetime64[ns]")
    df["trading_period"] = df["trading_period"].astype("int64")
    for c in TABLE_COLUMNS[3:]:
        df[c] = df[c].astype("float64")
    return df


def from_table(df: pd.DataFrame) -> list[PeriodBid]:
    """Inverse of :func:`to_table` (no rounding or rule checks; use validate for those)."""
    out: dict[tuple, dict] = {}
    key_map = dict(zip(PRODUCTS, ("energy", "primary", "contingency", "regulation")))
    for r in df.itertuples(index=False):
        rec = r._asdict()
        d = pd.Timestamp(rec["trading_date"]).date()
        k = (d, int(rec["trading_period"]))
        bands = []
        for i in range(1, N_BANDS + 1):
            p, q = rec[f"price_{i}"], rec[f"quantity_{i}"]
            if pd.notna(p) or pd.notna(q):
                bands.append(Band(float(p), float(q)))
        out.setdefault(k, {})[key_map[rec["product"]]] = tuple(bands)
    return [PeriodBid(d, p, **v) for (d, p), v in sorted(out.items())]
