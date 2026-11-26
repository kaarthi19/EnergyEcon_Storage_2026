"""Strategy protocol and registry: the extension point for the bidding logic.

A strategy turns a month and an :class:`~bessbid.infoset.InfoSet` into one
:class:`~bessbid.bids.PeriodBid` per trading period. It must read market data only through
the InfoSet (so every read is time-gated and audited), compute each period's features with
that period's own cutoff (``infoset.cutoff(slot.start)``), and be deterministic.

Strategies are registered by name as ``"module:attribute"`` targets and imported lazily, so a
strategy whose module does not exist yet (``full``, built from engine/forecast/planner) only
fails when it is asked for.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from .bids import PeriodBid
from .config import Config
from .periods import month_slots

__all__ = [
    "Strategy",
    "register_strategy",
    "get_strategy",
    "available_strategies",
    "check_complete",
    "StrategyError",
]


class StrategyError(RuntimeError):
    pass


@runtime_checkable
class Strategy(Protocol):
    #: Registry name, written to the run manifest.
    name: str
    #: False if the strategy needs no market data (the CLI then skips loading it).
    needs_data: bool

    def generate(self, month: str, infoset, cfg: Config) -> list[PeriodBid]:
        """One PeriodBid per trading period of ``month`` ('YYYY-MM')."""
        ...


_REGISTRY: dict[str, str | Callable[..., Strategy]] = {
    "regulation": "bessbid.strategy_regulation:RegulationOnlyStrategy",
    "full": "bessbid.strategy_full:FullStrategy",
}


def register_strategy(name: str, target: str | Callable[..., Strategy]) -> None:
    """Register a factory (or a lazy ``"module:attr"`` path) under ``name``."""
    _REGISTRY[name] = target


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)


def get_strategy(name: str, options: Mapping[str, Any] | None = None, **kwargs) -> Strategy:
    """Instantiate the strategy registered as ``name``.

    ``options``: the strategy's ``[strategy.<name>]`` config table. A factory with a
    ``from_options(options, **kwargs)`` classmethod gets the table; any other factory gets
    its keys as keyword arguments. Unknown keys raise :class:`StrategyError`."""
    if name not in _REGISTRY:
        raise StrategyError(f"unknown strategy {name!r}; known: {available_strategies()}")
    target = _REGISTRY[name]
    if isinstance(target, str):
        mod, _, attr = target.partition(":")
        try:
            factory = getattr(importlib.import_module(mod), attr)
        except (ImportError, AttributeError) as e:
            raise StrategyError(f"strategy {name!r} is not available ({target}): {e}") from e
    else:
        factory = target
    try:
        if options:
            make = getattr(factory, "from_options", None)
            s = make(options, **kwargs) if make is not None else factory(**dict(options), **kwargs)
        else:
            s = factory(**kwargs)
    except (TypeError, ValueError) as e:
        raise StrategyError(f"bad options for strategy {name!r}: {e}") from e
    if not isinstance(s, Strategy):
        raise StrategyError(f"{target} does not implement the Strategy protocol")
    return s


def check_complete(bids: Sequence[PeriodBid], month: str) -> None:
    """Exactly one PeriodBid per trading period of the month, nothing else."""
    want = [(s.date, s.period) for s in month_slots(month)]
    got = sorted((b.date, b.period) for b in bids)
    if got != want:
        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))
        dup = len(got) - len(set(got))
        raise StrategyError(
            f"strategy output does not cover {month} exactly: {len(missing)} missing "
            f"(e.g. {missing[:3]}), {len(extra)} extra (e.g. {extra[:3]}), {dup} duplicates"
        )
