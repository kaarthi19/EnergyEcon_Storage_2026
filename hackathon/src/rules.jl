# Battery and market rules, defined once and shared by the optimiser, the SOC guard, the
# simulator and the tests:
#   * the combined transfer limit (power rating capped by the offered capacity),
#   * the joint capability the market enforces when clearing,
#   * the energy needed to sustain raise / lower awards,
#   * the SOC change under expected reserve deployment,
#   * the exact worst case of these over every outcome the market can clear from a set of offers.

"""
    transfer_cap(cfg) / transfer_cap(market, battery)

Combined transfer limit for energy, reserve and regulation (MW): the power rating, capped by
the offered capacity of RC383 §5.2B.2.5 when one is set (`offered_capacity_mw > 0`).
"""
transfer_cap(m::MarketConfig, b::BatteryConfig) =
    m.offered_capacity_mw > 0 ? min(b.power_mw, m.offered_capacity_mw) : b.power_mw
transfer_cap(cfg::Config) = transfer_cap(cfg.market, cfg.battery)

"Offered capacity written into the bids (§5.2B.2.5); equals the transfer limit used everywhere."
offered_capacity(cfg::Config) = transfer_cap(cfg)

"""
Effective MW cap for a reserve product. 0 in the config means the transfer limit.
RC383 §3.2.1 allows ESS reserve capability up to twice the rating when charging (subject to
PSO-approved standing capability data), so caps up to 2x the transfer limit are accepted.
"""
reserve_cap(m::MarketConfig, b::BatteryConfig, p::Symbol) =
    (Pc = transfer_cap(m, b); c = get(m.reserve_cap_mw, p, 0.0); c > 0 ? min(c, 2 * Pc) : Pc)

sustain_hours(m::MarketConfig, p::Symbol) =
    p == :regulation ? m.sustain_regulation : p == :primary ? m.sustain_primary : m.sustain_contingency

"Deployment fractions (MWh/MW/h) of regulation up, regulation down, primary, contingency."
deployment_fractions(m::MarketConfig) = (m.deploy_reg_up, m.deploy_reg_down, m.deploy_primary, m.deploy_contingency)

"""
    deployment_energy_coef(cfg[, fr]) -> Dict

Change of SOC (MWh) per MW of reserve award over one period when the reserves are deployed
at fractions `fr` = (reg up, reg down, primary, contingency); default the expected ones.
"""
function deployment_energy_coef(cfg::Config, fr = deployment_fractions(cfg.market))
    η, dt = eta_one_way(cfg.battery), cfg.market.period_hours
    up, dn, pri, con = fr
    return Dict(:regulation => dt * (η * dn - up / η), :primary => -dt * pri / η, :contingency => -dt * con / η)
end

"""
    deployment_grid_mwh(reg, pri, con, cfg[, fr])

Energy (MWh) delivered to the grid (negative: taken from it) over one period by deploying
the reserve awards at fractions `fr` (default: expected). Paid at the energy price when
`market.pay_deployment_energy` is on, in settlement and in the plan alike. Linear.
"""
function deployment_grid_mwh(reg, pri, con, cfg::Config, fr = deployment_fractions(cfg.market))
    up, dn, fp, fc = fr
    return cfg.market.period_hours * ((up - dn) * reg + fp * pri + fc * con)
end

"""
    raise_mw(reg, pri, con, m) / raise_mw(dict, m)

Raise capability the market reserves on top of the energy transfer. RC383 D.17.2.4A limits
transfer + reserve + regulation <= max capacity *separately for each reserve offer*, so with
`reserve_headroom = "per_class"` primary and contingency share headroom (max); with
"summed" they are stacked (conservative).
"""
raise_mw(reg, pri, con, m::MarketConfig) = reg + (m.reserve_headroom == "per_class" ? max(pri, con) : pri + con)
raise_mw(d::AbstractDict, m::MarketConfig) = raise_mw(d[:regulation], d[:primary], d[:contingency], m)

"""
    raise_needs(net, reg, pri, con, m) -> Tuple

Grid-side energy (MWh) the period's awards must be able to deliver from stored energy: the
net transfer over the period plus each raise award for its sustain time. A raise reserve
deployed while charging first cuts the charge, so the transfer counts net. With per-class
headroom primary and contingency are separate branches; the need is the largest branch
(`max(0, maximum(...))`). Linear in its arguments, so it also takes JuMP expressions.
"""
function raise_needs(net, reg, pri, con, m::MarketConfig)
    base = net * m.period_hours + reg * m.sustain_regulation
    m.reserve_headroom == "per_class" ?
        (base + pri * m.sustain_primary, base + con * m.sustain_contingency) :
        (base + pri * m.sustain_primary + con * m.sustain_contingency,)
end

"Grid-side energy (MWh) the battery must be able to absorb: net charging plus regulation-lower."
lower_need(net, reg, m::MarketConfig) = -net * m.period_hours + reg * m.sustain_regulation

