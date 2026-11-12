# Market Rules Notes: Energy Storage in the SWEM

Notes from reading the EMC rule change papers that govern energy storage offers and market clearing. Researched on 12 Nov 2026. The papers themselves are EMC documents and are not redistributed here; each entry gives the URL.

**Headline:** since 12 Aug 2025 the market clearing engine (MCE) models state of charge (SoC). This package was first built on RC383 alone, which left SoC to the participant. RC386 and RC390 change that, and the package does not yet model it. See "Changes the package needs" below.

## Papers read

| Paper | Status | Link |
|---|---|---|
| RC386 Incorporation of State-of-Charge in MCE modelling of ESS (EMC/RCP/139/2024/RC386) | Approved by EMA (EMA considered 26 Jul 2024); effective 12 Aug 2025 | https://www.home.emcsg.com/-/media/Market-Administration/Rule-Changes/Proposals-Considered-by-RCP---Sorted-by-Effective-Date/2025/EMC386-EMA-CY_EffDate.pdf |
| RC390 Market Clearing and Offers for Energy Storage Systems (EMC/RCP/146/2025/RC390) | Approved by EMA (EMA considered 5 Jun 2025); effective 12 Aug 2025 | https://www.home.emcsg.com/-/media/Market-Administration/Rule-Changes/Proposals-Considered-by-RCP---Sorted-by-Effective-Date/2025/EMC390-EMA-CY_EffDate.pdf |
| RC387 Enhancement to Fallback Mechanisms for StartGeneration and PriorScheduledPurchase (EMC/RCP/141/2024/RC387) | Approved by EMA (EMA considered 26 Jul 2024); effective 12 Aug 2025 | https://www.home.emcsg.com/-/media/Market-Administration/Rule-Changes/Proposals-Considered-by-RCP---Sorted-by-Effective-Date/2025/EMC387-EMA-CY_EffDate.pdf |
| Singapore Electricity Market Rules, 1 Jan 2022 edition (pre-ESS text, used only for Appendix 5A reserve standards, Ch6 10.4 gate closure and Ch5 App 5D AFPS) | EMA edition dated 1 Jan 2022, hosted by a third-party site, not EMC or EMA | https://policy.thinkbluedata.com/sites/default/files/Singapore%20Electricity%20Market%20Rules%20%282022%20Ed.%29.pdf |

## What each paper says

### RC386 Incorporation of State-of-Charge in MCE modelling of ESS (EMC/RCP/139/2024/RC386)

Approved by EMA (EMA considered 26 Jul 2024); effective 12 Aug 2025. Rule refs: App 6D Sec A-D, App 6E E.1A, App 6G G.3, Chap 8. Annex 1 footnote says the text was still under legal review and assumed RC383; RC390 later renumbers and corrects it.

