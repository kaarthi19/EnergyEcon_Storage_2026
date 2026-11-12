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
`j_fc` is the matching position in the forecast `f`.
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
    for p in PRODUCTS
        qs = p == :energy ? s.energy_grid_quantiles : s.reserve_grid_quantiles
        g = _grid(f, p, j_fc, qs, cfg)
        orig = cm.prices[p][j_model]
        g, xs = sweep_price!(cm, p, j_model, g)
        set_price!(cm, p, j_model, orig)
        ntr = p == :energy ? m.max_energy_tranches : m.max_reserve_tranches
        offers[p] = offer_from_grid(p, g, xs, ntr)
    end
    # Limit how far the energy offer can move from the plan. Offers become "open" for the
    # SOC guard of later periods (gate closure), and every MW of price response widens the
    # SOC interval those periods must cover, which costs reserve capability.
    flex = s.energy_flex_mw
    if flex > 0
        Pc = transfer_cap(cfg)
        x0 = planned[:energy]
        # With energy_flex_to_zero the offer can always back off to 0 MW (stop charging in a
        # price spike, stop discharging in a price crash), priced at its opportunity cost.
        lo = s.energy_flex_to_zero ? min(x0, 0.0) - flex : x0 - flex
        hi = s.energy_flex_to_zero ? max(x0, 0.0) + flex : x0 + flex
        offers[:energy] = clip_offer(offers[:energy], max(-Pc, lo), min(Pc, hi))
    end
    return PeriodBids(row, ts, offers, planned)
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
    soc_quantile_bounds_after(pb, lo, hi, f, j, q, cfg) -> (lo, hi)

SOC interval after an open offer. The offer is cleared at every combination of energy price
in {1-q tail, mean, q tail} and reserve prices in {1-q tail, q tail} of the forecast; the
interval spans the lowest SOC reached from `lo` and the highest reached from `hi`. The
market can trade energy against regulation, so no single combination is always the extreme.
"""
function soc_quantile_bounds_after(pb::PeriodBids, lo::Float64, hi::Float64, f::PriceForecast,
                                   j::Int, q::Float64, cfg::Config)
    qq(p, x) = forecast_quantiles(f, p, j, [x])[1]
    e_opts = (qq(:energy, 1 - q), f.mean[:energy][j], qq(:energy, q))
    r_opts = (1 - q, q)
    lo2, hi2 = Inf, -Inf
    for λe in e_opts, rq in r_opts
        pr = Dict{Symbol,Float64}(:energy => λe)
        for p in RESERVES
            pr[p] = max(0.0, qq(p, rq))
        end
        lo2 = min(lo2, soc_expected_after(pb, lo, pr, cfg))
        hi2 = max(hi2, soc_expected_after(pb, hi, pr, cfg))
    end
    return min(lo2, hi2), max(lo2, hi2)
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
    rolling_bids(df, τ, soc_now, pending, cfg) -> PeriodBids

Rolling mode. At decision origin `τ` (end of period τ, SOC `soc_now` known) produce the
offer for trading period t = τ + lead. `pending` holds already-submitted offers for
periods τ+1 .. t-1 (oldest first).
"""
function rolling_bids(df::DataFrame, τ::Int, soc_now::Float64, pending::Vector{PeriodBids}, cfg::Config)
    s = cfg.strategy
    t = τ + s.lead_periods
    last_row = min(nrow(df), t + s.horizon_periods - 1)
    f = forecast_prices(df, τ, (τ + 1):last_row, cfg)
    # Propagate SOC through offers already in the market.
    soc_exp, lo, hi = soc_now, soc_now, soc_now
    for (i, pb) in enumerate(pending)
        pr = Dict(p => f.mean[p][i] for p in PRODUCTS)
        soc_exp = soc_expected_after(pb, soc_exp, pr, cfg)
        lo, hi = s.soc_guard == "quantile" ?
            soc_quantile_bounds_after(pb, lo, hi, f, i, s.soc_guard_quantile, cfg) :
            soc_bounds_after(pb, lo, hi, cfg)
    end
    if s.soc_guard == "expected" && !isempty(pending)
        lo = max(soc_min(cfg.battery), soc_exp - s.soc_margin_mwh)
        hi = min(soc_max(cfg.battery), soc_exp + s.soc_margin_mwh)
    end
    j0 = t - τ
    n = last_row - t + 1
    cm = build_coopt(cfg, soc_exp, _window_prices(f, j0, n))
    pb = bids_from_model!(cm, f, 1, j0, t, df.timestamp[t], cfg)
    return guard_soc(pb, lo, hi, cfg)
end

"""
    day_ahead_bids(df, τ, soc_now, rows, cfg) -> Vector{PeriodBids}

Day-ahead mode: at origin τ, produce offers for every row in `rows` (e.g. the next trading
day) from one forecast, with SOC bounds propagated worst-case through the day. This is
more conservative than rolling mode because later periods must stay feasible for every
clearing outcome of earlier ones.
"""
function day_ahead_bids(df::DataFrame, τ::Int, soc_now::Float64, rows::UnitRange{Int}, cfg::Config)
    s = cfg.strategy
    last_row = min(nrow(df), max(last(rows), first(rows) + s.horizon_periods - 1))
    f = forecast_prices(df, τ, (τ + 1):last_row, cfg)
    j0 = first(rows) - τ
    n = last_row - first(rows) + 1
    # Expected SOC at the start of the first bid period (periods between τ and rows have no
    # offers in this mode, so the battery idles there).
    cm = build_coopt(cfg, soc_now, _window_prices(f, j0, n))
    out = PeriodBids[]
    lo, hi = soc_now, soc_now
    soc_exp = soc_now
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
            pb = guard_soc(pb, max(soc_min(cfg.battery), soc_exp - margin),
                           min(soc_max(cfg.battery), soc_exp + margin), cfg)
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
