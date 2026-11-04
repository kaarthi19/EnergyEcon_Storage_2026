# Energy + reserve co-optimisation of the BESS over a look-ahead window (JuMP + HiGHS).
#
# Storage physics follow code/bilevel_cvx.jl (SOC dynamics, one-way efficiency, no
# simultaneous charge/discharge via a binary), extended with three reserve products,
# reserve headroom and energy-sustainability constraints, expected reserve deployment
# and half-hourly periods. The lower-level market of the bilevel model is replaced by
# forecast prices (price taker) or, optionally, a linear price-impact term (price maker).

mutable struct CoOptModel
    model::JuMP.Model
    H::Int
    c::Vector{VariableRef}      # charge MW
    d::Vector{VariableRef}      # discharge MW
    r::Dict{Symbol,Vector{VariableRef}}   # reserve MW per product
    soc::Vector{VariableRef}    # MWh at end of each period
    prices::Dict{Symbol,Vector{Float64}}
    cfg::Config
end

struct PeriodSolution
    net_mw::Float64                         # discharge - charge in the first period
    charge_mw::Float64
    discharge_mw::Float64
    reserve_mw::Dict{Symbol,Float64}
    objective::Float64
end

"Expected change of SOC (MWh) per MW of reserve award over one period."
function deployment_energy_coef(cfg::Config)
    m, η, dt = cfg.market, eta_one_way(cfg.battery), cfg.market.period_hours
    return Dict(
        :regulation => dt * (η * m.deploy_reg_down - m.deploy_reg_up / η),
        :primary => -dt * m.deploy_primary / η,
        :contingency => -dt * m.deploy_contingency / η,
    )
end

sustain_hours(m::MarketConfig, p::Symbol) =
    p == :regulation ? m.sustain_regulation : p == :primary ? m.sustain_primary : m.sustain_contingency

function _optimizer()
    opt = optimizer_with_attributes(HiGHS.Optimizer, "output_flag" => false,
                                    "mip_rel_gap" => 1e-4, "time_limit" => 60.0)
    return opt
end

