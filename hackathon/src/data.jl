# Market data loading into one canonical half-hourly table.
#
# Canonical columns (prices in $/MWh, reserve prices in $/MW/h):
#   timestamp  DateTime  start of the trading period
#   period     Int       trading period of the day (1..periods_per_day)
#   price_energy, price_regulation, price_primary, price_contingency   realised prices
#   pd_energy, pd_regulation, pd_primary, pd_contingency, pd_demand      pre-dispatch (optional)
#
# EMA's file layout is not known before 13 Nov 2026; the mapping lives in [data] of the config.
# ASSUMPTION: one pre-dispatch value per row, issued early enough to be known at every
# decision that uses it. Several runs per period need a pd_issue_time column (see
# `_latest_pd_runs`); otherwise rows that repeat a timestamp with different values are an error.

const PRICE_COLS = Dict(p => Symbol("price_", p) for p in PRODUCTS)
const PD_COLS = Dict(p => Symbol("pd_", p) for p in PRODUCTS)
const CANONICAL = vcat([Symbol("price_", p) for p in PRODUCTS], [Symbol("pd_", p) for p in PRODUCTS], [:pd_demand])

has_predispatch(df::DataFrame, p::Symbol) = hasproperty(df, PD_COLS[p]) && !all(isnan, df[!, PD_COLS[p]])

# Error that names the file, the column and the first bad value.
_bad(path, col, row, v, what) =
    error("$(basename(path)): column \"$col\", data row $row: cannot read $(repr(v)) as $what")

"Numbers from a CSV column: numeric as is; text with `thousands_separator` removed; missing -> NaN."
function _parse_numbers(col::AbstractVector, path, name, dc::DataConfig)
    out = Vector{Float64}(undef, length(col))
    for (i, v) in enumerate(col)
        if v === missing
            out[i] = NaN
        elseif v isa Real
            out[i] = Float64(v)
        else
            s = strip(String(v))
            isempty(dc.thousands_separator) || (s = replace(s, dc.thousands_separator => ""))
            x = s in dc.missing_strings ? NaN : tryparse(Float64, s)
            x === nothing && _bad(path, name, i, v, "a number")
            out[i] = x
        end
    end
    return out
end

function _parse_times(col::AbstractVector, path, name, fmt::AbstractString, T::Type)
    df_ = DateFormat(fmt)
    out = Vector{T}(undef, length(col))
    for (i, v) in enumerate(col)
        v === missing && _bad(path, name, i, v, "$T (format \"$fmt\")")
        if v isa T
            out[i] = v
        else
            x = tryparse(T, strip(string(v)), df_)
            x === nothing && _bad(path, name, i, v, "$T (format \"$fmt\")")
            out[i] = x
        end
    end
    return out
end

function _column(raw::DataFrame, path, name)
    name in names(raw) || error("$(basename(path)): column \"$name\" not found; columns are $(join(names(raw), ", "))")
    return raw[!, name]
end

# Columns parsed by this loader with the configured formats, never guessed by CSV.jl.
_text_cols(dc::DataConfig) = filter(!isempty, [dc.timestamp_col, dc.date_col, dc.period_col,
                                               get(dc.columns, "pd_issue_time", "")])

function _read_one(path::AbstractString, dc::DataConfig, m::MarketConfig)
    raw = CSV.read(path, DataFrame; stringtype = String, missingstring = dc.missing_strings,
                   types = Dict(c => String for c in _text_cols(dc)), validate = false)
    out = DataFrame()
    step = Minute(round(Int, m.period_hours * 60))
    if !isempty(dc.date_col)
        dates = _parse_times(_column(raw, path, dc.date_col), path, dc.date_col, dc.date_format, Date)
        per = _parse_numbers(_column(raw, path, dc.period_col), path, dc.period_col, dc)
        for (i, x) in enumerate(per)
            (isinteger(x) && 1 <= x <= m.periods_per_day) ||
                _bad(path, dc.period_col, i, raw[i, dc.period_col], "a trading period in 1..$(m.periods_per_day)")
        end
        out.timestamp = DateTime.(dates) .+ step .* (Int.(per) .- 1)
    else
        out.timestamp = _parse_times(_column(raw, path, dc.timestamp_col), path, dc.timestamp_col, dc.timestamp_format, DateTime)
        dc.timestamp_is_period_end && (out.timestamp .-= step)
    end
    for c in CANONICAL
        src = get(dc.columns, String(c), String(c))
        src in names(raw) && (out[!, c] = _parse_numbers(raw[!, src], path, src, dc))
    end
    iss = get(dc.columns, "pd_issue_time", "")
    if !isempty(iss)
        out.pd_issue_time = _parse_times(_column(raw, path, iss), path, iss, dc.timestamp_format, DateTime)
    end
    out[!, :_file] .= basename(path)
    return out
