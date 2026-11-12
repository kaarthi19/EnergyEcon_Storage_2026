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
    @test cfg.strategy.lead_periods == 3          # 65-min gate closure
    @test cfg.market.reserve_headroom == "per_class"
    @test cfg.market.max_charge_pairs == 5 && cfg.market.max_discharge_pairs == 5
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
    @test all(net .+ s.regulation_mw .+ max.(s.primary_mw, s.contingency_mw) .<= 100 + 1e-6)
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

@testset "RC383 energy storage offers" begin
    # Worked example of RC383 §3.1.2: 3 charging and 2 discharging pairs.
    pairs = [(slot = 1, price = -200.0, mw = -1.0), (slot = 2, price = 0.0, mw = -1.0),
             (slot = 3, price = 200.0, mw = -1.0), (slot = 6, price = 400.0, mw = 1.0),
             (slot = 7, price = 600.0, mw = 1.0)]
    o = from_storage_pairs(pairs)
    for (λ, want) in ((-300, -3), (-100, -2), (100, -1), (300, 0), (500, 1), (800, 2))
        @test cleared_mw(o, λ) ≈ want
    end
    # and back to identical pairs
    back = storage_pairs(o, cfg)
    @test [(x.slot, x.price, x.mw) for x in back] == [(x.slot, x.price, x.mw) for x in pairs]
    # A curve with many steps is reduced to <= 5 + 5 pairs, preserving its range.
    big = Offer(:energy, -100.0, collect(10.0:10.0:200.0), fill(10.0, 20))
    pb = B.PeriodBids(1, DateTime(2026), Dict(:energy => conform_storage_offer(big, cfg),
                      :regulation => Offer(:regulation, 0.0, Float64[], Float64[]),
                      :primary => Offer(:primary, 0.0, Float64[], Float64[]),
                      :contingency => Offer(:contingency, 0.0, Float64[], Float64[])))
    sp = storage_pairs(pb.offers[:energy], cfg)
    @test count(x -> x.slot <= 5, sp) <= 5 && count(x -> x.slot >= 6, sp) <= 5
    @test min_mw(pb.offers[:energy]) ≈ -100 && max_mw(pb.offers[:energy]) ≈ 100
    @test isempty(validate_bids(pb, cfg))
    # always-charging and always-discharging curves, and the empty offer (§5.2B.4)
    @test storage_pairs(Offer(:energy, -40.0, Float64[], Float64[]), cfg) == [(slot = 1, price = 4500.0, mw = -40.0)]
    @test storage_pairs(Offer(:energy, 30.0, Float64[], Float64[]), cfg) == [(slot = 6, price = -4500.0, mw = 30.0)]
    @test storage_pairs(Offer(:energy, 0.0, Float64[], Float64[]), cfg) == [(slot = 6, price = 0.0, mw = 0.0)]
    # a tranche crossing 0 MW: charging price strictly below discharging price
    sp2 = storage_pairs(Offer(:energy, -20.0, [150.0], [50.0]), cfg)
    @test [x.mw for x in sp2] == [-20.0, 30.0] && sp2[1].price < sp2[2].price
    # per-class headroom: primary and contingency share the same headroom (D.17.2.4A)
    @test raise_mw(Dict(:regulation => 10.0, :primary => 50.0, :contingency => 60.0), cfg.market) == 70
    cs = B.Config(cfg.battery, B.MarketConfig(reserve_headroom = "summed"), cfg.strategy, cfg.forecast, cfg.simulation, cfg.data)
    @test raise_mw(Dict(:regulation => 10.0, :primary => 50.0, :contingency => 60.0), cs.market) == 120
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
    # charging at 100 MW leaves up to 200 MW of raise capability (RC383 §3.2.1) if caps allow
    pb2 = B.PeriodBids(1, DateTime(2026), Dict(
        :energy => Offer(:energy, -100.0, Float64[], Float64[]),
        :regulation => Offer(:regulation, 0.0, Float64[], Float64[]),
        :primary => Offer(:primary, 0.0, Float64[], Float64[]),
        :contingency => Offer(:contingency, 0.0, [1.0], [180.0])))
    net, res = clear_period(pb2, Dict(:energy => 50.0, :regulation => 0.0, :primary => 0.0, :contingency => 20.0), cfg)
    @test net ≈ -100 && res[:contingency] ≈ 180
end

@testset "bid generation and export" begin
    τ = 3 * 48
    bl = generate_bids(df, τ, 50.0, cfg)
    @test length(bl) == 1 && bl[1].row == τ + cfg.strategy.lead_periods
    @test isempty(validate_bids(bl[1], cfg))
    # works when the table ends at the decision time
    bl2 = generate_bids(df[1:τ, :], τ, 50.0, cfg)
    @test bl2[1].timestamp == df.timestamp[τ + cfg.strategy.lead_periods]
    tab = offers_table(bl, cfg)
    @test Set(tab.product) ⊆ Set(["energy", "regulation", "primary", "contingency"])
    e = tab[tab.product .== "energy", :]
    @test all((e.direction .== "charge") .== (e.slot .<= 5))
    @test all(e.quantity_mw[e.slot .<= 5] .<= 0) && all(e.quantity_mw[e.slot .>= 6] .>= 0)
    @test issorted(e.price) && nrow(e) <= 10
    @test all(e.offered_capacity_mw .== 100)
    @test all(tab.facility_id .== cfg.market.facility_id) && all(tab.offer_type .== "variation")
    @test all(round.(tab.price; digits = 2) .== tab.price)
    da = generate_bids(df, τ - 10, 50.0, with_strategy(cfg; mode = "day_ahead"))
    @test length(da) == 48 && df.period[da[1].row] == 1
    @test all(isempty(validate_bids(pb, cfg)) for pb in da)
end

# ---- helpers for the rule / guard tests -------------------------------------------------
mkmarket(m; kw...) = (d = Dict{Symbol,Any}(f => getfield(m, f) for f in fieldnames(MarketConfig));
                      merge!(d, Dict{Symbol,Any}(kw)); MarketConfig(; d...))
mkcfg(c; market = c.market, strategy = c.strategy) = B.Config(c.battery, market, strategy, c.forecast, c.simulation, c.data)
noff(p) = Offer(p, 0.0, Float64[], Float64[])
# Independent check of the guard: max of a*x + h.r over the clearing polytope by LP, with the
# joint capability constraints clear_period applies.
function lpmax(a, h, xl, xu, R, c)
    Pc = transfer_cap(c)
    md = B.Model(B._highs())
    B.@variable(md, clamp(xl, -Pc, Pc) <= x <= clamp(xu, -Pc, Pc))
    B.@variable(md, 0 <= r[i = 1:3] <= R[i])
    if c.market.reserve_headroom == "per_class"
        B.@constraint(md, x + r[1] + r[2] <= Pc); B.@constraint(md, x + r[1] + r[3] <= Pc)
    else
        B.@constraint(md, x + r[1] + r[2] + r[3] <= Pc)
    end
    B.@constraint(md, x + r[1] <= Pc); B.@constraint(md, -x + r[1] <= Pc)
    B.@objective(md, Max, a * x + sum(h[i] * r[i] for i in 1:3))
    B.optimize!(md)
    return B.objective_value(md)
