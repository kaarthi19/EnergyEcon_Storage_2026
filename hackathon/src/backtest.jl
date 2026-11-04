# Closed-loop backtest: bid with information available at each decision time, clear against
# realised prices in the local simulator, feed the actual SOC back, repeat.

struct BacktestResult
    periods::DataFrame
    bids::Vector{PeriodBids}
    summary::DataFrame
end

default_start_row(cfg::Config) = max(fc_min_origin(cfg), 7 * cfg.market.periods_per_day) + 1

"""
    run_backtest(df, cfg; start_row, end_row, progress=true) -> BacktestResult
"""
function run_backtest(df::DataFrame, cfg::Config; start_row::Int = default_start_row(cfg),
                      end_row::Int = nrow(df), progress::Bool = true)
    s = cfg.strategy
    L = s.lead_periods
    start_row - L >= fc_min_origin(cfg) || error("start_row too early: need one day of history plus lead time")
    end_row <= nrow(df) || error("end_row beyond data")
    rng = MersenneTwister(cfg.simulation.seed)
    bids = Dict{Int,PeriodBids}()
    outcomes = Dict{Int,PeriodOutcome}()
    soc = initial_soc(cfg.battery)
    t0 = time()

    settle!(t) = begin
        pb = get(bids, t, idle_bids(t, df.timestamp[t]))
        pr = realised_prices(df, t)
        net, res = clear_period(pb, pr, cfg)
        out = settle_period(net, res, soc, pr, cfg; rng = rng)
        outcomes[t] = out
        soc = out.soc_end
    end

    if s.mode == "rolling"
        for τ in (start_row - L):(end_row - L)
            τ >= start_row && settle!(τ)
            t = τ + L
            pending = [bids[k] for k in (τ + 1):(t - 1) if haskey(bids, k)]
            bids[t] = rolling_bids(df, τ, soc, pending, cfg)
            if progress && (t - start_row) % cfg.market.periods_per_day == 0
                @info "bidding" timestamp = df.timestamp[t] soc = round(soc; digits = 1) elapsed_s = round(time() - t0; digits = 1)
            end
        end
        for t in max(start_row, end_row - L + 1):end_row
            settle!(t)
        end
    else
        ppd = cfg.market.periods_per_day
        day_starts = [r for r in start_row:end_row if df.period[r] == 1]
        isempty(day_starts) || first(day_starts) == start_row ||
            @warn "day_ahead mode: periods before the first day boundary are left idle"
        t = start_row
        while t <= end_row
            if df.period[t] == 1
                rows = t:min(end_row, t + ppd - 1)
                # SOC known at the origin is the SOC after the last settled period.
                for pb in day_ahead_bids(df, t - L, soc, rows, cfg)
                    bids[pb.row] = pb
                end
                progress && @info "bidding day" date = Date(df.timestamp[t]) soc = round(soc; digits = 1)
            end
            settle!(t)
            t += 1
        end
    end

    rows = start_row:end_row
    periods = DataFrame(
        timestamp = df.timestamp[rows], period = df.period[rows],
        price_energy = df.price_energy[rows], price_regulation = df.price_regulation[rows],
        price_primary = df.price_primary[rows], price_contingency = df.price_contingency[rows],
        net_mw = [outcomes[t].net_mw for t in rows],
        regulation_mw = [outcomes[t].reserve_mw[:regulation] for t in rows],
        primary_mw = [outcomes[t].reserve_mw[:primary] for t in rows],
        contingency_mw = [outcomes[t].reserve_mw[:contingency] for t in rows],
        soc_start_mwh = [outcomes[t].soc_start for t in rows],
        soc_end_mwh = [outcomes[t].soc_end for t in rows],
        rev_energy = [outcomes[t].revenue[:energy] for t in rows],
        rev_regulation = [outcomes[t].revenue[:regulation] for t in rows],
        rev_primary = [outcomes[t].revenue[:primary] for t in rows],
        rev_contingency = [outcomes[t].revenue[:contingency] for t in rows],
        penalty = [outcomes[t].revenue[:penalty] for t in rows],
        shortfall_mwh = [outcomes[t].shortfall_mwh for t in rows],
        sustain_violation = [outcomes[t].sustain_violation for t in rows],
        throughput_mwh = [outcomes[t].throughput_mwh for t in rows],
    )
    bl = [bids[t] for t in rows if haskey(bids, t)]
    return BacktestResult(periods, bl, summarize(periods, df, rows, cfg))
end

"""
    perfect_foresight(df, rows, cfg) -> (revenue, schedule)

Upper bound: co-optimise the whole window with realised prices (LP, no binaries).
"""
function perfect_foresight(df::DataFrame, rows::UnitRange{Int}, cfg::Config)
    c2 = with_strategy(cfg; use_binary = false, price_impact = 0.0, terminal_value_mode = "hard",
                       terminal_soc_frac = cfg.battery.initial_soc_frac)
    prices = Dict(p => df[rows, PRICE_COLS[p]] for p in PRODUCTS)
    cm = build_coopt(c2, initial_soc(cfg.battery), prices)
    optimize!(cm.model)
    sch = coopt_schedule(cm)
    dt = cfg.market.period_hours
    rev = dt * (sum(prices[:energy] .* (sch.discharge_mw .- sch.charge_mw)) +
                sum(prices[:regulation] .* sch.regulation_mw) + sum(prices[:primary] .* sch.primary_mw) +
                sum(prices[:contingency] .* sch.contingency_mw))
    return rev, sch
end

function summarize(periods::DataFrame, df::DataFrame, rows::UnitRange{Int}, cfg::Config)
    tot(c) = sum(periods[!, c])
    market_rev = tot(:rev_energy) + tot(:rev_regulation) + tot(:rev_primary) + tot(:rev_contingency)
    pf, _ = perfect_foresight(df, rows, cfg)
    cycles = tot(:throughput_mwh) / (2 * cfg.battery.energy_mwh)
    days = length(rows) / cfg.market.periods_per_day
    DataFrame(
        metric = ["days", "net_market_revenue", "revenue_energy", "revenue_regulation", "revenue_primary",
                  "revenue_contingency", "shortfall_penalty", "net_revenue_after_penalty",
                  "degradation_cost_internal", "equivalent_full_cycles", "cycles_per_day",
                  "shortfall_mwh", "sustain_violation_periods", "soc_min_seen_mwh", "soc_max_seen_mwh",
                  "perfect_foresight_revenue", "capture_ratio"],
        value = [days, market_rev, tot(:rev_energy), tot(:rev_regulation), tot(:rev_primary),
                 tot(:rev_contingency), tot(:penalty), market_rev + tot(:penalty),
                 cfg.battery.degradation_cost * tot(:throughput_mwh), cycles, cycles / days,
                 tot(:shortfall_mwh), count(periods.sustain_violation),
                 minimum(periods.soc_end_mwh), maximum(periods.soc_end_mwh),
                 pf, pf > 0 ? market_rev / pf : NaN],
    )
end
