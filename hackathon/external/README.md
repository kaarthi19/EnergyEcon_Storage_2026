# External data files

Small reference files that are safe to keep in the repository. See `../docs/EXTERNAL_DATA_PLAN.md` for the full shortlist and the rules for using each source without look-ahead.

| File | Source | Licence | Status |
|---|---|---|---|
| `sg_public_holidays.csv` | Ministry of Manpower via data.gov.sg (`d_8ef23381f9417e4d4254ee8b4dcdb176`), downloaded 11 Nov 2026. Covers 2020-01-01 to 2027-12-25. | Singapore Open Data Licence v1.0 | Not yet used by the algorithm |
| `sg_market_events_draft.csv` | Hand-coded dates of rule changes, capacity changes and reserve requirement changes, with a source URL per row | Facts compiled by us; several rows cite EMC pages | Draft. Some rows are inferred or from press reports and need checking. Not yet used by the algorithm |

Contains information from the Ministry of Manpower's public holidays dataset accessed on 11 Nov 2026 from data.gov.sg, which is made available under the terms of the Singapore Open Data Licence version 1.0 (https://data.gov.sg/open-data-licence).
