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
    # reserve quantities are never negative (an LP's -1e-12 must not become a negative base)
    product == :energy || (x = max.(x, 0.0))
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

"""
Lower a reserve offer's maximum quantity to `cap`, trimming the highest-priced tranches
first. The result never has a negative base.
"""
cap_offer(o::Offer, cap::Float64) = (c = max(cap, 0.0); clip_offer(o, clamp(o.base_mw, 0.0, c), c))

struct PeriodBids
    row::Int                          # row of the trading period in the market data
    timestamp::DateTime
    offers::Dict{Symbol,Offer}
    planned::Dict{Symbol,Float64}     # forecast-optimal quantities (energy = net MW)
end
PeriodBids(row, ts, offers) = PeriodBids(row, ts, offers, Dict(p => 0.0 for p in PRODUCTS))

"""
    guard_soc(bids, soc_lo, soc_hi, cfg) -> PeriodBids

Trim the offers so that every outcome the market can clear from them (energy anywhere in the
offer's range, each reserve between 0 and its offered maximum, within the joint capability
`clear_period` enforces) is compliant for every start SOC in [soc_lo, soc_hi]: no
sustainability violation (`sustain_violation_mwh`), and the end SOC under expected
deployment stays inside the SOC limits (no shortfall). Worst cases are evaluated exactly
over the clearing polytope (`worst_case_needs`), keeping `soc_guard_buffer_mwh` away from
the limits.

Trimming order: first quantities above the forecast-optimal plan (reserves in the order
contingency, primary, regulation, then energy), then the planned quantities themselves;
raise side before lower side. Quantities are cut in that order until the offers comply,
then given back in reverse order as far as the rules allow, so the higher-priority
quantities are kept and nothing is cut more than needed (the rules are in MWh, so a cut
is sized by the energy it saves). The energy offer is finally conformed to the RC383 pair
structure.
"""
function guard_soc(pb::PeriodBids, soc_lo::Float64, soc_hi::Float64, cfg::Config)
    b, m = cfg.battery, cfg.market
    η, Pc = eta_one_way(b), transfer_cap(cfg)
    buf = cfg.strategy.soc_guard_buffer_mwh                     # safety buffer inside the SOC limits
    up_store = max(0.0, soc_lo - soc_min(b) - buf)              # MWh above the floor
    dn_store = max(0.0, soc_max(b) - soc_hi - buf)              # MWh below the ceiling
    o = copy(pb.offers)
    plan = pb.planned
    # Energy beyond the transfer limit can never clear, reserves never above their cap. Work on
    # the RC383 form: there an always-discharge block is a tranche at the price floor, which the
    # market may leave uncleared to fit reserves, so the clearable range always contains 0.
    e = conform_storage_offer(clip_offer(o[:energy], -Pc, Pc), cfg)
    x0l, x0u = min_mw(e), max_mw(e)                              # x0l <= 0 <= x0u
    # State: reserve maxima, a cap :xu on the discharge and :ch on the charge (MW). Values only
    # go down while trimming, so the clearing polytope only shrinks.
    S = Dict{Symbol,Float64}(:xu => x0u, :ch => -x0l,
                             (p => min(max(max_mw(o[p]), 0.0), reserve_cap(m, b, p)) for p in RESERVES)...)
    erange(S) = (-S[:ch], S[:xu])
    # worst-case excess (MWh) of the raise-side (:up) or lower-side (:dn) rules
    function excess(side, S)
        xl, xu = erange(S)
        w = worst_case_needs(xl, xu, (S[:regulation], S[:primary], S[:contingency]), cfg)
        # summed over raise branches, so cutting one reserve class counts while another binds
        side == :up ? sum(max(0.0, r - η * up_store) for r in w.raise_branches) + max(0.0, w.drain - up_store) :
                      max(0.0, w.lower - dn_store / η) + max(0.0, w.fill - dn_store)
    end
    tol = 1e-9
    ok(side, S) = side == :both ? ok(:up, S) && ok(:dn, S) : excess(side, S) <= tol
    # Largest value of S[k] in [lo, hi] with the side's rules met (they only get harder as S[k]
    # grows); S[lo] must comply.
    function largest_ok!(side, S, k, lo, hi)
        S[k] = hi; ok(side, S) && return S
        a, z = lo, hi
        for _ in 1:50
            mid = 0.5 * (a + z); S[k] = mid
            ok(side, S) ? (a = mid) : (z = mid)
        end
        S[k] = a
        return S
    end
    # Cut along `steps` (key => level) until the side complies, then give back in reverse.
    function trim!(side, S, steps)
        ok(side, S) && return S
        cut = Tuple{Symbol,Float64}[]
        for (k, level) in steps
            S[k] <= level && continue
            push!(cut, (k, S[k])); S[k] = level
            ok(side, S) && break
        end
        ok(side, S) || return S
        for i in length(cut):-1:1
            k, was = cut[i]
            largest_ok!(side, S, k, S[k], was)
        end
        return S
    end
    planned(p) = max(plan[p], 0.0)
    S0 = copy(S)

    for _ in 1:2
        # Energy alone must comply whatever the reserves clear: mandatory, so first.
        Z = merge(S, Dict(p => 0.0 for p in RESERVES))
        trim!(:up, Z, [:xu => max(plan[:energy], 0.0), :xu => 0.0]); S[:xu] = Z[:xu]
        trim!(:dn, Z, [:ch => max(-plan[:energy], 0.0), :ch => 0.0]); S[:ch] = Z[:ch]
        # raise side: above plan (reserves, then energy), then the plan itself
        trim!(:up, S, [:contingency => planned(:contingency), :primary => planned(:primary),
                       :regulation => planned(:regulation), :xu => max(plan[:energy], 0.0),
                       :contingency => 0.0, :primary => 0.0, :regulation => 0.0, :xu => 0.0])
        # lower side: regulation above plan, charging beyond plan, then the plan itself
        trim!(:dn, S, [:regulation => planned(:regulation), :ch => max(-plan[:energy], 0.0),
                       :regulation => 0.0, :ch => 0.0])
        ok(:both, S) && break
    end
    # One side's cuts can leave room on the other (e.g. regulation cut for the lower side frees
    # raise headroom): give reserves back, plan levels first, in priority order, as far as both
    # sides allow. Energy is not given back: price response in an offer that is still open at
    # later gate closures widens the SOC interval those periods must cover (in a 2-day default
    # backtest giving energy back cost ~15% of revenue through trimmed later regulation).
    if ok(:both, S)
        level = Dict(p => planned(p) for p in RESERVES)
        for tier in (1, 2), k in (:regulation, :primary, :contingency)
            target = tier == 1 ? min(S0[k], level[k]) : S0[k]
            S[k] < target && largest_ok!(:both, S, k, S[k], target)
        end
    end
    if !ok(:both, S)
        # not reachable with non-negative deployment fractions (an idle offer always complies)
        @warn "guard_soc: offers trimmed to idle" maxlog = 5
        for k in keys(S); S[k] = 0.0; end
    end
    # rounding the pairs and reserve quantities down only shrinks the polytope further
    o[:energy] = conform_storage_offer(clip_offer(e, erange(S)...), cfg)
    for p in RESERVES
        o[p] = round_offer(cap_offer(o[p], S[p]))
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

