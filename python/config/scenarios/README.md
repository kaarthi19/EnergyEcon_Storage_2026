# Scenario configs: what to run when EMA answers

We sent EMA nine questions (capacity rule, SOC breaches, price-maker re-clearing,
pre-dispatch reading, ties at the clearing price, ramp limit, blank rows, November process,
IP). The submitted workbook assumes the cautious answer to each. This folder holds a ready
config for each likely answer, so that when EMA replies the team only switches the config
and reruns:

```bash
cd python
./run_all.sh --data-dir "/path/to/ema bidding comp" --work-dir /path/to/work \
    --config config/scenarios/<name>.toml
```

A scenario file holds only the keys it changes; everything else keeps the default of
`config/default.toml`. Keys of different files do not overlap except where noted, so two
answers can be combined by copying both files' keys into one file (see "Combining answers").
`scripts/scenario_matrix.sh` regenerates every scenario's November workbook (and, with
`--evaluate`, its walk-forward) into a work dir. **The default config is unchanged**: it
still gives the submitted workbook,
`c6260df22a9262adfc8bc3ea4743f2502b21d0c93e0e070e35d1cc3d831ed1a5`.

**How the numbers were made.** Each config was run through the Aug-Oct walk-forward exactly
like the default (`python -m bessbid evaluate --no-pf --config <file>`, with
`--soc-policy clip|reject` where the scenario says EMA clips or rejects). Gains are per 30
days, as a price maker / price taker, against **the default config judged under the same
assumption** (the default's own uplift over Regulation-only is +114,581 maker / +129,804
taker; Regulation-only earns 1,480,804 / 1,625,458). SOC ranges are in MWh under the
convention named. "Single miss" counts, for every energy band of the Aug-Oct plans, whether
the SOC would leave 5-95 % if that one band failed to clear and all others cleared (under
the all-loss-on-discharge convention unless stated). Ramp, Regulation sustainability (S7)
and the capacity rows (S3) were checked period by period on the walk-forward's plan files
(`evaluation_plan_<month>.csv`); offer price limits (S9) with
`python -m bessbid validate <workbook> --config <file>` on the November workbooks.

## Decision table

