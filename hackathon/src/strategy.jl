# Bid generation: forecast -> co-optimise -> parametric price grids -> monotone offers -> SOC guard.

function _grid(f::PriceForecast, p::Symbol, j::Int, qs::Vector{Float64}, cfg::Config)
    m = cfg.market
    g = forecast_quantiles(f, p, j, qs)
    if p == :energy
        g = vcat(m.price_floor, g, m.price_cap)
    else
        g = vcat(0.0, max.(g, 0.0), m.price_cap)
    end
    return sort(unique(round.(clamp.(g, m.price_floor, m.price_cap); digits = 2)))
end

"""
    sweep_price!(cm, p, j, grid; tol_mw=0.5, depth=14) -> (prices, quantities)

Evaluate the optimal first-period response x(λ) of product `p` on `grid`, then bisect
every interval where x jumps until the switching price is located to within about 1 \$/MWh
(or 1%). The returned points are sorted by price.
"""
function sweep_price!(cm::CoOptModel, p::Symbol, j::Int, grid::Vector{Float64};
                      tol_mw::Float64 = 0.5, depth::Int = 14)
    resp(λ) = (set_price!(cm, p, j, λ); sol = solve!(cm; j = j);
               p == :energy ? sol.net_mw : sol.reserve_mw[p])
    pts = Dict{Float64,Float64}(λ => resp(λ) for λ in grid)
    function refine(a, b, d)
        xa, xb = pts[a], pts[b]
        (abs(xb - xa) <= tol_mw || d == 0 || b - a <= max(1.0, 0.01 * abs(b))) && return
        mid = round(0.5 * (a + b); digits = 2)
        (mid <= a || mid >= b) && return
        pts[mid] = resp(mid)
        refine(a, mid, d - 1); refine(mid, b, d - 1)
    end
    gs = sort(collect(keys(pts)))
    for k in 2:length(gs)
        refine(gs[k-1], gs[k], depth)
    end
    ks = sort(collect(keys(pts)))
    return ks, [pts[k] for k in ks]
end

"""
    bids_from_model!(cm, f, j_model, j_fc, row, ts, cfg) -> PeriodBids

Sweep the price of each product in window position `j_model` over a grid of forecast
quantiles, re-solving the co-optimisation each time, and turn the responses into offers.
`j_fc` is the matching position in the forecast `f`. With `strategy.energy_offer = "plan"`
the energy offer is the planned net MW instead (`plan_energy_offer`, no energy sweep); with
`strategy.reserve_plan_price = "zero"` the planned MW of each reserve is offered at 0 \$/MWh.
"""
function bids_from_model!(cm::CoOptModel, f::PriceForecast, j_model::Int, j_fc::Int,
                          row::Int, ts::DateTime, cfg::Config)
    s, m = cfg.strategy, cfg.market
    offers = Dict{Symbol,Offer}()
    base = solve!(cm; j = j_model)
    planned = Dict{Symbol,Float64}(:energy => base.net_mw)
    for p in RESERVES
        planned[p] = base.reserve_mw[p]
    end
    plan = s.energy_offer == "plan"
    for p in PRODUCTS
        plan && p == :energy && continue
        qs = p == :energy ? s.energy_grid_quantiles : s.reserve_grid_quantiles
        g = _grid(f, p, j_fc, qs, cfg)
        orig = cm.prices[p][j_model]
        g, xs = sweep_price!(cm, p, j_model, g)
        set_price!(cm, p, j_model, orig)
        ntr = p == :energy ? m.max_energy_tranches : m.max_reserve_tranches
        offers[p] = offer_from_grid(p, g, xs, ntr)
        if p != :energy && s.reserve_plan_price == "zero"
            # Once the energy schedule is fixed, the planned reserve MW has almost no opportunity
            # cost, and a sweep price above the realised price would leave it unawarded.
            x0 = max(planned[p], 0.0)
            offers[p] = clip_offer(offers[p], x0, max(max_mw(offers[p]), x0))
        end
    end
    if plan
        offers[:energy] = plan_energy_offer(planned[:energy], f, j_fc, cfg)
    elseif s.energy_flex_mw > 0
        # Limit how far the energy offer can move from the plan. Offers become "open" for the
        # SOC guard of later periods (gate closure), and every MW of price response widens the
        # SOC interval those periods must cover, which costs reserve capability.
        Pc, x0, flex = transfer_cap(cfg), planned[:energy], s.energy_flex_mw
        offers[:energy] = clip_offer(offers[:energy], max(-Pc, x0 - flex), min(Pc, x0 + flex))
    end
    return PeriodBids(row, ts, offers, planned)
