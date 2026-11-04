# Local market simulator used for backtesting. EMA's own evaluation simulator is not public;
# this one mimics a co-optimised, price-taking clearing of the BESS's offers.
#
# Per trading period, given realised prices, the BESS's awards maximise its offer surplus
#     Σ_k (λ - p_k) q_k   over energy and reserve tranches
# subject to the joint capability constraints the market enforces:
#     net export + Σ raise reserves <= P,   import + regulation <= P.
# A tranche priced at or below the market price clears (energy sells, charging buys).

struct PeriodOutcome
    net_mw::Float64
    reserve_mw::Dict{Symbol,Float64}
    soc_start::Float64
    soc_end::Float64
    revenue::Dict{Symbol,Float64}   # $ by product
    shortfall_mwh::Float64
    sustain_violation::Bool
    sustain_violation_mwh::Float64   # how far awards exceeded the sustainable energy
    throughput_mwh::Float64
end

function clear_period(pb::PeriodBids, prices::Dict{Symbol,Float64}, cfg::Config)
    P = cfg.battery.power_mw
    model = Model(optimizer_with_attributes(HiGHS.Optimizer, "output_flag" => false))
    e = pb.offers[:energy]
    @variable(model, 0 <= qe[k in eachindex(e.prices)] <= e.sizes[k])
    q = Dict{Symbol,Any}()
    base = Dict{Symbol,Any}()
    for p in RESERVES
        o = pb.offers[p]
        q[p] = @variable(model, [k in eachindex(o.prices)], lower_bound = 0, upper_bound = o.sizes[k])
        base[p] = @variable(model, lower_bound = 0, upper_bound = o.base_mw)  # base cleared at price >= 0
    end
    x = @expression(model, e.base_mw + sum(qe; init = 0.0))
    rtot(p) = base[p] + sum(q[p]; init = 0.0)
    # Joint capability (RC383 D.17.2.4A, D.18.1.6/7), limited by the offered capacity.
    Pc = min(P, offered_capacity(cfg))
    if cfg.market.reserve_headroom == "per_class"
        @constraint(model, x + rtot(:regulation) + rtot(:primary) <= Pc)
        @constraint(model, x + rtot(:regulation) + rtot(:contingency) <= Pc)
        @constraint(model, x + rtot(:regulation) <= Pc)
    else
        @constraint(model, x + sum(rtot(p) for p in RESERVES) <= Pc)
    end
    @constraint(model, -x + rtot(:regulation) <= Pc)
    eps = 1e-6   # tie-break: prefer clearing at-the-money tranches
    @objective(model, Max,
        sum((prices[:energy] - e.prices[k] + eps) * qe[k] for k in eachindex(e.prices); init = 0.0) +
        sum((prices[p] - pb.offers[p].prices[k] + eps) * q[p][k] for p in RESERVES for k in eachindex(pb.offers[p].prices); init = 0.0) +
        sum((prices[p] + eps) * base[p] for p in RESERVES))
    optimize!(model)
    if !is_solved_and_feasible(model)
        # Base energy position alone violates joint capability: clear energy only.
        return e.base_mw, Dict(p => 0.0 for p in RESERVES)
    end
    return value(x), Dict(p => value(rtot(p)) for p in RESERVES)
end

"""
    settle_period(net_mw, reserves, soc, prices, cfg; rng) -> PeriodOutcome

Apply awards physically: move SOC, deploy reserves (expected or random), clamp at SOC
limits and record any shortfall and sustainability breach.
"""
function settle_period(net::Float64, res::Dict{Symbol,Float64}, soc::Float64,
                       prices::Dict{Symbol,Float64}, cfg::Config; rng = nothing)
    b, m, sim = cfg.battery, cfg.market, cfg.simulation
    η, dt = eta_one_way(b), m.period_hours
    Emin, Emax = soc_min(b), soc_max(b)

    # Sustainability check of awards at the start of the period (compliance metric).
    # Raise reserves deployed while charging first reduce the charge, so only the part above
    # the charging rate drains stored energy (and symmetrically for regulation-lower).
    need_up = max(0.0, net * dt + raise_energy(res, m)) / η
    need_dn = η * max(0.0, -net * dt + res[:regulation] * sustain_hours(m, :regulation))
    viol_mwh = max(0.0, Emin - (soc - need_up), (soc + need_dn) - Emax)
    viol = viol_mwh > 1e-6

    dep = deployment_energy_coef(cfg)
    deploy = sum(dep[p] * res[p] for p in RESERVES)
    if sim.stochastic_deployment && rng !== nothing
        deploy *= 2 * rand(rng)   # mean-preserving random deployment
    end
    ch, dis = max(-net, 0.0), max(net, 0.0)
    soc_new = soc + dt * (η * ch - dis / η) + deploy
    shortfall = 0.0
    if soc_new < Emin
        shortfall = (Emin - soc_new) * η     # MWh the grid did not receive
        soc_new = Emin
    elseif soc_new > Emax
        shortfall = (soc_new - Emax) / η     # MWh the battery could not absorb
        soc_new = Emax
    end
    rev = Dict{Symbol,Float64}(:energy => prices[:energy] * net * dt)
    for p in RESERVES
        rev[p] = prices[p] * res[p] * dt
    end
    rev[:penalty] = -sim.shortfall_penalty * shortfall
    return PeriodOutcome(net, res, soc, soc_new, rev, shortfall, viol, viol_mwh, (ch + dis) * dt)
end

realised_prices(df::DataFrame, t::Int) = Dict(p => df[t, PRICE_COLS[p]] for p in PRODUCTS)