end
lincoef(f) = (c0 = f(0.0, 0.0, 0.0, 0.0);
              (f(1.0, 0.0, 0.0, 0.0) - c0, [f(0.0, 1.0, 0.0, 0.0) - c0, f(0.0, 0.0, 1.0, 0.0) - c0, f(0.0, 0.0, 0.0, 1.0) - c0]))
"Worst raise need, lower need, SOC drain and SOC fill over everything the market can clear (by LP)."
function lp_worst(g::PeriodBids, c)
    m = c.market
    e = g.offers[:energy]; xl, xu = min_mw(e), max_mw(e)
    R = [max_mw(g.offers[p]) for p in RESERVES]
    nb = length(raise_needs(0.0, 0.0, 0.0, 0.0, m))
    raise = maximum(lpmax(lincoef((x, a, b, d) -> raise_needs(x, a, b, d, m)[i])..., xl, xu, R, c) for i in 1:nb)
    lower = lpmax(lincoef((x, a, b, d) -> lower_need(x, a, m))..., xl, xu, R, c)
    drain, fill = -Inf, -Inf
    for (l, u, f) in ((xl, min(xu, 0.0), (x, a, b, d) -> soc_change(-x, 0.0, a, b, d, c)),
                      (max(xl, 0.0), xu, (x, a, b, d) -> soc_change(0.0, x, a, b, d, c)))
        l <= u || continue
        ac, hc = lincoef(f)
        drain = max(drain, lpmax(-ac, -hc, l, u, R, c)); fill = max(fill, lpmax(ac, hc, l, u, R, c))
    end
    return (raise = raise, lower = lower, drain = drain, fill = fill)
end
"The guard's guarantee for start SOC anywhere in [lo, hi] (no buffer needed)."
function guarantee_holds(g, lo, hi, c; tol = 1e-6)
    η = B.eta_one_way(c.battery)
    w = lp_worst(g, c)
    return w.raise <= η * (lo - 5) + tol && w.lower <= (95 - hi) / η + tol &&
           w.drain <= lo - 5 + tol && w.fill <= 95 - hi + tol
end

@testset "RC383 pairs: whole cents, strict order, idempotent" begin
    # a tranche crossing 0 MW and two prices in one cent (reviewer cases)
    sp = storage_pairs(Offer(:energy, -20.0, [-128.0], [50.0]), cfg)
    @test [x.price for x in sp] == [-128.0, -127.99] && [x.mw for x in sp] == [-20.0, 30.0]
    o = conform_storage_offer(Offer(:energy, 0.0, [-32.009, -32.006], [20.0, 30.0]), cfg)
    @test [x.price for x in storage_pairs(o, cfg)] == [-32.01, -32.0]
    # no duplicate prices at the cap or floor after clamping
    @test [x.price for x in storage_pairs(Offer(:energy, 0.0, [4499.995, 4499.999], [10.0, 10.0]), cfg)] == [4499.99, 4500.0]
    @test [x.price for x in storage_pairs(Offer(:energy, -20.0, [-4500.0, -4499.999], [10.0, 10.0]), cfg)] == [-4500.0, -4499.99]
    # the always-discharge (floor) / always-charge (cap) blocks are never averaged into a tranche
    sp = storage_pairs(Offer(:energy, 2.0, [100.0, 120, 140, 160, 180], [30.0, 15, 15, 15, 15]), cfg)
    @test sp[1] == (slot = 6, price = -4500.0, mw = 2.0) && length(sp) == 5
    sp = storage_pairs(Offer(:energy, -80.0, [10.0, 20, 30, 40, 50], [10.0, 10, 10, 10, 38]), cfg)
    @test sp[end] == (slot = 5, price = 4500.0, mw = -2.0) && sp[end - 1].mw == -38.0
    rng = MersenneTwister(5)
    nfail = 0
    for _ in 1:3000
        n = rand(rng, 0:14)
        base = rand(rng) < 0.5 ? -100 * rand(rng) : 100 * rand(rng) * (rand(rng) < 0.3)
        ps = rand(rng) < 0.3 ? sort(round.(4500 .- 0.03 .* rand(rng, n); digits = 3)) :        # near the cap
             rand(rng) < 0.3 ? sort(round.(-120 .+ 0.02 .* rand(rng, n); digits = 3)) :        # near-equal
             sort(9000 .* rand(rng, n) .- 4500)
        sz = rand(rng, n) .* (100 - abs(base)) ./ max(n, 1)
        sz = max.(sz, 0.002)
        off = Offer(:energy, base, ps, sz)
        sp = storage_pairs(off, cfg)
        pr = [x.price for x in sp]
        ok = all(x -> round(x; digits = 2) == x, pr) && all(diff(pr) .> 0) &&
             all(x -> -4500 <= x <= 4500, pr) && count(x -> x.slot <= 5, sp) <= 5 && count(x -> x.slot >= 6, sp) <= 5
        # exported pairs are exactly what the simulator clears
        co = conform_storage_offer(off, cfg)
        co2 = conform_storage_offer(co, cfg)
        ok &= storage_pairs(co, cfg) == sp && (co2.base_mw, co2.prices, co2.sizes) == (co.base_mw, co.prices, co.sizes)
        # quantities only ever rounded down, by at most 0.001 MW per pair
        ok &= max_mw(off) < 0 ? max_mw(co) == 0 :            # always-charge block becomes a pair at the cap
              max_mw(off) - 0.011 <= max_mw(co) <= max_mw(off) + 1e-9
        ok &= base <= 0 ? abs(min_mw(co) - base) <= 0.006 : cleared_mw(co, -4500.0) >= base - 0.006
        nfail += !ok
    end
    @test nfail == 0
end

@testset "negative reserve base never reaches the market" begin
    @test B.round_offer(Offer(:primary, -1e-12, Float64[], Float64[])).base_mw == 0
    @test offer_from_grid(:primary, [0.0, 10.0], [-1e-12, -1e-12], 5).base_mw == 0
    @test min_mw(B.cap_offer(Offer(:primary, -0.001, [3.0], [50.0]), 20.0)) == 0
    # reviewer case: a -0.001 MW primary base made clear_period infeasible (cleared base energy only)
    mk(pbase) = B.PeriodBids(1, DateTime(2026), Dict(:energy => Offer(:energy, -99.995, [5.0, 9.88], [70.0, 29.995]),
        :regulation => Offer(:regulation, 0.0, [1.0], [50.0]), :primary => Offer(:primary, pbase, Float64[], Float64[]),
        :contingency => noff(:contingency)))
    n0 = B.CLEAR_FALLBACKS[]
    for λ in (-50.0, 0.0, 50.0, 200.0)
        pr = Dict(:energy => λ, :regulation => 10.0, :primary => 5.0, :contingency => 5.0)
        a, ra = clear_period(mk(-0.001), pr, cfg); b, rb = clear_period(mk(0.0), pr, cfg)
        @test a ≈ b && all(ra[p] ≈ rb[p] for p in RESERVES)
    end
    @test B.CLEAR_FALLBACKS[] == n0
    # an energy base outside the transfer limit: loud fallback, counted, clamped to the limit
    bad = B.PeriodBids(1, DateTime(2026), Dict(:energy => Offer(:energy, 150.0, Float64[], Float64[]),
                       (p => Offer(p, 10.0, Float64[], Float64[]) for p in RESERVES)...))
    pr = Dict(:energy => 100.0, :regulation => 10.0, :primary => 5.0, :contingency => 5.0)
    net, res = @test_logs (:warn,) match_mode = :any clear_period(bad, pr, cfg)
    @test net == 100 && all(res[p] == 0 for p in RESERVES) && B.CLEAR_FALLBACKS[] == n0 + 1
    @test any(occursin("outside [0, cap]", e) for e in validate_bids(B.PeriodBids(1, DateTime(2026),
              Dict(:energy => noff(:energy), :regulation => noff(:regulation), :primary => Offer(:primary, -0.001, Float64[], Float64[]),
                   :contingency => noff(:contingency))), cfg))