"Energy (MWh, grid side) a set of raise awards must be able to sustain, with no energy transfer."
raise_energy(reg, pri, con, m::MarketConfig) = maximum(raise_needs(0.0, reg, pri, con, m))
raise_energy(d::AbstractDict, m::MarketConfig) = raise_energy(d[:regulation], d[:primary], d[:contingency], m)

"""
    soc_change(ch, dis, reg, pri, con, cfg)

SOC change (MWh) over one period with charge `ch` and discharge `dis` MW and the given
reserve awards deployed at fractions `fr` (default: the expected rates). Linear, so it also
takes JuMP expressions.
"""
function soc_change(ch, dis, reg, pri, con, cfg::Config, fr = deployment_fractions(cfg.market))
    η, dt = eta_one_way(cfg.battery), cfg.market.period_hours
    dep = deployment_energy_coef(cfg, fr)
    return dt * (η * ch - dis / η) + dep[:regulation] * reg + dep[:primary] * pri + dep[:contingency] * con
end

"""
    sustain_violation_mwh(net, res, soc, cfg) -> MWh

How far the awards at the start of a period exceed what the SOC can sustain: raise needs
must be deliverable above the minimum SOC, regulation-lower plus charging absorbable below
the maximum. 0 when compliant.
"""
sustain_violation_mwh(net, res::AbstractDict, soc, cfg::Config) =
    max(0.0, sustain_violation_sides(net, res[:regulation], res[:primary], res[:contingency], soc, cfg)...)

"""
    sustain_violation_sides(net, reg, pri, con, soc, cfg) -> (raise, lower)

The two parts of `sustain_violation_mwh`: MWh by which the raise needs go below the minimum
SOC, and by which charging plus regulation-lower go above the maximum (each >= 0).
"""
function sustain_violation_sides(net, reg, pri, con, soc, cfg::Config)
    b, m = cfg.battery, cfg.market
    η = eta_one_way(b)
    need_up = max(0.0, maximum(raise_needs(net, reg, pri, con, m))) / η
    need_dn = η * max(0.0, lower_need(net, reg, m))
    return max(0.0, soc_min(b) - (soc - need_up)), max(0.0, (soc + need_dn) - soc_max(b))
end

"""
    sustainable_share(net, res, soc, cfg) -> (raise, lower)

Largest shares s_up, s_dn in [0, 1] of the awards that the SOC could sustain under
`sustain_violation_sides`: scaling every raise award (regulation, primary, contingency) by
s_up clears the raise-side violation, scaling regulation by s_dn the lower-side one, with the
energy transfer as awarded. Used to withhold payment for reserve that could not be backed.
"""
function sustainable_share(net, res::AbstractDict, soc, cfg::Config)
    r, p, c = res[:regulation], res[:primary], res[:contingency]
    up(s) = sustain_violation_sides(net, s * r, s * p, s * c, soc, cfg)[1]
    dn(s) = sustain_violation_sides(net, s * r, 0.0, 0.0, soc, cfg)[2]
    # each side's violation is non-decreasing in s, so bisect for the largest compliant share
    function largest(f)
        f(1.0) <= 1e-6 && return 1.0
        f(0.0) > 1e-6 && return 0.0
        a, z = 0.0, 1.0
        for _ in 1:50
            mid = 0.5 * (a + z)
            f(mid) <= 1e-6 ? (a = mid) : (z = mid)
        end
        return a
    end
    return largest(up), largest(dn)
end

# ---------------------------------------------------------------------------------------
# Worst case over the clearing polytope
# ---------------------------------------------------------------------------------------
#
# From offers with energy range [xl, xu] and reserve maxima R = (reg, pri, con) the market
# can clear any x in [xl, xu] and reserves 0 <= r_p <= R_p subject to joint capability:
#   per_class: x + reg + pri <= Pc, x + reg + con <= Pc;   summed: x + reg + pri + con <= Pc;
#   and -x + reg <= Pc.
# For a fixed (x, reg) the best pri/con is a concave piecewise-linear function of the spare
# capacity Pc - x - reg, with kinks where that capacity equals R_pri, R_con or their sum. The
# maximum of a*x + h.r over the polytope is therefore attained at a vertex of the (x, reg)
# polygon cut by those kink lines, and enumerating the pairwise line intersections is exact.

"Best h_pri*pri + h_con*con with spare capacity `c` (h >= 0)."
function _rest_value(c, hp, hc, Rp, Rc, per_class::Bool)
    per_class && return hp * min(Rp, c) + hc * min(Rc, c)
    # summed: one shared capacity, fill the higher-valued product first
    (h1, R1, h2, R2) = hp >= hc ? (hp, Rp, hc, Rc) : (hc, Rc, hp, Rp)
    t1 = min(R1, c)
    return h1 * t1 + h2 * min(R2, c - t1)
end

