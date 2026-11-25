# Questions for EMA

Draft clarification email, revised on 25 Nov 2026 after EMA released the datasets, the template and the submission email. EMA's answers already settle most of the 21 questions in the earlier draft (see git history): Python or R, a one-month test period, initial SOC 50%, activation 0 MWh, the template format, the 65-minute gate closure and the 28-day stack delay. What follows is only what is still open.

**Phrasing.** EMA may send its answers to every participant. The questions are therefore written neutrally and do not describe what we have inferred about the market simulation.

The deadline is 11 Dec 2026, 23:59 SGT. Send early so the answers can still change the code.

---

## Draft email

**To:** EMA_Energy_Storage@ema.gov.sg
**Subject:** BESS Bidding Algorithm Hackathon: clarification questions on the datasets and template

Dear EMA Energy Storage team,

Thank you for releasing the datasets and the submission template. We would be grateful for clarification on the points below. Questions 1 to 4 matter most to us.

1. **Combined capacity rule.** The template says the combined capacity offered across Energy and Ancillary Services must not exceed 100 MW. In a given period, does this mean:
   - (a) the sum of all energy quantities offered (charging and discharging, in absolute value) plus all reserve and regulation quantities is at most 100 MW; or
   - (b) discharging plus reserves plus regulation is at most 100 MW, with charging counted separately?

   Is the limit checked on the quantities offered or on the quantities cleared? May reserve or regulation be offered in a period in which the battery is offered for charging?

2. **State-of-charge limits in the simulation.** If clearing our offers in a period would take the battery below 5% or above 95%, what happens? Possible treatments: the dispatch is reduced to the feasible amount, the offer is not cleared, a penalty is applied, or the submission is penalised or disqualified. How is the 85% round-trip efficiency applied: equally on charging and discharging, or entirely on one side? Is there any requirement or valuation for the state of charge at the end of the test month?

3. **Effect of our offers on clearing.** Will the simulation re-clear the market with our offers added to the generator offer stacks, so that our offers can change clearing prices? Or will our offers be settled against clearing prices determined without them?

4. **Use of pre-dispatch forecasts.** For the bid for a period starting at time T, may we use pre-dispatch forecasts issued at or before T − 65 minutes for other trading periods, including later periods? Or only the forecasts issued for period T itself? Should the assumed_publication_time_sgt column be taken as the time each offer-stack row becomes available?

5. **Clearing of individual price bands.** Is a band offered at exactly the clearing price cleared? Can a band clear partially? What are the minimum and maximum offer prices allowed for Energy and for each ancillary service? Are negative offer prices allowed?

6. **Rows without offers.** If we do not wish to offer a product in a period, should we leave its row in the template with all bands blank, or omit the row? Should quantities be given to one decimal place?

7. **Training data as a guide to the test month.** May we assume the test-month market follows the same simulation as the training months? This covers generator outages and their timing, demand patterns and reserve requirements. Will the simulation of the test month continue from the end of the training period, or start fresh on 1 November?

8. **Intellectual property.** Our algorithm builds on our own pre-existing, MIT-licensed research code. Does the assignment in clause 20 cover only material created for the hackathon, or also pre-existing code and open-source components included in a submission? May participants describe their methodology in academic publications afterwards?

Thank you for your help.

Kind regards,
[Name, organisation, lead registrant]

---

## What each answer changes in the Python package (`python/bessbid`)

| Question | Current assumption | Effect of a different answer |
|---|---|---|
| 1 | Strict reading (a), checked on offered MW | Reading (b) frees about 1.5–2% of revenue: offer regulation on top of charging |
| 2 | No breach is acceptable; SOC is planned to be feasible under all three efficiency conventions with a margin; end the month at ≥ 50% | If EMA clips dispatch, price-responsive energy bands become safer and worth more; a looser end-of-month rule frees about $10k |
| 3 | Evaluated under both price-maker and price-taker clearing; fixed energy offers clear in either case | If EMA settles against fixed prices, scarcity revenue is higher (no self-relief) |
| 4 | Strict: T's own lead-2h/lead-3h vintages; stacks published by T − 65 min | The broad reading allows multi-vintage net-demand forecasts (small accuracy gain) |
| 5 | Ties treated conservatively; offer prices kept inside the observed price range | Offer pricing of the always-clearing bands |
| 6 | Rows kept, bands blank; MW to 0.1, rounded toward zero | Writer format only |
| 7 | Same simulation; event calendar learned from training data; no ramp state carried over | If the process changes, the event layer is switched off (regulation plus small arbitrage) |
| 8 | Unresolved; see the IP note in the README | — |
