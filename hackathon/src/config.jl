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
    max_energy_tranches::Int = 10      # tranches kept when building the curve (before RC383 conform)
    max_charge_pairs::Int = 5          # RC383 §5.2B.2.4(a): pairs 1-5 charge
    max_discharge_pairs::Int = 5       # RC383 §5.2B.2.4(b): pairs 6-10 discharge
    max_reserve_tranches::Int = 5
    reserve_headroom::String = "per_class"   # RC383 D.17.2.4A: limit applies per reserve offer
    offered_capacity_mw::Float64 = 0.0       # §5.2B.2.5 max combined transfer (0 = power rating)
    ramp_up_mw_per_min::Float64 = 100.0      # §5.2B.2.6
    ramp_down_mw_per_min::Float64 = 100.0
    deploy_reg_up::Float64 = 0.10
    deploy_reg_down::Float64 = 0.10
    deploy_primary::Float64 = 0.02
    deploy_contingency::Float64 = 0.01
    sustain_regulation::Float64 = 0.5
    sustain_primary::Float64 = 0.5
    sustain_contingency::Float64 = 0.5
    reserve_cap_mw::Dict{Symbol,Float64} = Dict(:regulation => 0.0, :primary => 0.0, :contingency => 0.0)
    facility_id::String = "BESS1"            # §5.2B.2.1 identity of the energy storage facility
    offer_type::String = "variation"         # §5.2B.2.2 "standing" offer or offer "variation"
end

Base.@kwdef struct StrategyConfig
    mode::String = "rolling"
    horizon_periods::Int = 96
    lead_periods::Int = 3
    terminal_soc_frac::Float64 = 0.5
    terminal_value_mode::String = "soft"
    use_binary::Bool = false
    energy_grid_quantiles::Vector{Float64} = [0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98]
    reserve_grid_quantiles::Vector{Float64} = [0.05, 0.25, 0.5, 0.75, 0.95]
    price_impact::Float64 = 0.0
    soc_guard::String = "quantile"
    soc_guard_quantile::Float64 = 0.999
    soc_margin_mwh::Float64 = 5.0
    soc_guard_buffer_mwh::Float64 = 0.5
    energy_flex_mw::Float64 = 0.0      # max MW the energy offer may deviate from plan (0 = off)
    energy_flex_to_zero::Bool = true   # the capped offer can always back off to 0 MW
    sustain_charge_credit::Bool = false # plan's sustainability credits planned charging (net transfer)
end

Base.@kwdef struct ForecastConfig
    method::String = "ridge"
    train_days::Int = 28
    ridge_alpha::Float64 = 1.0
    origin_stride_periods::Int = 12
    pd_horizon_periods::Int = 48
    conditional_errors::Bool = true     # error bands by period of day and lead time
    error_period_window::Int = 2        # ± trading periods pooled for the period-of-day scale
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
    date_format::String = "yyyy-mm-dd"          # date column in date + period mode
    timestamp_is_period_end::Bool = false       # timestamp column marks the END of each period
    missing_strings::Vector{String} = ["", "NA", "N/A", "-"]
    thousands_separator::String = ","           # stripped from numbers read as text ("" = none)
    max_missing_share::Float64 = 0.05           # error if more of a realised price column is missing
    pd_min_lead_periods::Int = 0                # with a pd_issue_time column: runs kept must be issued
                                                # this many periods ahead (0 = strategy.lead_periods)
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

# Unknown keys are an error: a misspelt key would otherwise silently run the default.
function _check_keys(d::AbstractDict, valid, where::AbstractString)
    for k in keys(d)
        k in valid || error("unknown config key \"$k\" in [$where]; valid keys: $(join(sort(collect(valid)), ", "))")
    end
end

function _convert_field(ft, v, key, where)
    try
        ft <: AbstractVector && return convert(ft, eltype(ft) <: Real ? Float64.(v) : String.(v))
        return convert(ft, v)
    catch
        error("config key \"$key\" in [$where]: cannot use $(repr(v)) as $(ft)")
    end