end

@testset "rules: one definition shared by settlement and guard" begin
    m = cfg.market
    # settle_period's sustain check is the rule in rules.jl
    for (net, reg, pri, con, soc) in ((-40.0, 0.0, 140.0, 140.0, 72.0), (30.0, 50.0, 10.0, 20.0, 20.0), (-80.0, 90.0, 0.0, 0.0, 80.0))
        res = Dict(:regulation => reg, :primary => pri, :contingency => con)
        out = settle_period(net, res, soc, Dict(p => 0.0 for p in PRODUCTS), cfg)
        @test out.sustain_violation_mwh ≈ sustain_violation_mwh(net, res, soc, cfg)
        ch, dis = max(-net, 0.0), max(net, 0.0)
        @test out.soc_end ≈ clamp(soc + soc_change(ch, dis, reg, pri, con, cfg), 5, 95)
    end
    # charging earns credit: raise reserve can be backed by curtailing the charge
    @test maximum(raise_needs(-40.0, 0.0, 100.0, 0.0, m)) ≈ -20 + 50
    @test transfer_cap(cfg) == 100
    @test transfer_cap(mkcfg(cfg; market = mkmarket(cfg.market; offered_capacity_mw = 60.0))) == 60
    # the exact worst case matches an LP over the clearing polytope
    rng = MersenneTwister(3)
    for _ in 1:40
        c = mkcfg(cfg; market = mkmarket(cfg.market; reserve_headroom = rand(rng, ("per_class", "summed")),
                  sustain_regulation = rand(rng, (0.25, 1.0)), sustain_primary = rand(rng, (0.5, 2.0)),
                  reserve_cap_mw = Dict(p => 200.0 for p in RESERVES)))
        xl = -100 * rand(rng); xu = 100 * rand(rng); R = Tuple(200 * rand(rng) for _ in 1:3)
        g = B.PeriodBids(1, DateTime(2026), Dict(:energy => Offer(:energy, xl, [1.0], [xu - xl]),
                         (p => Offer(p, R[i], Float64[], Float64[]) for (i, p) in enumerate(RESERVES))...))
        w, wl = worst_case_needs(xl, xu, R, c), lp_worst(g, c)
        @test isapprox(w.raise, wl.raise; atol = 1e-6) && isapprox(w.lower, wl.lower; atol = 1e-6)
        @test isapprox(w.drain, wl.drain; atol = 1e-6) && isapprox(w.fill, wl.fill; atol = 1e-6)
    end
end

@testset "SOC guard: reviewer regressions" begin
    mk(p, q) = q > 0 ? Offer(p, 0.0, [1.0], [q]) : noff(p)
    pr = Dict(:energy => 100.0, :regulation => 10.0, :primary => 10.0, :contingency => 10.0)
    # raise reserve while charging can exceed P: the old "bounded by P" shortcut was unsafe
    for (h, soc, ch, caps, reg) in ((0.75, 87.5, 5.0, 0.0, 100.0), (0.6, 72.0, 40.0, 200.0, 0.0))
        c = mkcfg(cfg; market = mkmarket(cfg.market; sustain_regulation = h, sustain_primary = h, sustain_contingency = h,
                  reserve_cap_mw = Dict(p => caps for p in RESERVES)))
        q = caps > 0 ? caps : 100.0
        o = Dict(:energy => Offer(:energy, -ch, Float64[], Float64[]), :regulation => mk(:regulation, reg),
                 :primary => mk(:primary, q), :contingency => mk(:contingency, q))
        g = guard_soc(B.PeriodBids(1, DateTime(2026), o), soc, soc, c)
        @test isempty(validate_bids(g, c))
        net, res = clear_period(g, pr, c)
        @test !settle_period(net, res, soc, pr, c).sustain_violation
        @test guarantee_holds(g, soc, soc, c)
        @test max_mw(g.offers[:primary]) > 90          # trimmed, not dropped
    end
    # energy is only cut to what it needs for dt, not for the longest reserve sustain time
    c = mkcfg(cfg; market = mkmarket(cfg.market; sustain_contingency = 2.0))
    g = guard_soc(B.PeriodBids(1, DateTime(2026), Dict(:energy => Offer(:energy, 0.0, [50.0], [100.0]),
                  (p => noff(p) for p in RESERVES)...)), 50.0, 50.0, c)
    @test max_mw(g.offers[:energy]) > 80
    # per class: contingency is not cut when primary is what binds (avail 22.5 MWh -> 45 MW)
    g = guard_soc(B.PeriodBids(1, DateTime(2026), Dict(:energy => noff(:energy), :regulation => noff(:regulation),
                  :primary => Offer(:primary, 50.0, Float64[], Float64[]), :contingency => Offer(:contingency, 45.0, Float64[], Float64[]))),
                  5.5 + 22.5 / B.eta_one_way(cfg.battery), 50.0, cfg)
    @test max_mw(g.offers[:primary]) ≈ 45 atol = 0.01
    @test max_mw(g.offers[:contingency]) ≈ 45 atol = 0.01
    # large deployment fractions drain both primary and contingency: no shortfall
    c = mkcfg(cfg; market = mkmarket(cfg.market; sustain_primary = 0.5, sustain_contingency = 0.0, deploy_primary = 0.85,
              deploy_contingency = 0.99, reserve_cap_mw = Dict(p => 200.0 for p in RESERVES)))
    g = guard_soc(B.PeriodBids(1, DateTime(2026), Dict(:energy => noff(:energy), :regulation => noff(:regulation),
                  :primary => mk(:primary, 100.0), :contingency => mk(:contingency, 100.0))), 84.0, 84.0, c)
    net, res = clear_period(g, pr, c)
    @test settle_period(net, res, 84.0, pr, c).shortfall_mwh == 0
    @test guarantee_holds(g, 84.0, 84.0, c)
end