Chosen design (Option 2, sec 3.2): the ESS sends SoC to PSO by RTU every few seconds, and PSO passes it to EMC in the Network Status File 10 minutes before each period. The MCE then constrains ESS schedules by SoC. Option 1 (participant self-management with ESS-specific gate-closure exemptions) and Option 3 (the ESS operator sends SoC to EMC) were rejected.
(a) Offers: there is NO new offer field. SoC is not offered. The new SoC-related parameters are standing capability data (App 6E E.1A.1.12 MaxSoC, E.1A.1.13 MinSoC, E.1A.1.14 discharging efficiency, E.1A.1.15 charging efficiency; MaxCapacity MWh is E.1A.1.11 from RC383). D.15.4.3-4.5 split the schedule into EnergyStorageDischarging and EnergyStorageCharging, with EnergyStorageTransfer = Dis - Ch. Sec 5.4: because the MCE enforces SoC, offer quantities 'can sum to the maximum MWh storage', and the MCE constrains the ESS off in later periods.
(b) MCE use: D.12A.1 sets ExpectedStartSoC = StartSoC - PriorScheduledGeneration*(RampingTime/60)/(DischargingEff*MaxCapacity) when discharging, or StartSoC - PriorScheduledGeneration*(RampingTime/60)*ChargingEff/MaxCapacity when charging or idle, capped to [MinSoC, MaxSoC]. StartSoC comes from PSO (G.3.4). PriorScheduledGeneration is the prior period's RTS schedule. The body text (sec 4.1) uses 1/6 h, i.e. 10 minutes.
Fallbacks (D.12A.1.1-1.4): EndSoC of the RTS, then the STS, then the PDS, then MinSoC.
Limits: D.12A.5 SoCDischargeLimitMWh = (ExpectedStartSoC - MinSoC)*MaxCapacity; D.12A.6 SoCChargeLimitMWh = (MaxSoC - ExpectedStartSoC)*MaxCapacity.
Constraints (D.19A; MW form, RHS = limit/(time/3600)):
- D.19A.1: [Dis + Reg + Pri*(PriResSustainTime/DispatchPeriod) + Con*((DispatchPeriod - PriResSustainTime)/DispatchPeriod)]/DischargingEff - Ch*ChargingEff - ExcessDischarging <= SoCDischargeLimitMWh/(DispatchPeriod/3600).
- D.19A.2: (Dis + Reg + Pri)/DischargingEff - Ch*ChargingEff - ExcessPrimaryDischarging <= SoCDischargeLimitMWh/(PriResSustainTime/3600).
- D.19A.3: (Dis + Reg + Con)/DischargingEff - Ch*ChargingEff - ExcessContingencyDischarging <= SoCDischargeLimitMWh/(ConResSustainTime/3600).
- D.19A.4: (Ch + Reg)*ChargingEff - Dis/DischargingEff - ExcessCharging <= SoCChargeLimitMWh/(DispatchPeriod/3600). Reserves are excluded from this constraint (footnote 13).
Stated assumptions (sec 4.2): energy is held for 30 minutes; regulation up and down for 30 minutes; primary for 10 minutes from the period start; contingency for 30 minutes, and for 20 minutes after 10 minutes of primary.
The constraints are soft: D.21.5 and D.21.5.6 add FacilitySoCViolation = sum of the four Excess variables to the violation group.
Forecast runs (D.25.1.14, sec 4.3): EndSoC = ExpectedStartSoC - Dis/(DischargingEff*MaxCap) + Ch*ChargingEff/MaxCap, carried to the next period. It ignores reserve and regulation effects (sec 4.3; Senoko comment 4). Auxiliary load is not modelled.
(c) Reserve and regulation: PriResSustainTime is set per App 5A A.2.2 and ConResSustainTime per A.2.4 (D.3). Reserve classes are indexed primary and contingency (D.2).
(d) Gate closure: no ESS gate-closure exemption was adopted. The Option 1 exemptions in sec 3.1.2(2) were not recommended. Sec 5.2 says that with SoC in the MCE the operator 'never has to amend energy offers' for SoC.
(e) Compliance: sec 3.1 lists the existing deterrents: AFPS for energy deviations, no payment for non-delivered ancillary services, and downgrading of the Reserve Provider Group. No new ESS penalty is introduced.

### RC390 Market Clearing and Offers for Energy Storage Systems (EMC/RCP/146/2025/RC390)

Approved by EMA (EMA considered 5 Jun 2025); effective 12 Aug 2025. Rule refs: Chap 5, Chap 6, App 6B, App 6D, App 6J, Chap 8, and the Market Operations Market Manual (Standing Offers ... Standing Capability Data).

