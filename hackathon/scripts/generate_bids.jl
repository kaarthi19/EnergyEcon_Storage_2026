# Generate offers from data available at a decision time and the current SOC.
#   julia --project=hackathon hackathon/scripts/generate_bids.jl --data <csv or dir> --soc <MWh>
#         [--origin <timestamp|row>] [--pending <bids.csv|dir>[,<bids.csv|dir>...]]
#         [--config file.toml] [--out bids.csv]
# --origin: decision time, default the last row of the data (bid for the future period).
# --soc: SOC (MWh) at the end of the origin period; required.
# --pending: offers already submitted (files written by this script or the backtest), used
#   for the periods between the origin and the bid period, which are still open at gate
#   closure. Without them those periods are covered by their worst case (safe, but the battery
#   earns almost nothing; strategy.require_pending = true makes it an error).
# rolling mode: offers for the period `lead_periods` after the origin.
# day_ahead mode: offers for the next full trading day after origin + lead.
# Only rows up to the origin (plus pre-dispatch within its horizon) are used. Exits with
# status 1 if an offer fails validation (the offers are still written, for inspection).
include("common.jl")

"""
    generate_bids_main(args; check = validate_bids) -> exit status

The command line above as a function (the test suite calls it): writes the offers and
returns 0, or 1 if `check` reports a problem for any offer.
"""
function generate_bids_main(args::AbstractVector{<:AbstractString}; check = validate_bids)
    a = parse_args(args)
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
        errs = check(pb, cfg)
        isempty(errs) || (@error "offer validation failed" pb.timestamp errs; bad = true)
    end
    out = get(a, "out", joinpath(HACKATHON_DIR, "results", "bids.csv"))
    mkpath(dirname(abspath(out)))
    write_bids(out, bl, cfg)
    println("wrote offers for $(length(bl)) trading period(s) to $out")
    return bad ? 1 : 0
end

if abspath(PROGRAM_FILE) == @__FILE__     # run as a script, not included
    exit(generate_bids_main(ARGS))
end