end

function _fill(::Type{T}, d::AbstractDict, where::AbstractString; skip = ()) where {T}
    fields = [String(f) for f in fieldnames(T) if !(String(f) in skip)]
    _check_keys(d, fields, where)
    kw = Dict{Symbol,Any}()
    for f in fields
        haskey(d, f) && (kw[Symbol(f)] = _convert_field(fieldtype(T, Symbol(f)), d[f], f, where))
    end
    return T(; kw...)
end

# Market keys whose TOML name differs from the struct field (sub-tables of [market]).
const _DEPLOY_KEYS = Dict("regulation_up" => :deploy_reg_up, "regulation_down" => :deploy_reg_down,
                          "primary" => :deploy_primary, "contingency" => :deploy_contingency)
const _SUSTAIN_KEYS = Dict("regulation" => :sustain_regulation, "primary" => :sustain_primary,
                           "contingency" => :sustain_contingency)
const _SECTIONS = ("battery", "market", "strategy", "forecast", "simulation", "data")
const _DATA_COLUMN_KEYS = ("price_energy", "price_regulation", "price_primary", "price_contingency",
                           "pd_energy", "pd_regulation", "pd_primary", "pd_contingency", "pd_demand",
                           "pd_issue_time")

function _load_market(m::AbstractDict)
    subs = ("deployment", "sustain_hours", "reserve_cap_mw")
    flat = [String(f) for f in fieldnames(MarketConfig)
            if !(f in values(_DEPLOY_KEYS) || f in values(_SUSTAIN_KEYS) || f == :reserve_cap_mw)]
    _check_keys(m, vcat(flat, collect(subs)), "market")
    kw = Dict{Symbol,Any}()
    for k in flat
        haskey(m, k) && (kw[Symbol(k)] = _convert_field(fieldtype(MarketConfig, Symbol(k)), m[k], k, "market"))
    end
    for (sub, map) in (("deployment", _DEPLOY_KEYS), ("sustain_hours", _SUSTAIN_KEYS))
        d = _get(m, sub, Dict())
        _check_keys(d, keys(map), "market.$sub")
        for (k, f) in map
            haskey(d, k) && (kw[f] = _convert_field(Float64, d[k], k, "market.$sub"))
        end
    end
    caps = _get(m, "reserve_cap_mw", Dict())
    _check_keys(caps, String.(RESERVES), "market.reserve_cap_mw")
    kw[:reserve_cap_mw] = Dict{Symbol,Float64}(p => 0.0 for p in RESERVES)
    for (k, v) in caps
        kw[:reserve_cap_mw][Symbol(k)] = _convert_field(Float64, v, k, "market.reserve_cap_mw")
    end
    return MarketConfig(; kw...)
end

