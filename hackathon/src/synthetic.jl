# Synthetic SWEM-like data so the pipeline can be developed and tested before EMA
# releases the real datasets. The numbers are illustrative only.

"""
    synthetic_market_data(; days=42, start=DateTime(2026,1,1), seed=1, cfg=Config(), stress=false)

Half-hourly energy and reserve prices with a daily shape, AR(1) noise, price spikes and
pre-dispatch forecasts that are noisy versions of the realised prices.

`stress = true` adds heavier energy price events the pre-dispatch columns do not announce
(they stay those of the unstressed data, which is otherwise identical): about two spikes a
day of 500-4000 \$/MWh at any hour, more often in the night and midday hours when the plan
charges, and about one collapse to -500..-50 \$/MWh every two days, more often in the
evening when it discharges. Reserve prices follow the stressed tightness. For measuring the
cost of a fixed-quantity energy offer through a spike.
"""
function synthetic_market_data(; days::Int = 42, start::DateTime = DateTime(2026, 1, 1),
                               seed::Int = 1, cfg::Config = Config(), stress::Bool = false)
    rng = MersenneTwister(seed)
    m = cfg.market
    n = days * m.periods_per_day
    step = Minute(round(Int, m.period_hours * 60))
    ts = [start + step * (i - 1) for i in 1:n]
    per = period_of_day.(ts, m.period_hours)
    hod = (per .- 1) .* m.period_hours                       # hour of day
    weekend = [dayofweek(t) >= 6 for t in ts]

    # Daily shape: morning ramp, midday solar dip, evening peak.
    shape = 150 .+ 40 .* exp.(-((hod .- 9.5) ./ 2.0) .^ 2) .- 25 .* exp.(-((hod .- 13) ./ 2.0) .^ 2) .+
            90 .* exp.(-((hod .- 19.5) ./ 1.8) .^ 2) .- 30 .* exp.(-((hod .- 3.5) ./ 2.5) .^ 2)
    shape .-= 15 .* weekend

    ar = zeros(n)
    for i in 2:n
        ar[i] = 0.9 * ar[i-1] + 12 * randn(rng)
    end
    spikes = zeros(n)
    for i in 1:n
        if rand(rng) < 0.006 * (1 + 2 * (17 <= hod[i] <= 21))
            len = rand(rng, 1:4)
            mag = 300 + 1500 * rand(rng)^2
            spikes[i:min(n, i + len - 1)] .+= mag
        end
    end
    energy = clamp.(shape .+ ar .+ spikes, m.price_floor, m.price_cap)
    # Occasional low/negative prices overnight.
    for i in 1:n
        if hod[i] < 6 && rand(rng) < 0.01
            energy[i] = -50 * rand(rng)
        end
    end

    z_reg, z_pri, z_con = randn(rng, n), randn(rng, n), randn(rng, n)
    reserves(e, sp) = (tight = clamp.((e .- 150) ./ 200, 0, 5);   # system tightness proxy
                       (max.(0, 25 .+ 15 .* tight .+ 5 .* z_reg), max.(0, 3 .+ 4 .* tight .+ 1.5 .* z_pri),
                        max.(0, 10 .+ 30 .* tight .+ 6 .* z_con .+ 0.2 .* sp)))
    reg, pri, con = reserves(energy, spikes)

    # One pre-dispatch value per row with noise that does not depend on lead time: a table
    # cannot hold several vintages, so a backtest on this data cannot show how pre-dispatch
    # accuracy decays with lead. Treat its pre-dispatch value as optimistic at long leads.
    noisy(x, s) = x .+ s .* randn(rng, n) .* (1 .+ abs.(x) ./ 400)
    demand = 5500 .+ 1200 .* (shape .- 150) ./ 100 .+ 100 .* randn(rng, n)

    pd = (noisy(energy, 25.0), noisy(reg, 4.0), noisy(pri, 1.0), noisy(con, 6.0))
    if stress
        # own random stream, so the unstressed data and its pre-dispatch stay as they are
        srng = MersenneTwister(seed + 1_000_003)
        extra = zeros(n)
        for i in 1:n
            charging_hours = hod[i] < 7 || 10 <= hod[i] < 16
            if rand(srng) < 0.03 * (1 + charging_hours)
                len = rand(srng, 1:3)
                extra[i:min(n, i + len - 1)] .+= 500 + 3500 * rand(srng)^2
            end
        end
        energy = clamp.(energy .+ extra, m.price_floor, m.price_cap)
        for i in 1:n
            if extra[i] == 0 && rand(srng) < 0.006 * (1 + 3 * (17 <= hod[i] <= 22))
                energy[i] = -50 - 450 * rand(srng)
            end
        end
        reg, pri, con = reserves(energy, spikes .+ extra)
    end
    return canonicalize(DataFrame(
        timestamp = ts,
        price_energy = energy, price_regulation = reg, price_primary = pri, price_contingency = con,
        pd_energy = pd[1], pd_regulation = pd[2], pd_primary = pd[3], pd_contingency = pd[4],
        pd_demand = demand,
    ), cfg)
end