"Round a quantity down to 0.001 MW (never offer more than planned). The 1e-6 absorbs float
noise so that rounding an already rounded quantity leaves it unchanged."
round_mw(x::Real) = floor(x * 1000 + 1e-6) / 1000

"""
    storage_pairs(offer, cfg) -> Vector{NamedTuple{(:slot, :price, :mw)}}

RC383 §5.2B.2.4 pairs for an energy offer: charging pairs in slots 1-5 (mw <= 0) and
discharging pairs in slots 6-10 (mw >= 0), prices strictly ascending in whole cents
(§5.2B.3.1) within the price limits, every charging price below every discharging price.
An offer that never transfers is a single (slot 6, 0.00 \$/MWh, 0.0 MW) pair (§5.2B.4).
Idempotent on a conformed offer: the pairs exported are exactly the ones cleared.
"""
function storage_pairs(o::Offer, cfg::Config)
    m = cfg.market
    chp, chq, dsp, dsq = Float64[], Float64[], Float64[], Float64[]
    pos = o.base_mw
    always_ds = pos > 1e-9 ? pos : 0.0                                # discharge at any price
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
    always_ch = pos < -1e-9 ? -pos : 0.0                              # charge at any price
    # Pieces that round below 0.001 MW would be dropped at export anyway; drop them now, so
    # they neither take a price cent or a slot (no slot gaps) nor reach the merge (zero-size
    # tranches would average to 0/0). An always-block only takes a slot if it survives too.
    for (pr, q) in ((chp, chq), (dsp, dsq))
        keep = round_mw.(q) .> 0
        keepat!(pr, keep); keepat!(q, keep)
    end
    round_mw(always_ch) > 0 || (always_ch = 0.0)
    round_mw(always_ds) > 0 || (always_ds = 0.0)
    # Merge real tranches only: averaging the always-blocks (priced at the cap or floor) into
    # a tranche would offer large quantities far from their opportunity cost.
    nch = m.max_charge_pairs - (always_ch > 0 && m.max_charge_pairs > 1)
    nds = m.max_discharge_pairs - (always_ds > 0 && m.max_discharge_pairs > 1)
    _merge_pairs!(chp, chq, nch)
    _merge_pairs!(dsp, dsq, nds)
    always_ch > 0 && (push!(chp, m.price_cap); push!(chq, always_ch))
    always_ds > 0 && (pushfirst!(dsp, m.price_floor); pushfirst!(dsq, always_ds))
    _merge_pairs!(chp, chq, m.max_charge_pairs)                      # only if a side has 1 slot
    _merge_pairs!(dsp, dsq, m.max_discharge_pairs)
    # Strictly ascending whole cents within the price limits, charging below discharging.
    F, C = round(Int, 100 * m.price_floor), round(Int, 100 * m.price_cap)
    c = clamp.(round.(Int, 100 .* vcat(chp, dsp)), F, C)
    for i in 2:length(c); c[i] = max(c[i], c[i-1] + 1); end
    isempty(c) || (c[end] = min(c[end], C))
    for i in length(c)-1:-1:1; c[i] = min(c[i], c[i+1] - 1); end
    nc = length(chp)
    out = NamedTuple{(:slot, :price, :mw),Tuple{Int,Float64,Float64}}[]
    for i in 1:nc
        q = round_mw(chq[i]); q > 0 && push!(out, (slot = i, price = c[i] / 100, mw = -q))
    end
    for i in eachindex(dsp)
        q = round_mw(dsq[i]); q > 0 && push!(out, (slot = 5 + i, price = c[nc + i] / 100, mw = q))
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

