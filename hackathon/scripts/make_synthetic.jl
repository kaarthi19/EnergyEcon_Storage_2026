# Write a synthetic SWEM-like dataset for development and testing.
#   julia --project=hackathon hackathon/scripts/make_synthetic.jl [--days 42] [--seed 1] [--out path.csv]
include("common.jl")
using CSV
a = parse_args(ARGS)
cfg = load_config(get(a, "config", default_config()))
df = synthetic_market_data(days = parse(Int, get(a, "days", "42")), seed = parse(Int, get(a, "seed", "1")), cfg = cfg)
out = get(a, "out", joinpath(HACKATHON_DIR, "data", "synthetic", "market.csv"))
mkpath(dirname(out))
CSV.write(out, df)
println("wrote $(nrow(df)) periods to $out")
