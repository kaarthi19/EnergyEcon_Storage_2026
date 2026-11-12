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

Rolling mode bids each period at its gate closure τ = t - lead_periods, after τ has settled,
with the offers for τ+1 .. t-1 still open; day_ahead mode bids each trading day at the gate
closure of its first period, the same way. The summary reports `net_market_revenue` (as
settled), `compliance_adjusted_revenue` (reserve payment withheld for awards the SOC could
not sustain, shortfall penalty applied), the same plus the value of the SOC change, and the
perfect-foresight benchmarks (`perfect_foresight`). For ranking runs use the strict metrics:
`strict_compliant_revenue` withholds the whole reserve payment of every period with a
sustain violation (and applies the shortfall penalty), and `compliant` is 1 only for a run
with no sustain violation, no shortfall, no clearing fallback and every offer valid.
"""
function run_backtest(df::DataFrame, cfg::Config; start_row::Int = default_start_row(cfg),
                      end_row::Int = nrow(df), progress::Bool = true)
    s = cfg.strategy
    L = s.lead_periods
    start_row - L - s.info_lag_periods >= fc_min_origin(cfg) ||
        error("start_row too early: need one day of history plus lead time (and info_lag_periods)")
    end_row <= nrow(df) || error("end_row beyond data")
    rng = MersenneTwister(cfg.simulation.seed)
    bids = Dict{Int,PeriodBids}()
    outcomes = Dict{Int,PeriodOutcome}()
    fellback = Dict{Int,Bool}()          # clearing fell back to the energy base (malformed offer)
    soc = initial_soc(cfg.battery)
    t0 = time()

    settle!(t) = begin
        pb = get(bids, t, idle_bids(t, df.timestamp[t]))
        pr = realised_prices(df, t)
        n0 = CLEAR_FALLBACKS[]
        net, res = clear_period(pb, pr, cfg)
        fellback[t] = CLEAR_FALLBACKS[] > n0
        out = settle_period(net, res, soc, pr, cfg; rng = rng)
        outcomes[t] = out
        soc = out.soc_end
    end

    if s.mode == "rolling"
        for τ in (start_row - L):(end_row - L)
            τ >= start_row && settle!(τ)
            t = τ + L
            pending = [get(bids, k, idle_bids(k, df.timestamp[k])) for k in (τ + 1):(t - 1)]
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
        # A day's offers are built at its gate closure τ = day start - L, with the SOC known
        # then; the previous day's offers for τ+1 .. day start - 1 are still open.
        for τ in (start_row - L):end_row
            τ >= start_row && settle!(τ)
            t = τ + L
            (start_row <= t <= end_row && df.period[t] == 1) || continue
            rows = t:min(end_row, t + ppd - 1)
            pending = [get(bids, k, idle_bids(k, df.timestamp[k])) for k in (τ + 1):(t - 1)]
            for pb in day_ahead_bids(df, τ, soc, pending, rows, cfg)
                bids[pb.row] = pb
            end
            progress && @info "bidding day" date = Date(df.timestamp[t]) soc = round(soc; digits = 1)
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
        sustain_violation_mwh = [outcomes[t].sustain_violation_mwh for t in rows],
        reserve_withheld = [outcomes[t].reserve_withheld for t in rows],
        throughput_mwh = [outcomes[t].throughput_mwh for t in rows],
        clearing_fallback = [fellback[t] for t in rows],
        bid_valid = [!haskey(bids, t) || isempty(validate_bids(bids[t], cfg)) for t in rows],
    )
    bl = [bids[t] for t in rows if haskey(bids, t)]
    return BacktestResult(periods, bl, summarize(periods, df, rows, cfg))
end

"""
    perfect_foresight(df, rows, cfg; end_soc_ge_start=false) -> (revenue, schedule)

Perfect-foresight benchmark: co-optimise the whole window with the realised prices (LP)
from the initial SOC, with no degradation cost and the simulator's rules (joint capability, award caps,
expected deployment, and the net-transfer sustainability rule `settle_period` checks, i.e.
`sustain_charge_credit = true`); `revenue` is the market revenue of that schedule, as
settled (deployment energy included when `market.pay_deployment_energy`).

