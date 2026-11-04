# Closed-loop backtest on historical (or synthetic) data.
#   julia --project=hackathon hackathon/scripts/backtest.jl --data <csv or dir> [--config file.toml]
#         [--start <timestamp|row>] [--end <timestamp|row>] [--out results/dir]
include("common.jl")
a = parse_args(ARGS)
cfg = load_config(get(a, "config", default_config()))
df = haskey(a, "data") ? load_market_data(a["data"], cfg) : synthetic_market_data(days = 21, cfg = cfg)
haskey(a, "data") || @warn "no --data given: using 21 days of synthetic data"
st = haskey(a, "start") ? row_of(df, a["start"]) : BESSBidding.default_start_row(cfg)
en = haskey(a, "end") ? row_of(df, a["end"]) : nrow(df)
@info "backtest" mode = cfg.strategy.mode forecast = cfg.forecast.method from = df.timestamp[st] to = df.timestamp[en]
res = run_backtest(df, cfg; start_row = st, end_row = en)
out = get(a, "out", joinpath(HACKATHON_DIR, "results", "backtest"))
write_backtest(res, out, cfg)
show(res.summary; allrows = true); println()
println("results written to $out")
