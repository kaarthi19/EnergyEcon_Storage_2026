# Submission email (draft)

Fill the bracketed fields, then send to EMA from the address the team registered with. Attach the three files named in the body. The file names follow RUNBOOK.md, "File names". Check that the workbook hash in the body matches the file you attach. Renaming a file does not change its hash.

---

**To:** EMA_Energy_Storage@ema.gov.sg

**Subject:** BESS Bidding Algorithm Hackathon Phase 1 submission: [Team name]

Dear EMA BESS Hackathon team,

Please find attached our team's Phase 1 submission for the BESS Bidding Algorithm Hackathon:

1. `[Team]_BESS_Nov2026_bids.xlsx`: the November 2026 bid schedule in EMA's template. It has 5,760 rows covering Energy, Primary Reserve, Contingency Reserve and Regulation. SHA-256: `c6260df22a9262adfc8bc3ea4743f2502b21d0c93e0e070e35d1cc3d831ed1a5`.
2. `[Team]_BESS_strategy.pdf`: the strategy description. It covers the methodology, the key assumptions, how the battery's operating constraints are handled, and which steps are automated and which need a person.
3. `[Team]_BESS_code_v1.0-submission.zip`: the complete source code (Python, MIT licence), release `v1.0-submission`. It uses open-source packages only, and the solver is HiGHS. The zip contains none of EMA's data.

**How to reproduce the workbook.** Unzip the code and install the pinned packages. A single command then runs the whole pipeline: it generates the bids, checks them against the template rules, and draws the figures. Replace the two folders with your own paths:

```bash
cd bess-bidding-algorithm/python
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-figures.txt
./run_all.sh --skip-tests --data-dir "<folder with the competition datasets and template>" --work-dir "<empty folder outside the code folder>"
```

The command writes `submission.xlsx` to the work folder. It should be byte-identical to the attached workbook, with the SHA-256 above. The run is deterministic, uses no internet connection, and stops with an error if the November pre-dispatch file is missing.

- **Python:** 3.10 or newer. We tested with Python 3.13 (3.13.18) on Linux, using the first pinned set in `requirements.txt` (numpy 2.5.3, pandas 3.0.5, highspy 1.16.0). The second pinned set, for Python 3.10 and 3.11 (tested with 3.11), produces the same workbook.
- **Runtime:** about 4 minutes on one CPU core, with peak memory of about 2 GB. The first run spends about 40 seconds of that caching the inputs. The optional unit tests (drop `--skip-tests`) add about 6 minutes and need no data.
- **Details:** `python/README.md` in the zip lists every command, the expected file hashes and the test suite.

**Repository.** The code is also at https://github.com/kaarthi19/bess-bidding-algorithm. The repository is private until the submission deadline. If you would like access before then, please reply with the GitHub usernames to invite. An archived copy is deposited on Zenodo [DOI: 10.5281/zenodo.XXXXXXX], with the files under embargo until 12 December 2026.

Thank you for organising the hackathon. We are happy to answer any questions about the submission.

Kind regards,

[Name, affiliation]

---

## Checks before sending

- The attachments are the three files named above. The workbook's SHA-256 is the one in the body (`sha256sum`, or `Get-FileHash` on Windows). The zip is the one built from the tag, about 220 kB.
- Fill the Zenodo DOI only if it was reserved; otherwise delete that sentence.
- The signature contains only the name and affiliation. Add no phone number or other personal contact details.
- If the team falls back to the Regulation-only workbook (RUNBOOK.md, "Fallback"), replace item 1 and its hash. Add one sentence: "This workbook offers 100 MW of Regulation at S$0.01/MWh in every period. It is the fallback described in Section [x] of the strategy document."
- If EMA has answered our questions and the bids changed as a result, update the hash, and state the config file used in the reproduce command (`--config config/scenarios/<file>.toml`).