Completes and corrects RC383 and RC386, and adds the market-manual offer format.
(a) Offers:
- Ch6 5.2B.2.4: pairs 1-5 are charging, with quantity <= 0.0 MW; pairs 6-10 are discharging, with quantity >= 0.0 MW.
- 5.2B.2.6: the ramp rates are the allowable increase and decrease in output or withdrawal.
- 5.2B.6.3: the sum of discharge quantities must also be <= the offered maximum combined transfer limit (5.2B.2.5).
- 5.2B.7: that limit is in MW to 1 dp and >= 0.
- 5.2B.8: ramp rates in MW/min to 1 dp, >= 0, and <= standing-capability maximums.
- Market Manual Table 2 (standing offer) and Table 6 (offer variation): Type 'EGO'; fields Participant, Type, Unit, Day (Mon-Sun) or Date, Period 1-48, Ramp Up, Ramp Down, Capacity (= combined transfer limit), Band price/quantity pairs, and an optional External Reference. Prices run from -99,999,999,999.99 to 99,999,999,999.99 $/MWh; quantities in MW to 1 dp, and may be negative.
- Footnote to Table 2: a standing offer 'requires 10 price-quantity pairs'. A variation requires 1 to 10.
- New Rule 3A: total charging must be <= both the standing max charge limit and the offered Capacity, and the same applies to discharging.
- Amended Rule 5: prices must increase across pairs with non-zero quantity, and a 0.0 MW pair carries $0.00.
- Rule 8: prices lie within EnergyPriceMax and EnergyPriceMin.
- Mandatory offer variation (MM 3.5, footnotes 14-15) applies to the charge quantity against expected charging capability.
- App 6B B.6.3: initial SoC from PSO is an MCE input. B.2.1: energy storage offers are MCE inputs.
- Ch8: 'offer variation' now includes energy storage offers.
(b) MCE:
- D.12A.1-D.12A.2: StartGeneration fallbacks for ESS follow RC387 (RTS, then STS, then PDS, then zero).
- D.12A.5: ExpectedStartGeneration is ramp-limited toward PriorScheduledGeneration, using the previous period's offered ramp rates.
- D.12A.6-7: EnergyTransferEndMax/Min = ExpectedStartGeneration +/- ramp/60*RemainingTime.
- SoC section renumbered to D.12B. D.12B.1: StartSoC (as in RC386), fallback RTS EndSoC, then STS, then PDS, then MinSoC. D.12B.2: the first PDS period uses STS EndSoC for the preceding period, then the PDS, then MinSoC. D.12B.3: the market outlook uses PDS EndSoC, then MinSoC. D.12B.4: later periods chain EndSoC. New D.12B.5 caps all of these to [MinSoC, MaxSoC]. D.12B.6-7 are the limits.
- D.19A.1-4 are restated with Regulation_l(es).
- D.25.1.14 EndSoC is corrected to include the DispatchPeriod/3600 factor: EndSoC = ExpectedStartSoC - Dis*(DP/3600)/(DischargingEff*MaxCap) + Ch*ChargingEff*(DP/3600)/MaxCap.
- D.17.1.1A: ESS risk constraint uses EnergyStorageTransfer.
- D.20.1.1: ESS is included in generic security constraints.
- D.24.1: MEP is calculated at all ESS nodes, not only when the ESS is discharging.
- App 6J J.3: ExcessUpRamp_es, ExcessDownRamp_es, ExcessDischarging, ExcessPrimaryDischarging, ExcessContingencyDischarging and ExcessCharging are added to the violation-penalty table. In the extracted layout the listed block is 20*VoLL / 10,000 MW; how the rows group is not fully clear.
- D.21.5B: the ESS facility violation includes SoC and line flow.
(c) Reserve and regulation:
- Ch6 5.3.2.6, 5.3.8, 5.3.9: ESS reserve offers carry no reserve proportion (MM Rule 11 and the footnotes to Tables 3 and 7). Reserve offers keep 1-5 pairs, prices >= 0, classes PRI and CON.
- D.13A.2: an ESS regulation offer is used if a valid energy storage offer exists. The RegulationMin offer-sum eligibility check is removed for ESS.
- D.18.1.7: corrected so that Transfer - Reg + DeficitRegGen >= RegulationMin.
- MM 4.5.1: reserve proportion and low/medium/high load reserve fields do not apply to ESS.
- MM 4.5.10: ESS standing data fields are MAXIMUM CAPACITY (MWh), MAXIMUM CHARGE LIMIT and MAXIMUM DISCHARGE LIMIT (MW), CHARGING and DISCHARGING EFFICIENCY, and MAXIMUM and MINIMUM STATE OF CHARGE (%).
(d) Gate closure: not amended. Ch6 10.4 is absent from the rule refs.
(e) Compliance: Ch5 11.2.1.1 extends the synchronisation-approval obligation to energy storage offers. No new ESS penalty.

### RC387 Enhancement to Fallback Mechanisms for StartGeneration and PriorScheduledPurchase (EMC/RCP/141/2024/RC387)

Approved by EMA (EMA considered 26 Jul 2024); effective 12 Aug 2025. Rule refs: Ch6 7.5.1.5, 7.6.1A; App 6D D.3, D.8.3, D.8.4, D.12.1, D.12.2, D.12.10-D.12.13.

