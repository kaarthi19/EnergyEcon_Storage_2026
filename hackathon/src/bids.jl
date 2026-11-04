# Offer curves: construction from parametric co-optimisation, SOC safety guard, export.
#
# Energy offer: one monotone curve over net MW in [-P, +P] (charging negative, discharging
# positive). Cleared net MW at price λ is
#     base_mw + Σ size_k  over tranches with price_k <= λ.
# Below the lowest tranche price the battery sits at `base_mw` (most charging); each tranche
# either reduces charging or adds discharge as the price rises. This is how the
# charge(-)/discharge(+) convention of RC383 §3.1.2 is assumed to work. Check the exact
# field layout against RC383 before submitting; see `offers_table`.
#
# Reserve offer: monotone quantity in [0, cap]: base_mw at any price >= 0, plus tranches.

struct Offer
    product::Symbol
    base_mw::Float64
    prices::Vector{Float64}   # ascending
    sizes::Vector{Float64}    # > 0
end

cleared_mw(o::Offer, λ::Real) = o.base_mw + sum((o.sizes[k] for k in eachindex(o.prices) if o.prices[k] <= λ); init = 0.0)
max_mw(o::Offer) = o.base_mw + sum(o.sizes; init = 0.0)
min_mw(o::Offer) = o.base_mw

"""
    offer_from_grid(product, grid_prices, quantities, max_tranches) -> Offer

Build a step offer from parametric solutions x(λ_k). Quantities are made non-decreasing
(parametric LPs are monotone in their own price; the MILP can show tiny inversions).
Each step is priced at the midpoint of the two grid prices that bracket it.
"""
function offer_from_grid(product::Symbol, g::Vector{Float64}, x::Vector{Float64}, max_tranches::Int; tol = 1e-3)
    order = sortperm(g)
    g, x = g[order], accumulate(max, x[order])
    prices, sizes = Float64[], Float64[]
    for k in 2:length(g)
        δ = x[k] - x[k-1]
        if δ > tol
            push!(prices, 0.5 * (g[k-1] + g[k])); push!(sizes, δ)
        end
    end
    return merge_tranches(Offer(product, x[1], prices, sizes), max_tranches)
end

"Merge the smallest tranche into its neighbour until at most `n` tranches remain."
function merge_tranches(o::Offer, n::Int)
    p, s = copy(o.prices), copy(o.sizes)
    while length(s) > max(n, 1)
        k = argmin(s)
        nb = k == 1 ? 2 : k == length(s) ? k - 1 : (s[k-1] <= s[k+1] ? k - 1 : k + 1)
        tot = s[k] + s[nb]
        p[nb] = (p[k] * s[k] + p[nb] * s[nb]) / tot
        s[nb] = tot
        deleteat!(p, k); deleteat!(s, k)
    end
    return Offer(o.product, o.base_mw, p, s)
end

"Cap the cleared quantity of an offer to the interval [lo, hi] for every price."
function clip_offer(o::Offer, lo::Float64, hi::Float64)
    lo > hi && (hi = lo)
    base = clamp(o.base_mw, lo, hi)
    pos = base
    prices, sizes = Float64[], Float64[]
    cum = o.base_mw
    for k in eachindex(o.prices)
        cum += o.sizes[k]
        target = clamp(cum, lo, hi)
        if target > pos + 1e-9
            push!(prices, o.prices[k]); push!(sizes, target - pos); pos = target
        end
    end
    return Offer(o.product, base, prices, sizes)
end

"Lower an offer's maximum quantity to `cap`, trimming the highest-priced tranches first."
cap_offer(o::Offer, cap::Float64) = clip_offer(o, min(o.base_mw, max(cap, 0.0)), max(cap, min(o.base_mw, cap)))

struct PeriodBids
    row::Int                          # row of the trading period in the market data
    timestamp::DateTime
    offers::Dict{Symbol,Offer}
    planned::Dict{Symbol,Float64}     # forecast-optimal quantities (energy = net MW)
end
PeriodBids(row, ts, offers) = PeriodBids(row, ts, offers, Dict(p => 0.0 for p in PRODUCTS))