end

"""
    load_market_data(path, cfg) -> DataFrame

`path` is a CSV file or a directory of CSV files. The files are stacked into one table
(same columns: e.g. one file per month; different columns: joined on time) and rows that
share a timestamp are merged, which is an error if they disagree. Returns the canonical
table sorted by time, with a regular half-hourly index. Realised-price gaps are
forward-filled and reported (error above `data.max_missing_share`); missing pre-dispatch
columns are left as NaN.
"""
function load_market_data(path::AbstractString, cfg::Config)
    files = isdir(path) ? sort(filter(f -> endswith(lowercase(f), ".csv"), readdir(path; join = true))) : [path]
    isempty(files) && error("no CSV files found at $path")
    df = vcat([_read_one(f, cfg.data, cfg.market) for f in files]...; cols = :union)
    hasproperty(df, :pd_issue_time) && (df = _latest_pd_runs(df, cfg))
    return canonicalize(df, cfg)
end

"Minimum lead (periods) of the pre-dispatch runs kept by `_latest_pd_runs`."
pd_min_lead(cfg::Config) = cfg.data.pd_min_lead_periods > 0 ? cfg.data.pd_min_lead_periods : cfg.strategy.lead_periods

# Several pre-dispatch runs per period, with issue times: keep for each period the latest
# run issued by the decision time of its offer (the end of period t - L). Earlier decisions
# (longer leads) would need older vintages, which one value per row cannot hold, so the
# table is marked and the forecaster uses pre-dispatch only up to lead L.
function _latest_pd_runs(df::DataFrame, cfg::Config)
    L = pd_min_lead(cfg)
    step = Minute(round(Int, cfg.market.period_hours * 60))
    cutoff = df.timestamp .+ step .- step * L
    pdc = [c for c in names(df) if startswith(c, "pd_") && c != "pd_issue_time"]
    keep = df.pd_issue_time .<= cutoff
    late = df[.!keep, :]
    # Realised prices on dropped rows still count (they are checked against the kept rows).
    df = df[keep, :]
    for c in pdc
        late[!, c] .= NaN
    end
    sort!(df, [:timestamp, order(:pd_issue_time; rev = true)])
    df = df[.!nonunique(df, :timestamp), :]
    @warn "pre-dispatch vintages: kept the latest run issued at least $L period(s) before each period ends; " *
          "pre-dispatch is used only up to lead $L (finer vintage handling is not implemented)" maxlog = 1
    df = vcat(df, late; cols = :union)
    select!(df, Not(:pd_issue_time))
    metadata!(df, "pd_max_lead", L; style = :note)
    return df
end

"""
    canonicalize(df, cfg) -> DataFrame

Sort, merge rows that share a timestamp (error if they disagree), regularise to the
trading-period grid, add `period`, drop leading rows without realised prices and
forward-fill the remaining gaps.
"""
function canonicalize(df::DataFrame, cfg::Config)
    m = cfg.market
    md = "pd_max_lead" in metadatakeys(df) ? metadata(df, "pd_max_lead") : nothing
    df = _merge_duplicates(df)
    nrow(df) > 0 || error("no data rows")
    step = Minute(round(Int, m.period_hours * 60))
    all(t -> mod(Dates.value(t - df.timestamp[1]), Dates.value(Millisecond(step))) == 0, df.timestamp) ||
        error("timestamps are not on the $(Dates.value(step))-minute trading-period grid " *
              "(check data.timestamp_format / timestamp_is_period_end)")
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
        all(isnan, full[!, PRICE_COLS[p]]) && error("realised price column for $p is missing; check [data.columns]")
    end
    # Rows before every realised price is known are dropped, not back-filled from later values.
    first_ok = maximum(findfirst(!isnan, full[!, PRICE_COLS[p]]) for p in PRODUCTS)
    if first_ok > 1
        @warn "dropping $(first_ok - 1) leading rows without all realised prices (data starts $(full.timestamp[first_ok]))"
        full = full[first_ok:end, :]
    end
    for p in PRODUCTS
        col = full[!, PRICE_COLS[p]]
        nmiss = count(isnan, col)
        share = nmiss / length(col)
        share > cfg.data.max_missing_share &&
            error("$(round(100share; digits = 1))% of $(PRICE_COLS[p]) is missing after loading (limit " *
                  "$(100cfg.data.max_missing_share)%, data.max_missing_share); check the files and [data] settings")
        if nmiss > 0
            gap = _longest_nan_run(col)
            @warn "forward-filling $nmiss missing $(PRICE_COLS[p]) values (longest gap $gap periods)"
        end
        full[!, PRICE_COLS[p]] = _ffill(col)
    end
    full.period = period_of_day.(full.timestamp, m.period_hours)
    out = full[!, vcat([:timestamp, :period], CANONICAL)]
    md === nothing || metadata!(out, "pd_max_lead", md; style = :note)
    return out
