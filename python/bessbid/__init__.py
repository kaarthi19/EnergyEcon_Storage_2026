"""bessbid: bid generation for EMA's BESS bidding hackathon (November 2026).

Module map (P0 foundation; later modules plug into the Strategy protocol):

* :mod:`bessbid.config`   - :class:`Config` and :func:`load_config`
* :mod:`bessbid.periods`  - trading-period arithmetic (naive SGT)
* :mod:`bessbid.io`       - find and load EMA's files (stack time-shift fix, parquet cache)
* :mod:`bessbid.infoset`  - :class:`InfoSet`, the time-gated, audited data accessor
* :mod:`bessbid.bids`     - :class:`PeriodBid` and builders
* :mod:`bessbid.validate` - independent template/rule validator
* :mod:`bessbid.writer`   - fill EMA's template deterministically; read it back
* :mod:`bessbid.strategy` - :class:`Strategy` protocol and registry
* :mod:`bessbid.strategy_regulation` - regulation-only fallback
* :mod:`bessbid.cli`      - ``python -m bessbid generate|validate|inspect``

Planned extension modules: engine, backtest, calendar, netdemand, stackmodel, forecast,
planner, soc, and ``strategy_full`` (registered as strategy ``"full"``).
"""

__version__ = "0.1.0"

from .bids import (  # noqa: E402
    AS_PRODUCTS,
    PRODUCTS,
    Band,
    BidError,
    BidRow,
    PeriodBid,
    energy_curve,
    energy_fixed,
    from_table,
    make_period_bid,
    offered_capacity,
    regulation_only,
    round_mw,
    round_price,
    to_table,
)
from .config import Config, load_config  # noqa: E402
from .infoset import AuditEntry, InfoSet, LeakError  # noqa: E402
from .io import (  # noqa: E402
    DataFiles,
    Dataset,
    SchemaError,
    find_files,
    find_template,
    load_all,
    load_clearing,
    load_predispatch,
    load_stacks,
)
from .periods import Slot, month_slots, period_start  # noqa: E402
from .strategy import Strategy, available_strategies, get_strategy, register_strategy  # noqa: E402
from .strategy_regulation import RegulationOnlyStrategy  # noqa: E402
from .validate import Violation, validate_table, validate_xlsx  # noqa: E402
from .writer import read_back, write_submission  # noqa: E402

__all__ = [
    "__version__",
    "AS_PRODUCTS",
    "PRODUCTS",
    "Band",
    "BidError",
    "BidRow",
    "PeriodBid",
    "energy_curve",
    "energy_fixed",
    "from_table",
    "make_period_bid",
    "offered_capacity",
    "regulation_only",
    "round_mw",
    "round_price",
    "to_table",
    "Config",
    "load_config",
    "AuditEntry",
    "InfoSet",
    "LeakError",
    "DataFiles",
    "Dataset",
    "SchemaError",
    "find_files",
    "find_template",
    "load_all",
    "load_clearing",
    "load_predispatch",
    "load_stacks",
    "Slot",
    "month_slots",
    "period_start",
    "Strategy",
    "available_strategies",
    "get_strategy",
    "register_strategy",
    "RegulationOnlyStrategy",
    "Violation",
    "validate_table",
    "validate_xlsx",
    "read_back",
    "write_submission",
]
