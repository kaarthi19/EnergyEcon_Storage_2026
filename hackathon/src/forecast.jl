# Price forecasting with a strict information set.
#
# At decision origin `τ` (row index) the algorithm may use realised prices in rows 1:τ and
# pre-dispatch values for target rows t with t - τ <= the pre-dispatch horizon available at τ
# (see `pd_horizon_at`). Nothing after the origin is read, so the forecaster respects EMA's
# data cut-off by construction.

"""
Forecast errors over the training window, with the trading period of day and the lead time
(periods after the forecast origin) of each error.
"""
struct Residuals
    r::Vector{Float64}
    period::Vector{Int}
    lead::Vector{Int}
end
Residuals() = Residuals(Float64[], Int[], Int[])
Base.isempty(x::Residuals) = isempty(x.r)

"""
Heteroscedastic error model: error = scale(period, lead) × z, where `z` is drawn from the
pooled standardised errors. scale = robust spread (MAD) of errors within ±`period_window`
trading periods of the target's period of day, times a lead-time factor (MAD in the
target's lead bucket relative to the overall MAD). Quiet overnight periods and short leads
get narrow bands; spike-prone evening periods and long leads get wide ones.
"""
struct ErrorModel
    period_scale::Vector{Float64}   # per trading period of day
    lead_edges::Vector{Int}         # upper lead of each bucket
    lead_factor::Vector{Float64}
    z::Vector{Float64}              # pooled standardised errors
end

struct PriceForecast
    targets::UnitRange{Int}
    mean::Dict{Symbol,Vector{Float64}}        # point forecast per product over `targets`
    residuals::Dict{Symbol,Vector{Float64}}   # pooled residual sample (actual - forecast)
    errors::Dict{Symbol,Union{ErrorModel,Nothing}}  # conditional error model (or nothing)
    periods::Vector{Int}                      # trading period of day of each target
    leads::Vector{Int}                        # lead (periods after origin) of each target
    # The SOC guard's error sample and model: out of sample (for ridge the two above are the
    # fit's own residuals, which set the offer price grids).
    guard_residuals::Dict{Symbol,Vector{Float64}}
    guard_errors::Dict{Symbol,Union{ErrorModel,Nothing}}
    # out-of-sample errors at lead h (index) from EVERY origin of the training window, for the
    # short leads of the offers still open at gate closure (`guard_leads`)
    lead_residuals::Dict{Symbol,Vector{Vector{Float64}}}
    profile::Dict{Symbol,Vector{Float64}}     # mean of the last 7 days' same-period prices
end

_mad(x) = isempty(x) ? 0.0 : 1.4826 * median(abs.(x .- median(x)))
_spread(x) = (m = _mad(x); m > 1e-9 ? m : (isempty(x) ? 0.0 : std(x; corrected = false)))

const LEAD_EDGES = [1, 2, 4, 8, 16, 48, typemax(Int)]

function fit_error_model(res::Residuals, ppd::Int, window::Int; min_n::Int = 30)
    length(res.r) < max(min_n, 50) && return nothing
    overall = _spread(res.r)
    overall <= 1e-9 && return nothing
    ps = zeros(ppd)
    for k in 1:ppd
        d = mod.(res.period .- k .+ ppd ÷ 2, ppd) .- ppd ÷ 2      # circular distance
        sel = abs.(d) .<= window
        ps[k] = count(sel) >= min_n ? max(_spread(res.r[sel]), 0.05 * overall) : overall
    end
    lf = ones(length(LEAD_EDGES))
    lo = 0
    for (b, hi) in enumerate(LEAD_EDGES)
        sel = (res.lead .> lo) .& (res.lead .<= hi)
        lf[b] = count(sel) >= min_n ? max(_spread(res.r[sel]) / overall, 0.05) : 1.0
        lo = hi
    end
    scale = [ps[res.period[i]] * lf[_lead_bucket(res.lead[i])] for i in eachindex(res.r)]
    return ErrorModel(ps, LEAD_EDGES, lf, res.r ./ scale)
end

_lead_bucket(h::Int) = something(findfirst(>=(h), LEAD_EDGES), length(LEAD_EDGES))