"""
    load_config(path) -> Config

Read a TOML configuration file. Missing keys fall back to the defaults above; unknown
sections or keys are an error.
"""
function load_config(path::AbstractString)
    raw = TOML.parsefile(path)
    _check_keys(raw, _SECTIONS, "top level")
    battery = _fill(BatteryConfig, _get(raw, "battery", Dict()), "battery")
    market = _load_market(_get(raw, "market", Dict()))
    strategy = _fill(StrategyConfig, _get(raw, "strategy", Dict()), "strategy")
    forecast = _fill(ForecastConfig, _get(raw, "forecast", Dict()), "forecast")
    simulation = _fill(SimulationConfig, _get(raw, "simulation", Dict()), "simulation")
    d = _get(raw, "data", Dict())
    cols = _get(d, "columns", Dict())
    _check_keys(cols, _DATA_COLUMN_KEYS, "data.columns")
    data = _fill(DataConfig, Dict(k => v for (k, v) in d if k != "columns"), "data"; skip = ("columns",))
    data = DataConfig(; (f => getfield(data, f) for f in fieldnames(DataConfig) if f != :columns)...,
                      columns = Dict{String,String}(String(k) => String(v) for (k, v) in cols))
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
    s.soc_guard in ("worst_case", "quantile", "expected") || error("soc_guard must be worst_case, quantile or expected")
    s.energy_flex_mw >= 0 || error("energy_flex_mw must be >= 0")
    0.5 <= s.soc_guard_quantile < 1 || error("soc_guard_quantile must be in [0.5, 1)")
    m.reserve_headroom in ("per_class", "summed") || error("reserve_headroom must be per_class or summed")
    m.offered_capacity_mw >= 0 || error("offered_capacity_mw must be >= 0 (0 = power rating)")
    all(>=(0), (m.deploy_reg_up, m.deploy_reg_down, m.deploy_primary, m.deploy_contingency)) ||
        error("deployment fractions must be >= 0")
    all(>=(0), (m.sustain_regulation, m.sustain_primary, m.sustain_contingency)) || error("sustain hours must be >= 0")
    m.offer_type in ("standing", "variation") || error("market.offer_type must be standing or variation")
    !isempty(m.facility_id) || error("market.facility_id must not be empty")
    1 <= m.max_charge_pairs <= 5 && 1 <= m.max_discharge_pairs <= 5 || error("RC383 allows at most 5 charging and 5 discharging pairs")
    s.horizon_periods >= 1 || error("horizon_periods must be >= 1")
    s.lead_periods >= 1 || error("lead_periods must be >= 1")
    s.lead_periods <= s.horizon_periods || error("lead_periods must be <= horizon_periods")
    s.soc_margin_mwh >= 0 || error("soc_margin_mwh must be >= 0")
    0 <= s.soc_guard_buffer_mwh < (soc_max(b) - soc_min(b)) / 2 ||
        error("soc_guard_buffer_mwh must be in [0, half the usable SOC range)")
    soc_min(b) <= s.terminal_soc_frac * b.energy_mwh <= soc_max(b) || error("terminal_soc_frac outside SOC limits")
    for (k, qs) in (("energy_grid_quantiles", s.energy_grid_quantiles), ("reserve_grid_quantiles", s.reserve_grid_quantiles))
        !isempty(qs) && all(q -> 0 < q < 1, qs) || error("strategy.$k must be probabilities in (0, 1)")
    end
    b.power_mw > 0 && b.energy_mwh > 0 || error("power_mw and energy_mwh must be > 0")
    m.period_hours > 0 || error("period_hours must be > 0")
    m.periods_per_day >= 1 && isapprox(m.periods_per_day * m.period_hours, 24.0; atol = 1e-9) ||
        error("periods_per_day * period_hours must equal 24 (got $(m.periods_per_day) x $(m.period_hours))")
    m.max_energy_tranches >= 1 && m.max_reserve_tranches >= 1 || error("max_*_tranches must be >= 1")
    f = c.forecast
    c.forecast.method in ("ridge", "seasonal_naive", "predispatch") || error("unknown forecast method")
    f.train_days >= 1 || error("forecast.train_days must be >= 1")
    f.ridge_alpha >= 0 || error("forecast.ridge_alpha must be >= 0")
    f.origin_stride_periods >= 1 || error("forecast.origin_stride_periods must be >= 1")
    f.pd_horizon_periods >= 0 || error("forecast.pd_horizon_periods must be >= 0")
    f.error_period_window >= 0 || error("forecast.error_period_window must be >= 0")
    c.simulation.shortfall_penalty >= 0 || error("simulation.shortfall_penalty must be >= 0")
    d = c.data
    isempty(d.date_col) == isempty(d.period_col) || error("data.date_col and data.period_col must be set together")
    !isempty(d.date_col) || !isempty(d.timestamp_col) || error("data.timestamp_col (or date_col + period_col) must be set")
    0 <= d.max_missing_share <= 1 || error("data.max_missing_share must be in [0, 1]")
    d.pd_min_lead_periods >= 0 || error("data.pd_min_lead_periods must be >= 0")
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