@testset "SOC guard fuzz: every clearable outcome complies" begin
    rng = MersenneTwister(2026)
    function roff(p, lo, hi)
        n = rand(rng, 0:(p == :energy ? 8 : 5))
        base = rand(rng) < 0.3 ? (rand(rng, Bool) ? lo : hi) : lo + (hi - lo) * rand(rng)
        p != :energy && rand(rng) < 0.1 && (base = -1e-12)
        w = n > 0 ? (x = rand(rng, n); x ./ sum(x) .* (hi - base) * rand(rng)) : Float64[]
        keep = w .> 1e-3
        Offer(p, base, sort(rand(rng, n) .* 9000 .- 4500)[keep], w[keep])
    end
    rp() = (r = rand(rng); r < 0.2 ? -4500.0 : r < 0.4 ? 4500.0 : r < 0.5 ? 0.0 : 9000 * rand(rng) - 4500)
    nbad = 0
    for _ in 1:250
        m = mkmarket(cfg.market; reserve_headroom = rand(rng, ("per_class", "summed")),
                     sustain_regulation = rand(rng, (0.25, 0.5, 1.0, 2.0)), sustain_primary = rand(rng, (0.25, 0.5, 0.75, 2.0)),
                     sustain_contingency = rand(rng, (0.25, 0.6, 1.5, 2.0)), offered_capacity_mw = rand(rng) < 0.2 ? 60.0 : 0.0,
                     reserve_cap_mw = Dict(p => rand(rng, (0.0, 50.0, 150.0, 200.0)) for p in RESERVES))
        c = mkcfg(cfg; market = m, strategy = with_strategy(cfg; soc_guard_buffer_mwh = rand(rng, (0.0, 0.5))).strategy)
        offers = Dict{Symbol,Offer}(:energy => roff(:energy, -100.0, 100.0))
        for p in RESERVES; offers[p] = roff(p, 0.0, B.reserve_cap(m, c.battery, p)); end
        plan = Dict{Symbol,Float64}(:energy => 200 * rand(rng) - 100, (p => rand(rng) * 100 for p in RESERVES)...)
        r = rand(rng)
        lo = r < 0.15 ? 5.0 : r < 0.3 ? 95.0 : 5.0 + 90 * rand(rng)
        hi = rand(rng) < 0.3 ? lo : clamp(lo + 90 * rand(rng)^2, 5.0, 95.0)
        g = guard_soc(B.PeriodBids(1, DateTime(2026), offers, plan), lo, hi, c)
        ok = isempty(validate_bids(g, c)) && guarantee_holds(g, lo, hi, c)
        for k in 1:3
            soc = k == 1 ? lo : k == 2 ? hi : lo + (hi - lo) * rand(rng)
            pr = Dict(:energy => rp(), (p => max(0.0, rp()) for p in RESERVES)...)
            net, res = clear_period(g, pr, c)
            out = settle_period(net, res, soc, pr, c)
            ok &= out.sustain_violation_mwh <= 1e-6 && out.shortfall_mwh <= 1e-6
        end
        nbad += !ok
    end
    @test nbad == 0
end

@testset "offered capacity limits plan, guard and validation" begin
    c60 = mkcfg(cfg; market = mkmarket(cfg.market; offered_capacity_mw = 60.0))
    τ = 7 * 48 + 36
    pend = [B.idle_bids(k, df.timestamp[k]) for k in (τ + 1):(τ + 2)]
    pb = rolling_bids(df, τ, 80.0, pend, c60)
    @test isempty(validate_bids(pb, c60))
    @test max_mw(pb.offers[:energy]) <= 60 + 1e-9 && min_mw(pb.offers[:energy]) >= -60 - 1e-9
    @test all(max_mw(pb.offers[p]) <= 60 + 1e-9 for p in RESERVES)
    pl = pb.planned
    @test pl[:energy] + pl[:regulation] + max(pl[:primary], pl[:contingency]) <= 60 + 1e-6
    cm = build_coopt(c60, 50.0, Dict(p => df[1:96, B.PRICE_COLS[p]] for p in PRODUCTS))
    B.optimize!(cm.model)
    s = coopt_schedule(cm)
    net = s.discharge_mw .- s.charge_mw
    @test all(net .+ s.regulation_mw .+ max.(s.primary_mw, s.contingency_mw) .<= 60 + 1e-6)
    @test all(s.charge_mw .<= 60 + 1e-6) && all(-net .+ s.regulation_mw .<= 60 + 1e-6)
    hand = B.PeriodBids(1, DateTime(2026), Dict(:energy => Offer(:energy, -80.0, [100.0], [160.0]),
                        (p => noff(p) for p in RESERVES)...))
    errs = validate_bids(hand, c60)
    @test any(occursin("offered capacity", e) for e in errs)
end

@testset "optimiser: no simultaneous charge and discharge in the bid period" begin
    c = with_strategy(cfg; terminal_value_mode = "soft")
    prices = Dict(:energy => [-80.0, -1000.0], :regulation => [0.0, 0.0], :primary => [0.0, 0.0], :contingency => [0.0, 0.0])
    sol = solve!(build_coopt(c, 50.0, prices; reserves = false))
    milp = solve!(build_coopt(with_strategy(c; use_binary = true), 50.0, prices; reserves = false))
    @test min(sol.charge_mw, sol.discharge_mw) <= 1e-6
    @test sol.net_mw ≈ milp.net_mw atol = 1e-4
    # with charge credit the plan can back contingency by curtailing a charge (as settled)
    p1 = Dict(:energy => [-100.0], :regulation => [0.0], :primary => [0.0], :contingency => [50.0])
    with_credit = solve!(build_coopt(with_strategy(cfg; sustain_charge_credit = true), 10.0, p1))
    without = solve!(build_coopt(cfg, 10.0, p1))
    @test with_credit.reserve_mw[:contingency] > without.reserve_mw[:contingency] + 50
    res = Dict(:regulation => 0.0, :primary => 0.0, :contingency => with_credit.reserve_mw[:contingency])
    @test sustain_violation_mwh(with_credit.net_mw, res, 10.0, cfg) <= 1e-6
end

@testset "config: unknown keys are errors, validate catches bad values" begin
    mktempdir() do d
        base = read(joinpath(@__DIR__, "..", "configs", "default.toml"), String)
        tryload(txt) = (f = joinpath(d, "c.toml"); write(f, txt); try load_config(f); nothing catch e; sprint(showerror, e) end)
        @test tryload(base) === nothing
        @test occursin("lead_period", something(tryload(replace(base, "lead_periods = 3" => "lead_period = 1")), ""))
        @test occursin("valid keys", something(tryload(replace(base, "soc_guard = \"quantile\"" => "soc_gaurd = \"worst_case\"")), ""))
        @test occursin("stratgy", something(tryload(base * "\n[stratgy]\nlead_periods = 1\n"), ""))
        @test occursin("reg_up", something(tryload(replace(base, "regulation_up = 0.10" => "reg_up = 0.30")), ""))
        @test occursin("reserve_headrom", something(tryload(replace(base, "reserve_headroom = \"per_class\"" => "reserve_headrom = \"summed\"")), ""))
        @test occursin("USEP_X", something(tryload(replace(base, "pd_demand = \"pd_demand\"" => "USEP_X = \"a\"")), ""))
        @test occursin("horizon", something(tryload(replace(base, "horizon_periods = 96" => "horizon_periods = 2")), ""))
        @test occursin("error_period_window", something(tryload(replace(base, "error_period_window = 2" => "error_period_window = -1")), ""))
        @test occursin("soc_guard_buffer", something(tryload(replace(base, "soc_guard_buffer_mwh = 0.5" => "soc_guard_buffer_mwh = 50")), ""))
        @test occursin("24", something(tryload(replace(base, "period_hours = 0.5" => "period_hours = 0.25")), ""))
        @test occursin("lead_periods", something(tryload(replace(base, "lead_periods = 3" => "lead_periods = 0")), ""))
        c = load_config(joinpath(@__DIR__, "..", "configs", "default.toml"))
        write(joinpath(d, "c.toml"), replace(base, "regulation_up = 0.10" => "regulation_up = 0.30", "energy_flex_to_zero = true" => "energy_flex_to_zero = false"))
        c2 = load_config(joinpath(d, "c.toml"))
        @test c2.market.deploy_reg_up == 0.30 && c2.strategy.energy_flex_to_zero == false
    end
    # Every struct field is documented in default.toml, with the same default.
    raw = B.TOML.parsefile(joinpath(@__DIR__, "..", "configs", "default.toml"))
    for (sec, T) in (("battery", B.BatteryConfig), ("strategy", B.StrategyConfig), ("forecast", B.ForecastConfig),
                     ("simulation", B.SimulationConfig), ("data", B.DataConfig))
        for f in fieldnames(T)
            f == :columns && continue
            @test haskey(raw[sec], String(f))
            haskey(raw[sec], String(f)) && @test raw[sec][String(f)] == getfield(T(), f)
        end
    end
    dm = B.MarketConfig()
    for f in fieldnames(B.MarketConfig)
        f in (:deploy_reg_up, :deploy_reg_down, :deploy_primary, :deploy_contingency,
              :sustain_regulation, :sustain_primary, :sustain_contingency, :reserve_cap_mw) && continue
        @test raw["market"][String(f)] == getfield(dm, f)
    end
    @test cfg.market.deploy_reg_up == dm.deploy_reg_up && cfg.market.sustain_primary == dm.sustain_primary