"""
    build_coopt(cfg, soc0, prices; reserves=true) -> CoOptModel

`prices[p]` is a vector over the look-ahead window for each product in PRODUCTS.
"""
function build_coopt(cfg::Config, soc0::Float64, prices::Dict{Symbol,Vector{Float64}};
                     reserves::Bool = true)
    b, m, s = cfg.battery, cfg.market, cfg.strategy
    H = length(prices[:energy])
    η, dt, P = eta_one_way(b), m.period_hours, b.power_mw
    Emin, Emax = soc_min(b), soc_max(b)
    soc0 = clamp(soc0, Emin, Emax)
    k = s.price_impact
    binary = s.use_binary && k == 0.0

    model = Model(_optimizer())
    @variable(model, 0 <= c[1:H] <= P)
    @variable(model, 0 <= d[1:H] <= P)
    @variable(model, Emin <= soc[1:H] <= Emax)
    r = Dict{Symbol,Vector{VariableRef}}()
    for p in RESERVES
        cap = reserves ? reserve_cap(m, b, p) : 0.0
        r[p] = @variable(model, [1:H], lower_bound = 0, upper_bound = cap, base_name = String(p))
    end
    dep = deployment_energy_coef(cfg)
    prev(j) = j == 1 ? soc0 : soc[j-1]

    for j in 1:H
        @constraint(model, soc[j] == prev(j) + dt * (η * c[j] - d[j] / η) +
                                     sum(dep[p] * r[p][j] for p in RESERVES))
        # Power headroom (RC383 D.17.2.4A, D.18.1.6/7): transfer + reserve + regulation <= P,
        # per reserve class ("per_class") or with primary and contingency stacked ("summed");
        # transfer - regulation >= -P.
        if m.reserve_headroom == "per_class"
            @constraint(model, d[j] - c[j] + r[:regulation][j] + r[:primary][j] <= P)
            @constraint(model, d[j] - c[j] + r[:regulation][j] + r[:contingency][j] <= P)
            @constraint(model, d[j] - c[j] + r[:regulation][j] <= P)
        else
            @constraint(model, d[j] - c[j] + sum(r[p][j] for p in RESERVES) <= P)
        end
        @constraint(model, c[j] - d[j] + r[:regulation][j] <= P)
        # Energy needed to sustain awarded raise reserves / regulation-lower (see raise_energy).
        if m.reserve_headroom == "per_class"
            zj = @variable(model, lower_bound = 0)
            @constraint(model, zj >= r[:primary][j] * m.sustain_primary)
            @constraint(model, zj >= r[:contingency][j] * m.sustain_contingency)
            @constraint(model, prev(j) - (d[j] * dt + r[:regulation][j] * m.sustain_regulation + zj) / η >= Emin)
        else
            @constraint(model, prev(j) - (d[j] * dt + sum(r[p][j] * sustain_hours(m, p) for p in RESERVES)) / η >= Emin)
        end
        @constraint(model, prev(j) + η * (c[j] * dt + r[:regulation][j] * sustain_hours(m, :regulation)) <= Emax)
    end
    if binary
        @variable(model, u[1:H], Bin)
        @constraint(model, [j in 1:H], c[j] <= P * u[j])
        @constraint(model, [j in 1:H], d[j] <= P * (1 - u[j]))
    else
        @constraint(model, [j in 1:H], c[j] + d[j] <= P)
    end

    target = s.terminal_soc_frac * b.energy_mwh
    if s.terminal_value_mode == "hard"
        @constraint(model, soc[H] >= min(target, Emax))
        tv = 0.0
    else
        # Value stored energy at the median forecast energy price, net of discharge losses.
        tv = max(0.0, median(prices[:energy])) * η
    end

    obj = @expression(model, sum(dt * (prices[:energy][j] * (d[j] - c[j]) - b.degradation_cost * (c[j] + d[j]))
                                  for j in 1:H) +
                             sum(dt * prices[p][j] * r[p][j] for p in RESERVES for j in 1:H) +
                             tv * (soc[H] - target))
    if k > 0
        @objective(model, Max, obj - sum(dt * k * (d[j] - c[j])^2 for j in 1:H))
    else
        @objective(model, Max, obj)
    end
    return CoOptModel(model, H, c, d, r, soc, deepcopy(prices), cfg)
end

"Change the price of product `p` in window position `j` (updates objective coefficients)."
function set_price!(cm::CoOptModel, p::Symbol, j::Int, λ::Float64)
    dt = cm.cfg.market.period_hours
    deg = cm.cfg.battery.degradation_cost
    cm.prices[p][j] = λ
    if p == :energy
        set_objective_coefficient(cm.model, cm.d[j], dt * (λ - deg))
        set_objective_coefficient(cm.model, cm.c[j], dt * (-λ - deg))
    else
        set_objective_coefficient(cm.model, cm.r[p][j], dt * λ)
    end
    return cm
end

function solve!(cm::CoOptModel; j::Int = 1)
    optimize!(cm.model)
    st = termination_status(cm.model)
    if !(st in (MOI.OPTIMAL, MOI.LOCALLY_SOLVED) || (st == MOI.TIME_LIMIT && has_values(cm.model)))
        error("co-optimisation failed: $st")
    end
    ch, dis = value(cm.c[j]), value(cm.d[j])
    return PeriodSolution(dis - ch, ch, dis, Dict(p => value(cm.r[p][j]) for p in RESERVES),
                          objective_value(cm.model))
end

"Full schedule of the current solution (for diagnostics and perfect-foresight benchmarks)."
function coopt_schedule(cm::CoOptModel)
    DataFrame(charge_mw = value.(cm.c), discharge_mw = value.(cm.d),
              regulation_mw = value.(cm.r[:regulation]), primary_mw = value.(cm.r[:primary]),
              contingency_mw = value.(cm.r[:contingency]), soc_mwh = value.(cm.soc))
end
