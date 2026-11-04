# Generate offers from data available at a decision time and the current SOC.
#   julia --project=hackathon hackathon/scripts/generate_bids.jl --data <csv or dir> --origin <timestamp|row>
#         --soc <MWh> [--config file.toml] [--out bids.csv]
# rolling mode: offers for the period `lead_periods` after the origin.
# day_ahead mode: offers for the next full trading day after origin + lead.
# Only rows up to the origin (plus pre-dispatch within its horizon) are used.
include("common.jl")
a = parse_args(ARGS)
cfg = load_config(get(a, "config", default_config()))
haskey(a, "data") || error("--data is required")
df = load_market_data(a["data"], cfg)
τ = haskey(a, "origin") ? row_of(df, a["origin"]) : nrow(df) - cfg.strategy.lead_periods
soc = parse(Float64, get(a, "soc", string(cfg.battery.initial_soc_frac * cfg.battery.energy_mwh)))
bl = generate_bids(df, τ, soc, cfg)
for pb in bl
    errs = validate_bids(pb, cfg)
    isempty(errs) || @warn "offer validation" pb.timestamp errs
end
out = get(a, "out", joinpath(HACKATHON_DIR, "results", "bids.csv"))
mkpath(dirname(out))
write_bids(out, bl, cfg)
println("wrote offers for $(length(bl)) trading period(s) to $out")
