"""Run configuration for the BESS bidding package.

All parameters live in one frozen dataclass, :class:`Config`. Defaults follow the EMA
hackathon specification (100 MW / 100 MWh, SOC 5-95 %, 85 % round trip, initial SOC 50 %,
30-minute periods) and the Phase A findings (strict capacity rule, 65-minute gate closure,
per-row stack publication times).

Overrides come from an optional TOML or JSON file. The file may be flat
(``soc_margin_mwh = 3.0``) or grouped in tables (``[bess] soc_margin_mwh = 3.0``); table
names are ignored and keys are merged. Unknown keys raise ``ValueError`` so typos never
pass silently.
"""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = ["Config", "load_config", "CAPACITY_RULES", "STACK_PUB_RULES"]

#: Readings of "combined capacity offered across Energy and AS must not exceed 100 MW".
#: ``strict``: sum |energy band MW| + sum of all AS band MW <= power_mw (our default).
#: ``max_side``: max(total charge MW, total discharge MW) + sum AS MW <= power_mw.
CAPACITY_RULES = ("strict", "max_side")

#: When does an offer-stack row become usable?
#: ``column``: the file's assumed_publication_time_sgt (= ts + 28 d 8 h), the default.
#: ``ts_plus_28d``: ts + 28 days, as the template's example text says (laxer by 8 h).
#: ``latest``: the later of the two (equal to ``column`` on the EMA files).
STACK_PUB_RULES = ("column", "ts_plus_28d", "latest")


def _default_conventions(rte: float = 0.85) -> tuple[tuple[float, float], ...]:
    s = math.sqrt(rte)
    return ((1.0, rte), (s, s), (rte, 1.0))


@dataclass(frozen=True)
class Config:
    # ---- BESS (EMA Assumptions sheet) -------------------------------------------------
    power_mw: float = 100.0
    energy_mwh: float = 100.0
    soc_min_frac: float = 0.05
    soc_max_frac: float = 0.95
    round_trip_eff: float = 0.85
    initial_soc_frac: float = 0.50
    period_hours: float = 0.5
    periods_per_day: int = 48
    #: (eta_charge, eta_discharge) pairs the plan must stay feasible under. EMA has not
    #: said how the 85 % loss is split, so SOC must hold for every convention listed.
    efficiency_conventions: tuple[tuple[float, float], ...] = field(
        default_factory=_default_conventions
    )
    #: SOC safety margin kept inside [soc_min, soc_max] by planners (MWh).
    soc_margin_mwh: float = 2.0
    #: Minimum SOC at the end of the month (fraction); EMA has not set one.
    terminal_soc_frac: float = 0.50

    # ---- Offer prices ---------------------------------------------------------------------
    #: Price of an always-clearing discharge band (clears when offer < clearing price).
    #: 0.01 rather than 0.00: the template's number format shows zero as '-', which a reader
    #: could take for a blank cell. Training energy prices never fell below 133.
    discharge_floor_price: float = 0.01
    #: Price of an always-clearing charge band (clears when offer > clearing price).
    #: Training energy prices never exceeded 2058.4; 3000 also avoids charging into a
    #: deeper shortage.
    charge_cap_price: float = 3000.0
    #: Price used for always-clearing AS bands (0.01, not 0.00, for the same display reason;
    #: training reserve prices never fell below 1.8).
    as_floor_price: float = 0.01
    #: Hard bounds the validator accepts for any offer price (SWEM limits, assumption).
    offer_price_min: float = -4500.0
    offer_price_max: float = 4500.0

    # ---- Template / rule interpretation ------------------------------------------------
    capacity_rule: str = "strict"
    #: MW quantities are rounded toward zero to this resolution, so rounding can never
    #: push an offer over the capacity limit.
    mw_resolution: float = 0.1
    max_energy_bands: int = 10
    max_as_bands: int = 5
    #: Require strictly increasing prices within a row (equal prices are merged by the
    #: builders, so this only bites on hand-made tables).
    strict_ascending: bool = True

    # ---- Information rules ---------------------------------------------------------------
    gate_closure_min: int = 65
    stack_pub_rule: str = "column"
    #: Strict reading: only period T's own pre-dispatch rows, with these leads (hours),
    #: preferred in this order.
    predispatch_leads: tuple[float, ...] = (2.0, 3.0)
    strict_predispatch: bool = True
    #: Offer stacks before this date are an exact copy of later data (June == July) and
    #: must not enter fits.
    stack_fit_start: str = "2026-07-01"

    # ---- Market model constants (used by engine / backtest modules) ----------------------
    shortfall_price_primary: float = 750.0
    shortfall_price_contingency: float = 1000.0
    shortfall_price_regulation: float = 1200.0
    voll: float = 4500.0
    ramp_up_frac: float = 0.42

    # ---- Run ---------------------------------------------------------------------------
    month: str = "2026-11"
    #: Fixed workbook created/modified time, so identical inputs give identical bytes.
    fixed_timestamp: str = "2026-11-01T00:00:00"

    def __post_init__(self) -> None:
        errs = []
        if self.capacity_rule not in CAPACITY_RULES:
            errs.append(f"capacity_rule must be one of {CAPACITY_RULES}, got {self.capacity_rule!r}")
        if self.stack_pub_rule not in STACK_PUB_RULES:
            errs.append(f"stack_pub_rule must be one of {STACK_PUB_RULES}, got {self.stack_pub_rule!r}")
        if not 0 <= self.soc_min_frac < self.soc_max_frac <= 1:
            errs.append("need 0 <= soc_min_frac < soc_max_frac <= 1")
        if not self.soc_min_frac <= self.initial_soc_frac <= self.soc_max_frac:
            errs.append("initial_soc_frac must lie in [soc_min_frac, soc_max_frac]")
        if not 0 < self.round_trip_eff <= 1:
            errs.append("round_trip_eff must be in (0, 1]")
        for ec, ed in self.efficiency_conventions:
            if not (0 < ec <= 1 and 0 < ed <= 1) or abs(ec * ed - self.round_trip_eff) > 1e-9:
                errs.append(
                    f"efficiency convention ({ec}, {ed}) must have eta_c*eta_d == round_trip_eff"
                )
        if self.mw_resolution <= 0:
            errs.append("mw_resolution must be > 0")
        if self.gate_closure_min < 0:
            errs.append("gate_closure_min must be >= 0")
        if self.discharge_floor_price >= self.charge_cap_price:
            # Not a template rule (one period never holds both), but it signals a typo.
            errs.append("discharge_floor_price must be below charge_cap_price")
        for name in ("discharge_floor_price", "charge_cap_price", "as_floor_price"):
            v = getattr(self, name)
            if not self.offer_price_min <= v <= self.offer_price_max:
                errs.append(f"{name}={v} outside [offer_price_min, offer_price_max]")
            if abs(v * 100 - round(v * 100)) > 1e-9:
                errs.append(f"{name}={v} is not a whole number of cents")
        if errs:
            raise ValueError("invalid Config: " + "; ".join(errs))

    # ---- Derived quantities --------------------------------------------------------------
    @property
    def soc_min_mwh(self) -> float:
        return self.soc_min_frac * self.energy_mwh

    @property
    def soc_max_mwh(self) -> float:
        return self.soc_max_frac * self.energy_mwh

    @property
    def initial_soc_mwh(self) -> float:
        return self.initial_soc_frac * self.energy_mwh

    @property
    def period_minutes(self) -> int:
        return int(round(self.period_hours * 60))

    def replace(self, **changes: Any) -> "Config":
        return dataclasses.replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["efficiency_conventions"] = [list(x) for x in self.efficiency_conventions]
        d["predispatch_leads"] = list(self.predispatch_leads)
        return d


