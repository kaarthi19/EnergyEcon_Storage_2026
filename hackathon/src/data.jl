# Market data loading into one canonical half-hourly table.
#
# Canonical columns (prices in $/MWh, reserve prices in $/MW/h):
#   timestamp  DateTime  start of the trading period
#   period     Int       trading period of the day (1..periods_per_day)
#   price_energy, price_regulation, price_primary, price_contingency   realised prices
#   pd_energy, pd_regulation, pd_primary, pd_contingency, pd_demand      pre-dispatch (optional)
#
# EMA's file layout is not known before 13 Nov 2026; the mapping lives in [data] of the config.

const PRICE_COLS = Dict(p => Symbol("price_", p) for p in PRODUCTS)
const PD_COLS = Dict(p => Symbol("pd_", p) for p in PRODUCTS)
const CANONICAL = vcat([Symbol("price_", p) for p in PRODUCTS], [Symbol("pd_", p) for p in PRODUCTS], [:pd_demand])

has_predispatch(df::DataFrame, p::Symbol) = hasproperty(df, PD_COLS[p]) && !all(isnan, df[!, PD_COLS[p]])

function _read_one(path::AbstractString, dc::DataConfig, m::MarketConfig)
    raw = CSV.read(path, DataFrame; stringtype = String)
    out = DataFrame()
    if !isempty(dc.date_col) && !isempty(dc.period_col)
        dates = raw[!, dc.date_col]
        dates = eltype(dates) <: Date ? dates : Date.(string.(dates))
        per = Int.(raw[!, dc.period_col])
        mins = round(Int, m.period_hours * 60)
        out.timestamp = DateTime.(dates) .+ Minute.((per .- 1) .* mins)
    else
        ts = raw[!, dc.timestamp_col]
        out.timestamp = eltype(ts) <: DateTime ? ts : DateTime.(string.(ts), DateFormat(dc.timestamp_format))
    end
    for c in CANONICAL
        src = get(dc.columns, String(c), String(c))
        if src in names(raw)
            out[!, c] = Float64.(coalesce.(raw[!, src], NaN))
        end
    end
    return out
end

"""
    load_market_data(path, cfg) -> DataFrame

`path` is a CSV file or a directory of CSV files (joined on timestamp). Returns the
canonical table sorted by time, with a regular half-hourly index. Gaps in realised
prices are forward-filled and reported; missing pre-dispatch columns are left as NaN.
"""
function load_market_data(path::AbstractString, cfg::Config)
    files = isdir(path) ? sort(filter(f -> endswith(lowercase(f), ".csv"), readdir(path; join = true))) : [path]
    isempty(files) && error("no CSV files found at $path")
    df = _read_one(files[1], cfg.data, cfg.market)
    for f in files[2:end]
        other = _read_one(f, cfg.data, cfg.market)
        newcols = setdiff(names(other), names(df))
        df = outerjoin(df, other[!, vcat("timestamp", newcols)]; on = :timestamp)
    end
    return canonicalize(df, cfg)
end

"Sort, regularise to the trading-period grid, add `period`, fill gaps."
function canonicalize(df::DataFrame, cfg::Config)
    m = cfg.market
    sort!(df, :timestamp)
    unique!(df, :timestamp)
    step = Minute(round(Int, m.period_hours * 60))
    grid = DataFrame(timestamp = collect(df.timestamp[1]:step:df.timestamp[end]))
    full = leftjoin(grid, df; on = :timestamp)
    sort!(full, :timestamp)
    for c in CANONICAL
        if !hasproperty(full, c)
            full[!, c] = fill(NaN, nrow(full))
        else
            full[!, c] = Float64.(coalesce.(full[!, c], NaN))
        end
    end
    for p in PRODUCTS
        col = full[!, PRICE_COLS[p]]
        all(isnan, col) && error("realised price column for $p is missing; check [data.columns]")
        nmiss = count(isnan, col)
        nmiss > 0 && @warn "forward-filling $nmiss missing $(PRICE_COLS[p]) values"
        full[!, PRICE_COLS[p]] = _ffill(col)
    end
    full.period = period_of_day.(full.timestamp, m.period_hours)
    return full[!, vcat([:timestamp, :period], CANONICAL)]
end

period_of_day(ts::DateTime, period_hours::Float64) =
    (hour(ts) * 60 + minute(ts)) ÷ round(Int, period_hours * 60) + 1

function _ffill(v::Vector{Float64})
    out = copy(v)
    first_ok = findfirst(!isnan, out)
    first_ok === nothing && return out
    out[1:first_ok-1] .= out[first_ok]
    for i in first_ok+1:length(out)
        isnan(out[i]) && (out[i] = out[i-1])
    end
    return out
end