end

"""
    plan_energy_offer(x0, f, j, cfg) -> Offer

Energy offer for `strategy.energy_offer = "plan"`: the planned net MW `x0` at any price (as RC383
pairs, one always-charge pair at the price cap or one always-discharge pair at the price floor;
below 0.001 MW the zero offer). With `strategy.energy_backoff_quantile = q > 0` a charge stops
(0 MW) once the price reaches the upper edge of `price_box(f, j, q)` and a discharge is offered
only from its lower edge up, which protects against unforecast spikes (collapses).

How the quantile guard treats the back-off: it bounds the offer's clearing over the price box
of `strategy.soc_guard_quantile`, so a back-off priced inside that box (q <= soc_guard_quantile,
including q equal to it: the box edges are inside) widens the SOC interval of later periods by
up to |x0| MW for the period and is covered. A back-off priced outside it (q above the guard's
quantile) does not widen the interval and is not covered: a sustain violation is then possible
only when the realised price leaves the guard's box and the back-off clears.
"""
function plan_energy_offer(x0::Float64, f::PriceForecast, j::Int, cfg::Config)
    q = cfg.strategy.energy_backoff_quantile
    round_mw(abs(x0)) == 0 && return Offer(:energy, 0.0, Float64[], Float64[])
    q == 0 && return Offer(:energy, x0, Float64[], Float64[])
    lo, hi = price_box(f, j, q, cfg)
    # whole cents, rounded into the box, so q = soc_guard_quantile stays covered
    if x0 < 0
        return Offer(:energy, x0, [floor(hi[1] * 100) / 100], [-x0])    # stop charging from here
    end
    return Offer(:energy, 0.0, [ceil(lo[1] * 100) / 100], [x0])          # discharge only from here
end

"""
Worst-case SOC interval after a period with the given offers, starting from [lo, hi]: the
lowest and highest end SOC under expected deployment over every outcome the market can
clear from the offers (exact, see `worst_case_needs`).
"""
function soc_bounds_after(pb::PeriodBids, lo::Float64, hi::Float64, cfg::Config)
    b = cfg.battery
    e = pb.offers[:energy]
    w = worst_case_needs(min_mw(e), max_mw(e), Tuple(max_mw(pb.offers[p]) for p in RESERVES), cfg)
    return clamp(lo - w.drain, soc_min(b), soc_max(b)), clamp(hi + w.fill, soc_min(b), soc_max(b))
end

"""
    price_box(f, j, q, cfg) -> (lo, hi)

Box of prices (energy, regulation, primary, contingency) the quantile guard covers at target
position `j` of forecast `f`: the [1-q, q] conservative forecast tails of each product
(`forecast_quantiles`), widened to contain the point forecast and the 7-day same-period
mean, within the price limits (reserve prices >= 0). The mean matters after a price spike:
the regression carries the spike into the next periods, the price can fall back within one
period, and errors from ordinary origins do not show that (in a 3-day truncated-table run
the SOC guard missed it and the battery ran 52 MWh short).
"""
function price_box(f::PriceForecast, j::Int, q::Float64, cfg::Config)
    m = cfg.market
    lo, hi = zeros(4), zeros(4)
    for (i, p) in enumerate(PRODUCTS)
        a, z = forecast_quantiles(f, p, j, [1 - q, q]; conservative = true)
        μ, π = f.mean[p][j], f.profile[p][j]
        floor_p = p == :energy ? m.price_floor : 0.0
        lo[i] = clamp(min(a, μ, π), floor_p, m.price_cap)
        hi[i] = clamp(max(z, μ, π), floor_p, m.price_cap)
    end
    return Tuple(lo), Tuple(hi)
end

"""
    soc_quantile_bounds_after(pb, lo, hi, f, j, q, cfg) -> (lo, hi)

SOC interval after an open offer, starting from [lo, hi]: the lowest and highest end SOC
under expected deployment over every outcome the market can clear from the offer for ANY
prices inside the guard's price box (`price_box`: the [1-q, q] forecast tails of all four
products). The outcomes are bounded by `box_clearing_range` (which allows for the joint
clearing trading energy against reserve) and the extremes over them are exact
(`worst_case_needs`), so the interval is guaranteed whenever the realised prices of the open
period lie in the box.
"""
function soc_quantile_bounds_after(pb::PeriodBids, lo::Float64, hi::Float64, f::PriceForecast,
                                   j::Int, q::Float64, cfg::Config)
    b = cfg.battery
    plo, phi = price_box(f, j, q, cfg)
    xl, xu, R = box_clearing_range(pb.offers[:energy], Tuple(pb.offers[p] for p in RESERVES), plo, phi, cfg)
    w = worst_case_needs(xl, xu, R, cfg)
    return clamp(lo - w.drain, soc_min(b), soc_max(b)), clamp(hi + w.fill, soc_min(b), soc_max(b))
