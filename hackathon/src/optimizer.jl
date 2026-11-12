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

# One thread and a fixed seed so the same inputs always give the same offers.
_highs(attrs::Pair...) = optimizer_with_attributes(HiGHS.Optimizer, "output_flag" => false,
                                                   "threads" => 1, "random_seed" => 1234, attrs...)
_optimizer() = _highs("mip_rel_gap" => 1e-4, "time_limit" => 60.0)

"""
    build_coopt(cfg, soc0, prices; reserves=true) -> CoOptModel

`prices[p]` is a vector over the look-ahead window for each product in PRODUCTS.
"""
function build_coopt(cfg::Config, soc0::Float64, prices::Dict{Symbol,Vector{Float64}};
                     reserves::Bool = true)
    b, m, s = cfg.battery, cfg.market, cfg.strategy
    H = length(prices[:energy])
    η = eta_one_way(b)
    Pc = transfer_cap(cfg)      # combined transfer limit (rating capped by offered capacity)
    Emin, Emax = soc_min(b), soc_max(b)
    soc0 = clamp(soc0, Emin, Emax)
    k = s.price_impact
    binary = s.use_binary && k == 0.0

    model = Model(_optimizer())
    @variable(model, 0 <= c[1:H] <= Pc)
    @variable(model, 0 <= d[1:H] <= Pc)
    @variable(model, Emin <= soc[1:H] <= Emax)
    r = Dict{Symbol,Vector{VariableRef}}()
    for p in RESERVES
        cap = reserves ? reserve_cap(m, b, p) : 0.0
        r[p] = @variable(model, [1:H], lower_bound = 0, upper_bound = cap, base_name = String(p))
    end
    prev(j) = j == 1 ? soc0 : soc[j-1]

    for j in 1:H
        reg, pri, con = r[:regulation][j], r[:primary][j], r[:contingency][j]
        @constraint(model, soc[j] == prev(j) + soc_change(c[j], d[j], reg, pri, con, cfg))
        # Power headroom (RC383 D.17.2.4A, D.18.1.6/7), the same joint capability the market
        # clears against: transfer + reserve + regulation <= Pc, per reserve class
        # ("per_class") or with primary and contingency stacked ("summed"); transfer -
        # regulation >= -Pc.
        if m.reserve_headroom == "per_class"
            @constraint(model, d[j] - c[j] + reg + pri <= Pc)
            @constraint(model, d[j] - c[j] + reg + con <= Pc)
            @constraint(model, d[j] - c[j] + reg <= Pc)
        else
            @constraint(model, d[j] - c[j] + reg + pri + con <= Pc)
        end
        @constraint(model, c[j] - d[j] + reg <= Pc)
        # Sustainability, the rule settle_period and guard_soc use (rules.jl): the transfer
        # plus the raise awards must be deliverable above Emin, charging plus regulation-lower
        # must fit below Emax. With sustain_charge_credit the transfer counts net (reserve
        # backed by curtailing a planned charge, as settled); without it only the planned
        # discharge (charge) counts on the raise (lower) side, because the energy offer can
        # clear at any point of its curve and the guard then cuts reserve that relied on it.
        credit = s.sustain_charge_credit
        for need in raise_needs(credit ? d[j] - c[j] : 1.0 * d[j], reg, pri, con, m)
            @constraint(model, prev(j) - need / η >= Emin)
        end
        @constraint(model, prev(j) + η * lower_need(credit ? d[j] - c[j] : -1.0 * c[j], reg, m) <= Emax)
    end
    if binary
        @variable(model, u[1:H], Bin)
        @constraint(model, [j in 1:H], c[j] <= Pc * u[j])
        @constraint(model, [j in 1:H], d[j] <= Pc * (1 - u[j]))
    else
        @constraint(model, [j in 1:H], c[j] + d[j] <= Pc)
    end

    target = s.terminal_soc_frac * b.energy_mwh
    if s.terminal_value_mode == "hard"
        @constraint(model, soc[H] >= min(target, Emax))
        tv = 0.0
    else
        # Value stored energy at the median forecast energy price, net of discharge losses.
        tv = max(0.0, median(prices[:energy])) * η
    end

    dt = m.period_hours
    obj = @expression(model, sum(dt * (prices[:energy][j] * (d[j] - c[j]) - b.degradation_cost * (c[j] + d[j]))
                                  for j in 1:H) +
                             sum(_reserve_coef(cfg, prices, p, j) * r[p][j] for p in RESERVES for j in 1:H) +
                             tv * (soc[H] - target))
    if k > 0
        @objective(model, Max, obj - sum(dt * k * (d[j] - c[j])^2 for j in 1:H))
    else
        @objective(model, Max, obj)
    end
    return CoOptModel(model, H, c, d, r, soc, deepcopy(prices), cfg)
