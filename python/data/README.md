# Data

This folder holds no data. It contains this note and the provenance register,
[`DATA_SOURCES.md`](DATA_SOURCES.md).

EMA's hackathon datasets are confidential (hackathon clause 21), so they are not part of
this repository. Teams receive them from EMA. Copying them into the repository, or anything
derived from them (caches, bid workbooks, plans, manifests, figures), is not allowed.
`python/.gitignore` blocks those file types, and everything in this folder except the two
README files, as a safeguard.

## Where to put the data

Anywhere, as long as it is outside the repository. Pass the folder with `--data-dir`:

```bash
python -m bessbid generate --data-dir "/path/to/ema bidding comp" --work-dir /path/to/work \
    --out /path/to/work/submission.xlsx
```

The code searches the folder recursively and recognises the files by name (see
`DATA_SOURCES.md`). EMA's folder layout, with its nested "(Part 1) ..." folders, works
as released. The template `BESS_Competition_Template.xlsx` is found there as well; if it is
somewhere else, pass `--template`.

The code never writes to the data folder. Caches (parquet, in `<work-dir>/cache`) and all
outputs go to the `--work-dir` you choose; keep it outside the repository as well. Output
and work dirs inside the data dir are refused.