"""
    clearing_worst(a, h, xl, xu, R, cfg) -> Float64

Exact maximum of `a*x + h[1]*reg + h[2]*pri + h[3]*con` over every outcome the simulator can
clear from offers with energy in [xl, xu] and reserve maxima `R` (see `clear_period`).
"""
function clearing_worst(a::Real, h::NTuple{3,<:Real}, xl::Real, xu::Real, R::NTuple{3,<:Real}, cfg::Config)
    Pc = transfer_cap(cfg)
    per_class = cfg.market.reserve_headroom == "per_class"
    # a negative coefficient is never worst: offer that product at 0 instead
    hr, hp, hc = max.(0.0, h)
    Rr, Rp, Rc = max.(0.0, R)
    # energy outside the transfer limit cannot clear (the simulator clamps its fallback)
    xl, xu = clamp(xl, -Pc, Pc), clamp(xu, -Pc, Pc)
    xl > xu && (xl = xu)
    # lines α*x + β*reg = γ: polygon edges, then the kink lines x + reg = Pc - k
    L = [(1.0, 0.0, xl), (1.0, 0.0, xu), (0.0, 1.0, 0.0), (0.0, 1.0, Rr),
         (-1.0, 1.0, Pc), (1.0, 1.0, Pc), (1.0, 1.0, Pc - Rp), (1.0, 1.0, Pc - Rc), (1.0, 1.0, Pc - Rp - Rc)]
    tol = 1e-9 * max(1.0, Pc)
    best = -Inf
    for i in 1:length(L), j in (i + 1):length(L)
        a1, b1, c1 = L[i]; a2, b2, c2 = L[j]
        det = a1 * b2 - a2 * b1
        abs(det) < 1e-12 && continue
        x = (c1 * b2 - c2 * b1) / det
        g = (a1 * c2 - a2 * c1) / det
        (xl - tol <= x <= xu + tol && -tol <= g <= Rr + tol && g <= Pc + x + tol && g <= Pc - x + tol) || continue
        x = clamp(x, xl, xu)
        g = clamp(g, 0.0, max(0.0, min(Rr, Pc + x, Pc - x)))
        best = max(best, a * x + hr * g + _rest_value(max(0.0, Pc - x - g), hp, hc, Rp, Rc, per_class))
    end
    return best
end

"Linear coefficients (on x, (reg, pri, con)) of a function that is linear in its 4 arguments."
function _coefs(f)
    c0 = f(0.0, 0.0, 0.0, 0.0)
    return f(1.0, 0.0, 0.0, 0.0) - c0, (f(0.0, 1.0, 0.0, 0.0) - c0, f(0.0, 0.0, 1.0, 0.0) - c0, f(0.0, 0.0, 0.0, 1.0) - c0)
end

"""
    worst_case_needs(xl, xu, R, cfg) -> NamedTuple

Exact worst cases over every outcome the market can clear from offers with energy range
[xl, xu] and reserve maxima R = (reg, pri, con), using the rules above:
  raise : largest raise need (grid-side MWh, `raise_needs`); `raise_branches` per branch
  lower : largest lower need (grid-side MWh, `lower_need`)
  drain : largest SOC fall under expected deployment (MWh, `soc_change`)
  fill  : largest SOC rise under expected deployment (MWh)
"""
function worst_case_needs(xl::Real, xu::Real, R::NTuple{3,<:Real}, cfg::Config)
    m = cfg.market
    nb = length(raise_needs(0.0, 0.0, 0.0, 0.0, m))
    branches = ntuple(nb) do i
        a, h = _coefs((x, r1, r2, r3) -> raise_needs(x, r1, r2, r3, m)[i])
        clearing_worst(a, h, xl, xu, R, cfg)
    end
    a, h = _coefs((x, r1, r2, r3) -> lower_need(x, r1, m))
    lower = clearing_worst(a, h, xl, xu, R, cfg)
    # soc_change is linear on each side of x = 0 (charging and discharging efficiencies)
    ach, hch = _coefs((x, r1, r2, r3) -> soc_change(-x, 0.0, r1, r2, r3, cfg))   # x <= 0
    ads, hds = _coefs((x, r1, r2, r3) -> soc_change(0.0, x, r1, r2, r3, cfg))    # x >= 0
    drain, fill = -Inf, -Inf
    if xl <= 0
        drain = max(drain, clearing_worst(-ach, .-hch, xl, min(xu, 0.0), R, cfg))
        fill = max(fill, clearing_worst(ach, hch, xl, min(xu, 0.0), R, cfg))
    end
    if xu >= 0
        drain = max(drain, clearing_worst(-ads, .-hds, max(xl, 0.0), xu, R, cfg))
        fill = max(fill, clearing_worst(ads, hds, max(xl, 0.0), xu, R, cfg))
    end
    return (raise = maximum(branches), raise_branches = branches, lower = lower, drain = drain, fill = fill)
end