end

"""
Objective coefficient (\$ per MW) of reserve `p` in window position `j`: the reserve price,
plus the energy its expected deployment delivers at the energy price when
`market.pay_deployment_energy` is on (the same term settle_period pays).
"""
function _reserve_coef(cfg::Config, prices, p::Symbol, j::Int)
    coef = cfg.market.period_hours * prices[p][j]
    if cfg.market.pay_deployment_energy
        g = deployment_grid_mwh(p == :regulation ? 1.0 : 0.0, p == :primary ? 1.0 : 0.0,
                                p == :contingency ? 1.0 : 0.0, cfg)
        coef += prices[:energy][j] * g
    end
    return coef
end

"Change the price of product `p` in window position `j` (updates objective coefficients)."
function set_price!(cm::CoOptModel, p::Symbol, j::Int, λ::Float64)
    dt = cm.cfg.market.period_hours
    deg = cm.cfg.battery.degradation_cost
    cm.prices[p][j] = λ
    if p == :energy
        set_objective_coefficient(cm.model, cm.d[j], dt * (λ - deg))
        set_objective_coefficient(cm.model, cm.c[j], dt * (-λ - deg))
        if cm.cfg.market.pay_deployment_energy   # deployment energy is paid at this price
            for q in RESERVES
                set_objective_coefficient(cm.model, cm.r[q][j], _reserve_coef(cm.cfg, cm.prices, q, j))
            end
        end
    else
        set_objective_coefficient(cm.model, cm.r[p][j], _reserve_coef(cm.cfg, cm.prices, p, j))
    end
    return cm
end

function _optimize_checked!(cm::CoOptModel)
    optimize!(cm.model)
    st = termination_status(cm.model)
    if st == MOI.TIME_LIMIT && has_values(cm.model)
        @warn "co-optimisation stopped at the time limit; using the best solution found" maxlog = 5
    elseif !(st in (MOI.OPTIMAL, MOI.LOCALLY_SOLVED))
        error("co-optimisation failed: $st")
    end
    return cm
end

_period_solution(cm::CoOptModel, j::Int) =
    (ch = value(cm.c[j]); dis = value(cm.d[j]);
     PeriodSolution(dis - ch, ch, dis, Dict(p => value(cm.r[p][j]) for p in RESERVES), objective_value(cm.model)))

"""
    solve!(cm; j=1) -> PeriodSolution

Solve and return the solution for window position `j`. Without the binary the LP can charge
and discharge at once (it burns energy at strongly negative prices), which a net-MW offer
cannot do. If that happens in period `j`, re-solve with charge and with discharge fixed to
0 in turn and keep the better one: exactly the MILP's branch for that period, at the cost of
two LPs and only when it occurs.
"""
function solve!(cm::CoOptModel; j::Int = 1)
    _optimize_checked!(cm)
    sol = _period_solution(cm, j)
    min(sol.charge_mw, sol.discharge_mw) <= 1e-6 && return sol
    best = nothing
    for v in (cm.c[j], cm.d[j])
        ub = upper_bound(v)
        set_upper_bound(v, 0.0)
        _optimize_checked!(cm)
        s2 = _period_solution(cm, j)
        set_upper_bound(v, ub)
        (best === nothing || s2.objective > best.objective) && (best = s2)
    end
    return best
end

"Full schedule of the current solution (for diagnostics and perfect-foresight benchmarks)."
function coopt_schedule(cm::CoOptModel)
    DataFrame(charge_mw = value.(cm.c), discharge_mw = value.(cm.d),
              regulation_mw = value.(cm.r[:regulation]), primary_mw = value.(cm.r[:primary]),
              contingency_mw = value.(cm.r[:contingency]), soc_mwh = value.(cm.soc))
end
