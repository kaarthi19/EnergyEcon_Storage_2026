# Configuration: typed structs populated from a TOML file.

const PRODUCTS = (:energy, :regulation, :primary, :contingency)
const RESERVES = (:regulation, :primary, :contingency)

Base.@kwdef struct BatteryConfig
    power_mw::Float64 = 100.0
    energy_mwh::Float64 = 100.0
    soc_min_frac::Float64 = 0.05
    soc_max_frac::Float64 = 0.95
    round_trip_eff::Float64 = 0.85
    initial_soc_frac::Float64 = 0.50
    degradation_cost::Float64 = 5.0
end

eta_one_way(b::BatteryConfig) = sqrt(b.round_trip_eff)
soc_min(b::BatteryConfig) = b.soc_min_frac * b.energy_mwh
soc_max(b::BatteryConfig) = b.soc_max_frac * b.energy_mwh
initial_soc(b::BatteryConfig) = b.initial_soc_frac * b.energy_mwh

Base.@kwdef struct MarketConfig
    period_hours::Float64 = 0.5
    periods_per_day::Int = 48
    price_floor::Float64 = -4500.0
    price_cap::Float64 = 4500.0
    max_energy_tranches::Int = 10
    max_reserve_tranches::Int = 5
    deploy_reg_up::Float64 = 0.10
    deploy_reg_down::Float64 = 0.10
    deploy_primary::Float64 = 0.02
    deploy_contingency::Float64 = 0.01
    sustain_regulation::Float64 = 0.5
    sustain_primary::Float64 = 0.5
    sustain_contingency::Float64 = 0.5
    reserve_cap_mw::Dict{Symbol,Float64} = Dict(:regulation => 0.0, :primary => 0.0, :contingency => 0.0)
end

"Effective MW cap for a reserve product (0 in the config means the battery power rating)."
reserve_cap(m::MarketConfig, b::BatteryConfig, p::Symbol) =
    (c = get(m.reserve_cap_mw, p, 0.0); c > 0 ? min(c, b.power_mw) : b.power_mw)

Base.@kwdef struct StrategyConfig
    mode::String = "rolling"
    horizon_periods::Int = 96
    lead_periods::Int = 1
    terminal_soc_frac::Float64 = 0.5
    terminal_value_mode::String = "soft"
    use_binary::Bool = false
    energy_grid_quantiles::Vector{Float64} = [0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98]
    reserve_grid_quantiles::Vector{Float64} = [0.05, 0.25, 0.5, 0.75, 0.95]
    price_impact::Float64 = 0.0
    soc_guard::String = "worst_case"
    soc_margin_mwh::Float64 = 5.0
end

Base.@kwdef struct ForecastConfig
    method::String = "ridge"
    train_days::Int = 28
    ridge_alpha::Float64 = 1.0
    origin_stride_periods::Int = 12
    pd_horizon_periods::Int = 48
end

Base.@kwdef struct SimulationConfig
    shortfall_penalty::Float64 = 4500.0
    stochastic_deployment::Bool = false
    seed::Int = 7
end

Base.@kwdef struct DataConfig
    timestamp_col::String = "timestamp"
    date_col::String = ""
    period_col::String = ""
    timestamp_format::String = "yyyy-mm-ddTHH:MM:SS"
    columns::Dict{String,String} = Dict{String,String}()
end

Base.@kwdef struct Config
    battery::BatteryConfig = BatteryConfig()
    market::MarketConfig = MarketConfig()
    strategy::StrategyConfig = StrategyConfig()
    forecast::ForecastConfig = ForecastConfig()
    simulation::SimulationConfig = SimulationConfig()
    data::DataConfig = DataConfig()
end

_get(d, k, default) = haskey(d, k) ? d[k] : default

function _fill(::Type{T}, d::AbstractDict) where {T}
    kw = Dict{Symbol,Any}()
    for f in fieldnames(T)
        s = String(f)
        if haskey(d, s)
            v = d[s]
            ft = fieldtype(T, f)
            kw[f] = ft <: AbstractVector ? convert(ft, Float64.(v)) : convert(ft, v)
        end
    end
    return T(; kw...)
end

