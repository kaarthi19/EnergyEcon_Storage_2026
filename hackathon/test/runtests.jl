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