"""
    forecast_quantiles(f, p, j, qs; conservative=false)

Quantiles of the forecast distribution for product `p` at target position `j`. Uses the
conditional error model when available, else the pooled residuals (`residuals`/`errors`:
the offer price grids use these). `conservative = true` is for the SOC guard: it takes on
each side (lower tail for q <= 0.5, upper tail above) the widest of the conditional and the
pooled quantile of both error samples (`residuals`/`errors` and the out-of-sample
`guard_residuals`/`guard_errors`) and, at the short leads of `guard_leads`, of that lead's
out-of-sample errors from every origin. A narrow conditional band in a quiet period must not
let through offers whose clearing the pooled errors say is possible, in-sample errors
understate the error of a forecast, and errors at the open offers' leads after a rare origin
(e.g. a one-period price dip) are missed by origins `origin_stride_periods` apart.
"""
function forecast_quantiles(f::PriceForecast, p::Symbol, j::Int, qs::AbstractVector{<:Real};
                            conservative::Bool = false)
    base = f.mean[p][j]
    function band(r, em)
        pooled = isempty(r) ? fill(base, length(qs)) : base .+ quantile(r, qs)
        cond = em === nothing ? pooled :
            base .+ em.period_scale[f.periods[j]] * em.lead_factor[_lead_bucket(f.leads[j])] .* quantile(em.z, qs)
        return cond, pooled
    end
    cond, pooled = band(f.residuals[p], f.errors[p])
    conservative || return cond
    widest(a, b) = [q <= 0.5 ? min(x, y) : max(x, y) for (q, x, y) in zip(qs, a, b)]
    out = widest(cond, pooled)
    out = widest(out, widest(band(f.guard_residuals[p], f.guard_errors[p])...))
    lr = f.lead_residuals[p]
    h = f.leads[j]
    (h <= length(lr) && !isempty(lr[h])) && (out = widest(out, base .+ quantile(lr[h], qs)))
    return out
end

# Most recent realised value for the same trading period of day, known at origin τ.
function _last_same_period(price::Vector{Float64}, t::Int, τ::Int, ppd::Int)
    k = max(1, cld(t - τ, ppd))
    i = t - k * ppd
    return i >= 1 ? price[i] : price[max(1, min(τ, length(price)))]
end

function _profile(price::Vector{Float64}, t::Int, τ::Int, ppd::Int, ndays::Int = 7)
    k0 = max(1, cld(t - τ, ppd))
    s, c = 0.0, 0
    for k in k0:k0+ndays-1
        i = t - k * ppd
        i < 1 && break
        s += price[i]; c += 1
    end
    return c == 0 ? _last_same_period(price, t, τ, ppd) : s / c
end

"""
    pd_horizon_at(df, p, τ, cfg) -> Int

Pre-dispatch horizon available at origin `τ`: the lead of the last target row within
`pd_horizon_periods` that has a pre-dispatch value (0 if none, e.g. when the table ends at
the decision time). Training features, serving features and the residual model all use
this one cut-off, so the model is never served a pre-dispatch pattern it was not trained on.
"""
function pd_horizon_at(df::DataFrame, p::Symbol, τ::Int, cfg::Config)
    hasproperty(df, PD_COLS[p]) || return 0
    H = cfg.forecast.pd_horizon_periods
    # Pre-dispatch vintages from an issue-time column are only valid up to their minimum lead.
    "pd_max_lead" in metadatakeys(df) && (H = min(H, metadata(df, "pd_max_lead")))
    col = df[!, PD_COLS[p]]
    for t in min(nrow(df), τ + H):-1:(τ + 1)
        isnan(col[t]) || return t - τ
    end
    return 0
end

function _features(df::DataFrame, p::Symbol, t::Int, τ::Int, cfg::Config, hpd::Int)
    ppd = cfg.market.periods_per_day
    price = df[!, PRICE_COLS[p]]
    h = t - τ
    last_same = _last_same_period(price, t, τ, ppd)
    prof = _profile(price, t, τ, ppd)
    pdv, pd_ok = NaN, 0.0
    if h <= hpd
        pdv = df[t, PD_COLS[p]]
        pd_ok = isnan(pdv) ? 0.0 : 1.0
    end
    pd_or_prof = pd_ok == 1.0 ? pdv : prof
    decay = exp(-h / 6)
    ang = 2π * (df.period[t] - 1) / ppd
    wk = dayofweek(df.timestamp[t]) >= 6 ? 1.0 : 0.0
    return [1.0, pd_or_prof, pd_ok, pd_ok * pd_or_prof, last_same, prof,
            decay * price[τ], decay, sin(ang), cos(ang), sin(2ang), cos(2ang), wk]
end

function _seasonal_naive(df, p, targets, τ, cfg)
    price = df[!, PRICE_COLS[p]]
    [_last_same_period(price, t, τ, cfg.market.periods_per_day) for t in targets]
end

function _predispatch(df, p, targets, τ, cfg, hpd::Int)
    sn = _seasonal_naive(df, p, targets, τ, cfg)
    hpd > 0 || return sn
    out = copy(sn)
    for (j, t) in enumerate(targets)
        v = df[t, PD_COLS[p]]
        (t - τ <= hpd && !isnan(v)) && (out[j] = v)
    end
    return out