"""
    load_config(path) -> Config

Read a TOML configuration file. Missing keys fall back to the defaults above.
"""
function load_config(path::AbstractString)
    raw = TOML.parsefile(path)
    battery = _fill(BatteryConfig, _get(raw, "battery", Dict()))
    m = _get(raw, "market", Dict())
    dep = _get(m, "deployment", Dict())
    sus = _get(m, "sustain_hours", Dict())
    caps = _get(m, "reserve_cap_mw", Dict())
    market = MarketConfig(
        period_hours = Float64(_get(m, "period_hours", 0.5)),
        periods_per_day = Int(_get(m, "periods_per_day", 48)),
        price_floor = Float64(_get(m, "price_floor", -4500.0)),
        price_cap = Float64(_get(m, "price_cap", 4500.0)),
        max_energy_tranches = Int(_get(m, "max_energy_tranches", 10)),
        max_reserve_tranches = Int(_get(m, "max_reserve_tranches", 5)),
        deploy_reg_up = Float64(_get(dep, "regulation_up", 0.10)),
        deploy_reg_down = Float64(_get(dep, "regulation_down", 0.10)),
        deploy_primary = Float64(_get(dep, "primary", 0.02)),
        deploy_contingency = Float64(_get(dep, "contingency", 0.01)),
        sustain_regulation = Float64(_get(sus, "regulation", 0.5)),
        sustain_primary = Float64(_get(sus, "primary", 0.5)),
        sustain_contingency = Float64(_get(sus, "contingency", 0.5)),
        reserve_cap_mw = Dict(Symbol(k) => Float64(v) for (k, v) in caps),
    )
    strategy = _fill(StrategyConfig, _get(raw, "strategy", Dict()))
    forecast = _fill(ForecastConfig, _get(raw, "forecast", Dict()))
    simulation = _fill(SimulationConfig, _get(raw, "simulation", Dict()))
    d = _get(raw, "data", Dict())
    data = DataConfig(
        timestamp_col = String(_get(d, "timestamp_col", "timestamp")),
        date_col = String(_get(d, "date_col", "")),
        period_col = String(_get(d, "period_col", "")),
        timestamp_format = String(_get(d, "timestamp_format", "yyyy-mm-ddTHH:MM:SS")),
        columns = Dict{String,String}(String(k) => String(v) for (k, v) in _get(d, "columns", Dict())),
    )
    validate(Config(; battery, market, strategy, forecast, simulation, data))
end

function validate(c::Config)
    b, m, s = c.battery, c.market, c.strategy
    0 <= b.soc_min_frac < b.soc_max_frac <= 1 || error("SOC limits must satisfy 0 <= min < max <= 1")
    b.soc_min_frac <= b.initial_soc_frac <= b.soc_max_frac || error("initial SOC outside SOC limits")
    0 < b.round_trip_eff <= 1 || error("round_trip_eff must be in (0, 1]")
    m.price_floor < m.price_cap || error("price_floor must be below price_cap")
    s.mode in ("rolling", "day_ahead") || error("strategy.mode must be rolling or day_ahead")
    s.terminal_value_mode in ("soft", "hard") || error("terminal_value_mode must be soft or hard")
    s.soc_guard in ("worst_case", "expected") || error("soc_guard must be worst_case or expected")
    s.horizon_periods >= 1 || error("horizon_periods must be >= 1")
    s.lead_periods >= 1 || error("lead_periods must be >= 1")
    c.forecast.method in ("ridge", "seasonal_naive", "predispatch") || error("unknown forecast method")
    return c
end

"Copy of `cfg` with some strategy fields replaced."
function with_strategy(cfg::Config; kw...)
    d = Dict{Symbol,Any}(f => getfield(cfg.strategy, f) for f in fieldnames(StrategyConfig))
    merge!(d, Dict{Symbol,Any}(kw))
    return Config(cfg.battery, cfg.market, StrategyConfig(; d...), cfg.forecast, cfg.simulation, cfg.data)
end

"Copy of `cfg` with some forecast fields replaced."
function with_forecast(cfg::Config; kw...)
    d = Dict{Symbol,Any}(f => getfield(cfg.forecast, f) for f in fieldnames(ForecastConfig))
    merge!(d, Dict{Symbol,Any}(kw))
    return Config(cfg.battery, cfg.market, cfg.strategy, ForecastConfig(; d...), cfg.simulation, cfg.data)
end
