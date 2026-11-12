# Generate offers from data available at a decision time and the current SOC.
#   julia --project=hackathon hackathon/scripts/generate_bids.jl --data <csv or dir> --soc <MWh>
#         [--origin <timestamp|row>] [--pending <bids.csv|dir>[,<bids.csv|dir>...]]
#         [--config file.toml] [--out bids.csv]
# --origin: decision time, default the last row of the data (bid for the future period).
# --soc: SOC (MWh) at the end of the origin period; required.
# --pending: offers already submitted (files written by this script or the backtest), used
#   for the periods between the origin and the bid period, which are still open at gate
#   closure. Without them those periods are covered by their worst case (conservative bids).
# rolling mode: offers for the period `lead_periods` after the origin.
# day_ahead mode: offers for the next full trading day after origin + lead.
# Only rows up to the origin (plus pre-dispatch within its horizon) are used. Exits with
# status 1 if an offer fails validation.
include("common.jl")
a = parse_args(ARGS)
cfg = load_config(get(a, "config", default_config()))
haskey(a, "data") || error("--data is required")
haskey(a, "soc") && a["soc"] != "true" || error("--soc <MWh> is required: the SOC at the end of the origin period")
df = load_market_data(a["data"], cfg)
τ = haskey(a, "origin") ? row_of(df, a["origin"]) : nrow(df)
soc = parse(Float64, a["soc"])
pending = haskey(a, "pending") ? read_bids(String.(split(a["pending"], ",")); df = df) : PeriodBids[]
bl = generate_bids(df, τ, soc, cfg; pending = pending)
bad = false
for pb in bl
    errs = validate_bids(pb, cfg)
    isempty(errs) || (@error "offer validation failed" pb.timestamp errs; global bad = true)
end
out = get(a, "out", joinpath(HACKATHON_DIR, "results", "bids.csv"))
mkpath(dirname(abspath(out)))
write_bids(out, bl, cfg)
println("wrote offers for $(length(bl)) trading period(s) to $out")
bad && exit(1)
