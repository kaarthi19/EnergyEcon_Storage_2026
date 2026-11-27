# Submission runbook: EMA BESS Bidding Algorithm Hackathon, Phase 1

This runbook covers everything from freezing the code to making the repository public after the deadline. Work through it in order. Each step lists the commands to run and the checks it must pass before you move on.

## Key facts

| | |
|---|---|
| **Deadline** | **Fri 11 Dec 2026, 23:59 SGT** = 15:59 UTC = **07:59 PST** = **10:59 EST** on the same day. No daylight saving applies in December. |
| **Internal target** | Email sent by **Thu 10 Dec, 18:00 SGT** (10 Dec, 02:00 PST). This leaves a full day for a resend. |
| **Submission repository** | `kaarthi19/bess-bidding-algorithm` (GitHub, private). Local clone: `/home/user/bess-bidding-algorithm`, branch `main`. |
| **Release tag** | `v1.0-submission`. This name is already in `.zenodo.json` and both `CITATION.cff` files. |
| **Workbook SHA-256** | `c6260df22a9262adfc8bc3ea4743f2502b21d0c93e0e070e35d1cc3d831ed1a5` |
| **Plan CSV SHA-256** | `164bbefd51859fe1c9d7512f5c481ecc174318850ab16397d5d558511f3cb3a5` |
| **Bid table SHA-256** | `2240964c8146686f238c01d686b897774955ed4aaaafd73229675c8c25c2ce30` (the manifest's `table_sha256`) |
| **Fallback workbook SHA-256** | `bd47ec3bf7871bdac60c5bc2a71c6c53119efeaa12f0f5205273528f464af488` (Regulation-only; `table_sha256` `d65a08e09e7b9642404c8337e7cd9250a69b2417740b2b8405ecae6c5ed08d21`) |
| **Email** | `EMA_Energy_Storage@ema.gov.sg`, with three attachments: the zip, the workbook and the PDF. The draft is in `email_draft.md`. |
| **Confidentiality** | Hackathon clause 21. EMA's data, and every file derived from it (workbook, plan CSV, manifest, figures, caches), stays out of git and off every public service. It goes only to EMA. Never run anything inside the data folder. |

**State on 27 Nov 2026.**

- The submission repository's `python/` is identical to the development branch `py-build` at `55f2d9d`, apart from local caches.
- The local `main` is at `3fb95a2`, with a clean working tree and no tags.
- The local clone has no remote-tracking refs, so it is unclear what GitHub holds. Step 1 settles that.
- The history (3 commits) contains no csv, xlsx, parquet, zip, pickle or image files.
- `git archive` of HEAD gives a zip of about 222 kB with 72 files.
- `python/README.md` already lists the three hashes above.
- A re-run of `run_all.sh --skip-tests --skip-figures` from `3fb95a2` reproduced all three hashes, using Python 3.13.18 with the pinned set. The manifest showed `strategy: full`, `fallback: null`, 0 validation errors, 0 leaks, `fit_cut` 2026-10-31 00:00, a passing calendar check, and git `3fb95a2` not dirty. Generate took 211.6 s and peak memory was 1,981 MB. The outputs were deleted afterwards.
- The Regulation-only fallback was generated from `3fb95a2` with the Python 3.13 pinned set. It took 3.3 s, the validator reported no violations, and its hash is the one above.

## Timeline

| When (SGT) | Step |
|---|---|
| Until the freeze | Doc fixes. Apply the EMA answers, if any arrive (see "When EMA's answers arrive"). |
| **Tue 8 Dec, 18:00** | **Freeze and tag** (step 1). After this, change nothing unless a check fails. |
| Wed 9 Dec | Regenerate and verify (step 2). Build the zip (step 3). Finish and export the strategy PDF (step 4). |
| Thu 10 Dec, morning | Zenodo: reserve the DOI and upload (step 6, parts a–c). Run the pre-flight checklist. |
| **Thu 10 Dec, by 18:00** | **Send the email** (step 5). Publish the Zenodo record (step 6, part d). |
| Fri 11 Dec | Buffer for a resend or the fallback. Last moment to send: 23:59 SGT. |
| Sat 12 Dec onwards | The Zenodo embargo lifts. Make the repository public and add the DOI (step 7). **Both depend on the disclosure gate.** |

---

## Pre-flight checklist

Tick every item before the email goes out.

**Code and tag**

- [ ] `git -C /home/user/bess-bidding-algorithm status --porcelain` prints nothing.
- [ ] `git rev-parse v1.0-submission^{commit}` equals `git rev-parse HEAD` on `main`, and equals the tag on GitHub (`git ls-remote origin refs/tags/v1.0-submission`).
- [ ] `git ls-files | grep -Ei '\.(csv|xlsx?|parquet|zip|pkl|np[yz])$'` prints nothing.
- [ ] `git rev-list --all --objects | grep -Ei '\.(csv|xlsx?|parquet|zip|pkl|np[yz])$'` prints nothing. This checks the whole history, which becomes public later.

**Workbook regenerated from the tag (step 2)**

- [ ] `sha256sum submission.xlsx` gives `c6260df2…d1a5`.
- [ ] `sha256sum submission.plan.csv` gives `164bbefd…b3a5`.
- [ ] The manifest `table_sha256` is `2240964c…ce30`.
- [ ] The manifest shows `"strategy": "full"` and `"fallback": null`. If the full strategy throws an error, `generate` silently switches to the Regulation-only workbook and records the reason in `fallback`, so a non-null value means the file is **not** the full strategy.
- [ ] The manifest shows `rows` = 5760, `roundtrip_ok` = true, and `validation.errors` = 0 with `validation.warnings` = 0.
- [ ] The manifest's `information_audit.leaks` is 0. The run completes `assert_no_leak()` without error.
- [ ] `input_warnings` and `plan.info.warnings` are both empty. `plan.info.calendar_check.ok` is true.
- [ ] `run_info.git.commit` is the tag's commit and `run_info.git.dirty` is false.
- [ ] `python -m bessbid validate submission.xlsx` prints `OK: no violations` and exits with code 0.

**Timing (information) rules.** The code enforces these and the manifest audits them. Confirm that nothing overrode them:

- [ ] The run used `config/default.toml` unchanged, or the scenario file the team agreed on. The manifest `config` shows `gate_closure_min` = 65, `strict_predispatch` = true and `stack_pub_rule` = "column".
- [ ] The fit cut is 00:00 on 31 Oct (`plan.info.fit_cut`). Day D's plan is made at 00:00 on D−1. The guard for period T uses only T's own lead-2 h vintage, issued by T − 65 min. Stacks are used from their `assumed_publication_time_sgt`. Training clearing data is used only for periods that ended before the decision time.
- [ ] `--allow-incomplete-inputs` was **not** used. Without it, the run stops if the November pre-dispatch file is missing or incomplete.

**Files**

- [ ] All three files have the names given under "File names" below.
- [ ] The workbook still matches the hash after it is renamed.
- [ ] The zip came from `tools/make_submission_zip.sh` at the tag, and its hash is recorded.
- [ ] The PDF carries the tag, the commit, the workbook hash and the DOI (or the DOI line has been removed), and its numbers match the run.
- [ ] The email body has the correct hash, and the signature is only `[Name, affiliation]`.

**Deadline**

- [ ] The email is sent before **11 Dec 23:59 SGT (07:59 PST / 10:59 EST)**. Keep the sent message, with its timestamp, as proof.

---

## 1. Freeze and tag

Do this after the last doc fix lands, and not before EMA's answers have been dealt with or written off.

**Optional fixes before the tag.** None of these changes the workbook bytes. Step 2 confirms that.

- `python/docs/rules.md`, line 23: the text says "stacks lag >= 29 days". The correct figure is ">= 28 days 9 hours" (publication is at ts + 28 d 8 h, plus the 65-minute gate). See `doc_review.md`, item 5.
- `python/bessbid/__init__.py` sets `__version__ = "0.1.0"`, but `pyproject.toml` says 1.0.0 and the citation files say `v1.0-submission`. The manifest records `package_version` 0.1.0. Align them to 1.0.0. This changes only the manifest, not the workbook.
- Before the repository goes public, any redactions required by the disclosure gate. Committing them before the tag keeps the emailed zip, Zenodo and GitHub identical.

**Freeze.**

```bash
cd /home/user/bess-bidding-algorithm
git fetch origin                      # see what GitHub holds; reconcile if main differs
git status --porcelain                # must print nothing
git log --oneline -5
diff -rq python <py-build checkout>/python -x .venv -x __pycache__ -x .ruff_cache -x .pytest_cache   # optional: same code as the dev branch
git tag -a v1.0-submission -m "EMA BESS hackathon Phase 1 submission (November 2026 bids)"
git show --no-patch v1.0-submission
# human step, once step 2 passes:
git push origin main v1.0-submission
```

**Do not move the tag once it has been pushed, emailed or uploaded.** If something must change after that point, make a new commit, tag it `v1.0.1-submission`, update `.zenodo.json` and `CITATION.cff` to match, and repeat steps 2–4.

## 2. Regenerate the workbook from the tag in a fresh environment

Use a folder outside both the repository and the data folder. `run_all.sh` refuses to run with a work folder inside the repository, and `generate` refuses one inside the data folder.

```bash
DATA="/path/to/ema bidding comp"          # EMA's folder: datasets + BESS_Competition_Template.xlsx
SUB="$HOME/bess-submission-v1.0"          # outside the repo and the data folder
mkdir -p "$SUB"
git clone --branch v1.0-submission /home/user/bess-bidding-algorithm "$SUB/repo"   # or clone from GitHub
cd "$SUB/repo/python"
python3.13 -m venv "$SUB/venv"
. "$SUB/venv/bin/activate"
pip install -r requirements.txt -r requirements-figures.txt
./run_all.sh --data-dir "$DATA" --work-dir "$SUB/work"      # tests (~6 min) + generate (~4 min) + validate + figures
sha256sum "$SUB/work/submission.xlsx" "$SUB/work/submission.plan.csv"
python -m bessbid validate "$SUB/work/submission.xlsx"; echo "exit $?"
python - "$SUB/work/submission.manifest.json" <<'EOF'
import json, sys
m = json.load(open(sys.argv[1]))
info = m["plan"]["info"]
print("strategy", m["strategy"], "| fallback", m["fallback"], "| rows", m["rows"], "| roundtrip", m["roundtrip_ok"])
print("xlsx ", m["output_sha256"]); print("table", m["table_sha256"]); print("plan ", m["plan"]["sha256"])
print("validation", m["validation"], "| leaks", m["information_audit"]["leaks"])
print("input_warnings", m["input_warnings"], "| plan warnings", info.get("warnings"), "| calendar ok", info["calendar_check"]["ok"])
print("fit_cut", info.get("fit_cut"), "| git", m["run_info"]["git"], "| python", m["run_info"]["python"], "| runtime_s", m["run_info"]["runtime_s"])
EOF
```

**Expected output.**

- The log shows `calendar check: ... outage events agree in all 1440 periods; 2 predicted surge window(s) confirmed 2026-11-01 00:00 (+639 MW), 2026-11-19 00:00 (+747 MW)`.
- It also shows `OK: no violations` and `read-back identical: True; sha256 c6260df22a9262ad...`, and the run ends with `Done.`
- The two hashes match the "Key facts" table, and every item in the checklist passes.

**If a hash differs:**

1. Check the Python version and packages in the manifest's `run_info`. Both pinned sets are meant to give the same bytes, but the reference hashes come from the Python 3.13 set.
2. Check that `$DATA` is EMA's original Phase 1 folder. The manifest `inputs` lists every file with its hash, so you can diff it against the manifest from an earlier good run.
3. Check the config file.
4. If the workbook matches but the plan CSV does not, the bids are still right; record the cause. If the workbook does not match and the cause cannot be found by 10 Dec noon SGT, see "Fallback".

Keep `$SUB/work` until the competition ends. It holds the exact submitted workbook and its manifest. It is derived data, so keep it local and out of every cloud folder that is shared publicly.

## 3. Build the code zip

`tools/make_submission_zip.sh` builds the zip with `git archive` from the committed tree. It refuses to run if the working tree has uncommitted changes. It `cd`s to the repository root first, so **give it an absolute output path**; a relative path would land inside the repository. `*.zip` is gitignored, so even then the tree stays clean.

```bash
cd "$SUB/repo"                       # the tagged clone (or /home/user/bess-bidding-algorithm at the tag)
git describe --exact-match --tags    # must print v1.0-submission
tools/make_submission_zip.sh "$SUB/out/[Team]_BESS_code_v1.0-submission.zip"
unzip -l "$SUB/out/"*_code_*.zip | tail -1          # ~72 files
unzip -l "$SUB/out/"*_code_*.zip | grep -Ei '\.(csv|xlsx?|parquet|pkl|np[yz])$'   # must print nothing
sha256sum "$SUB/out/"*_code_*.zip   # record it (git archive zips are reproducible for the same commit and git version)
```

Create `$SUB/out` first (`mkdir -p "$SUB/out"`). The zip unpacks to `bess-bidding-algorithm/`. It must contain the root `README.md`, `LICENSE`, `CITATION.cff`, `.zenodo.json`, `tools/` and `python/`.

## 4. Export the strategy document to PDF

The strategy document is the Claude Docs doc "BESS Bidding Strategy: Description for EMA" (node `547ac274-f149`; reviewed at rev 21).

1. Apply the edits in `doc_review.md`: Priority 1 in full, then Priority 2, and Priority 3–4 as time allows. Paste `comparison_section.md` as the section "How the strategy compares with simpler alternatives".
2. Fill in the "Submission details" box (`doc_review.md`, item 19):
   - the tag `v1.0-submission`
   - the full commit SHA (`git rev-parse v1.0-submission^{commit}`)
   - the workbook hash and the bid-table hash
   - the Zenodo DOI, if reserved (step 6, part a), with "embargoed until 12 December 2026"
3. Change the figure caption to "bessbid v1.0-submission".
4. Check the numbers against the step-2 run. The figure numbers come from `$SUB/work/figures/*_numbers.json`.
5. Export to PDF, either from the doc's export menu or by asking Claude to export the doc to PDF. Save it as `$SUB/out/[Team]_BESS_strategy.pdf`.
6. Open the PDF and check it:
   - Tables are not cut off, both figures appear, and the nested-list errors (`doc_review.md`, item 14) are fixed.
   - No link points at a private resource that a judge cannot open. The GitHub link is fine; the email offers access to it.
7. Keep the doc's sharing set to private or team-only. It contains November forecasts derived from EMA's data. The PDF goes only to EMA.

## 5. Send the email

Use `email_draft.md`. Fill in `[Team]` and `[Name, affiliation]` and the DOI, or delete the DOI sentence. Attach the three files from `$SUB/out/` plus the renamed workbook.

### File names

| File | Name | Source |
|---|---|---|
| Bid workbook | `[Team]_BESS_Nov2026_bids.xlsx` | `cp "$SUB/work/submission.xlsx" "$SUB/out/[Team]_BESS_Nov2026_bids.xlsx"` (the hash is unchanged) |
| Strategy PDF | `[Team]_BESS_strategy.pdf` | Step 4 |
| Code zip | `[Team]_BESS_code_v1.0-submission.zip` | Step 3 |

Use the team name exactly as registered with EMA, with no spaces; use `_` instead. If EMA's instructions name the files differently, follow EMA.

Before you press send:

- Run `sha256sum "$SUB/out/"*` once more, and compare the workbook hash with the email body.
- The three attachments come to under 1 MB in total, so a size limit is not a concern.
- Send from the address registered with EMA, and copy the team.

## 6. Zenodo

The Zenodo GitHub integration archives only public repositories, and this one stays private until the deadline. Upload the zip by hand and copy the metadata from `.zenodo.json`.

**a. Reserve the DOI.** Log in at zenodo.org and choose **New upload**. Under "Digital Object Identifier", choose **Get a DOI now** (reserve). The DOI then goes into the PDF and the email before the record is published.

**b. Upload the files.** Upload `[Team]_BESS_code_v1.0-submission.zip`, the same file as the emailed one. Check that its hash matches. **Do not upload the workbook, the PDF or anything else derived from EMA's data.**

**c. Enter the metadata from `.zenodo.json`.**

| Field | Value |
|---|---|
| Resource type | Software |
| Title | `bessbid: a bidding algorithm for a 100 MW / 100 MWh battery in EMA's BESS Bidding Algorithm Hackathon` |
| Creators | Gnapathy, Kaarthi A, University of California San Diego |
| Description | The `description` text from `.zenodo.json`, including "The competition datasets are confidential and are not included." |
| Version | `v1.0-submission` |
| Licence | MIT |
| Keywords | battery energy storage; electricity market bidding; ancillary services; Singapore wholesale electricity market; market clearing |
| Access | **Embargoed**: files restricted, embargo date **2026-12-12** |
| Related identifier (optional) | `https://github.com/kaarthi19/bess-bidding-algorithm`, relation "is supplement to" (what the GitHub integration uses). It will not resolve until the repository is public. |

**d. Publish.** Do this after the email has gone and the zip hash has been checked. Once published, the files cannot be replaced. A correction needs a new version, which gets a new version DOI under the same concept DOI.

**Embargo and the disclosure gate.** On 12 Dec the embargo lifts on its own and the zip becomes public.

The zip's docs contain details derived from the confidential data:

- monthly revenues
- per-day trades
- surge MW by date
- forecast-bias medians
- data-file quirks

See `doc_review.md`, "Disclosure".

Before publishing, check whether Zenodo lets you edit the embargo date of this published record. Then decide one of the following:

- **(i)** EMA has answered question 9, or the docs were redacted before the tag. Keep 2026-12-12.
- **(ii)** Neither has happened. Set a later embargo date at upload, for example 2027-03-31, and bring it forward once cleared, using the edit you confirmed works.

The `.zenodo.json` date of 2026-12-12 is the team's default. Option (ii) changes only the Zenodo record, not the repository.

## 7. After the deadline: make the repository public and add the DOI

**Disclosure gate.** Do this first. Make the repository public only when one of these holds:

- EMA's answer to question 9 (IP and publication) allows it, or
- a redaction commit has removed the data-derived detail listed in `doc_review.md`, "Disclosure", item 3.

Remember that the full history becomes public, not just the latest commit. If redaction is needed and the detail is also in earlier commits, publish a fresh repository built from the redacted tree, or rewrite the history. Either way, the DOI record and the tag must still match what was emailed. If history is rewritten, the public `v1.0-submission` would differ from the emailed zip, so note that in the README.

**Then:**

1. On GitHub, open the repository, then **Settings → General → Danger Zone → Change visibility → Public**. First confirm that the history check in the pre-flight checklist still passes on `origin`.
2. Add the DOI with one commit on `main`, after the tag. Do not move the tag.
   - Root `README.md` and `python/README.md`, "Citation" section: replace "its DOI will be added here once published" with the DOI link (`https://doi.org/10.5281/zenodo.NNNNNNN`). Optionally add the Zenodo DOI badge at the top of the root README.
   - Both `CITATION.cff` files (root and `python/`): add `doi: 10.5281/zenodo.NNNNNNN`, or an `identifiers:` entry of type `doi`. Use the concept DOI if you want citations to cover future versions. `cffconvert --validate` checks the file, if installed.
   - Commit with the message "Add the Zenodo DOI", then push.
3. Optionally create a GitHub release from the tag `v1.0-submission`. **If the Zenodo GitHub integration is switched on, every release creates a new Zenodo record.** Leave it off, or accept a second DOI.
4. Check that the Zenodo record's related identifier now resolves.

---

## Fallback

Use this if something fails late. Work down the list and stop at the first option that works.

**F1. Last verified workbook.** Suppose regeneration from the tag fails on submission day, for example because pip cannot install a package or the machine is down. If a workbook already exists from an earlier verified run, with hash `c6260df2…d1a5` and a manifest showing `strategy: full`, `fallback: null` and zero validation errors, submit that workbook.

The code is unchanged, so it is the same submission. Say in the email that it was produced by `v1.0-submission`. Only the hash matters.

**F2. Regulation-only workbook.** Use this if no verified full-strategy workbook exists, for example because a hash mismatch cannot be explained or the data folder is damaged.

```bash
cd "$SUB/repo/python"; . "$SUB/venv/bin/activate"
python -m bessbid generate --strategy regulation --data-dir "$DATA" --work-dir "$SUB/work-fallback" --out "$SUB/work-fallback/submission_regulation.xlsx"
# or, without the data folder: --template "/path/to/BESS_Competition_Template.xlsx" instead of --data-dir
python -m bessbid validate "$SUB/work-fallback/submission_regulation.xlsx"
sha256sum "$SUB/work-fallback/submission_regulation.xlsx"   # expect bd47ec3bf7871bdac60c5bc2a71c6c53119efeaa12f0f5205273528f464af488
```

- **What it bids.** 100 MW of Regulation at S$0.01/MWh in every period, and nothing else. Energy, Primary and Contingency rows are left blank, as in the template's own example. It takes about 3 seconds and reads only the template, not the market data.
- **Why it is safe.** Activation is 0 MWh, so the state of charge stays at 50% and cannot breach any limit under any efficiency convention. Because it uses no market data, it cannot break an information rule. It clears whether or not EMA re-clears the market with our offers.
- **What it costs.** About S$1.48M per 30 days in the Aug–Oct backtest as a price maker, against S$1.60M for the full strategy. That is about S$115k per 30 days less, or about S$100k less in the November expectation.
- **What else changes.** Add a short note to the PDF and the email saying that the submitted workbook is the Regulation-only fallback that the strategy document describes. Prepare this paragraph in advance. The code zip and the tag stay the same, because the fallback is a mode of the same code.
- **Generate it early.** Produce the fallback during step 2 and keep it next to the main workbook. Then F2 costs nothing on the day.

**F3. Generic recovery.** Use this if Python itself is unavailable. Use any machine: the code needs Python 3.10 or newer and runs offline once the packages are installed. A Python 3.11 venv with the second pinned set also works.

**Late-change rule.** After **10 Dec, 12:00 SGT**, do not change the config or the code for any reason, EMA's answers included. Either send the frozen submission or use F1/F2.

---

## When EMA's answers arrive

The decision table, which maps each possible answer to a config change and its expected effect, lives in **`python/config/scenarios/README.md`**. That file is being written in the development branch, together with one ready-made override file per scenario. Use the table; do not improvise on submission day.

1. **Record the answer.** Log the date, the question number (1–9) and EMA's exact words in the team notes.
2. **Look up the table row.** If the answer matches our current assumption, nothing changes. Note it in the strategy document's "Key assumptions" table as "confirmed by EMA" and stop.
3. **If the row calls for a change,** apply the override:
   - Run `./run_all.sh --config config/scenarios/<file>.toml ...` in a fresh work folder.
   - Or, if the team adopts the change as the new default, edit `config/default.toml`. The README states that running with `default.toml` gives the reference bytes, so the README hashes change with it.
   - Team decisions stand regardless of the answer: the month ends at ≥ 50% state of charge (`terminal_soc_frac = 0.5`) and `event_recharge` stays `"none"`. Change them only by an explicit team decision, never on submission day.
4. **Re-validate.** All checklist items in step 2 must pass: validator, manifest, `fallback: null`, no leaks, calendar check.
5. **Re-hash.**
   - Record the new hashes for the workbook, the plan CSV and `table_sha256`.
   - Update the fingerprint table in `python/README.md` ("Determinism") and the "Key facts" table above.
   - Change the reproduce command in `README.md`, `python/README.md` and `email_draft.md` if a scenario config is used.
6. **Re-run the evidence the docs quote,** if the change affects it:
   - `python -m bessbid evaluate --data-dir "$DATA" --work-dir <work>` (about 15 min) for the Aug–Oct table in `docs/strategy.md`.
   - The figure sidecars for the figure numbers.
   - The November scenario numbers.
7. **Update the doc numbers.** In the strategy document, update:
   - the Summary
   - the backtest table
   - "Expected result for November"
   - the Key assumptions rows for the answered questions
   - the Submission details box (new hashes, and the new commit if one was made)

   Also update `docs/strategy.md`, "Assumptions to confirm with EMA".
8. **Commit and re-tag.** Commit the config and doc changes. If this happens before the tag, tag as usual. If the tag was already pushed or sent, tag `v1.0.1-submission` and update `.zenodo.json` and `CITATION.cff`. Then repeat steps 2–6 of this runbook.
9. **Answers after the email has gone.**
   - If the answer would change the bids and it arrives before the deadline, ask EMA whether a replacement is allowed. If it is, resubmit with the subject "… (replaces our submission of [date])".
   - If it arrives after the deadline, change nothing and keep the answer for Phase 2.

**Which questions touch which settings.** These are pointers only; the decision table has the actual rule.

| Question | What it is about | Setting it touches |
|---|---|---|
| 1 | Capacity rule | `capacity_rule` |
| 2 | State of charge outside 5–95%, efficiency split, month-end SOC | `soc_margin_mwh`, `efficiency_conventions`, `terminal_soc_frac` (team decision: keep 0.5) |
| 3 | Price maker versus price taker | None; it only changes which revenue number we quote |
| 4 | Pre-dispatch rule | `strict_predispatch`, `predispatch_leads` |
| 5 | Ties at the clearing price, offer price limits | `offer_price_min`, `offer_price_max`, `as_floor_price`, `discharge_floor_price`, `charge_cap_price` |
| 6 | Battery ramp limit | `bess_ramp_mw`. A faster limit, or none, costs nothing. A slower one needs a smaller value and a re-run. |
| 7 | Blank rows, MW resolution | `mw_resolution` and the writer |
| 8 | November process | Data-loading assumptions |
| 9 | IP and publication | The disclosure gate in steps 6 and 7 |