end

# Merge rows that share a timestamp: each column takes the row values that are present,
# and differing values are an error (a silent pick would load a revised or wrong price).
function _merge_duplicates(df::DataFrame)
    df = sort(df, :timestamp)
    cols = [c for c in CANONICAL if hasproperty(df, c)]
    for c in cols
        df[!, c] = Float64.(coalesce.(df[!, c], NaN))
    end
    dup = nonunique(df, :timestamp)
    any(dup) || return select(df, vcat([:timestamp], cols))
    ts = df.timestamp
    keep = trues(nrow(df))
    i = 1
    while i <= nrow(df)
        j = i
        while j < nrow(df) && ts[j + 1] == ts[i]
            j += 1
        end
        for c in cols, k in i+1:j
            a, v = df[i, c], df[k, c]
            isnan(v) && continue
            if isnan(a)
                df[i, c] = v
            elseif !isapprox(a, v; rtol = 1e-9, atol = 1e-9)
                src = hasproperty(df, :_file) ? " (files $(df._file[i]), $(df._file[k]))" : ""
                error("conflicting rows for $(ts[i]): $c = $a and $v$src. Remove the duplicate, " *
                      "or map a pd_issue_time column for several pre-dispatch runs")
            end
        end
        keep[i+1:j] .= false
        i = j + 1
    end
    return select(df[keep, :], vcat([:timestamp], cols))
end

function _longest_nan_run(v::Vector{Float64})
    best, cur = 0, 0
    for x in v
        cur = isnan(x) ? cur + 1 : 0
        best = max(best, cur)
    end
    return best
end

period_of_day(ts::DateTime, period_hours::Float64) =
    (hour(ts) * 60 + minute(ts)) ÷ round(Int, period_hours * 60) + 1

"Forward-fill NaNs. Leading NaNs stay NaN (callers drop or report them)."
function _ffill(v::AbstractVector{Float64})
    out = copy(v)
    first_ok = findfirst(!isnan, out)
    first_ok === nothing && return out
    for i in first_ok+1:length(out)
        isnan(out[i]) && (out[i] = out[i-1])
    end
    return out
end

"""
    sanitize_history(df, τ, cfg) -> DataFrame

Make a caller's table safe to forecast from at origin `τ` (used by `generate_bids` and
`forecast_prices`, which may get a table that did not come through `load_market_data`):
`missing` and integer columns become Float64 with NaN, and NaN realised prices in rows
1:τ are forward-filled with a warning (leading ones take the first observed value; they
are training history only). Errors if a realised price column has no value up to `τ`.
Rows after `τ` are not touched. Returns `df` itself when nothing needs fixing.
"""
function sanitize_history(df::DataFrame, τ::Int, cfg::Config)
    1 <= τ <= nrow(df) || error("origin row $τ outside the table (1..$(nrow(df)))")
    for p in PRODUCTS
        hasproperty(df, PRICE_COLS[p]) || error("table has no $(PRICE_COLS[p]) column")
    end
    cols = [c for c in CANONICAL if hasproperty(df, c)]
    dirty(c) = eltype(df[!, c]) !== Float64
    gaps(p) = any(isnan, view(df[!, PRICE_COLS[p]], 1:τ))
    any(dirty, cols) || any(p -> gaps(p), PRODUCTS) || return df
    df = copy(df)
    for c in cols
        dirty(c) || continue
        v = df[!, c]
        bad = findfirst(x -> !(x === missing || x isa Real), v)
        bad === nothing || error("column $c, row $bad: $(repr(v[bad])) is not a number")
        df[!, c] = Float64[x === missing ? NaN : Float64(x) for x in v]
    end
    for p in PRODUCTS
        col = df[!, PRICE_COLS[p]]
        h = col[1:τ]
        n = count(isnan, h)
        n == 0 && continue
        n == τ && error("no realised $(PRICE_COLS[p]) at or before origin row $τ")
        @warn "filling $n missing $(PRICE_COLS[p]) values at or before origin row $τ" maxlog = 10
        f = _ffill(h)
        k = findfirst(!isnan, f)
        f[1:k-1] .= f[k]
        col = copy(col)
        col[1:τ] = f
        df[!, PRICE_COLS[p]] = col
    end
    return df
end