| # | If EMA says | Use config | Gain vs default, per 30 d (maker / taker) | Compliance under that answer (Aug-Oct) | Risk |
|---|---|---|---|---|---|
| S1 | SOC is enforced by clipping or rejecting dispatch, no penalty | **keep `default.toml`** (`s1_pr_bands_events.toml` exists but loses) | 0 / 0 for the default; the price-responsive variant -39.8k / -41.3k (clip), -48.1k / -45.0k (reject) | default: 0 clipped, 0 rejected, SOC 7.0-93.0 | none for the default; extra bands displace Regulation and drain SOC the plan needs later |
| S2a | SOC breaches are penalised (cost per MWh or per period) | `s2_caps30.toml` (cheap) or `s2_margin6.toml` | caps30 -2.2k / -8.1k; margin6 -9.3k / -11.2k | 0 breaches; worst single miss 15 MWh (caps30) or 35 MWh (margin6) outside the limits | breaches only if a band fails to clear, which never happened in any backtest |
| S2b | Any SOC breach disqualifies the submission | `s2_robust_c20_m12.toml` | -27.4k / -38.1k | 0 breaches, and **0 single-miss breaches** (1130 cases) | gives up a quarter of the energy uplift to make every single miss safe |
| S3 | Capacity rule is the physical reading: discharge + AS <= 100 and charge + Regulation <= 100 | **switch to `s3_physical_contingency.toml`** | +21.3k / +21.6k (`_planned` variant +21.4k / +21.8k) | physical rows at most 100 MW in every period; Contingency (29.4 MW on average) in all 460 charging periods; SOC 7.0-93.0, 0 breaches (activation 0) | under the strict reading those 460 periods sum to 100-150 MW, so switch only on a clear "physical" answer; the `_planned` variant adds about 0.1k for a different plan, not worth it |
| S3' | Capacity is `max_side` (max(charge, discharge) + all AS <= 100) | **keep `default.toml`** | 0 / 0 | same bids | the planned schedule never charges and discharges in one period, so `max_side` adds nothing; price-responsive pairs that it would allow lose (S1) |
| S4a | Loss is all on discharge (eta_c = 1, eta_d = 0.85) | `s4_eff_discharge.toml` | 0 / 0 (same bids as the default) | SOC 7.0-93.0, 0 breaches | none; it is the default's binding convention |
| S4b | Loss is split evenly (sqrt) | `s4_eff_sqrt.toml` | +6.6k / +8.2k | SOC 7.6-92.4 under sqrt, 0 breaches | breaches under the other two conventions (by design), so only if EMA confirms |
| S4c | Loss is all on charge (eta_c = 0.85, eta_d = 1) | `s4_eff_charge.toml` | +14.9k / +18.3k | SOC 7.5-92.5 under that convention, 0 breaches | as S4b |
| S5 | Broad pre-dispatch reading allowed | **keep `default.toml`** (`s5_broad_predispatch.toml` exists) | +0.1k / -2.2k | default (strict reading) is valid under the broad reading too; SOC 7.0-93.0, 0 breaches either way | none: the extra rows add no forecast skill |
| S6a | No BESS ramp limit | **keep `default.toml`** (`s6_no_ramp.toml` exists) | -0.1k / +0.0k | default ramps at most 42 MW, valid with no limit; SOC 7.0-93.0, 0 breaches | none; without the limit steps reach 98.6 MW and the worst single miss grows from 41 to 56 MWh |
| S6b | Ramp limit 20 MW per period | **switch to `s6_ramp20.toml`** | +0.1k / -2.5k | default: 477 of 4416 periods step more than 20 MW (143 of 1440 in November); `s6_ramp20`: max step 20 MW, 0 over; SOC 7.0-93.0, 0 breaches | none; costs at most 2.5k as a taker. For a different rate, set `[market] bess_ramp_mw` to it |
| S7 | Regulation must be sustainable from stored energy for 30 min each way (RC386-style) | **switch to `s7_regulation_sustain_planned.toml`** | +814.8k / +908.0k against the default with its Regulation cut to what it sustains (`s7_regulation_sustain_posthoc.toml`, 401k / 425k per 30 d); +81.6k / +89.8k over the 76.5 MW Regulation-only fallback (1,134,577 / 1,243,475); -379.2k / -421.9k against today's unconstrained default | default as submitted: Regulation not sustainable in 4253 of 4416 periods (by up to 96.6 MW); `_planned`: 0 periods, SOC 7.0-65.0, 0 breaches; single miss 3 of 239 cases, at most 9 MWh | the rule itself costs about a quarter of revenue; the plan keeps SOC low-to-mid so that up- and down-Regulation both stay sustainable. Never use the `_posthoc` variant (-733k / -818k against its own fallback) |
| S8 | A band priced exactly at the clearing price does not clear, or clears partially | **keep `default.toml`** | 0 / 0 | every fixed band is far from any clearing price (see "Notes") | none |
| S9 | Offer prices limited to [-1000, 1000] (or to the training maximum) | **switch to `s9_offer_cap1000.toml`** (`s9_offer_cap2058.toml` for the training-max cap) | 0 / 0 (same Aug-Oct revenue: no charging period met an energy price above the cap) | default: 158 charge bands at 3000 in the November workbook break either cap; each `s9_*` workbook validates clean against its cap; SOC 7.0-93.0, 0 breaches | a charge band priced at 1000 fails to clear if the energy price exceeds 1000 (32 of 5904 training periods), which would shift the SOC like a single miss; none at 2058 |
| S9' | Negative offer prices allowed | **keep `default.toml`** | 0 / 0 | - | none: the lowest training energy price is 133.76 |

## November workbook fingerprints

SHA-256 of the November workbook each config gives (`scripts/scenario_matrix.sh` with the
same data folder; every one passes `bessbid validate` against its own config). Only configs
whose workbook differs from the default are listed; `s4_eff_discharge.toml` gives the
default's workbook.

