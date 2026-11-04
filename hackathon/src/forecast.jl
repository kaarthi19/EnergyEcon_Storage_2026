# Price forecasting with a strict information set.
#
# At decision origin `τ` (row index) the algorithm may use realised prices in rows 1:τ and
# pre-dispatch values for target rows t with t - τ <= pd_horizon_periods. Nothing after the
# origin is read, so the forecaster respects EMA's data cut-off by construction.

struct PriceForecast
    targets::UnitRange{Int}
    mean::Dict{Symbol,Vector{Float64}}        # point forecast per product over `targets`
    residuals::Dict{Symbol,Vector{Float64}}   # empirical residual sample (actual - forecast)
end

"Quantiles of the forecast distribution for product `p` at target position `j`."
function forecast_quantiles(f::PriceForecast, p::Symbol, j::Int, qs::AbstractVector{<:Real})
    r = f.residuals[p]
    base = f.mean[p][j]
    isempty(r) && return fill(base, length(qs))
    return base .+ quantile(r, qs)
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

function _features(df::DataFrame, p::Symbol, t::Int, τ::Int, cfg::Config)
    ppd = cfg.market.periods_per_day
    price = df[!, PRICE_COLS[p]]
    h = t - τ
    last_same = _last_same_period(price, t, τ, ppd)
    prof = _profile(price, t, τ, ppd)
    pdv, pd_ok = NaN, 0.0
    if hasproperty(df, PD_COLS[p]) && h <= cfg.forecast.pd_horizon_periods
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

function _predispatch(df, p, targets, τ, cfg)
    sn = _seasonal_naive(df, p, targets, τ, cfg)
    hasproperty(df, PD_COLS[p]) || return sn
    out = copy(sn)
    for (j, t) in enumerate(targets)
        v = df[t, PD_COLS[p]]
        (t - τ <= cfg.forecast.pd_horizon_periods && !isnan(v)) && (out[j] = v)
    end
    return out
end

# Backtest residuals of a simple method over the training window (for scenario quantiles).
function _method_residuals(fn, df, p, τ, H, cfg)
    fc = cfg.forecast
    lo = max(fc_min_origin(cfg), τ - fc.train_days * cfg.market.periods_per_day)
    res = Float64[]
    price = df[!, PRICE_COLS[p]]
    for o in lo:fc.origin_stride_periods:(τ - 1)
        tg = (o + 1):min(o + H, τ)
        isempty(tg) && continue
        append!(res, price[tg] .- fn(df, p, tg, o, cfg))
    end
    return res
end

"Earliest origin at which a forecast can be produced (one day of history)."
fc_min_origin(cfg::Config) = cfg.market.periods_per_day

"""
    forecast_prices(df, τ, targets, cfg) -> PriceForecast

Forecast all four prices for rows `targets` using information available at row `τ`.
"""
function forecast_prices(df::DataFrame, τ::Int, targets::UnitRange{Int}, cfg::Config)
    τ >= fc_min_origin(cfg) || error("need at least one day of history before the first decision")
    first(targets) > τ || error("targets must lie after the decision origin")
    H = length(targets) + (first(targets) - τ - 1)
    means = Dict{Symbol,Vector{Float64}}()
    resid = Dict{Symbol,Vector{Float64}}()
    for p in PRODUCTS
        method = cfg.forecast.method
        if method == "seasonal_naive"
            means[p] = _seasonal_naive(df, p, targets, τ, cfg)
            resid[p] = _method_residuals(_seasonal_naive, df, p, τ, H, cfg)
        elseif method == "predispatch"
            means[p] = _predispatch(df, p, targets, τ, cfg)
            resid[p] = _method_residuals(_predispatch, df, p, τ, H, cfg)
        else
            means[p], resid[p] = _ridge(df, p, τ, targets, H, cfg)
        end
        if p != :energy
            means[p] = max.(means[p], 0.0)   # reserve prices are non-negative
        end
        means[p] = clamp.(means[p], cfg.market.price_floor, cfg.market.price_cap)
    end
    return PriceForecast(targets, means, resid)
end

function _ridge(df, p, τ, targets, H, cfg)
    fc = cfg.forecast
    price = df[!, PRICE_COLS[p]]
    lo = max(fc_min_origin(cfg), τ - fc.train_days * cfg.market.periods_per_day)
    rows = Vector{Vector{Float64}}()
    ys = Float64[]
    for o in lo:fc.origin_stride_periods:(τ - 1)
        for t in (o + 1):min(o + H, τ)          # labels must be known at τ
            push!(rows, _features(df, p, t, o, cfg)); push!(ys, price[t])
        end
    end
    if length(ys) < 50
        return _seasonal_naive(df, p, targets, τ, cfg), _method_residuals(_seasonal_naive, df, p, τ, H, cfg)
    end
    X = reduce(vcat, permutedims.(rows))
    # Standardise non-intercept columns for a well-conditioned ridge.
    μ = vec(mean(X; dims = 1)); σ = vec(std(X; dims = 1))
    μ[1] = 0.0; σ[1] = 1.0
    σ[σ .< 1e-9] .= 1.0
    Z = (X .- μ') ./ σ'
    Λ = fc.ridge_alpha * Matrix{Float64}(I, size(Z, 2), size(Z, 2)); Λ[1, 1] = 0.0
    β = (Z' * Z + Λ) \ (Z' * ys)
    resid = ys .- Z * β
    Xt = reduce(vcat, permutedims.([_features(df, p, t, τ, cfg) for t in targets]))
    pred = ((Xt .- μ') ./ σ') * β
    return pred, resid
end