Not ESS-specific; read sections 1-3.3 and the Annex headings.
Schedule timing (Table 1): the RTS (dispatch run) runs 5 minutes before the period. The STS (look-ahead run, 12 periods) starts 34 minutes before its first forecast period. The PDS (day-ahead run, 24-72 periods) runs every 2 hours, starting 135 minutes before its first period. The MOS (week-ahead run, 288 periods) runs daily at 2am.
StartGeneration comes from the PSO Network Status File 10 minutes before each period. ExpectedStartGeneration then drives the ramp limits EndMax/Min = ExpectedStart +/- ramp x 30 min.
Fallbacks: for RTS and the first STS period, PSO value, then prior RTS, then latest STS (medium load), then latest PDS, then 0. For the first PDS period, latest STS, then latest PDS, then 0. This removes the four leading PDS periods. The same pattern applies to PriorScheduledPurchase for LRFs.
Footnote 12: an ESS may have RegulationMin = 0.
For the five questions: (a) no offer change; (b) it defines the fallback chain RC390 reuses for ESS StartGeneration and StartSoC; (c), (d), (e) nothing.

### Singapore Electricity Market Rules, 1 Jan 2022 edition (pre-ESS text, used only for Appendix 5A reserve standards, Ch6 10.4 gate closure and Ch5 App 5D AFPS)

EMA edition dated 1 Jan 2022, hosted by a third-party site, not EMC or EMA. It is superseded in the parts the 2025 papers amend. The clauses used here were not in any of those papers' rule references, so they are probably unchanged, but this is not verified against the current consolidated rules.

(c) App 5A:
- A.2.1: two reserve classes, primary and contingency.
- A.2.2: primary reserve must reach its scheduled MW within 9 s of a contingency, automatically, and hold it until 10 minutes from the trigger. This gives PriResSustainTime = 600 s.
- A.2.4: contingency reserve must reach its scheduled MW within 10 minutes of instruction and hold it for not less than 30 minutes. This gives ConResSustainTime = 1800 s.
- A.1.5: regulation response period is 60 s.
(d) Ch6 10.4.1: no offer variation or revised standing offer within 65 minutes before the period. Exceptions: (a) ramp profiles around synchronisation; (b) revised capability for the three periods after a forced outage or failure to synchronise; (c)-(e) helping resolve surplus or shortfall advisories or high-risk states; (f) LRF outages. 10.4.1.2: the price must be unchanged except for additional quantities. 10.4.3: EMC reports every gate-closure revision to the MSCP.
(e) Ch5 3.7.3 and App 5D (AFPS): a GRF deviates if 1/2 x |EndScheduledQty - EndGeneration| x 1/2 h > 2.5 MWh, i.e. more than 10 MW at end of period. The penalty, as extracted, is Max{2 x (USEP + HEUC) x [1/2 x |dev| x 1/2 h - 2.5 MWh], $5000}. 3.7.4: the MSCP keeps its other sanction powers. Ch5 4.6: Reserve Provider Groups based on performance.

## Settled by the rule text

- **The MCE models SoC since 12 Aug 2025. Our "MCE ignores SoC" assumption is wrong.** Sources: RC386 Annex 1 D.12A and D.19A, and RC390 D.12B and D.19A (both effective 12 Aug 2025). Energy, regulation and reserve schedules for an ESS are limited by SoCDischargeLimitMWh = (ExpectedStartSoC - MinSoC) x MaxCapacity and SoCChargeLimitMWh = (MaxSoC - ExpectedStartSoC) x MaxCapacity (RC390 D.12B.6-7).
- **Who supplies SoC: PSO, not the participant.** The ESS sends SoC to PSO by RTU, and PSO passes it to EMC in the Network Status File 10 minutes before each period (RC386 sec 3.2; App 6G G.3.4; RC390 App 6B B.6.3). There is no SoC field in the energy storage offer (RC390 Market Manual Tables 2 and 6).
- **How the starting SoC is estimated (RC390 D.12B.1, D.12B.5).** ExpectedStartSoC = StartSoC - PriorScheduledGeneration x (RampingTime/60) / (DischargingEff x MaxCap) when discharging. When charging or idle it is StartSoC - PriorScheduledGeneration x (RampingTime/60) x ChargingEff / MaxCap. The result is capped to [MinSoC, MaxSoC].
- **Fallbacks for the starting SoC:**
  - RTS and first STS period (D.12B.1.1-1.4): EndSoC of the RTS, then the STS (normal load), then the PDS, then MinSoC.
  - First PDS period (D.12B.2): EndSoC of the latest STS for the preceding period, then the PDS, then MinSoC.
  - Market outlook (D.12B.3): PDS EndSoC, then MinSoC.
  - Later forecast periods (D.12B.4): chain from the previous period's EndSoC.