With the end SOC free (the default) this is an upper bound on `net_market_revenue` of any
backtest over `rows` that has no sustain violation and no shortfall under expected
deployment: such a run's dispatch is a feasible point of this LP with the same revenue.
(Stochastic deployment, violations or shortfalls take a run outside the LP, so the bound
does not apply to them.) `end_soc_ge_start = true` adds SOC at the end >= the start SOC,
the benchmark for a strategy that must not run the battery down.
"""
function perfect_foresight(df::DataFrame, rows::UnitRange{Int}, cfg::Config; end_soc_ge_start::Bool = false)
    b = cfg.battery
    bat = BatteryConfig(power_mw = b.power_mw, energy_mwh = b.energy_mwh, soc_min_frac = b.soc_min_frac,
                        soc_max_frac = b.soc_max_frac, round_trip_eff = b.round_trip_eff,
                        initial_soc_frac = b.initial_soc_frac, degradation_cost = 0.0)
    # "hard" with the target at the SOC floor leaves the end SOC free (and adds no terminal value)
    st = with_strategy(cfg; use_binary = false, price_impact = 0.0, terminal_value_mode = "hard",
                       terminal_soc_frac = end_soc_ge_start ? b.initial_soc_frac : b.soc_min_frac,
                       sustain_charge_credit = true).strategy
    # the simulator never awards more than award_cap: the benchmark's reserve bounds too
    m = cfg.market
    caps = Dict{Symbol,Float64}(p => (a = award_cap(cfg, p); isfinite(a) ? min(a, reserve_cap(m, b, p)) :
                                      get(m.reserve_cap_mw, p, 0.0)) for p in RESERVES)
    mk = MarketConfig(; (f => getfield(m, f) for f in fieldnames(MarketConfig) if f != :reserve_cap_mw)...,
                      reserve_cap_mw = caps)
    c2 = Config(bat, mk, st, cfg.forecast, cfg.simulation, cfg.data)
    prices = Dict(p => Vector{Float64}(df[rows, PRICE_COLS[p]]) for p in PRODUCTS)
    cm = build_coopt(c2, initial_soc(b), prices)
    _optimize_checked!(cm)
    # no degradation, no terminal value: the objective is the market revenue
    return objective_value(cm.model), coopt_schedule(cm)
end

function summarize(periods::DataFrame, df::DataFrame, rows::UnitRange{Int}, cfg::Config)
    tot(c) = sum(periods[!, c])
    market_rev = tot(:rev_energy) + tot(:rev_regulation) + tot(:rev_primary) + tot(:rev_contingency)
    # Compliance-adjusted: no payment for reserve the SOC could not sustain, penalty applied.
    adjusted = market_rev - tot(:reserve_withheld) + tot(:penalty)
    # Strict: a period whose awards the SOC could not sustain earns no reserve payment at all
    # (the proportional withholding above still ranked a non-compliant guard first).
    v = periods.sustain_violation
    strict = market_rev - sum(periods.rev_regulation[v] .+ periods.rev_primary[v] .+ periods.rev_contingency[v];
                              init = 0.0) + tot(:penalty)
    compliant = !any(v) && tot(:shortfall_mwh) <= 1e-6 && !any(periods.clearing_fallback) && all(periods.bid_valid)
    # Value the SOC change over the window at the median realised energy price, net of
    # discharge losses, so runs that end with different stored energy compare fairly.
    soc0, soc1 = periods.soc_start_mwh[1], periods.soc_end_mwh[end]
    soc_value = (soc1 - soc0) * max(0.0, median(periods.price_energy)) * eta_one_way(cfg.battery)
    pf, _ = perfect_foresight(df, rows, cfg)
    pf_ge, _ = perfect_foresight(df, rows, cfg; end_soc_ge_start = true)
    cycles = tot(:throughput_mwh) / (2 * cfg.battery.energy_mwh)
    days = length(rows) / cfg.market.periods_per_day
    m = [
        "days" => days,
        "net_market_revenue" => market_rev,
        "revenue_energy" => tot(:rev_energy),
        "revenue_regulation" => tot(:rev_regulation),
        "revenue_primary" => tot(:rev_primary),
        "revenue_contingency" => tot(:rev_contingency),
        "shortfall_penalty" => tot(:penalty),
        "net_revenue_after_penalty" => market_rev + tot(:penalty),
        "reserve_payment_withheld" => tot(:reserve_withheld),
        "compliance_adjusted_revenue" => adjusted,
        "strict_compliant_revenue" => strict,
        "compliant" => compliant,
        "start_soc_mwh" => soc0,
        "end_soc_mwh" => soc1,
        "soc_change_value" => soc_value,
        "compliance_adjusted_revenue_incl_soc_value" => adjusted + soc_value,
        "degradation_cost_internal" => cfg.battery.degradation_cost * tot(:throughput_mwh),
        "equivalent_full_cycles" => cycles,
        "cycles_per_day" => cycles / days,
        "shortfall_mwh" => tot(:shortfall_mwh),
        "sustain_violation_periods" => count(periods.sustain_violation),
        "max_sustain_violation_mwh" => maximum(periods.sustain_violation_mwh),
        "clearing_fallback_periods" => count(periods.clearing_fallback),
        "invalid_bid_periods" => count(!, periods.bid_valid),
        "soc_min_seen_mwh" => minimum(periods.soc_end_mwh),
        "soc_max_seen_mwh" => maximum(periods.soc_end_mwh),
        "perfect_foresight_bound" => pf,
        "perfect_foresight_end_soc_ge_start" => pf_ge,
        "capture_ratio" => pf > 0 ? market_rev / pf : NaN,
    ]
    return DataFrame(metric = first.(m), value = Float64.(last.(m)))
end
