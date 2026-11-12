# Synthetic SWEM-like data so the pipeline can be developed and tested before EMA
# releases the real datasets. The numbers are illustrative only.

"""
    synthetic_market_data(; days=42, start=DateTime(2026,1,1), seed=1, cfg=Config())

Half-hourly energy and reserve prices with a daily shape, AR(1) noise, price spikes and
pre-dispatch forecasts that are noisy versions of the realised prices.
"""
function synthetic_market_data(; days::Int = 42, start::DateTime = DateTime(2026, 1, 1),
                               seed::Int = 1, cfg::Config = Config())
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

    tight = clamp.((energy .- 150) ./ 200, 0, 5)              # system tightness proxy
    reg = max.(0, 25 .+ 15 .* tight .+ 5 .* randn(rng, n))
    pri = max.(0, 3 .+ 4 .* tight .+ 1.5 .* randn(rng, n))
    con = max.(0, 10 .+ 30 .* tight .+ 6 .* randn(rng, n) .+ 0.2 .* spikes)

    # One pre-dispatch value per row with noise that does not depend on lead time: a table
    # cannot hold several vintages, so a backtest on this data cannot show how pre-dispatch
    # accuracy decays with lead. Treat its pre-dispatch value as optimistic at long leads.
    noisy(x, s) = x .+ s .* randn(rng, n) .* (1 .+ abs.(x) ./ 400)
    demand = 5500 .+ 1200 .* (shape .- 150) ./ 100 .+ 100 .* randn(rng, n)

    return canonicalize(DataFrame(
        timestamp = ts,
        price_energy = energy, price_regulation = reg, price_primary = pri, price_contingency = con,
        pd_energy = noisy(energy, 25.0), pd_regulation = noisy(reg, 4.0),
        pd_primary = noisy(pri, 1.0), pd_contingency = noisy(con, 6.0),
        pd_demand = demand,
    ), cfg)
end
