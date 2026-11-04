using Test, DataFrames, Dates, Random
include(joinpath(@__DIR__, "..", "src", "BESSBidding.jl"))
using .BESSBidding
const B = BESSBidding

cfg = load_config(joinpath(@__DIR__, "..", "configs", "default.toml"))
df = synthetic_market_data(days = 10, seed = 3, cfg = cfg)

@testset "BESSBidding" begin

@testset "config" begin
    @test cfg.battery.power_mw == 100 && cfg.battery.energy_mwh == 100
    @test B.soc_min(cfg.battery) ≈ 5 && B.soc_max(cfg.battery) ≈ 95
    @test B.eta_one_way(cfg.battery)^2 ≈ 0.85
    @test cfg.strategy.lead_periods == 1
    c2 = with_strategy(cfg; lead_periods = 3)
    @test c2.strategy.lead_periods == 3 && c2.battery === cfg.battery
end

@testset "data" begin
    @test nrow(df) == 10 * 48
    @test df.period[1] == 1 && df.period[48] == 48 && df.period[49] == 1
    @test all(diff(df.timestamp) .== Minute(30))
    mktempdir() do d
        f = joinpath(d, "m.csv")
        # EMA-style layout: date + period columns and different names
        raw = DataFrame(DATE = Date.(df.timestamp[1:96]), PERIOD = df.period[1:96],
                        USEP = df.price_energy[1:96], REG = df.price_regulation[1:96],
                        PRI = df.price_primary[1:96], CON = df.price_contingency[1:96])
        raw.USEP[5] = NaN
        B.CSV.write(f, raw)
        dc = B.DataConfig(date_col = "DATE", period_col = "PERIOD",
                          columns = Dict("price_energy" => "USEP", "price_regulation" => "REG",
                                         "price_primary" => "PRI", "price_contingency" => "CON"))
        c3 = B.Config(cfg.battery, cfg.market, cfg.strategy, cfg.forecast, cfg.simulation, dc)
        d2 = load_market_data(f, c3)
        @test nrow(d2) == 96
        @test d2.timestamp == df.timestamp[1:96]
        @test d2.price_energy[5] == d2.price_energy[4]        # forward-filled
        @test all(isnan, d2.pd_energy)                        # optional columns absent
    end
end

@testset "forecast uses no future information" begin
    τ = 5 * 48 + 7
    f1 = forecast_prices(df, τ, (τ + 1):(τ + 96), cfg)
    df2 = copy(df)
    df2[(τ + 1):end, :price_energy] .= 9999.0                 # corrupt the future
    df2[(τ + 1):end, :price_regulation] .= 9999.0
    f2 = forecast_prices(df2, τ, (τ + 1):(τ + 96), cfg)
    @test f1.mean[:energy] == f2.mean[:energy]
    @test f1.mean[:regulation] == f2.mean[:regulation]
    @test all(>=(0), f1.mean[:contingency])
    for m in ("seasonal_naive", "predispatch")
        g1 = forecast_prices(df, τ, (τ + 1):(τ + 48), with_forecast(cfg; method = m))
        g2 = forecast_prices(df2, τ, (τ + 1):(τ + 48), with_forecast(cfg; method = m))
        @test g1.mean[:energy] == g2.mean[:energy]
    end
end

@testset "co-optimisation respects battery limits" begin
    prices = Dict(p => df[1:96, B.PRICE_COLS[p]] for p in PRODUCTS)
    cm = build_coopt(cfg, 50.0, prices)
    B.optimize!(cm.model)
    s = coopt_schedule(cm)
    @test all(s.soc_mwh .>= 5 - 1e-6) && all(s.soc_mwh .<= 95 + 1e-6)
    @test all(s.charge_mw .+ s.discharge_mw .<= 100 + 1e-6)
    net = s.discharge_mw .- s.charge_mw
    @test all(net .+ s.regulation_mw .+ s.primary_mw .+ s.contingency_mw .<= 100 + 1e-6)
    @test all(-net .+ s.regulation_mw .<= 100 + 1e-6)
    # Higher own price never reduces the first-period discharge (parametric monotonicity).
    xs = Float64[]
    for λ in (-100.0, 0.0, 100.0, 200.0, 500.0, 2000.0)
        set_price!(cm, :energy, 1, λ); push!(xs, solve!(cm).net_mw)
    end
    @test issorted(xs; lt = (a, b) -> a < b - 1e-6)
    # Price-maker (QP) variant solves and trades less aggressively.
    cq = build_coopt(with_strategy(cfg; price_impact = 2.0), 50.0, prices)
    B.optimize!(cq.model)
    sq = coopt_schedule(cq)
    @test sum(abs.(sq.discharge_mw .- sq.charge_mw)) <= sum(abs.(net)) + 1e-6
end