end

@testset "loader: formats, split files, duplicates, gaps" begin
    n = 192
    src = df[1:n, :]
    mk(; kw...) = (dc = B.DataConfig(; columns = Dict("price_energy" => "USEP", "price_regulation" => "REG",
                       "price_primary" => "PRI", "price_contingency" => "CON"), kw...);
                   B.Config(cfg.battery, cfg.market, cfg.strategy, cfg.forecast, cfg.simulation, dc))
    prices(rows) = DataFrame(USEP = src.price_energy[rows], REG = src.price_regulation[rows],
                             PRI = src.price_primary[rows], CON = src.price_contingency[rows])
    err(f) = try f(); "" catch e; sprint(showerror, e) end
    mktempdir() do d
        w(name, t) = (p = joinpath(d, name); B.CSV.write(p, t); p)
        # dd/mm/yyyy and 01-Jan-2026 dates with a period column
        t = hcat(DataFrame(DATE = Dates.format.(Date.(src.timestamp), "dd/mm/yyyy"), PERIOD = src.period), prices(1:n))
        c = mk(date_col = "DATE", period_col = "PERIOD", date_format = "dd/mm/yyyy")
        @test load_market_data(w("ddmm.csv", t), c).timestamp == src.timestamp
        t.DATE = Dates.format.(Date.(src.timestamp), "dd-u-yyyy")
        @test load_market_data(w("ddmon.csv", t), mk(date_col = "DATE", period_col = "PERIOD", date_format = "dd-u-yyyy")).price_energy == src.price_energy
        # period out of range names file, column and value
        t.PERIOD[10] = 49
        e = err(() -> load_market_data(w("p49.csv", t), mk(date_col = "DATE", period_col = "PERIOD", date_format = "dd-u-yyyy")))
        @test occursin("p49.csv", e) && occursin("PERIOD", e) && occursin("49", e)
        # "1,234.50", "NA" and "-" values; a space-separated custom timestamp
        lines = ["time,USEP,REG,PRI,CON"]
        for i in 1:n
            u = i == 3 ? "\"1,234.50\"" : i == 4 ? "NA" : i == 5 ? "-" : string(src.price_energy[i])
            push!(lines, join([Dates.format(src.timestamp[i], "yyyy-mm-dd HH:MM"), u, src.price_regulation[i],
                               src.price_primary[i], src.price_contingency[i]], ","))
        end
        p = joinpath(d, "fmt.csv"); write(p, join(lines, "\n"))
        l = load_market_data(p, mk(timestamp_col = "time", timestamp_format = "yyyy-mm-dd HH:MM"))
        @test l.timestamp == src.timestamp && l.price_energy[3] == 1234.5
        @test l.price_energy[4] == l.price_energy[5] == 1234.5            # NA and - forward-filled
        @test l.price_energy[6] == src.price_energy[6]
        lines[8] = replace(lines[8], r",[^,]+$" => ",abc")
        write(p, join(lines, "\n"))
        e = err(() -> load_market_data(p, mk(timestamp_col = "time", timestamp_format = "yyyy-mm-dd HH:MM")))
        @test occursin("fmt.csv", e) && occursin("CON", e) && occursin("abc", e)
        e = err(() -> load_market_data(p, mk(timestamp_col = "time")))
        @test occursin("time", e) && occursin("yyyy-mm-ddTHH:MM:SS", e)
        # period-end timestamps
        t = hcat(DataFrame(ts = src.timestamp .+ Minute(30)), prices(1:n))
        @test load_market_data(w("end.csv", t), mk(timestamp_col = "ts", timestamp_is_period_end = true)).period == src.period
        # time-split directory: stacked, not forward-filled copies of the first file
        sd = mkpath(joinpath(d, "split"))
        for (k, r) in enumerate((1:96, 97:n))
            B.CSV.write(joinpath(sd, "part$k.csv"), hcat(DataFrame(timestamp = src.timestamp[r]), prices(r)))
        end
        l = load_market_data(sd, mk())
        @test nrow(l) == n && l.price_energy == src.price_energy && l.price_contingency == src.price_contingency
        # product-split directory: joined on time
        pd_ = mkpath(joinpath(d, "prod"))
        B.CSV.write(joinpath(pd_, "a.csv"), DataFrame(timestamp = src.timestamp, USEP = src.price_energy, REG = src.price_regulation))
        B.CSV.write(joinpath(pd_, "b.csv"), DataFrame(timestamp = src.timestamp, PRI = src.price_primary, CON = src.price_contingency))
        l = load_market_data(pd_, mk())
        @test nrow(l) == n && l.price_primary == src.price_primary && l.price_energy == src.price_energy
        # duplicates: identical rows merge, conflicting ones are an error
        t = hcat(DataFrame(timestamp = src.timestamp), prices(1:n))
        @test load_market_data(w("dup.csv", vcat(t, t[10:12, :])), mk()).price_energy == src.price_energy
        t2 = t[10:10, :]; t2.USEP .= 9999.0
        e = err(() -> load_market_data(w("dupc.csv", vcat(t, t2)), mk()))
        @test occursin("conflicting", e) && occursin(string(src.timestamp[10]), e)
        # too much missing is an error; leading missing rows are dropped, not back-filled
        t3 = copy(t); t3.REG[50:70] .= NaN
        @test occursin("max_missing_share", err(() -> load_market_data(w("gap.csv", t3), mk())))
        t4 = copy(t); t4.REG[1:5] .= NaN
        l = load_market_data(w("lead.csv", t4), mk())
        @test nrow(l) == n - 5 && l.timestamp[1] == src.timestamp[6] && l.price_regulation == src.price_regulation[6:n]
    end
end