| Config | Workbook SHA-256 |
|---|---|
| `default.toml` (submitted) | `c6260df22a9262adfc8bc3ea4743f2502b21d0c93e0e070e35d1cc3d831ed1a5` |
| `s1_pr_bands_events` | `6a5880347cde7ae2c1aad05de750c0c89ffa815e4ba82b22706c2f713537f545` |
| `s2_caps30` | `664edee9bcb19f76bb2982689c9cbf1997b209f66d4f2e4bfa7f951dad5d9ec5` |
| `s2_margin6` | `2c3071ea5663c54cdeeb17c5057ac1893b49732bfc3961e647368708e34b0d1e` |
| `s2_margin12` | `bd91f33f35b0f87a0ba193782c862ca6b4674b9d14015a1a4fb74255dc4e65b3` |
| `s2_margin25` | `d232965267cd8c60a960b3eb6e90d219fa6448cf2e34e6a24802c8928127edb2` |
| `s2_robust_c20_m12` | `861322adee8d45e2bb44d79aa34ef8a760764e9073d17a81705233200f925689` |
| `s3_physical_contingency` | `f3cb7de635288ef83d41e63c460435ce47cc300fd44e267557a1d5357effef03` |
| `s3_physical_contingency_planned` | `78a28ade45b2859144b3a7116d49df5d17262defdd84819001d999c08ca7d179` |
| `s4_eff_sqrt` | `b18ec1574f913b4e20c7655f17051fdb4cb0737a1d227a8aa8ed84e5c688dade` |
| `s4_eff_charge` | `ef6b51e8f9889f071287d11496b558fa8299263e3179ee039c71edfa5090de69` |
| `s5_broad_predispatch` | `4b8f71df19a34848cfcd94b3a8dec01f0a5ccc7a301227ccde6965c37a5c8413` |
| `s6_no_ramp` | `229c4503e6747db5205b8aacea55df683421f21619fed87fd68a0a8639aedb42` |
| `s6_ramp20` | `c96924fc798d1b64f726883fd73024b9b4356fdaad72a405f6ce3fc6a40276b4` |
| `s7_regulation_sustain_planned` | `7606a990731875bd529103bbd5d61af3acc3026d84c86334d075300f8b92a4d0` |
| `s7_regulation_sustain_posthoc` | `f33b4700f70d56fef92966fd2e473c165feeb67a93da36bad2a7bc6a9e5ea226` |
| `s9_offer_cap1000` | `09123ee91e55fe6ed9d42b61146bc9c052e8c0f692db7e17e1018dcaa016ce39` |
| `s9_offer_cap2058` | `029a6ab856b3b9d2f2eee96fd6381ab975fc07a26257cee00e534d7d64e6b42b` |

## Notes

- **S8, ties at the clearing price.** The planned energy bands are offered at 0.01
  (discharge) and 3000 (charge, or the S9 cap), Regulation at 0.01, Contingency (S3) at the
  AS floor. The lowest training energy price is 133.76 and the highest about 2058, so no
  fixed band sits at a clearing price; partial clearing could only affect a band that is
  marginal, and none is.
- **SOC exit code.** `evaluate` exits with status 2 for `s4_eff_sqrt` and `s4_eff_charge`
  because their plans breach under the two conventions they no longer plan for; that is by
  design (rows S4b, S4c).
- **S7 Regulation-only fallback.** Both S7 files also set `[strategy.regulation] mw = 76.5`,
  the Regulation that 50 % SOC sustains, so `--strategy regulation` stays valid under that
  answer.

## Combining answers

EMA will answer several questions at once. Put the keys of every config that applies into
one new file in this folder (for example `combo_s3_s6b.toml`), so that
`tests/test_contingency.py` checks it and `scripts/scenario_matrix.sh` builds it. Which
keys each file sets:

| Answer | Keys |
|---|---|
| S2 | `[bess] soc_margin_mwh`; `[strategy.full] charge_cap_mw`, `discharge_cap_mw`, `event_discharge_cap_mw` |
| S3 | `[rules] capacity_rule`; `[strategy.full] reserve_fill` (and `reserve_value`) |
| S4b, S4c | `[bess] efficiency_conventions`, `soc_margin_mwh`; `[strategy.full] y_step` |
| S5 | `[information] strict_predispatch` |
| S6 | `[market] bess_ramp_mw` |
| S7 | `[strategy.full] regulation_soc_rule`, `regulation_sustain_h`; `[strategy.regulation] mw` |
| S9 | `[offers] charge_cap_price`, `offer_price_min`, `offer_price_max` |

The only overlap is `soc_margin_mwh` between S2 and S4b/S4c. The 50 MWh start must lie on
the SOC planning grid, i.e. (45 - margin) / `y_step` must be a whole number, or `generate`
stops with "initial SOC must lie on the planning grid". Usable margins:

- S4c (`y_step = 0.85`): 2.5, 5.9, 11.85, 12.7 MWh. With S2b take 12.7: a missed 20 MW
  discharge then leaves 10 MWh more SOC, a missed 20 MW charge 8.5 MWh less.
- S4b (`y_step = 0.9219544457292888`): 2.5900954964527187, 6.277913279369869,
  11.809639953745602, 12.731594399474893 MWh.
- Default grid (`y_step = 0.5`): any multiple of 0.5.

The S2 files overlap one another (take one). Combinations have not been walked forward;
before submitting one, run `python -m bessbid evaluate --no-pf --config <file>` and check
it against the rows above.