- **Across periods and in forecast runs (RC390 D.25.1.14).** EndSoC = ExpectedStartSoC - Dis x 0.5 h / (DischargingEff x MaxCap) + Ch x ChargingEff x 0.5 h / MaxCap. It ignores reserve and regulation deployment (RC386 sec 4.3).
- **The four SoC constraints (RC390 D.19A.1-4; times from App 5A A.2.2 and A.2.4).**
  - D.19A.1, energy discharge: Dis + Reg + Pri x (1/3) + Con x (2/3), all divided by DischargingEff, minus Ch x ChargingEff, must be <= SoCDischargeLimitMWh / 0.5 h. In energy terms: energy and regulation for 30 min, primary for 10 min, then contingency for 20 min.
  - D.19A.2, primary window: (Dis + Reg + Pri) / DischargingEff - Ch x ChargingEff <= SoCDischargeLimitMWh / (1/6 h).
  - D.19A.3, contingency: (Dis + Reg + Con) / DischargingEff - Ch x ChargingEff <= SoCDischargeLimitMWh / 0.5 h.
  - D.19A.4, energy charge: (Ch + Reg) x ChargingEff - Dis / DischargingEff <= SoCChargeLimitMWh / 0.5 h. Reserves are not counted here (RC386 footnote 13).
  - Charging in the same period counts as a credit in all three discharge-side constraints.
  - All four are soft: their violation variables go into FacilitySoCViolation, with penalties in App 6J J.3 (RC390 D.21.5B.4, J.3).
- **Sustain durations to configure.** Primary reserve: reach scheduled MW within 9 s and hold it until 10 min after the trigger (App 5A A.2.2), so PriResSustainTime = 600 s. Contingency reserve: reach it within 10 min and hold it for at least 30 min (App 5A A.2.4), so ConResSustainTime = 1800 s. Regulation is treated as sustained for the whole 30-minute period, up and down (RC386 sec 4.2; D.19A).
- **Standing capability data for an ESS (RC383 E.1A.1.1-11, RC386 E.1A.1.12-15, RC390 Market Manual 4.5.10).** Max charge limit and max discharge limit (MW), max ramp up and down (MW/min), max reserve per class, max combined generation plus reserve per class, max regulation, RegulationMax and RegulationMin, MaxCapacity (MWh), MaxSoC and MinSoC (%), and charging and discharging efficiency. MinSoC and MaxSoC are "provided by the ESS operator and approved by the PSO" (RC386 sec 4.1).
- **Energy storage offer format.**
  - Price-quantity pairs: 1-5 are charging (quantity <= 0), 6-10 are discharging (quantity >= 0) (RC390 Ch6 5.2B.2.4).
  - Standing offers need 10 pairs; variations need 1-10 (Market Manual Table 2 note 7, Table 6).
  - Prices must increase across non-zero pairs, and a 0.0 MW pair is priced $0.00 (5.2B.4; Market Manual Rule 5). Prices are $/MWh to 2 dp, within EnergyPriceMin/Max (5.2B.3; Rule 8). Quantities are MW to 1 dp.
  - Offered "Capacity" (combined transfer limit) is MW to 1 dp and >= 0 (5.2B.7).
  - Ramp rates are MW/min to 1 dp, >= 0, and <= the standing values (5.2B.8; Rule 2).
  - Total charge must be <= the max charge limit and <= Capacity; the same holds for total discharge (5.2B.5, 5.2B.6.3; Rule 3A).
  - The type code is EGO.
- **Reserve and regulation offers for an ESS.**
  - Reserve offers carry no reserve proportion (Ch6 5.3.2.6, 5.3.8, 5.3.9; Market Manual Rule 11). They keep 1-5 pairs, prices >= 0, classes PRI and CON (Market Manual Tables 3 and 7).
  - An ESS regulation offer is used whenever a valid energy storage offer exists; there is no RegulationMin offer-sum check (RC390 D.13A.2).
  - Regulation range: Transfer + Reg <= RegulationMax and Transfer - Reg >= RegulationMin (RC383 D.18.1.6; RC390 D.18.1.7).
  - Joint headroom: Transfer + RawReserve + Reg <= ReserveGenerationMax, applied separately per reserve offer, i.e. per class (RC383 D.17.2.4A).