"Round reserve offer prices to 2 decimals and quantities down to 0.001 MW; the base is never negative."
function round_offer(o::Offer)
    keep = [k for k in eachindex(o.sizes) if round_mw(o.sizes[k]) > 0]
    Offer(o.product, max(0.0, round_mw(o.base_mw)), round.(o.prices[keep]; digits = 2), round_mw.(o.sizes[keep]))
end

"""
    validate_bids(pb, cfg) -> Vector{String}

Check offers against RC383 §5.2B and the configured market limits. Returns a list of
problems (empty = OK).
"""
function validate_bids(pb::PeriodBids, cfg::Config)
    m, Pc = cfg.market, transfer_cap(cfg)
    errs = String[]
    for p in PRODUCTS
        haskey(pb.offers, p) || (push!(errs, "missing $p offer"); continue)
        o = pb.offers[p]
        issorted(o.prices) || push!(errs, "$p prices not ascending")
        all(>(0), o.sizes) || push!(errs, "$p has non-positive tranche size")
        all(x -> m.price_floor <= x <= m.price_cap, o.prices) || push!(errs, "$p price outside limits")
        if p != :energy
            length(o.prices) + (o.base_mw > 0) <= m.max_reserve_tranches + 1 || push!(errs, "$p has too many tranches")
            (o.base_mw >= 0 && max_mw(o) <= reserve_cap(m, cfg.battery, p) + 1e-6) ||
                push!(errs, "$p MW outside [0, cap]")
            all(x -> round(x; digits = 2) == x, o.prices) || push!(errs, "$p prices must have 2 decimals")
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
    # §5.2B.2.5: the offered capacity limits the combined transfer
    sum((-x.mw for x in ch); init = 0.0) <= Pc + 1e-6 || push!(errs, "charging total exceeds the offered capacity / charge limit")
    sum((x.mw for x in ds); init = 0.0) <= Pc + 1e-6 || push!(errs, "discharging total exceeds the offered capacity / discharge limit")
    e2 = from_storage_pairs(pairs)
    abs(min_mw(e2) - min_mw(pb.offers[:energy])) < 0.01 && abs(max_mw(e2) - max_mw(pb.offers[:energy])) < 0.01 ||
        push!(errs, "energy offer is not representable as RC383 pairs")
    return errs
