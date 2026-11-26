"""Regulation-only fallback strategy.

Every period: the full rating (100 MW) of Regulation in one band at the AS floor price
(0.01), no Energy offer and no Primary or Contingency offer (those rows are written with all
bands blank, as the template's own example does for period 2).

Why this is the fallback:

* Regulation cleared above Contingency and Primary in 100 % of the 5904 training periods,
  so splitting the 100 MW across AS products never pays as a price taker;
* regulation activation is 0 MWh, so SOC stays at the initial 50 % all month and no SOC
  limit can be breached under any efficiency convention;
* it reads no market data, so it cannot break the information rules;
* the offer at 0.01 always clears, the same whether EMA re-clears the market with our offer
  (price maker) or prices it at precomputed clearing prices (price taker).

Training value: about 1.416 M SGD per 30 days as a price maker (1.576 M as a price taker).
"""

from __future__ import annotations

from dataclasses import dataclass

from .bids import PeriodBid, regulation_only
from .config import Config
from .periods import month_slots

__all__ = ["RegulationOnlyStrategy"]


@dataclass
class RegulationOnlyStrategy:
    name: str = "regulation"
    needs_data: bool = False
    #: MW of Regulation offered; None means the full rating.
    mw: float | None = None
    #: Offer price; None means cfg.as_floor_price (0.01).
    price: float | None = None

    def generate(self, month: str, infoset, cfg: Config) -> list[PeriodBid]:
        return [regulation_only(s.date, s.period, cfg, mw=self.mw, price=self.price) for s in month_slots(month)]