@testset "pre-dispatch vintages from an issue-time column" begin
    # Two runs per period: an early one (issued 4 periods ahead) and a late one (just before).
    n = 96
    ts = df.timestamp[1:n]
    step = Minute(30)
    early = DataFrame(timestamp = ts, price_energy = df.price_energy[1:n], price_regulation = df.price_regulation[1:n],
                      price_primary = df.price_primary[1:n], price_contingency = df.price_contingency[1:n],
                      pd_energy = fill(111.0, n), run = ts .- 4step)
    late = copy(early); late.pd_energy .= 999.0; late.run = ts
    dc = B.DataConfig(columns = Dict("pd_issue_time" => "run"))
    c = B.Config(cfg.battery, cfg.market, cfg.strategy, cfg.forecast, cfg.simulation, dc)
    mktempdir() do d
        p = joinpath(d, "long.csv"); B.CSV.write(p, vcat(early, late))
        l = load_market_data(p, c)
        @test nrow(l) == n && all(==(111.0), l.pd_energy)       # late run would leak: dropped
        @test B.pd_horizon_at(B.extend_rows(l, 200, c), :energy, 60, c) == cfg.strategy.lead_periods
        # Without the issue-time mapping the conflicting runs are an error, not a silent pick.
        @test_throws ErrorException load_market_data(p, cfg)
    end
end

@testset "forecast: pre-dispatch horizon available at the origin" begin
    τ = 6 * 48 + 11
    targets = (τ + 1):(τ + 98)
    short = B.extend_rows(df[1:τ, :], τ + 98, cfg)       # table ends at the decision time
    nopd = copy(df)
    for p in PRODUCTS
        nopd[!, B.PD_COLS[p]] .= NaN
    end
    @test B.pd_horizon_at(short, :energy, τ, cfg) == 0
    @test B.pd_horizon_at(df, :energy, τ, cfg) == cfg.forecast.pd_horizon_periods
    for m in ("ridge", "predispatch")
        c = with_forecast(cfg; method = m)
        f1 = forecast_prices(short, τ, targets, c)
        f2 = forecast_prices(nopd, τ, targets, c)
        for p in PRODUCTS
            @test f1.mean[p] == f2.mean[p] && all(isfinite, f1.mean[p])
            @test f1.residuals[p] == f2.residuals[p]
        end
    end
    # Partial horizon: pd known for 10 periods ahead is used up to lead 10 only.
    part = copy(df); part[(τ + 11):end, B.PD_COLS[:energy]] .= NaN
    @test B.pd_horizon_at(part, :energy, τ, cfg) == 10
    f = forecast_prices(part, τ, targets, with_forecast(cfg; method = "predispatch"))
    @test f.mean[:energy][1:10] == df.pd_energy[(τ + 1):(τ + 10)]
end

@testset "generate_bids survives gaps and missing in the caller's history" begin
    τ = 6 * 48 + 11
    soc = 50.0
    base = generate_bids(df[1:τ, :], τ, soc, cfg)
    for (name, mod!) in (("NaN at origin", d -> (d.price_energy[τ] = NaN)),
                         ("NaN in history", d -> (d.price_regulation[τ - 100] = NaN)),
                         ("missing values", d -> (d.price_primary = allowmissing(d.price_primary); d.price_primary[τ - 3] = missing)),
                         ("leading NaN", d -> (d.price_contingency[1:5] .= NaN)))
        d = df[1:τ, :]
        mod!(d)
        bl = @test_logs (:warn, r"filling") match_mode = :any generate_bids(d, τ, soc, cfg)
        @test length(bl) == 1 && isempty(validate_bids(bl[1], cfg))
        @test all(isfinite, bl[1].offers[:energy].prices)
    end
    d = df[1:τ, :]; d.price_energy .= NaN
    @test_throws ErrorException generate_bids(d, τ, soc, cfg)
    # forecast_prices on a raw table: finite, and it does not modify the caller's table
    d = df[1:τ + 98, :]; d.price_energy[τ] = NaN
    f = forecast_prices(d, τ, (τ + 1):(τ + 98), cfg)
    @test all(isfinite, f.mean[:energy]) && isnan(d.price_energy[τ])
end

@testset "SOC guard tails are never narrower than the pooled errors" begin
    τ = 6 * 48 + 11
    f = forecast_prices(df, τ, (τ + 1):(τ + 96), cfg)
    for p in PRODUCTS, j in (1, 10, 40)
        qs = [0.001, 0.2, 0.5, 0.8, 0.999]
        cons = B.forecast_quantiles(f, p, j, qs; conservative = true)
        cond = B.forecast_quantiles(f, p, j, qs)
        pooled = f.mean[p][j] .+ B.quantile(f.residuals[p], qs)
        @test all(cons[1:2] .<= min.(cond[1:2], pooled[1:2]) .+ 1e-12)
        @test all(cons[4:5] .>= max.(cond[4:5], pooled[4:5]) .- 1e-12)
    end
end

@testset "quantile guard: no sustain violation where conditional bands were too narrow" begin
    # README data (make_synthetic.jl --days 21). With the guard on conditional bands, the
    # energy prices of the two open periods before 2026-01-10T05:00 lay above their 99.9%
    # conditional quantile, the SOC fell below the guarded interval and reserves were short.
    d21 = synthetic_market_data(days = 21, seed = 1, cfg = cfg)
    row = findfirst(==(DateTime(2026, 1, 10, 5)), d21.timestamp)
    r = run_backtest(d21, cfg; start_row = row - 10, end_row = row + 2, progress = false)
    sm = Dict(r.summary.metric .=> r.summary.value)
    @test sm["sustain_violation_periods"] == 0 && sm["shortfall_mwh"] == 0
end