end

"""
    offers_table(bids_list, cfg) -> DataFrame

One row per price-quantity pair:
  facility_id, offer_type, timestamp, period, product, slot, price, quantity_mw, direction,
  offered_capacity_mw, ramp_up_mw_per_min, ramp_down_mw_per_min

Energy rows are RC383 energy storage offer pairs (§5.2B.2.4): slots 1-5 charging
(quantity <= 0, cleared when the clearing price is below the offer price), slots 6-10
discharging (quantity >= 0, cleared when the clearing price is above the offer price).
The facility identity (§5.2B.2.1), standing offer or offer variation (§5.2B.2.2), offered
capacity (§5.2B.2.5, maximum combined transfer for energy, reserve and regulation) and ramp
rates (§5.2B.2.6) are repeated on each row. Prices are written in whole cents.
Reserve and regulation rows are incremental tranches (slot 0 = quantity offered at 0 \$/MW/h).
"""
function offers_table(bl::Vector{PeriodBids}, cfg::Config)
    rows = NamedTuple[]
    m = cfg.market
    cap = offered_capacity(cfg)
    id = (facility_id = m.facility_id, offer_type = m.offer_type)
    for pb in bl
        per = period_of_day(pb.timestamp, m.period_hours)
        for x in storage_pairs(pb.offers[:energy], cfg)
            push!(rows, (id..., timestamp = pb.timestamp, period = per, product = "energy", slot = x.slot,
                         price = round(x.price; digits = 2), quantity_mw = x.mw,
                         direction = x.slot <= 5 ? "charge" : "discharge",
                         offered_capacity_mw = cap, ramp_up_mw_per_min = m.ramp_up_mw_per_min,
                         ramp_down_mw_per_min = m.ramp_down_mw_per_min))
        end
        for p in RESERVES
            o = pb.offers[p]
            o.base_mw > 0 && push!(rows, (id..., timestamp = pb.timestamp, period = per, product = String(p), slot = 0,
                                          price = 0.0, quantity_mw = o.base_mw, direction = "reserve",
                                          offered_capacity_mw = cap, ramp_up_mw_per_min = NaN, ramp_down_mw_per_min = NaN))
            for k in eachindex(o.prices)
                push!(rows, (id..., timestamp = pb.timestamp, period = per, product = String(p), slot = k,
                             price = round(o.prices[k]; digits = 2), quantity_mw = o.sizes[k], direction = "reserve",
                             offered_capacity_mw = cap, ramp_up_mw_per_min = NaN, ramp_down_mw_per_min = NaN))
            end
        end
    end
    return DataFrame(rows)
end
