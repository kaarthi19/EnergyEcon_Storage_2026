# Writing results and the EMA-facing bid generation entry point.

"Write per-period results, offers and summary of a backtest to `outdir`."
function write_backtest(res::BacktestResult, outdir::AbstractString, cfg::Config)
    mkpath(outdir)
    CSV.write(joinpath(outdir, "periods.csv"), res.periods)
    CSV.write(joinpath(outdir, "bids.csv"), offers_table(res.bids, cfg))
    CSV.write(joinpath(outdir, "summary.csv"), res.summary)
    return outdir
end

write_bids(path::AbstractString, bl::Vector{PeriodBids}, cfg::Config) =
    CSV.write(path, offers_table(bl, cfg))

"""
    generate_bids(df, origin_row, soc_now, cfg; pending=PeriodBids[], rows=nothing) -> Vector{PeriodBids}

Entry point for running the algorithm inside an external simulator (e.g. EMA's).
Uses only data in rows 1:origin_row (plus pre-dispatch for later rows within the
pre-dispatch horizon) and the battery's SOC at the end of `origin_row`.

- rolling mode: returns the offer for trading period origin_row + lead_periods.
  `pending` are offers already submitted for the periods in between.
- day_ahead mode: returns offers for `rows` (default: the next full trading day).
"""
function generate_bids(df::DataFrame, origin_row::Int, soc_now::Float64, cfg::Config;
                       pending::Vector{PeriodBids} = PeriodBids[], rows = nothing)
    need = origin_row + cfg.strategy.lead_periods + max(cfg.strategy.horizon_periods, cfg.market.periods_per_day) - 1
    df = extend_rows(df, need, cfg)
    if cfg.strategy.mode == "rolling"
        return [rolling_bids(df, origin_row, soc_now, pending, cfg)]
    end
    if rows === nothing
        t = origin_row + cfg.strategy.lead_periods
        while t <= nrow(df) && df.period[t] != 1
            t += 1
        end
        rows = t:min(nrow(df), t + cfg.market.periods_per_day - 1)
    end
    return day_ahead_bids(df, origin_row, soc_now, rows, cfg)
end

"""
    extend_rows(df, n, cfg) -> DataFrame

Append empty future trading periods (NaN prices) so the table has at least `n` rows. Only
timestamps/periods of these rows are used; their prices are never read by the forecaster.
"""
function extend_rows(df::DataFrame, n::Int, cfg::Config)
    nrow(df) >= n && return df
    step = Minute(round(Int, cfg.market.period_hours * 60))
    extra = n - nrow(df)
    ts = [df.timestamp[end] + step * k for k in 1:extra]
    add = DataFrame(timestamp = ts, period = period_of_day.(ts, cfg.market.period_hours))
    for c in names(df)
        c in ("timestamp", "period") && continue
        add[!, c] = fill(NaN, extra)
    end
    return vcat(df, add[!, names(df)])
end
