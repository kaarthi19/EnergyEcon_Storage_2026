# Questions for EMA

Draft clarification email for the BESS Bidding Algorithm Hackathon. Contact given in the Call for Participation: EMA_Energy_Storage@ema.gov.sg.

The second half of this file maps each answer to the setting it changes in the code.

---

## Draft email

**Subject:** BESS Bidding Algorithm Hackathon: clarification questions

Dear EMA Energy Storage team,

Thank you for organising the BESS Bidding Algorithm Hackathon. We are preparing a submission and would be grateful for clarification on the points below. Questions 1 to 6 affect the algorithm design the most; if only some can be answered, those are the ones we would value most.

**How the algorithm will be run**

1. **Timing of decisions.** How will EMA call the algorithm: once per trading period, once per day, or once for the whole evaluation period? At each call, what is the latest trading period for which realised prices and schedules are available to the algorithm?
2. **Gate closure.** Will the simulation enforce the 65-minute gate closure, so that the offer for a trading period is fixed before the two preceding periods have cleared? Or may the offer for each period be set with knowledge of the outcome of the period immediately before it?
3. **State of charge between periods.** Will the simulation pass the battery's current state of charge to the algorithm at each call, or must the algorithm track it from its own cleared quantities?
4. **Evaluation period.** Will the algorithm be evaluated on the period covered by the released datasets, or on a later period that participants do not see? If later, will pre-dispatch information for that period be supplied to the algorithm at each decision time?

**Market simulation and revenue**

5. **Effect of our offers on prices.** Will the offers be cleared against historical prices, with the battery as a price taker? Or will EMA re-run market clearing with our offers included, so that they can change prices? This matters most for regulation: the regulation requirement has recently been about 100 MW, the same size as the battery, so a 100 MW regulation offer could not clear in full at the historical price. Is there a limit on the quantity of each product the battery can be awarded?
6. **State-of-charge compliance.** What happens if a cleared schedule would take the battery outside the 5%–95% range, or if cleared reserve or regulation could not be delivered from the energy stored: is the schedule curtailed, is a penalty applied, or is the submission disqualified? For how long must the battery be able to sustain cleared regulation, primary reserve and contingency reserve? RC386 (state of charge in the market clearing engine) and RC390 took effect together with RC383: will the simulation apply their state-of-charge constraints when clearing, and do they require any offer fields beyond those in RC383?
7. **Definition of net market revenue.** Which prices and charges are used?
   - Energy: USEP or a nodal price, and for charging, are any other charges added (for example EMC and PSO fees, the Allocated Regulation Price, the Hourly Energy Uplift Charge, or reserve cost allocation)?
   - Reserves and regulation: is payment the cleared quantity times the class price, and is any reserve provider group or effectiveness factor applied?
8. **Activation of reserves and regulation.** Does the simulation model the energy delivered or absorbed when regulation and reserves are activated? If so, how is the activation determined, and how is that energy settled?
9. **Joint clearing.** When energy, regulation and reserve offers are all in the money and together exceed the battery's capability, how does the simulation choose the awards?
10. **Temporary Price Cap.** Does the simulation apply the Temporary Price Cap to energy, reserve and regulation prices? If so, will EMA provide the cap parameters for the evaluation period?

**Battery assumptions**

11. **Starting and ending state of charge.** What is the state of charge at the start of the evaluation, and is there any requirement or valuation for the state of charge at the end?
12. **Efficiency.** How is the 85% round-trip efficiency applied: split equally between charging and discharging, or entirely on one side? Are the 100 MW limits measured at the grid connection point?
13. **Standing capability data.** What values should we assume for the maximum charge and discharge limits, ramp rates, regulation minimum and maximum, and maximum reserve capability? In particular, may the battery offer reserve above 100 MW while charging, as RC383 section 3.2.1 allows?
14. **Other operating limits.** Are there any limits on cycling, throughput or minimum run times that the algorithm must respect?

**Offer formats**

15. **Energy storage offers.** We plan to follow Market Rules Chapter 6 section 5.2B as set out in RC383: up to five charging pairs and five discharging pairs, prices in ascending order to two decimals, plus the maximum combined transfer limit and ramp rates. Please confirm this, the price limits that apply, and the file format in which EMA wants the offers.
16. **Reserve and regulation offers.** What format should these take: how many price-quantity pairs per product, what price limits, and is regulation a single product covering both raise and lower?
17. **Revising offers.** Can an offer for a period be revised up to gate closure, as with offer variations in the market, or is one offer per period submitted in advance?