"""
    guard_soc(bids, soc_lo, soc_hi, cfg) -> PeriodBids

Make every combination the market can clear SOC-feasible for the trading period, given that
the SOC at its start lies in [soc_lo, soc_hi]. The market enforces joint capability
(net export + raise reserves <= P, import + regulation <= P), so the worst-case energy drain
is bounded by P as well as by the offers.
"""
function guard_soc(pb::PeriodBids, soc_lo::Float64, soc_hi::Float64, cfg::Config)
    b, m = cfg.battery, cfg.market
    η, dt, P = eta_one_way(b), m.period_hours, b.power_mw
    hmax = maximum([dt; [sustain_hours(m, p) for p in RESERVES]])
    o = copy(pb.offers)

    # --- raise direction: discharge + raise reserves must be sustainable from soc_lo ---
    avail_up = max(0.0, soc_lo - soc_min(b)) * η                  # deliverable MWh
    e = clip_offer(o[:energy], -P, min(P, avail_up / dt))
    cap_total = avail_up / hmax                                    # MW sustainable for hmax hours
    if cap_total < P
        # Trim in priority order so the forecast-optimal plan survives: first everything
        # above the plan (reserves, then energy), then the planned quantities themselves.
        plan = pb.planned
        rsum() = sum(max_mw(o[p]) for p in RESERVES)
        excess() = max(0.0, max(max_mw(e), 0.0) + rsum() - cap_total)
        for p in reverse(RESERVES)
            excess() <= 0 && break
            keep = max(plan[p], 0.0)
            o[p] = cap_offer(o[p], max(keep, max_mw(o[p]) - excess()))
        end
        if excess() > 0
            keep = max(plan[:energy], 0.0)
            e = clip_offer(e, -P, max(keep, max_mw(e) - excess()))
        end
        for p in reverse(RESERVES)
            excess() <= 0 && break
            o[p] = cap_offer(o[p], max(0.0, max_mw(o[p]) - excess()))
        end
        if excess() > 0
            e = clip_offer(e, -P, max(0.0, max_mw(e) - excess()))
        end
    end

    # --- lower direction: charge + regulation-lower must fit below soc_hi ---
    avail_dn = max(0.0, soc_max(b) - soc_hi) / η                  # MWh that can be absorbed (grid side)
    e = clip_offer(e, max(-P, -avail_dn / dt), max_mw(e))
    hreg = sustain_hours(m, :regulation)
    cap_dn = avail_dn / max(dt, hreg)
    if cap_dn < P
        plan = pb.planned
        excess_dn() = max(0.0, max(-min_mw(e), 0.0) + max_mw(o[:regulation]) - cap_dn)
        # regulation above plan, then charging beyond plan, then planned regulation, then charging
        o[:regulation] = cap_offer(o[:regulation], max(plan[:regulation], max_mw(o[:regulation]) - excess_dn()))
        if excess_dn() > 0
            keep_ch = max(-plan[:energy], 0.0)
            e = clip_offer(e, -max(keep_ch, -min_mw(e) - excess_dn()), max_mw(e))
        end
        excess_dn() > 0 && (o[:regulation] = cap_offer(o[:regulation], max(0.0, max_mw(o[:regulation]) - excess_dn())))
        excess_dn() > 0 && (e = clip_offer(e, min(0.0, min_mw(e) + excess_dn()), max_mw(e)))
    end
    o[:energy] = e
    return PeriodBids(pb.row, pb.timestamp, o, pb.planned)
end

"""
    validate_bids(pb, cfg) -> Vector{String}

Check offers against the configured market limits. Returns a list of problems (empty = OK).
"""
function validate_bids(pb::PeriodBids, cfg::Config)
    m, P = cfg.market, cfg.battery.power_mw
    errs = String[]
    for p in PRODUCTS
        haskey(pb.offers, p) || (push!(errs, "missing $p offer"); continue)
        o = pb.offers[p]
        issorted(o.prices) || push!(errs, "$p prices not ascending")
        all(>(0), o.sizes) || push!(errs, "$p has non-positive tranche size")
        all(x -> m.price_floor <= x <= m.price_cap, o.prices) || push!(errs, "$p price outside limits")
        ntr = length(o.prices) + (p == :energy ? (o.base_mw != 0) : (o.base_mw > 0))
        ntr <= (p == :energy ? m.max_energy_tranches + 1 : m.max_reserve_tranches + 1) ||
            push!(errs, "$p has too many tranches")
        if p == :energy
            (-P - 1e-6 <= min_mw(o) && max_mw(o) <= P + 1e-6) || push!(errs, "energy MW outside ±P")
        else
            (o.base_mw >= -1e-9 && max_mw(o) <= P + 1e-6) || push!(errs, "$p MW outside [0, P]")
        end
    end
    return errs
end

"""
    offers_table(bids_list, cfg) -> DataFrame

Long table, one row per tranche:
  timestamp, period, product, tranche, price, mw_from, mw_to, quantity_mw, direction

For energy, rows run from the most-charging position upward. Quantity below the first row's
`mw_from` is the unconditional position (`tranche = 0`, priced at the price floor, so it
always clears). A tranche crossing 0 MW is split into a charging row (negative MW) and a
discharging row (positive MW) at the same price.
"""
function offers_table(bl::Vector{PeriodBids}, cfg::Config)
    rows = NamedTuple[]
    pf = cfg.market.price_floor
    ppd, dt = cfg.market.periods_per_day, cfg.market.period_hours
    for pb in bl
        per = period_of_day(pb.timestamp, dt)
        for p in PRODUCTS
            o = pb.offers[p]
            k = 0
            function emit(price, a, bq)
                bq - a <= 1e-9 && return
                if p == :energy && a < 0 < bq
                    emit(price, a, 0.0); emit(price, 0.0, bq); return
                end
                dir = p == :energy ? (bq <= 0 ? "charge" : "discharge") : "reserve"
                push!(rows, (timestamp = pb.timestamp, period = per, product = String(p), tranche = k,
                             price = round(price; digits = 2), mw_from = round(a; digits = 3),
                             mw_to = round(bq; digits = 3), quantity_mw = round(bq - a; digits = 3),
                             direction = dir))
            end
            # Unconditional block: for energy, from 0 to base (charging if base < 0).
            if p == :energy
                o.base_mw < 0 && emit(pf, o.base_mw, 0.0) # charge |base| at any price ≥ floor
                o.base_mw > 0 && emit(pf, 0.0, o.base_mw)
            else
                o.base_mw > 0 && emit(0.0, 0.0, o.base_mw)
            end
            pos = o.base_mw
            for i in eachindex(o.prices)
                k += 1
                emit(o.prices[i], pos, pos + o.sizes[i])
                pos += o.sizes[i]
            end
        end
    end
    return DataFrame(rows)
end