- **Prices: the MEP is calculated at every ESS node, whether charging or discharging (RC390 D.24.1).**
- **Gate closure is not changed for ESS.** None of RC383, RC386, RC387 or RC390 lists Ch6 10.4 in its rule references, and RC386 rejected Option 1's ESS-specific exemptions. The 65-minute gate closure and the existing exceptions in 10.4.1.1 therefore apply. Prices may not change inside gate closure except for added quantities (10.4.1.2), and every revision inside it is reported to the MSCP (10.4.3) (2022-edition text).
- **Compliance.** The existing tools are AFPS for energy deviations, no payment for undelivered ancillary services, and Reserve Provider Group downgrade (RC386 sec 3.1; RC383 comment responses). The AFPS threshold is a 10 MW deviation at end of period (2.5 MWh test) (2022 App 5D D.3.1). No new ESS-specific penalty was introduced in any of the four papers.

## Changes the package needs

1. **Make the simulator's market clearing apply SoC (RC390 D.12B and D.19A).** Before clearing each period:
   - Take the SoC at T-10 min.
   - Compute ExpectedStartSoC using the prior period's schedule over 10 minutes, with the D.12B.1 formula and the [MinSoC, MaxSoC] cap.
   - Compute both MWh limits.
   - Clear energy, regulation, primary and contingency subject to D.19A.1-4. Model them as soft constraints with a large penalty (App 6J).

   Today the package assumes the market accepts any offer and leaves SoC feasibility to us. That overstates how much the MCE will award us and misstates where the risk sits.
2. **Fix the sustain settings in hackathon/src/config.jl.** Set sustain_primary = 1/6 h (currently 0.5) and keep sustain_contingency = 0.5 h and sustain_regulation = 0.5 h. Then replace raise_needs in hackathon/src/rules.jl with the MCE's three discharge-side constraints:
   - D.19A.1: energy and regulation for 30 min, primary 10 min, contingency 20 min, all stacked.
   - D.19A.2: a 10-minute window with energy, regulation and primary.
   - D.19A.3: 30 minutes with energy, regulation and contingency.
   - Use the D.19A.4 form for the charge side, with no reserves.
   - Divide by DischargingEff on the discharge side, multiply by ChargingEff on the charge side, and credit same-period charging (i.e. sustain_charge_credit = true when mimicking the MCE).

   Keep a separate stricter internal guard only as a deliberate risk choice, and label it so.
3. **Keep the per-class reserve headroom.** RC383 D.17.2.4A applies per reserve offer, so reserve_headroom = "per_class" is right. But SoC D.19A.1 stacks primary (10 min) and contingency (20 min) in energy terms, so the energy-side and MW-side combinations differ. Implement both.
4. **Add the standing capability data as explicit config.** MaxCapacity_MWh (100), MinSoC, MaxSoC, ChargingEff, DischargingEff, max charge limit and max discharge limit, max ramp up and down, max reserve per class, ReserveGenerationMax per class, max regulation, RegulationMin and RegulationMax. Use them in both offer validation and simulated clearing. Our eta_one_way should map to the two separate efficiencies.
5. **Make offer output match the market manual.**
   - Write the EGO CSV layout: Participant, Type=EGO, Unit, Day or Date, Period 1-48, Ramp Up, Ramp Down, Capacity, 10 bands, optional External Reference.
   - Standing offers always have 10 pairs (1-5 for charging, quantity <= 0; 6-10 for discharging, quantity >= 0). Prices must be strictly increasing across non-zero pairs, and every 0.0 MW pair is priced $0.00.
   - Round MW to 1 dp, prices to 2 dp, ramps to 1 dp.
   - Validate charge and discharge sums against the max limits and against Capacity (Rule 3A).
   - Reserve offers: 1-5 pairs, prices >= 0, no reserve proportion field.
   - Add validator tests for each of Rules 2, 3A, 5, 7, 8 and 11.
6. **Rethink the strategy's gate-closure SOC guard (hackathon/src/strategy.jl, bids.jl).** Because the MCE now throttles awards to SoC, energy offer quantities may sum to the full MWh (RC386 sec 5.4), so we can offer more instead of thinning offers 3 periods ahead.

   What is left for us to manage:
   - (i) The 10-minute estimation error in ExpectedStartSoC.
   - (ii) SoC drift from deploying reserve or regulation within a period, which the MCE's forecasts ignore.
   - (iii) The MinSoC fallback when PSO data is missing. That fallback blocks discharge and raise services.

   Simulate these instead of a blanket guard.