@testset "offer curves" begin
    o = offer_from_grid(:energy, [-4500.0, 50.0, 100.0, 200.0, 4500.0], [-80.0, -80.0, 0.0, 60.0, 90.0], 10)
    @test min_mw(o) == -80 && max_mw(o) == 90
    @test cleared_mw(o, -1000) == -80
    @test cleared_mw(o, 80) == 0.0        # step at midpoint 75
    @test cleared_mw(o, 4500) == 90
    @test issorted(o.prices)
    m = B.merge_tranches(o, 1)
    @test length(m.prices) == 1 && max_mw(m) ≈ 90
    c = B.clip_offer(o, -30.0, 40.0)
    @test min_mw(c) == -30 && max_mw(c) == 40
    # non-monotone raw responses are repaired
    o2 = offer_from_grid(:regulation, [0.0, 10.0, 20.0, 30.0], [0.0, 50.0, 40.0, 60.0], 5)
    @test max_mw(o2) == 60 && all(>(0), o2.sizes)
end

@testset "SOC guard makes every clearing feasible" begin
    rng = MersenneTwister(11)
    full(p) = p == :energy ? Offer(:energy, -100.0, [50.0, 150.0], [100.0, 100.0]) : Offer(p, 0.0, [5.0], [100.0])
    for soc in (5.0, 6.0, 20.0, 50.0, 80.0, 94.0, 95.0)
        pb = B.PeriodBids(1, DateTime(2026), Dict(p => full(p) for p in PRODUCTS),
                          Dict(:energy => 0.0, :regulation => 50.0, :primary => 0.0, :contingency => 0.0))
        g = guard_soc(pb, soc, soc, cfg)
        @test isempty(validate_bids(g, cfg))
        for _ in 1:200
            pr = Dict(:energy => 400 * randn(rng) + 100, :regulation => 30 * rand(rng),
                      :primary => 10 * rand(rng), :contingency => 60 * rand(rng))
            net, res = clear_period(g, pr, cfg)
            out = settle_period(net, res, soc, pr, cfg)
            @test !out.sustain_violation
            @test out.shortfall_mwh == 0
            @test 5 - 1e-6 <= out.soc_end <= 95 + 1e-6
        end
    end
end

@testset "clearing" begin
    pb = B.PeriodBids(1, DateTime(2026), Dict(
        :energy => Offer(:energy, -50.0, [100.0, 200.0], [50.0, 50.0]),
        :regulation => Offer(:regulation, 0.0, [10.0], [80.0]),
        :primary => Offer(:primary, 0.0, Float64[], Float64[]),
        :contingency => Offer(:contingency, 0.0, Float64[], Float64[])))
    net, res = clear_period(pb, Dict(:energy => 50.0, :regulation => 5.0, :primary => 0.0, :contingency => 0.0), cfg)
    @test net ≈ -50 && res[:regulation] ≈ 0
    net, res = clear_period(pb, Dict(:energy => 150.0, :regulation => 20.0, :primary => 0.0, :contingency => 0.0), cfg)
    @test net ≈ 0 && res[:regulation] ≈ 80
    # joint capability: discharging 50 MW leaves 50 MW for regulation
    net, res = clear_period(pb, Dict(:energy => 250.0, :regulation => 20.0, :primary => 0.0, :contingency => 0.0), cfg)
    @test net + res[:regulation] <= 100 + 1e-6
end

@testset "bid generation and export" begin
    τ = 3 * 48
    bl = generate_bids(df, τ, 50.0, cfg)
    @test length(bl) == 1 && bl[1].row == τ + 1
    @test isempty(validate_bids(bl[1], cfg))
    # works when the table ends at the decision time
    bl2 = generate_bids(df[1:τ, :], τ, 50.0, cfg)
    @test bl2[1].timestamp == df.timestamp[τ + 1]
    tab = offers_table(bl, cfg)
    @test Set(tab.product) ⊆ Set(["energy", "regulation", "primary", "contingency"])
    e = tab[tab.product .== "energy", :]
    @test all(e.mw_to .> e.mw_from)
    @test all((e.direction .== "charge") .== (e.mw_to .<= 0))
    da = generate_bids(df, τ - 10, 50.0, with_strategy(cfg; mode = "day_ahead"))
    @test length(da) == 48 && df.period[da[1].row] == 1
    @test all(isempty(validate_bids(pb, cfg)) for pb in da)
end

@testset "backtest" begin
    st = 7 * 48 + 1
    r = run_backtest(df, cfg; start_row = st, end_row = st + 47, progress = false)
    sm = Dict(r.summary.metric .=> r.summary.value)
    @test nrow(r.periods) == 48
    @test sm["sustain_violation_periods"] == 0
    @test sm["shortfall_mwh"] == 0
    @test sm["soc_min_seen_mwh"] >= 5 - 1e-6 && sm["soc_max_seen_mwh"] <= 95 + 1e-6
    @test sm["net_market_revenue"] <= sm["perfect_foresight_revenue"] + 1e-6
    @test sm["net_market_revenue"] > 0
    # SOC continuity
    @test all(r.periods.soc_start_mwh[2:end] .≈ r.periods.soc_end_mwh[1:end-1])
    mktempdir() do d
        write_backtest(r, d, cfg)
        @test isfile(joinpath(d, "periods.csv")) && isfile(joinpath(d, "bids.csv")) && isfile(joinpath(d, "summary.csv"))
    end
end

end # BESSBidding