_TUPLE_FIELDS = {"efficiency_conventions", "predispatch_leads"}


def _flatten(d: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, Mapping):
            for k2, v2 in _flatten(v).items():
                if k2 in out:
                    raise ValueError(f"config key {k2!r} given twice")
                out[k2] = v2
        else:
            if k in out:
                raise ValueError(f"config key {k!r} given twice")
            out[k] = v
    return out


def config_from_mapping(d: Mapping[str, Any], base: Config | None = None) -> Config:
    """Apply a (possibly nested) mapping of overrides to ``base`` (default Config())."""
    base = base or Config()
    flat = _flatten(d)
    names = {f.name for f in dataclasses.fields(Config)}
    unknown = sorted(set(flat) - names)
    if unknown:
        raise ValueError(f"unknown config keys: {unknown}")
    for k in _TUPLE_FIELDS & set(flat):
        v = flat[k]
        flat[k] = tuple(tuple(float(y) for y in x) if isinstance(x, (list, tuple)) else float(x) for x in v)
    return dataclasses.replace(base, **flat)


def load_config(path: str | Path | None = None) -> Config:
    """Default Config, with overrides from a ``.toml`` or ``.json`` file if given."""
    if path is None:
        return Config()
    p = Path(path)
    text = p.read_bytes()
    if p.suffix.lower() == ".json":
        data = json.loads(text.decode("utf-8"))
    elif p.suffix.lower() == ".toml":
        try:
            import tomllib  # Python >= 3.11
        except ModuleNotFoundError:  # pragma: no cover - Python 3.10
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError as e:
                raise RuntimeError("TOML config needs Python >= 3.11 or the tomli package; use JSON") from e
        data = tomllib.loads(text.decode("utf-8"))
    else:
        raise ValueError(f"config file must be .toml or .json: {p}")
    if not isinstance(data, Mapping):
        raise ValueError("config file must hold a table/object at top level")
    return config_from_mapping(data)