**Data**

18. **Dataset contents.** Which period will the datasets cover, at what data cut-off, and what will the pre-dispatch information contain? In particular, will every pre-dispatch run be provided with its issue time, or only the latest run for each period? Are timestamps period-beginning or period-ending, in Singapore time?
19. **External data.** For external data such as weather forecasts:
    - Must forecasts be the values as issued before each trading period, or may later actual values be used for periods before the cut-off?
    - Must external data be bundled with the submission, or may the algorithm download it when EMA runs it?
    - May we use public EMC data, such as the Temporary Price Cap parameters, the registered facility capacity list and vesting data? EMC's website terms do not allow redistribution, so we could not bundle it without permission. Alternatively, will EMA supply these?
    - May history from before the period covered by the released datasets be used, for example to compute a 28-day average at the start of that period?
    - Is openly licensed data that we download in advance and bundle with attribution acceptable (for example CC BY 4.0 archived weather forecasts)?

**Submission**

20. **Runtime environment.** What operating system and hardware will EMA use, is there a time limit per decision or in total, and will the machine have internet access? The Call prefers R or Python. Is a submission in Julia, using only open-source packages and the open-source HiGHS solver, acceptable?
21. **Intellectual property.** Our algorithm builds on our published, MIT-licensed research code. Does the assignment in clause 20 extend to pre-existing code and open-source components included in a submission, or only to material created for the hackathon? May participants describe their own methodology in academic publications afterwards?

Thank you for your help.

Kind regards,
[Name, organisation, lead registrant]

---

## What each answer changes

| Question | Current assumption | Where it is set |
|---|---|---|
| 1, 2 | Rolling, one decision per period; offer for period t fixed after period t−3 settles | `strategy.mode`, `strategy.lead_periods` |
| 3 | SOC supplied at each decision | `generate_bids(df, origin_row, soc_now, cfg)` |
| 4 | Pre-dispatch available up to 48 periods ahead of each decision | `forecast.pd_horizon_periods`, `forecast.method` |
| 5 | Price taker for all products; reserve offers up to 100 MW each | `strategy.price_impact` (energy only), `[market.reserve_cap_mw]` (cap the regulation offer below the requirement) |
| 6 | No violations tolerated; reserves sustainable for 0.5 h; RC386 and RC390 not yet read | `strategy.soc_guard`, `strategy.soc_guard_quantile`, `[market.sustain_hours]`, `simulation.shortfall_penalty` |
| 7 | Energy at one price; reserves at quantity × price; no other charges | `src/simulator.jl` (`settle_period`) |
| 8 | Expected activation of 10% (regulation, each direction), 2% (primary), 1% (contingency); not settled as energy | `[market.deployment]` |
| 9 | Awards maximise offer surplus under RC383 joint capability, per reserve class | `market.reserve_headroom`, `src/simulator.jl` (`clear_period`) |
| 10 | Not modelled; forecasts are not clipped at the caps | `src/forecast.jl` (to add once parameters are known) |
| 11 | Start at 50%; stored energy at the end valued at the forecast price | `battery.initial_soc_frac`, `strategy.terminal_value_mode`, `strategy.terminal_soc_frac` |
| 12 | Split equally: √0.85 each way | `battery.round_trip_eff`, `eta_one_way` in `src/config.jl` |
| 13 | 100 MW limits; 100 MW/min ramps; reserve capped at 100 MW | `[market.reserve_cap_mw]`, `market.ramp_up_mw_per_min`, `market.offered_capacity_mw` |
| 14 | None; an internal degradation cost of $5/MWh limits cycling | `battery.degradation_cost` |
| 15 | RC383 section 5.2B; limits of ±$4,500/MWh | `market.price_floor`, `market.price_cap`, `offers_table` in `src/bids.jl` |
| 16 | Up to 5 incremental tranches per product; regulation symmetric | `market.max_reserve_tranches`, `src/optimizer.jl` |
| 17 | One offer per period, fixed at the lead time | `strategy.lead_periods` |
| 18 | One pre-dispatch value per period; period-beginning timestamps | `[data]`, `src/data.jl` |
| 19 | No external data used yet | `docs/EXTERNAL_DATA_AND_SOFTWARE.md` |
| 20 | Julia 1.10, HiGHS, no internet needed at run time | `Project.toml`, `Manifest.toml` |
| 21 | Not resolved; see the IP note in `README.md` | — |
