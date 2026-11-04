# Offer curves: construction from parametric co-optimisation, SOC safety guard, export.
#
# Internally an energy offer is one monotone curve over net MW in [-P, +P]: the cleared
# transfer at price λ is   base_mw + Σ size_k over tranches with price_k <= λ.
# This is exactly an RC383 energy storage offer (Market Rules Ch.6 §5.2B, Appendix 6D D.15.4):
#   * charging pairs (slots 1-5, quantity <= 0) clear when offer price > clearing price,
#   * discharging pairs (slots 6-10, quantity >= 0) clear when offer price < clearing price,
#   * prices ascend, charging prices below discharging prices.
# With base_mw = Σ charging quantities, each charging pair (p, -q) is a tranche of size q at
# price p ("stop charging q MW once λ reaches p") and each discharging pair is a tranche at p.
# `storage_pairs` / `from_storage_pairs` convert between the two forms.
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
    raise_mw(reg, pri, con, m) / raise_mw(dict, m)

Raise capability the market reserves on top of the energy transfer. RC383 D.17.2.4A limits
transfer + reserve + regulation <= max capacity *separately for each reserve offer*, so with
`reserve_headroom = "per_class"` primary and contingency share headroom (max); with
"summed" they are stacked (conservative).
"""
raise_mw(reg, pri, con, m::MarketConfig) = reg + (m.reserve_headroom == "per_class" ? max(pri, con) : pri + con)
raise_mw(d::AbstractDict, m::MarketConfig) = raise_mw(d[:regulation], d[:primary], d[:contingency], m)

"Energy (MWh, grid side) a set of raise awards must be able to sustain."
raise_energy(reg, pri, con, m::MarketConfig) =
    reg * m.sustain_regulation + (m.reserve_headroom == "per_class" ?
        max(pri * m.sustain_primary, con * m.sustain_contingency) :
        pri * m.sustain_primary + con * m.sustain_contingency)
raise_energy(d::AbstractDict, m::MarketConfig) = raise_energy(d[:regulation], d[:primary], d[:contingency], m)

"""
    guard_soc(bids, soc_lo, soc_hi, cfg) -> PeriodBids

Make every combination the market can clear SOC-feasible for the trading period, given that
the SOC at its start lies in [soc_lo, soc_hi]. The market enforces joint capability
(RC383 D.17.2.4A, D.18.1.6/7), so the worst-case drain is bounded by P as well as by the
offers. The energy offer is finally conformed to the RC383 pair structure.
"""
function guard_soc(pb::PeriodBids, soc_lo::Float64, soc_hi::Float64, cfg::Config)
    b, m = cfg.battery, cfg.market
    η, dt, P = eta_one_way(b), m.period_hours, b.power_mw
    hmax = maximum([dt; [sustain_hours(m, p) for p in RESERVES]])
    o = copy(pb.offers)
    plan = pb.planned
    rmax() = Dict(p => max_mw(o[p]) for p in RESERVES)

    # --- raise direction: discharge + raise reserves must be sustainable from soc_lo ---
    buf = cfg.strategy.soc_guard_buffer_mwh                       # safety buffer inside the SOC limits
    avail_up = max(0.0, soc_lo - soc_min(b) - buf) * η            # deliverable MWh
    e = clip_offer(o[:energy], -P, min(P, avail_up / dt))
    cap_total = avail_up / hmax                                    # MW sustainable for hmax hours
    if cap_total < P
        # Trim in priority order so the forecast-optimal plan survives: first everything
        # above the plan (reserves, then energy), then the planned quantities themselves.
        excess() = max(0.0, max(max_mw(e), 0.0) + raise_mw(rmax(), m) - cap_total)
        for p in reverse(RESERVES)
            excess() <= 0 && break
            o[p] = cap_offer(o[p], max(max(plan[p], 0.0), max_mw(o[p]) - excess()))
        end
        if excess() > 0
            e = clip_offer(e, -P, max(max(plan[:energy], 0.0), max_mw(e) - excess()))
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
    avail_dn = max(0.0, soc_max(b) - soc_hi - buf) / η            # MWh that can be absorbed (grid side)
    e = clip_offer(e, max(-P, -avail_dn / dt), max_mw(e))
    hreg = sustain_hours(m, :regulation)
    cap_dn = avail_dn / max(dt, hreg)
    if cap_dn < P
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
    o[:energy] = conform_storage_offer(e, cfg)
    for p in RESERVES
        o[p] = round_offer(o[p])
    end
    return PeriodBids(pb.row, pb.timestamp, o, pb.planned)
end

# ---------------------------------------------------------------------------------------
# RC383 energy storage offer pairs
# ---------------------------------------------------------------------------------------

"Merge adjacent (price, |quantity|) pairs, smallest first, until at most `n` remain."
function _merge_pairs!(pr::Vector{Float64}, q::Vector{Float64}, n::Int)
    while length(q) > n
        k = argmin(q)
        nb = k == 1 ? 2 : k == length(q) ? k - 1 : (q[k-1] <= q[k+1] ? k - 1 : k + 1)
        tot = q[k] + q[nb]
        pr[nb] = (pr[k] * q[k] + pr[nb] * q[nb]) / tot
        q[nb] = tot
        deleteat!(pr, k); deleteat!(q, k)
    end
    return pr, q
end

"""
    storage_pairs(offer, cfg) -> Vector{NamedTuple{(:slot, :price, :mw)}}

