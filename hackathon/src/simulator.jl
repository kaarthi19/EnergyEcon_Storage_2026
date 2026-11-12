# Local market simulator used for backtesting. EMA's own evaluation simulator is not public;
# this one mimics a co-optimised, price-taking clearing of the BESS's offers.
#
# Per trading period, given realised prices, the BESS's awards maximise its offer surplus
#     Σ_k (λ - p_k) q_k   over energy and reserve tranches
# subject to the joint capability constraints the market enforces:
#     net export + Σ raise reserves <= Pc,   import + regulation <= Pc,
# where Pc = transfer_cap(cfg), the power rating capped by the offered capacity.
# A tranche priced at or below the market price clears (energy sells, charging buys).

struct PeriodOutcome
    net_mw::Float64
    reserve_mw::Dict{Symbol,Float64}
    soc_start::Float64
    soc_end::Float64
    revenue::Dict{Symbol,Float64}   # $ by product (energy on delivered MWh) and :penalty
    shortfall_mwh::Float64
    sustain_violation::Bool
    sustain_violation_mwh::Float64   # how far awards exceeded the sustainable energy
    throughput_mwh::Float64
    reserve_withheld::Float64        # $ of reserve payment for awards the SOC could not sustain
end

"Running count of clearings that fell back to the energy base (`run_backtest` reports the periods)."
const CLEAR_FALLBACKS = Ref(0)

"""
    clear_period(pb, prices, cfg) -> (net_mw, reserves)

Price-taking clearing of one period's offers against the joint capability limits (see the
file header), with `transfer_cap` as the combined transfer limit.
"""
function clear_period(pb::PeriodBids, prices::Dict{Symbol,Float64}, cfg::Config)
    Pc = transfer_cap(cfg)
    e = pb.offers[:energy]
    fallback() = begin
        # Only an energy base outside the transfer limit gets here (guarded offers never do).
        CLEAR_FALLBACKS[] += 1
        @warn "clear_period: offers cannot clear within joint capability; clearing the energy base only, clamped to the transfer limit" row = pb.row maxlog = 10
        return clamp(e.base_mw, -Pc, Pc), Dict(p => 0.0 for p in RESERVES)
    end
    abs(e.base_mw) > Pc + 1e-6 && return fallback()
    model = Model(_highs())
    @variable(model, 0 <= qe[k in eachindex(e.prices)] <= e.sizes[k])
    q = Dict{Symbol,Any}()
    base = Dict{Symbol,Any}()
    for p in RESERVES
        o = pb.offers[p]
        q[p] = @variable(model, [k in eachindex(o.prices)], lower_bound = 0, upper_bound = max(o.sizes[k], 0.0))
        # base cleared at price >= 0; a (malformed) negative base offers nothing
        base[p] = @variable(model, lower_bound = 0, upper_bound = max(o.base_mw, 0.0))
    end
    x = @expression(model, e.base_mw + sum(qe; init = 0.0))
    rtot(p) = base[p] + sum(q[p]; init = 0.0)
    # Joint capability (RC383 D.17.2.4A, D.18.1.6/7), limited by the offered capacity.
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
    is_solved_and_feasible(model) || return fallback()
    return value(x), Dict(p => value(rtot(p)) for p in RESERVES)
end

"""
    settle_period(net_mw, reserves, soc, prices, cfg; rng) -> PeriodOutcome

Apply awards physically: move SOC, deploy reserves (expected, or random with
`simulation.stochastic_deployment` and an `rng`), clamp at SOC limits and record any
shortfall and sustainability breach.

Settlement: energy is paid on the delivered quantity (scheduled MWh minus what the SOC
clamp kept from being delivered or absorbed), reserves on the award; the shortfall penalty
comes on top. With `market.pay_deployment_energy` the energy delivered by deploying
reserves is paid at the energy price as well. `reserve_withheld` is the reserve payment
for the share of the awards the start SOC could not sustain (`sustainable_share`); it is
reported, not deducted from `revenue` (the backtest's compliance-adjusted revenue does).
"""
function settle_period(net::Float64, res::Dict{Symbol,Float64}, soc::Float64,
                       prices::Dict{Symbol,Float64}, cfg::Config; rng = nothing)
    b, m, sim = cfg.battery, cfg.market, cfg.simulation
    η, dt = eta_one_way(b), m.period_hours
    Emin, Emax = soc_min(b), soc_max(b)
    reg, pri, con = res[:regulation], res[:primary], res[:contingency]

    # Sustainability check of awards at the start of the period (compliance metric), the
    # rule the guard and the optimiser use (rules.jl).
    viol_mwh = sustain_violation_mwh(net, res, soc, cfg)
    viol = viol_mwh > 1e-6

    fr = deployment_fractions(m)
    if sim.stochastic_deployment && rng !== nothing
        # Mean-preserving random deployment. Regulation raise and lower are drawn separately,
        # so the net regulation energy of a period can go either way.
        fr = map(f -> f * 2 * rand(rng), fr)
    end
    ch, dis = max(-net, 0.0), max(net, 0.0)
    soc_new = soc + soc_change(ch, dis, reg, pri, con, cfg, fr)
    shortfall = 0.0
    undelivered = 0.0                        # of the scheduled energy transfer (MWh, grid side)
    if soc_new < Emin
        shortfall = (Emin - soc_new) * η     # MWh the grid did not receive
        undelivered = min(shortfall, dis * dt)
        soc_new = Emin
    elseif soc_new > Emax
        shortfall = (soc_new - Emax) / η     # MWh the battery could not absorb
        undelivered = -min(shortfall, ch * dt)
        soc_new = Emax
    end
    λ = prices[:energy]
    rev = Dict{Symbol,Float64}(:energy => λ * (net * dt - undelivered))
    m.pay_deployment_energy && (rev[:energy] += λ * deployment_grid_mwh(reg, pri, con, cfg, fr))
    for p in RESERVES
        rev[p] = prices[p] * res[p] * dt
    end
    rev[:penalty] = -sim.shortfall_penalty * shortfall
    withheld = 0.0
    if viol
        s_up, s_dn = sustainable_share(net, res, soc, cfg)
        withheld = (1 - min(s_up, s_dn)) * rev[:regulation] + (1 - s_up) * (rev[:primary] + rev[:contingency])
    end
    return PeriodOutcome(net, res, soc, soc_new, rev, shortfall, viol, viol_mwh, (ch + dis) * dt, withheld)
end

realised_prices(df::DataFrame, t::Int) = Dict(p => df[t, PRICE_COLS[p]] for p in PRODUCTS)