7. **Make forecasts mimic the market.** Our price and award forecasts for STS and PDS-like horizons should chain EndSoC without deployment, as D.25.1.14 does, so we predict when the MCE will constrain us off.
8. **Add compliance accounting to the backtest.**
   - AFPS: a penalty when |end-of-period schedule - actual| > 10 MW, using the App 5D formula as extracted (verify).
   - Forfeit reserve and regulation payments in periods where SoC cannot sustain deployment.
   - Count gate-closure revisions as MSCP-reportable.
9. **Document the rule basis.** In the README, cite RC386, RC387 and RC390 alongside RC383 and state that the 12 Aug 2025 MCE enforces SoC.

## Still assumptions (questions for EMA)

- **Whether the hackathon's own market simulator implements the post-Aug-2025 MCE** (App 6D D.12B and D.19A SoC constraints, and D.17.2.4A and D.18.1.6-7). Ask EMA: "Does the evaluation clearing engine apply the RC386/RC390 SoC constraints, and with what StartSoC timing, i.e. T-10 min telemetry or exact start-of-period SoC?"
- **Values of MinSoC, MaxSoC, ChargingEff and DischargingEff, and the other standing capability data for the 100 MW / 100 MWh unit.** Ask EMA: "What standing capability data does the hackathon assume: MinSoC and MaxSoC, the efficiencies, max charge and discharge limits, max reserve per class and ReserveGenerationMax, RegulationMin and RegulationMax, and ramp rates? Is the reserve maximum while charging allowed up to twice the rating (RC383 sec 3.2.1)?"
- **The value of RampingTime in D.12B.1.** The RC386 body uses 10 minutes; I did not read the App 6D definition. Ask EMA to confirm RampingTime = 10 minutes.
- **The violation penalty price for the ESS SoC excess variables in App 6J J.3.** In the extracted layout the listed block is 20 x VoLL / 10,000 MW; how the rows group is unclear. Ask EMA: "What penalty applies to ExcessDischarging, ExcessCharging and the related variables?"
- **Whether standing offers really need exactly 10 pairs.** The Market Manual Table 2 note says so, while Ch6 5.2B.2.4 says 1 to 10. Ask EMA: "Must a submitted standing energy storage offer contain exactly 10 bands?"
- **How reserve and regulation deployment is simulated, and whether deployment energy is settled.** These are our deploy_* settings. Ask EMA: "How are activation and regulation energy modelled and paid in the evaluation?"
- **Whether AFPS, reserve non-payment, Reserve Provider Group effects and gate-closure rules are scored in the hackathon.** The AFPS formula and threshold come from the 2022 edition, which may have been amended. Ask EMA for the current Ch5 3.7.3 and App 5D text as applied to ESS. In particular, ask whether an ESS's deviation is measured on EnergyStorageTransfer, i.e. signed for charging.
- **Whether a 2025-2026 consolidated Market Rules or Market Manual changes anything beyond these papers.** I read only the 2022 edition and the amendment papers. The 391 TPC paper (effective 23 Jun 2026) was listed but not read. It touches Ch6 9.2 and App 6D D.24, not ESS clearing as far as its title shows.

## Not found

- **The current consolidated Market Rules (Ch5, Ch6, App 5A, 5D, 6D, 6E, 6G, 6J as in force after 12 Aug 2025).** www.emcsg.com fails TLS through the proxy and ema.gov.sg is behind a bot check. I used the 1 Jan 2022 edition from a third-party host (policy.thinkbluedata.com) only for unamended clauses (App 5A A.2, Ch6 10.4, App 5D).
- **The current EMC Market Operations Market Manual as a whole.** I have only the RC390 Annex 2 amendments to its 26 Jan 2022 version.
- **Any EMC user guide or FAQ for ESS offers.**
- **PSO standing capability data or registration guidance for ESS**, including how MinSoC and MaxSoC are approved.
- **The App 6D definitions of RampingTime and DispatchPeriod.** I assumed 10 min and 1800 s.
- **The exact App 6J penalty block for the SoC violation variables.**
- **Any ESS-specific AFPS or reserve-performance rule.** None appears in RC383, RC386, RC387 or RC390.
- **The register itself.** It lists nothing newer than RC391 (13 Mar 2025) for ESS. No later ESS rule change was found in either register (RCP proposals pages 1-3; EMA-directed pages 1-2). The register files are saved under (local copy, not in the repository)