RC383 §5.2B.2.4 pairs for an energy offer: charging pairs in slots 1-5 (mw <= 0) and
discharging pairs in slots 6-10 (mw >= 0), prices ascending to 2 decimals, every charging
price below every discharging price. An offer that never transfers is a single
(slot 6, 0.00 \$/MWh, 0.0 MW) pair (§5.2B.4).
"""
function storage_pairs(o::Offer, cfg::Config)
    m = cfg.market
    chp, chq, dsp, dsq = Float64[], Float64[], Float64[], Float64[]
    pos = o.base_mw
    pos > 1e-9 && (push!(dsp, m.price_floor); push!(dsq, pos))        # always discharge
    for k in eachindex(o.prices)
        a, z, p = pos, pos + o.sizes[k], o.prices[k]
        if z <= 1e-9
            push!(chp, p); push!(chq, z - a)
        elseif a >= -1e-9
            push!(dsp, p); push!(dsq, z - a)
        else
            push!(chp, p); push!(chq, -a); push!(dsp, p); push!(dsq, z)
        end
        pos = z
    end
    pos < -1e-9 && (push!(chp, m.price_cap); push!(chq, -pos))        # always charge
    _merge_pairs!(chp, chq, m.max_charge_pairs)
    _merge_pairs!(dsp, dsq, m.max_discharge_pairs)
    rp(x) = round(x; digits = 2)
    chp, dsp = rp.(chp), rp.(dsp)
    # charging prices strictly below discharging prices, each side strictly ascending
    for i in 2:length(chp); chp[i] = max(chp[i], chp[i-1] + 0.01); end
    if !isempty(chp) && !isempty(dsp) && dsp[1] <= chp[end]
        if chp[end] - 0.01 >= m.price_floor && (length(chp) < 2 || chp[end-1] < chp[end] - 0.01)
            chp[end] = rp(chp[end] - 0.01)
        end
        dsp[1] = max(dsp[1], chp[end] + 0.01)
    end
    for i in 2:length(dsp); dsp[i] = max(dsp[i], dsp[i-1] + 0.01); end
    rq(x) = floor(x * 1000) / 1000                                    # never round MW up
    out = NamedTuple{(:slot, :price, :mw),Tuple{Int,Float64,Float64}}[]
    for i in eachindex(chp)
        q = rq(chq[i]); q > 0 && push!(out, (slot = i, price = clamp(chp[i], m.price_floor, m.price_cap), mw = -q))
    end
    for i in eachindex(dsp)
        q = rq(dsq[i]); q > 0 && push!(out, (slot = 5 + i, price = clamp(dsp[i], m.price_floor, m.price_cap), mw = q))
    end
    isempty(out) && push!(out, (slot = 6, price = 0.0, mw = 0.0))
    return out
end

"Rebuild the internal curve from RC383 pairs (charging pairs clear below their price)."
function from_storage_pairs(pairs)
    base = sum((pr.mw for pr in pairs if pr.mw < 0); init = 0.0)
    ps = [(pr.price, abs(pr.mw)) for pr in pairs if pr.mw != 0]
    sort!(ps; by = first)
    return Offer(:energy, base, first.(ps), last.(ps))
end

"Round-trip an energy offer through the RC383 pair format so we clear exactly what is submitted."
conform_storage_offer(o::Offer, cfg::Config) = from_storage_pairs(storage_pairs(o, cfg))

"Round reserve offer prices to 2 decimals and quantities down to 0.001 MW."
function round_offer(o::Offer)
    rq(x) = floor(x * 1000) / 1000
    keep = [k for k in eachindex(o.sizes) if rq(o.sizes[k]) > 0]
    Offer(o.product, rq(o.base_mw), round.(o.prices[keep]; digits = 2), rq.(o.sizes[keep]))
end

"""
    validate_bids(pb, cfg) -> Vector{String}

