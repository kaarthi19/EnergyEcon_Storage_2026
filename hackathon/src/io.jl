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
    read_bids(paths; df=nothing) -> Vector{PeriodBids}

Read offers back from bids CSV files written by `write_bids` / `write_backtest` (one file,
several files, or a directory whose *.csv files are all read). Energy rows are RC383 pairs
(`from_storage_pairs`), reserve rows are the base (slot 0) and tranches; a product without
rows is an empty offer. With `df`, each offer's `row` is the row of its timestamp in `df`
(0 when the timestamp is not in the table); without it, `row` is 0. The planned quantities
are not in the file and read as 0. Sorted by timestamp; a timestamp that appears in several
files takes the offer from the file read last (paths in the order given, a directory's
files in name order), as an offer variation replaces the earlier offer.
"""
function read_bids(paths; df::Union{DataFrame,Nothing} = nothing)
    files = String[]
    for p in (paths isa AbstractString ? [paths] : paths)
        isdir(p) ? append!(files, sort(filter(f -> endswith(lowercase(f), ".csv"), readdir(p; join = true)))) :
                   push!(files, p)
    end
    isempty(files) && error("no bids CSV files in $(repr(paths))")
    byts = Dict{DateTime,PeriodBids}()
    for f in files
        tab = CSV.read(f, DataFrame; types = Dict(:timestamp => String, :product => String))
        for c in ("timestamp", "product", "slot", "price", "quantity_mw")
            c in names(tab) || error("$f is not a bids file: no $c column")
        end
        ts = DateTime.(tab.timestamp)
        for t in unique(ts)
            sub = tab[ts .== t, :]
            o = Dict{Symbol,Offer}()
            e = sub[sub.product .== "energy", :]
            o[:energy] = nrow(e) == 0 ? Offer(:energy, 0.0, Float64[], Float64[]) :
                from_storage_pairs([(slot = Int(r.slot), price = Float64(r.price), mw = Float64(r.quantity_mw))
                                    for r in eachrow(e)])
            for p in RESERVES
                r = sort(sub[sub.product .== String(p), :], :slot)
                base = sum(Float64.(r.quantity_mw[r.slot .== 0]); init = 0.0)
                tr = r[r.slot .> 0, :]
                o[p] = Offer(p, base, Float64.(tr.price), Float64.(tr.quantity_mw))
            end
            row = df === nothing ? 0 : something(findfirst(==(t), df.timestamp), 0)
            byts[t] = PeriodBids(row, t, o)
        end
    end
    return [byts[t] for t in sort(collect(keys(byts)))]
end

"""
    generate_bids(df, origin_row, soc_now, cfg; pending=PeriodBids[], rows=nothing) -> Vector{PeriodBids}

Entry point for running the algorithm inside an external simulator (e.g. EMA's).
Uses only data in rows 1:origin_row (plus pre-dispatch for later rows within the
pre-dispatch horizon) and the battery's SOC at the end of `origin_row`. The table may end
at `origin_row`; gaps in its realised prices are filled with a warning (`sanitize_history`).

`pending` are the offers already submitted (e.g. from `read_bids`), matched by timestamp to
the periods between the origin and the first bid period; others are ignored. Those offers
are still open at the decision time, so the SOC at the start of the bid period is uncertain.
A period whose offer is not supplied is covered by the worst case over every offer the
battery could have made, with a warning: never less safe, but with lead_periods > 1 the
battery then earns almost nothing. `strategy.require_pending = true` makes it an error.

- rolling mode: returns the offer for trading period origin_row + lead_periods.
- day_ahead mode: returns offers for `rows` (default: the next full trading day).
"""
function generate_bids(df::DataFrame, origin_row::Int, soc_now::Float64, cfg::Config;
                       pending::AbstractVector = PeriodBids[], rows = nothing)
    df = sanitize_history(df, origin_row, cfg)   # NaN/missing history must not reach the forecaster
    s, ppd = cfg.strategy, cfg.market.periods_per_day
    first_bid = origin_row + s.lead_periods
    # Extend a short table (empty future rows) as far as the full look-ahead window from the
    # first bid row, as run_backtest sees it on a complete table.
    if s.mode == "rolling"
        df = extend_rows(df, first_bid + s.horizon_periods - 1, cfg)
    else
        if rows === nothing
            df = extend_rows(df, first_bid + ppd - 1, cfg)   # the next day boundary is in here
            t = first_bid
            while df.period[t] != 1
                t += 1
            end
            rows = t:(t + ppd - 1)
        end
        df = extend_rows(df, max(last(rows), first(rows) + s.horizon_periods - 1), cfg)
        first_bid = first(rows)
    end
    opened = _open_offers(pending, df, (origin_row + 1):(first_bid - 1), cfg)
    s.mode == "rolling" && return [rolling_bids(df, origin_row, soc_now, opened, cfg)]
    return day_ahead_bids(df, origin_row, soc_now, opened, rows, cfg)
end

# Offers in `pending` for each row of `open_rows` (by timestamp; later entries win), `nothing`
# for a row without one.
function _open_offers(pending::AbstractVector, df::DataFrame, open_rows::UnitRange{Int}, cfg::Config)
    byts = Dict{DateTime,PeriodBids}(pb.timestamp => pb for pb in pending)
    out = Union{PeriodBids,Nothing}[]
    for k in open_rows
        pb = get(byts, df.timestamp[k], nothing)
        push!(out, pb === nothing ? nothing : PeriodBids(k, pb.timestamp, pb.offers, pb.planned))
    end
    miss = [df.timestamp[k] for (k, pb) in zip(open_rows, out) if pb === nothing]
    isempty(miss) && return out
    msg = "generate_bids: no offer supplied for the open period(s) $(join(miss, ", ")). Pass the offers " *
          "already submitted for them as `pending` (e.g. pending = read_bids(\"<bids CSV files or directory>\"), " *
          "or --pending on the command line); every offer submitted earlier may be passed, only these " *
          "timestamps are used."
    cfg.strategy.require_pending && error(msg * " (strategy.require_pending = true)")
    @warn msg * " Without them each is covered by the worst case over every offer the battery could " *
          "have made: the bids are safe but earn almost nothing." maxlog = 1
    return out
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
    out = vcat(df, add[!, names(df)])
    for k in metadatakeys(df)        # e.g. pd_max_lead from the loader
        metadata!(out, k, metadata(df, k); style = :note)
    end
    return out
end