end

# Backtest residuals of a simple method over the training window (for scenario quantiles);
# the method fits nothing, so they are out of sample.
function _method_residuals(fn, df, p, τ, H, cfg; stride::Int = cfg.forecast.origin_stride_periods)
    fc = cfg.forecast
    lo = max(fc_min_origin(cfg), τ - fc.train_days * cfg.market.periods_per_day)
    res = Residuals()
    price = df[!, PRICE_COLS[p]]
    for o in lo:stride:(τ - 1)
        tg = (o + 1):min(o + H, τ)
        isempty(tg) && continue
        r = price[tg] .- fn(df, p, tg, o, cfg)
        ok = isfinite.(r)
        append!(res.r, r[ok])
        append!(res.period, df.period[tg][ok]); append!(res.lead, (tg .- o)[ok])
    end
    return res
end

"Earliest origin at which a forecast can be produced (one day of history)."
fc_min_origin(cfg::Config) = cfg.market.periods_per_day

"""
Leads (periods after the forecast origin) of the offers still open when a rolling-mode
offer is made: 1 .. lead_periods - 1, shifted by `info_lag_periods`. The SOC guard clears
those offers over the forecast band, so their errors are sampled from every origin.
"""
guard_leads(cfg::Config) = cfg.strategy.lead_periods - 1 + cfg.strategy.info_lag_periods

"Residuals split by lead 1..n (one vector per lead)."
_by_lead(res::Residuals, n::Int) = [res.r[res.lead .== h] for h in 1:n]

"""
    forecast_prices(df, τ, targets, cfg) -> PriceForecast

Forecast all four prices for rows `targets` using information available at row `τ`.
Gaps in realised prices up to `τ` are filled first (`sanitize_history`), and a forecast
that is not finite falls back to seasonal naive with a warning.
"""
function forecast_prices(df::DataFrame, τ::Int, targets::UnitRange{Int}, cfg::Config)
    τ >= fc_min_origin(cfg) || error("need at least one day of history before the first decision")
    first(targets) > τ || error("targets must lie after the decision origin")
    last(targets) <= nrow(df) || error("targets beyond the end of the table (use extend_rows)")
    df = sanitize_history(df, τ, cfg)
    H = length(targets) + (first(targets) - τ - 1)
    means = Dict{Symbol,Vector{Float64}}()
    resid = Dict{Symbol,Vector{Float64}}()
    errs = Dict{Symbol,Union{ErrorModel,Nothing}}()
    gresid = Dict{Symbol,Vector{Float64}}()
    gerrs = Dict{Symbol,Union{ErrorModel,Nothing}}()
    lres = Dict{Symbol,Vector{Vector{Float64}}}()
    prof = Dict{Symbol,Vector{Float64}}()
    nl = guard_leads(cfg)
    fc = cfg.forecast
    for p in PRODUCTS
        method = fc.method
        hpd = pd_horizon_at(df, p, τ, cfg)
        pdfn = (d, q, tg, o, c) -> _predispatch(d, q, tg, o, c, hpd)
        if method == "seasonal_naive"
            means[p] = _seasonal_naive(df, p, targets, τ, cfg)
            res = oos = _method_residuals(_seasonal_naive, df, p, τ, H, cfg)    # fits nothing
            lr = _method_residuals(_seasonal_naive, df, p, τ, nl, cfg; stride = 1)
        elseif method == "predispatch"
            means[p] = pdfn(df, p, targets, τ, cfg)
            res = oos = _method_residuals(pdfn, df, p, τ, H, cfg)
            lr = _method_residuals(pdfn, df, p, τ, nl, cfg; stride = 1)
        else
            means[p], res, oos, lr = _ridge(df, p, τ, targets, H, cfg, hpd, nl)
        end
        if !all(isfinite, means[p])
            @warn "non-finite $p forecast at origin row $τ; using seasonal naive" maxlog = 10
            means[p] = _seasonal_naive(df, p, targets, τ, cfg)
            res = oos = _method_residuals(_seasonal_naive, df, p, τ, H, cfg)
            lr = _method_residuals(_seasonal_naive, df, p, τ, nl, cfg; stride = 1)
        end
        errmodel(x) = fc.conditional_errors ? fit_error_model(x, cfg.market.periods_per_day, fc.error_period_window) : nothing
        resid[p], errs[p] = res.r, errmodel(res)
        gresid[p], gerrs[p] = oos === res ? (resid[p], errs[p]) : (oos.r, errmodel(oos))
        lres[p] = _by_lead(lr, nl)
        prof[p] = [_profile(df[!, PRICE_COLS[p]], t, τ, cfg.market.periods_per_day) for t in targets]
        if p != :energy
            means[p] = max.(means[p], 0.0)   # reserve prices are non-negative
        end
        means[p] = clamp.(means[p], cfg.market.price_floor, cfg.market.price_cap)
    end
    return PriceForecast(targets, means, resid, errs, df.period[targets], collect(targets) .- τ,
                         gresid, gerrs, lres, prof)