@testset "settlement: delivered energy, withheld reserve, deployment energy" begin
    z = Dict(p => 0.0 for p in RESERVES)
    prs(e; reg = 0.0, pri = 0.0, con = 0.0) = Dict(:energy => e, :regulation => reg, :primary => pri, :contingency => con)
    # discharge 100 MW from near Emin at the cap: only the delivered MWh is paid
    o = settle_period(100.0, z, 10.0, prs(4500.0), cfg)
    @test o.shortfall_mwh > 40
    @test o.revenue[:energy] ≈ 4500 * (50 - o.shortfall_mwh)
    @test o.revenue[:energy] + o.revenue[:penalty] < 0
    # charging at the floor: payment only for the MWh absorbed
    o = settle_period(-100.0, z, 90.0, prs(-4500.0), cfg)
    @test o.shortfall_mwh > 40 && o.revenue[:energy] ≈ -4500 * -(50 - o.shortfall_mwh)
    # no clamp: unchanged settlement
    @test settle_period(40.0, z, 50.0, prs(100.0), cfg).revenue[:energy] ≈ 100 * 40 * 0.5
    # an award the SOC cannot sustain: the unsustainable share of its payment is withheld
    r = Dict(:regulation => 0.0, :primary => 100.0, :contingency => 0.0)
    o = settle_period(0.0, r, 20.0, prs(100.0; pri = 10.0), cfg)
    @test o.sustain_violation && o.revenue[:primary] ≈ 500
    s_up, s_dn = B.sustainable_share(0.0, r, 20.0, cfg)
    @test o.reserve_withheld ≈ (1 - s_up) * 500 && 0 < s_up < 1 && s_dn == 1
    @test sustain_violation_mwh(0.0, Dict(:regulation => 0.0, :primary => 100s_up, :contingency => 0.0), 20.0, cfg) <= 1e-6
    @test sustain_violation_mwh(0.0, Dict(:regulation => 0.0, :primary => 100s_up + 1, :contingency => 0.0), 20.0, cfg) > 0
    @test settle_period(0.0, r, 80.0, prs(100.0; pri = 10.0), cfg).reserve_withheld == 0
    # regulation-lower that cannot be absorbed withholds regulation only
    o = settle_period(-90.0, Dict(:regulation => 50.0, :primary => 0.0, :contingency => 0.0), 80.0, prs(0.0; reg = 10.0), cfg)
    @test o.sustain_violation && 0 < o.reserve_withheld <= o.revenue[:regulation]
    # deployment energy: unpaid by default, paid at the energy price with the flag
    r = Dict(:regulation => 0.0, :primary => 100.0, :contingency => 0.0)
    @test settle_period(0.0, r, 50.0, prs(150.0; pri = 3.0), cfg).revenue[:energy] == 0
    cpay = mkcfg(cfg; market = mkmarket(cfg.market; pay_deployment_energy = true))
    o = settle_period(0.0, r, 50.0, prs(150.0; pri = 3.0), cpay)
    @test o.revenue[:energy] ≈ 150 * 0.5 * cfg.market.deploy_primary * 100
    @test o.soc_end ≈ settle_period(0.0, r, 50.0, prs(150.0; pri = 3.0), cfg).soc_end
    # ... and the plan's objective carries the same term, also after a price sweep
    pr = Dict(p => fill(p == :energy ? 150.0 : 3.0, 4) for p in PRODUCTS)
    coef(cm, p, j) = B.JuMP.coefficient(B.objective_function(cm.model), cm.r[p][j])
    g(p) = B.deployment_grid_mwh(p == :regulation ? 1.0 : 0.0, p == :primary ? 1.0 : 0.0, p == :contingency ? 1.0 : 0.0, cfg)
    cm0, cm1 = build_coopt(cfg, 50.0, pr), build_coopt(cpay, 50.0, pr)
    for p in RESERVES
        @test coef(cm0, p, 2) ≈ 0.5 * 3.0
        @test coef(cm1, p, 2) ≈ 0.5 * 3.0 + 150 * g(p)
    end
    set_price!(cm1, :energy, 2, 400.0); set_price!(cm1, :primary, 2, 7.0)
    @test coef(cm1, :primary, 2) ≈ 0.5 * 7.0 + 400 * g(:primary)
    @test coef(cm1, :contingency, 2) ≈ 0.5 * 3.0 + 400 * g(:contingency)
    # stochastic deployment: regulation's net energy goes either way, mean preserved
    cst = B.Config(cfg.battery, cfg.market, cfg.strategy, cfg.forecast,
                   B.SimulationConfig(stochastic_deployment = true, seed = 1), cfg.data)
    rr = Dict(:regulation => 100.0, :primary => 0.0, :contingency => 0.0)
    rng = MersenneTwister(5)
    d = [settle_period(0.0, rr, 50.0, prs(0.0), cst; rng = rng).soc_end - 50 for _ in 1:2000]
    @test minimum(d) < -2 && maximum(d) > 2
    @test abs(B.mean(d) - (settle_period(0.0, rr, 50.0, prs(0.0), cfg).soc_end - 50)) < 0.1
end

@testset "perfect-foresight benchmark" begin
    rows = (7 * 48 + 1):(7 * 48 + 48)
    pf, sch = perfect_foresight(df, rows, cfg)
    pfge, schge = perfect_foresight(df, rows, cfg; end_soc_ge_start = true)
    @test pfge <= pf + 1e-6 && schge.soc_mwh[end] >= 50 - 1e-6
    # the reported number is the schedule's market revenue (no degradation, no terminal value)
    pr(p) = df[rows, B.PRICE_COLS[p]]
    rev = 0.5 * (sum(pr(:energy) .* (sch.discharge_mw .- sch.charge_mw)) + sum(pr(:regulation) .* sch.regulation_mw) +
                 sum(pr(:primary) .* sch.primary_mw) + sum(pr(:contingency) .* sch.contingency_mw))
    @test rev ≈ pf rtol = 1e-6
    # never below the old benchmark (degradation cost, terminal SOC >= start, gross sustain rule)
    old = with_strategy(cfg; terminal_value_mode = "hard", terminal_soc_frac = 0.5)
    cm = build_coopt(old, 50.0, Dict(p => Vector{Float64}(pr(p)) for p in PRODUCTS)); B.optimize!(cm.model)
    s0 = coopt_schedule(cm)
    rev0 = 0.5 * (sum(pr(:energy) .* (s0.discharge_mw .- s0.charge_mw)) + sum(pr(:regulation) .* s0.regulation_mw) +
                  sum(pr(:primary) .* s0.primary_mw) + sum(pr(:contingency) .* s0.contingency_mw))
    @test pfge >= rev0 - 1e-6
    # pay_deployment_energy is part of the benchmark's revenue
    cpay = mkcfg(cfg; market = mkmarket(cfg.market; pay_deployment_energy = true))
    @test perfect_foresight(df, rows, cpay)[1] >= pf - 1e-6
end

@testset "bids CSV round trip" begin
    τ = 7 * 48 + 20
    pend = [B.idle_bids(k, df.timestamp[k]) for k in (τ + 1):(τ + 2)]
    bl = [rolling_bids(df, τ + i, 50.0, [B.idle_bids(k, df.timestamp[k]) for k in (τ + i + 1):(τ + i + 2)], cfg) for i in 0:1]
    push!(bl, B.idle_bids(τ + 10, df.timestamp[τ + 10]))
    mktempdir() do d
        write_bids(joinpath(d, "a.csv"), bl[1:1], cfg)
        write_bids(joinpath(d, "b.csv"), bl[2:3], cfg)
        back = read_bids(d; df = df)
        @test length(back) == 3
        for (x, y) in zip(bl, back)
            @test y.row == x.row && y.timestamp == x.timestamp
            for p in PRODUCTS
                @test y.offers[p].base_mw == x.offers[p].base_mw
                @test y.offers[p].prices == x.offers[p].prices && y.offers[p].sizes == x.offers[p].sizes
            end
        end
        # a later file replaces an earlier offer for the same period (offer variation)
        write_bids(joinpath(d, "c.csv"), [B.PeriodBids(bl[1].row, bl[1].timestamp, B.idle_bids(0, bl[1].timestamp).offers)], cfg)
        @test max_mw(read_bids(d)[1].offers[:regulation]) == 0
        @test read_bids(joinpath(d, "a.csv"))[1].row == 0
    end
end

