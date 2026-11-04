"""
    BESSBidding

Bidding algorithm for a 100 MW / 100 MWh BESS in the Singapore Wholesale Electricity Market
(EMA BESS Bidding Algorithm Hackathon, Nov 2026). Produces half-hourly offers for Energy,
Regulation, Primary Reserve and Contingency Reserve.

Pipeline: load data -> forecast prices (no look-ahead) -> co-optimise energy and reserves
over a look-ahead window -> sweep each product's price to obtain monotone offer curves ->
SOC safety guard -> export / simulate.
"""
module BESSBidding

using CSV, DataFrames, Dates, HiGHS, JuMP, LinearAlgebra, Printf, Random, Statistics, TOML

include("config.jl")
include("data.jl")
include("synthetic.jl")
include("forecast.jl")
include("optimizer.jl")
include("bids.jl")
include("simulator.jl")
include("strategy.jl")
include("backtest.jl")
include("io.jl")

export Config, BatteryConfig, MarketConfig, StrategyConfig, ForecastConfig, SimulationConfig, DataConfig,
       load_config, with_strategy, with_forecast, PRODUCTS, RESERVES,
       load_market_data, synthetic_market_data, canonicalize,
       forecast_prices, PriceForecast,
       build_coopt, set_price!, solve!, coopt_schedule,
       Offer, PeriodBids, cleared_mw, max_mw, min_mw, offer_from_grid, guard_soc, validate_bids, offers_table,
       storage_pairs, from_storage_pairs, conform_storage_offer, raise_mw, raise_energy,
       clear_period, settle_period,
       rolling_bids, day_ahead_bids, generate_bids,
       run_backtest, perfect_foresight, BacktestResult,
       write_backtest, write_bids

end