end

"""
    _ridge(df, p, τ, targets, H, cfg, hpd, nl) -> (forecast, fit_residuals, oos_residuals, lead_residuals)

Ridge regression on features of (origin, target) pairs from the training window (origins
`origin_stride_periods` apart, labels known at τ). `fit_residuals` are the fit's own
(in-sample) errors. `oos_residuals` are out of sample: the window's origins are split into
`CV_FOLDS` contiguous blocks, and each block's errors come from a fit on the other blocks
without the origins whose label windows overlap the block's (purged; if that leaves fewer
rows than half the other blocks', only the block is left out). `lead_residuals` are such
errors at leads 1..nl from every origin of the window. In-sample errors understate the
error of a forecast: the SOC guard uses the out-of-sample ones.
"""
function _ridge(df, p, τ, targets, H, cfg, hpd::Int, nl::Int)
    fc = cfg.forecast
    price = df[!, PRICE_COLS[p]]
    lo = max(fc_min_origin(cfg), τ - fc.train_days * cfg.market.periods_per_day)
    rows = Vector{Vector{Float64}}()
    ys = Float64[]
    pers, leads, origs = Int[], Int[], Int[]
    for o in lo:fc.origin_stride_periods:(τ - 1)
        for t in (o + 1):min(o + H, τ)          # labels must be known at τ
            push!(rows, _features(df, p, t, o, cfg, hpd)); push!(ys, price[t])
            push!(pers, df.period[t]); push!(leads, t - o); push!(origs, o)
        end
    end
    if length(ys) < 50
        sn = _seasonal_naive(df, p, targets, τ, cfg)
        r = _method_residuals(_seasonal_naive, df, p, τ, H, cfg)
        return sn, r, r, _method_residuals(_seasonal_naive, df, p, τ, nl, cfg; stride = 1)
    end
    X = reduce(vcat, permutedims.(rows))
    # Standardise non-intercept columns for a well-conditioned ridge.
    μ = vec(mean(X; dims = 1)); σ = vec(std(X; dims = 1))
    μ[1] = 0.0; σ[1] = 1.0
    σ[σ .< 1e-9] .= 1.0
    Z = (X .- μ') ./ σ'
    Λ = fc.ridge_alpha * Matrix{Float64}(I, size(Z, 2), size(Z, 2)); Λ[1, 1] = 0.0
    fit(sel) = (Zs = Z[sel, :]; (Zs' * Zs + Λ) \ (Zs' * ys[sel]))
    β = fit(trues(length(ys)))
    Xt = reduce(vcat, permutedims.([_features(df, p, t, τ, cfg, hpd) for t in targets]))
    pred = ((Xt .- μ') ./ σ') * β

    # Out-of-sample errors by blocks of origins (time-contiguous, so a block is forecast by a
    # model that has not seen its labels).
    K = CV_FOLDS
    edges = [lo + round(Int, k * (τ - lo) / K) for k in 0:K]    # block k: origins in [edges[k], edges[k+1])
    resid = fill(NaN, length(ys))
    lr = Residuals()
    for k in 1:K
        a, z = edges[k], edges[k + 1] - 1
        a > z && continue
        held = (origs .>= a) .& (origs .<= z)
        purged = (origs .<= a - H) .| (origs .>= z + H)
        others = .!held
        train = count(purged) >= max(50, count(others) ÷ 2) ? purged : others
        count(train) >= 50 || (train = trues(length(ys)))   # too little history: in sample
        βk = fit(train)
        any(held) && (resid[held] = ys[held] .- Z[held, :] * βk)
        # every origin of the block at the open offers' leads
        for o in a:z, h in 1:nl
            t = o + h
            t <= τ || continue
            zt = (_features(df, p, t, o, cfg, hpd) .- μ) ./ σ
            push!(lr.r, price[t] - dot(zt, βk)); push!(lr.period, df.period[t]); push!(lr.lead, h)
        end
    end
    ok = isfinite.(resid)
    return pred, Residuals(ys .- Z * β, pers, leads), Residuals(resid[ok], pers[ok], leads[ok]), lr
end

"Blocks of training origins for the out-of-sample forecast errors (`_ridge`)."
const CV_FOLDS = 5