end

"Expected SOC after a period: clear offers at forecast prices."
function soc_expected_after(pb::PeriodBids, soc::Float64, prices::Dict{Symbol,Float64}, cfg::Config)
    net, res = clear_period(pb, prices, cfg)
    return settle_period(net, res, soc, prices, cfg).soc_end
end

function _window_prices(f::PriceForecast, from::Int, n::Int)
    Dict(p => f.mean[p][from:from+n-1] for p in PRODUCTS)
end

"""
    forecast_at(df, τ, last_row, cfg) -> (f, τf)

Price forecast for rows τf+1 .. last_row made at decision origin τ. The forecast origin is
τf = τ - `strategy.info_lag_periods`: realised prices are used only up to τf (row r of the
table is position r - τf of `f`). The SOC at τ and the open offers are not affected.
"""
function forecast_at(df::DataFrame, τ::Int, last_row::Int, cfg::Config)
    τf = τ - cfg.strategy.info_lag_periods
    return forecast_prices(df, τf, (τf + 1):last_row, cfg), τf
end

"""
    any_offer_bids(row, ts, cfg) -> PeriodBids

Stand-in for an open offer that is not known: energy over the whole transfer range and every
reserve at its cap, so `soc_bounds_after` gives the widest SOC interval any offer could reach.
"""
function any_offer_bids(row::Int, ts::DateTime, cfg::Config)
    Pc = transfer_cap(cfg)
    o = Dict{Symbol,Offer}(:energy => Offer(:energy, -Pc, [0.0], [2 * Pc]))
    for p in RESERVES
        o[p] = Offer(p, reserve_cap(cfg.market, cfg.battery, p), Float64[], Float64[])
    end
    return PeriodBids(row, ts, o)
end

"""
    soc_after_open_offers(pending, τ, soc_now, f, τf, cfg) -> (soc_exp, lo, hi)

SOC at the start of the first period after the open offers `pending` (rows τ+1, τ+2, ...;
oldest first), from the SOC `soc_now` known at the end of τ: the expected value (offers
cleared at the mean forecast) and the interval [lo, hi] the SOC guard must cover
(`strategy.soc_guard`). An entry `nothing` is an open offer that is not known; it is covered
by the worst case over every offer the battery could have submitted (`any_offer_bids`).
Rolling and day-ahead bidding both start from this.
"""
function soc_after_open_offers(pending::AbstractVector, τ::Int, soc_now::Float64, f::PriceForecast,
                               τf::Int, cfg::Config)
    s, b = cfg.strategy, cfg.battery
    soc_exp, lo, hi = soc_now, soc_now, soc_now
    unknown = false
    for (i, pb) in enumerate(pending)
        k = τ + i                       # row of this open offer
        j = k - τf                      # its position in the forecast
        if pb === nothing
            unknown = true
            lo, hi = soc_bounds_after(any_offer_bids(k, DateTime(0), cfg), lo, hi, cfg)
            soc_exp = clamp(soc_exp, lo, hi)
            continue
        end
        pb.row == k || error("open offer for row $(pb.row) given where row $k was expected")
        pr = Dict(p => f.mean[p][j] for p in PRODUCTS)
        soc_exp = soc_expected_after(pb, soc_exp, pr, cfg)
        lo, hi = s.soc_guard == "quantile" ?
            soc_quantile_bounds_after(pb, lo, hi, f, j, s.soc_guard_quantile, cfg) :
            soc_bounds_after(pb, lo, hi, cfg)
    end
    # an unknown open offer keeps its worst case: never narrow it to the expected path
    if s.soc_guard == "expected" && !isempty(pending) && !unknown
        lo = max(soc_min(b), soc_exp - s.soc_margin_mwh)
        hi = min(soc_max(b), soc_exp + s.soc_margin_mwh)
    end
    return soc_exp, lo, hi
end