@testset "generate_bids in a closed loop equals the backtest" begin
    L = cfg.strategy.lead_periods
    st, en = 7 * 48 + 30, 7 * 48 + 37
    r = run_backtest(df, cfg; start_row = st, end_row = en, progress = false)
    # External simulator: once per period, give generate_bids the history up to the origin
    # (realised prices after it are unknown), the SOC, and every offer submitted so far.
    soc = B.initial_soc(cfg.battery)
    mine = Dict{Int,PeriodBids}()
    mktempdir() do d
        # the battery had no offers before the window (the backtest leaves those periods idle)
        write_bids(joinpath(d, "before.csv"), [B.idle_bids(k, df.timestamp[k]) for k in (st - L + 1):(st - 1)], cfg)
        for τ in (st - L):(en - L)
            if τ >= st
                pr = B.realised_prices(df, τ)
                net, res = clear_period(mine[τ], pr, cfg)
                soc = settle_period(net, res, soc, pr, cfg).soc_end
            end
            hist = copy(df)
            for p in PRODUCTS; hist[(τ + 1):end, B.PRICE_COLS[p]] .= NaN; end
            files = filter(f -> endswith(f, ".csv"), readdir(d; join = true))
            bl = generate_bids(hist, τ, soc, cfg; pending = read_bids(files; df = df))
            mine[τ + L] = bl[1]
            write_bids(joinpath(d, "bids_$(τ + L).csv"), bl, cfg)
        end
    end
    @test length(r.bids) == en - st + 1
    for pb in r.bids
        x = mine[pb.row]
        for p in PRODUCTS
            @test x.offers[p].base_mw == pb.offers[p].base_mw
            @test x.offers[p].prices == pb.offers[p].prices && x.offers[p].sizes == pb.offers[p].sizes
        end
    end
    # Without the open offers: the SOC interval covers the one from the known offers, and the
    # offers keep the guard's guarantee for it (more conservative, never less safe).
    τ = en - L
    known = [r.bids[findfirst(b -> b.row == k, r.bids)] for k in (τ + 1):(τ + L - 1)]
    soc_τ = r.periods.soc_end_mwh[τ - st + 1]
    f, τf = B.forecast_at(df, τ, τ + 96, cfg)
    _, lo_k, hi_k = B.soc_after_open_offers(known, τ, soc_τ, f, τf, cfg)
    _, lo_u, hi_u = B.soc_after_open_offers(fill(nothing, L - 1), τ, soc_τ, f, τf, cfg)
    @test lo_u <= lo_k && hi_u >= hi_k && hi_u - lo_u > hi_k - lo_k
    blind = (@test_logs (:warn, r"no offer supplied") match_mode = :any generate_bids(df, τ, soc_τ, cfg))[1]
    @test isempty(validate_bids(blind, cfg))
    @test guarantee_holds(blind, lo_k, hi_k, cfg)
    @test guarantee_holds(blind, lo_u + cfg.strategy.soc_guard_buffer_mwh, hi_u - cfg.strategy.soc_guard_buffer_mwh, cfg) ||
          lo_u + 0.5 > hi_u - 0.5
    # the low-level entry points refuse a wrong number of open offers
    @test_throws ErrorException rolling_bids(df, τ, soc_τ, PeriodBids[], cfg)
    @test_throws ErrorException rolling_bids(df, τ, soc_τ, reverse(known), cfg)
end

@testset "day_ahead: bids made at the true origin" begin
    # cheap settings; "expected" guard so the last periods of a day carry real offers
    c = with_strategy(cfg; mode = "day_ahead", soc_guard = "expected", horizon_periods = 48,
                      energy_grid_quantiles = [0.1, 0.5, 0.9], reserve_grid_quantiles = [0.5])
    L = c.strategy.lead_periods
    st = 4 * 48 + 1; en = st + 48 + 1               # day 1 and the first 2 periods of day 2
    d2 = copy(df)
    for k in (st + 48 - L + 1):(st + 47)            # the open periods at day 2's gate closure
        d2.price_energy[k] = -3000.0                # their charging offers now clear
    end
    r1 = run_backtest(df, c; start_row = st, end_row = en, progress = false)
    r2 = run_backtest(d2, c; start_row = st, end_row = en, progress = false)
    k = st + 47 - st + 1
    @test r1.periods.soc_end_mwh[k] != r2.periods.soc_end_mwh[k]   # the spikes moved the SOC
    b1 = filter(b -> b.row >= st + 48, r1.bids); b2 = filter(b -> b.row >= st + 48, r2.bids)
    @test length(b1) == 2
    for (x, y) in zip(b1, b2), p in PRODUCTS
        @test x.offers[p].prices == y.offers[p].prices && x.offers[p].sizes == y.offers[p].sizes &&
              x.offers[p].base_mw == y.offers[p].base_mw
    end
    # the same as calling day_ahead_bids at τ with the SOC then and the open offers
    τ = st + 48 - L
    pend = [r1.bids[findfirst(b -> b.row == j, r1.bids)] for j in (τ + 1):(st + 47)]
    da = day_ahead_bids(df, τ, r1.periods.soc_end_mwh[τ - st + 1], pend, (st + 48):en, c)
    @test all(da[i].offers[p].sizes == b1[i].offers[p].sizes for i in 1:2, p in PRODUCTS)
end

@testset "info_lag_periods: forecast ignores the origin's price" begin
    τ = 7 * 48 + 20
    pend = [B.idle_bids(k, df.timestamp[k]) for k in (τ + 1):(τ + 2)]
    d2 = copy(df); d2.price_energy[τ] += 500; d2.price_regulation[τ] += 50
    same(a, b) = all(a.offers[p].prices == b.offers[p].prices && a.offers[p].sizes == b.offers[p].sizes &&
                     a.offers[p].base_mw == b.offers[p].base_mw for p in PRODUCTS)
    c1 = with_strategy(cfg; info_lag_periods = 1)
    @test same(rolling_bids(df, τ, 50.0, pend, c1), rolling_bids(d2, τ, 50.0, pend, c1))
    @test !same(rolling_bids(df, τ, 50.0, pend, cfg), rolling_bids(d2, τ, 50.0, pend, cfg))
    @test_throws ErrorException run_backtest(df, with_strategy(cfg; info_lag_periods = 5); start_row = 48 + 4, progress = false)
end

@testset "backtest" begin
    st = 7 * 48 + 1
    r = run_backtest(df, cfg; start_row = st, end_row = st + 47, progress = false)
    sm = Dict(r.summary.metric .=> r.summary.value)
    @test nrow(r.periods) == 48
    @test sm["sustain_violation_periods"] == 0
    @test sm["shortfall_mwh"] == 0
    @test sm["soc_min_seen_mwh"] >= 5 - 1e-6 && sm["soc_max_seen_mwh"] <= 95 + 1e-6
    # guaranteed: a compliant run under expected deployment is feasible for the benchmark LP
    @test sm["net_market_revenue"] <= sm["perfect_foresight_bound"] + 1e-6
    @test sm["perfect_foresight_end_soc_ge_start"] <= sm["perfect_foresight_bound"] + 1e-6
    @test sm["reserve_payment_withheld"] == 0
    @test sm["compliance_adjusted_revenue"] ≈ sm["net_market_revenue"]
    @test sm["end_soc_mwh"] == r.periods.soc_end_mwh[end]
    @test sm["compliance_adjusted_revenue_incl_soc_value"] ≈ sm["compliance_adjusted_revenue"] +
        (sm["end_soc_mwh"] - 50) * B.median(r.periods.price_energy) * B.eta_one_way(cfg.battery)
    @test sm["net_market_revenue"] > 0
    @test all(isempty(validate_bids(pb, cfg)) for pb in r.bids)
    @test sm["clearing_fallback_periods"] == 0
    # SOC continuity
    @test all(r.periods.soc_start_mwh[2:end] .≈ r.periods.soc_end_mwh[1:end-1])
    mktempdir() do d
        write_backtest(r, d, cfg)
        @test isfile(joinpath(d, "periods.csv")) && isfile(joinpath(d, "bids.csv")) && isfile(joinpath(d, "summary.csv"))
    end
end

end # BESSBidding