Check offers against RC383 §5.2B and the configured market limits. Returns a list of
problems (empty = OK).
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
        if p != :energy
            length(o.prices) + (o.base_mw > 0) <= m.max_reserve_tranches + 1 || push!(errs, "$p has too many tranches")
            (o.base_mw >= -1e-9 && max_mw(o) <= reserve_cap(m, cfg.battery, p) + 1e-6) ||
                push!(errs, "$p MW outside [0, cap]")
        end
    end
    haskey(pb.offers, :energy) || return errs
    pairs = storage_pairs(pb.offers[:energy], cfg)
    ch = [x for x in pairs if x.slot <= 5]
    ds = [x for x in pairs if x.slot >= 6]
    1 <= length(pairs) <= 10 || push!(errs, "energy storage offer must have 1-10 pairs (§5.2B.2.4)")
    length(ch) <= 5 && length(ds) <= 5 || push!(errs, "more than 5 charging or discharging pairs")
    all(x -> x.mw <= 0, ch) && all(x -> x.mw >= 0, ds) || push!(errs, "pair sign does not match slot (§5.2B.2.4)")
    pr = [x.price for x in pairs]
    all(diff(pr) .> 0) || push!(errs, "pair prices not strictly increasing")
    all(x -> x.mw != 0 || x.price == 0, pairs) || push!(errs, "0 MW pair must be priced at \$0.00 (§5.2B.4)")
    all(x -> round(x.price; digits = 2) == x.price, pairs) || push!(errs, "prices must have 2 decimals (§5.2B.3.1)")
    sum((-x.mw for x in ch); init = 0.0) <= P + 1e-6 || push!(errs, "charging total exceeds max charge limit (§5.2B.5)")
    sum((x.mw for x in ds); init = 0.0) <= P + 1e-6 || push!(errs, "discharging total exceeds max discharge limit (§5.2B.6)")
    e2 = from_storage_pairs(pairs)
    abs(min_mw(e2) - min_mw(pb.offers[:energy])) < 0.01 && abs(max_mw(e2) - max_mw(pb.offers[:energy])) < 0.01 ||
        push!(errs, "energy offer is not representable as RC383 pairs")
    return errs
end

offered_capacity(cfg::Config) = cfg.market.offered_capacity_mw > 0 ? cfg.market.offered_capacity_mw : cfg.battery.power_mw

"""
    offers_table(bids_list, cfg) -> DataFrame

One row per price-quantity pair:
  timestamp, period, product, slot, price, quantity_mw, direction,
  offered_capacity_mw, ramp_up_mw_per_min, ramp_down_mw_per_min

Energy rows are RC383 energy storage offer pairs (§5.2B.2.4): slots 1-5 charging
(quantity <= 0, cleared when the clearing price is below the offer price), slots 6-10
discharging (quantity >= 0, cleared when the clearing price is above the offer price).
The offered capacity (§5.2B.2.5, maximum combined transfer for energy, reserve and
regulation) and ramp rates (§5.2B.2.6) are repeated on each energy row.
Reserve and regulation rows are incremental tranches (slot 0 = quantity offered at 0 \$/MW/h).
"""
function offers_table(bl::Vector{PeriodBids}, cfg::Config)
    rows = NamedTuple[]
    m = cfg.market
    cap = offered_capacity(cfg)
    for pb in bl
        per = period_of_day(pb.timestamp, m.period_hours)
        for x in storage_pairs(pb.offers[:energy], cfg)
            push!(rows, (timestamp = pb.timestamp, period = per, product = "energy", slot = x.slot,
                         price = x.price, quantity_mw = x.mw,
                         direction = x.slot <= 5 ? "charge" : "discharge",
                         offered_capacity_mw = cap, ramp_up_mw_per_min = m.ramp_up_mw_per_min,
                         ramp_down_mw_per_min = m.ramp_down_mw_per_min))
        end
        for p in RESERVES
            o = pb.offers[p]
            o.base_mw > 0 && push!(rows, (timestamp = pb.timestamp, period = per, product = String(p), slot = 0,
                                          price = 0.0, quantity_mw = o.base_mw, direction = "reserve",
                                          offered_capacity_mw = cap, ramp_up_mw_per_min = NaN, ramp_down_mw_per_min = NaN))
            for k in eachindex(o.prices)
                push!(rows, (timestamp = pb.timestamp, period = per, product = String(p), slot = k,
                             price = o.prices[k], quantity_mw = o.sizes[k], direction = "reserve",
                             offered_capacity_mw = cap, ramp_up_mw_per_min = NaN, ramp_down_mw_per_min = NaN))
            end
        end
    end
    return DataFrame(rows)
end