"""
    rolling_bids(df, τ, soc_now, pending, cfg) -> PeriodBids

Rolling mode. At decision origin `τ` (end of period τ, SOC `soc_now` known) produce the
offer for trading period t = τ + lead. `pending` holds the already-submitted offers for
periods τ+1 .. t-1 (oldest first, exactly lead - 1 of them); an entry `nothing` stands for
an offer that is open but not known, covered by its worst case.
"""
function rolling_bids(df::DataFrame, τ::Int, soc_now::Float64, pending::AbstractVector, cfg::Config)
    s = cfg.strategy
    t = τ + s.lead_periods
    length(pending) == t - τ - 1 ||
        error("rolling_bids needs the $(t - τ - 1) open offers for rows $(τ + 1):$(t - 1), got $(length(pending))")
    last_row = min(nrow(df), t + s.horizon_periods - 1)
    f, τf = forecast_at(df, τ, last_row, cfg)
    # Propagate SOC through offers already in the market.
    soc_exp, lo, hi = soc_after_open_offers(pending, τ, soc_now, f, τf, cfg)
    j0 = t - τf
    n = last_row - t + 1
    cm = build_coopt(cfg, soc_exp, _window_prices(f, j0, n); guard = s.plan_within_guard ? (lo, hi) : nothing)
    pb = bids_from_model!(cm, f, 1, j0, t, df.timestamp[t], cfg)
    return guard_soc(pb, lo, hi, cfg)
end

"""
    day_ahead_bids(df, τ, soc_now, pending, rows, cfg) -> Vector{PeriodBids}

Day-ahead mode: at origin τ (SOC `soc_now` known at the end of τ), produce offers for every
row in `rows` (e.g. the next trading day) from one forecast. `pending` holds the offers
still open for rows τ+1 .. first(rows)-1 (the previous day's last periods; `nothing` = not
known), and the SOC interval is propagated through them as in `rolling_bids`, then through
every offer of the day. This is more conservative than rolling mode because later periods
must stay feasible for every clearing outcome of earlier ones.

Not competitive under the default quantile guard: the propagated interval only widens, so
on synthetic data it reaches about [5.5, 94.5] MWh by period ~37 and the guard trims the
last ~12 periods of every day to empty offers (a 2-day window made ~25% of the rolling
mode's revenue). It is kept for EMA's day-ahead submission format.
"""
function day_ahead_bids(df::DataFrame, τ::Int, soc_now::Float64, pending::AbstractVector,
                        rows::UnitRange{Int}, cfg::Config)
    s = cfg.strategy
    length(pending) == first(rows) - τ - 1 ||
        error("day_ahead_bids needs the $(first(rows) - τ - 1) open offers for rows $(τ + 1):$(first(rows) - 1), got $(length(pending))")
    last_row = min(nrow(df), max(last(rows), first(rows) + s.horizon_periods - 1))
    f, τf = forecast_at(df, τ, last_row, cfg)
    j0 = first(rows) - τf
    n = last_row - first(rows) + 1
    soc_exp, lo, hi = soc_after_open_offers(pending, τ, soc_now, f, τf, cfg)
    # plan_within_guard: only the first day period has its guard interval at build time
    cm = build_coopt(cfg, soc_exp, _window_prices(f, j0, n);
                     guard = s.plan_within_guard && s.soc_guard != "expected" ? (lo, hi) : nothing)
    out = PeriodBids[]
    margin = s.soc_margin_mwh
    for (k, t) in enumerate(rows)
        pb = bids_from_model!(cm, f, k, j0 + k - 1, t, df.timestamp[t], cfg)
        if s.soc_guard == "worst_case"
            pb = guard_soc(pb, lo, hi, cfg)
            lo, hi = soc_bounds_after(pb, lo, hi, cfg)
        elseif s.soc_guard == "quantile"
            pb = guard_soc(pb, lo, hi, cfg)
            lo, hi = soc_quantile_bounds_after(pb, lo, hi, f, j0 + k - 1, s.soc_guard_quantile, cfg)
        else
            # the first period also keeps the worst case of unknown open offers
            plo, phi = max(soc_min(cfg.battery), soc_exp - margin), min(soc_max(cfg.battery), soc_exp + margin)
            k == 1 && ((plo, phi) = (min(plo, lo), max(phi, hi)))
            pb = guard_soc(pb, plo, phi, cfg)
            pr = Dict(p => f.mean[p][j0 + k - 1] for p in PRODUCTS)
            soc_exp = soc_expected_after(pb, soc_exp, pr, cfg)
        end
        push!(out, pb)
    end
    return out
end

"An offer that never clears anything (used for idle periods)."
idle_bids(row::Int, ts::DateTime) =
    PeriodBids(row, ts, Dict(p => Offer(p, 0.0, Float64[], Float64[]) for p in PRODUCTS))
