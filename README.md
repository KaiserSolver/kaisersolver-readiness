# kaisersolver readiness record

Every readiness run of **kaisersolver**, a solver on [CoW Protocol](https://cow.fi), with a
checksum on everything: the report as it was published, the structured facts behind it, and
fingerprints of the raw evidence it was built from. This is the running record promised in
[Measuring solver readiness before production: a v0 proposal](https://forum.cow.fi/t/measuring-solver-readiness-before-production-a-v0-proposal/3572)
on the CoW forum.

The reports are the output of [`cow-backtester --readiness`](https://github.com/KaiserSolver/cow-backtester):
real production auctions replayed against the solver's endpoint and scored against what won on-chain.
The tool is for any solver; this repository is one solver's record of applying it to itself. Until
2026-09-21 these reports lived in the tool's own repository under `docs/readiness/`; they were moved
here unchanged (each `run.json` names the commit and path they came from).

## Runs

<!-- runs:start -->
| Chain | Run | Window (blocks) | Verdict | Warns | Tool | Engine | Report sha256 | Status |
|---|---|---|---|---|---|---|---|---|
| arbitrum-one | [2026-09-14](runs/arbitrum-one/2026-09-14/report.md) | 505098473–505365109 | REVIEW | bid coverage, competitive vs winners, prices look plausible | 0.11.0 | `3a36e59e5` | `0f57032f1d2c` | current |
| base | [2026-08-22](runs/base/2026-08-22/report.md) | 50314011–50316011 | REVIEW | competitive vs winners, prices look plausible | 0.10.0 | `bbefaf759` | `5084859d5c00` | superseded |
| base | [2026-09-14](runs/base/2026-09-14/report.md) | 51300926–51336483 | REVIEW | bid coverage, latency headroom, prices look plausible | 0.11.0 | `e777ab394` | `a73babcbb114` | current |
| bnb | [2026-09-14](runs/bnb/2026-09-14/report.md) | 121850336–122000731 | REVIEW | bid coverage, latency headroom, competitive vs winners, prices look plausible, scan coverage, field coverage | 0.11.0 | `3a36e59e5` | `60828bbd2734` | current |
<!-- runs:end -->

Status `superseded` marks a run whose method the tool has since replaced; it stays as history and
its `run.json` names the run that replaces it.

## Layout

```
runs/<chain>/<run-id>/
  report.md         the report, byte-for-byte as published
  run.json          window, verdict, every check, tool + engine versions, provenance, evidence fingerprints
  SHA256SUMS        sha256 of report.md and run.json
  EVIDENCE.sha256   sha256 / size / line count of the evidence files (not in this repository)
index.json          every run's summary, regenerated from the run.json files
SHA256SUMS          sha256 of every file in the repository
tools/              add_run.py (record a run), verify.py (check the record), regenerate.py (rebuild the manifests),
                    build_report.py (rows → report.md + figures), run_month.py (the monthly run, end to end),
                    monthly.json (its config), make_fixture.py + fixtures/ (genuine tool output for the tests), tests
```

A run id is the UTC date the run started, with a suffix when a chain has more than one run that day.

## Verifying

Anything with `sha256sum` can check the committed files:

```
sha256sum -c SHA256SUMS                                   # the whole repository
(cd runs/base/2026-09-14 && sha256sum -c SHA256SUMS)      # one run
```

`tools/verify.py` goes further: it re-parses every report's readiness screen and checks it against
`run.json`, regenerates `index.json` and the table above and compares, and recomputes the root
manifest. CI runs all of it on every push, so a report, record or index that drifts from its digest
fails the build.

```
python3 tools/verify.py
```

**Evidence.** Each report is built from the tool's `--json-out` rows (one line per replayed auction)
and, for the 0.11 runs, an archive of the auction bodies with a per-body SHA-256 manifest. Those
files carry the solver's every bid and are ours; they are not published. `EVIDENCE.sha256` records
their digests, sizes and line counts, so anyone who is handed a copy can check that it is the copy
the report was built from:

```
python3 tools/verify.py --evidence-root <dir laid out as runs/<chain>/... and archive/...>
```

The reports themselves quote the body-archive manifest digest they were built against, and the
reports for 2026-09-14 are byte-identical to the versions cited on the forum (the tool repository at
commit `e7b3b73`); `git show e7b3b73:docs/readiness/<file> | sha256sum` there reproduces the digest
in `run.json` here.

## Recording a run

1. Run `cow-backtester --readiness --compete --watch 60` with `--json-out` and `--archive-bodies`, then
   write the public report from its rows:
   `tools/build_report.py --chain <chain> --from-block <lo> --to-block <hi> --rows <file>... --compete --solver-url <url> --engine-sha <sha> --manifest <archive>/manifest.jsonl --own-address <0x..> --out-dir build/<chain>/<run-id>`
   reads every `--json-out` file of the run (one row per auction id, the first replay kept, restricted
   to the block window), rebuilds the counters the tool accrues and re-runs the tool's own
   `readiness_report()` over them, and writes `report.md`, `figures.json`, `meta.json` and
   `readiness.json`. The verdict and the screen are the tool's; the prose around them is filled from
   the same numbers (`--bottom-line` replaces the one editorial sentence).
2. `tools/add_run.py --chain <chain> --run-id <YYYY-MM-DD> --report <report.md> --evidence-root <dir> --evidence <rel>[:kind] ... --figures build/.../figures.json --meta build/.../meta.json`
   copies the report in, fingerprints the evidence, writes `run.json` + `SHA256SUMS` +
   `EVIDENCE.sha256`, and regenerates `index.json`, the table above and the root `SHA256SUMS`.
   Verdict, window, checks, budget, tool and engine versions are parsed from the report's readiness
   screen; `--figures` attaches the generator's figures and `--meta` merges anything else (the
   `generator` block names `tools/build_report.py`, its sha256 and the commit it ran from).
3. `python3 tools/verify.py --evidence-root <dir>` then commit.

A commit that only changes `tools/` has no run to add; `python3 tools/regenerate.py` rebuilds the root
`SHA256SUMS` (and `index.json` + the table) so `verify.py` passes.

## Monthly runs

From October 2026 a report per chain is published every month. `tools/run_month.py` does the whole
thing on the engine host — one ~20 h `--readiness --watch 60 --compete` run per chain starting
00:00 UTC on the 1st, reported over its own block window (the same shape as the 2026-09-14 reports):

```
tools/run_month.py --chain base --dry-run     # everything but git/gh; prints the commands it would run
tools/run_month.py --chain base               # cron: 0 0 1 * *  (one line per chain, staggered by a minute)
tools/run_month.py --chain base --skip-run --run-id 2026-10-01   # rebuild from evidence already on disk
```

It runs the tool until the deadline (a clean SIGINT stop; exit 130 means the last cycle was cut short
and is left out of the window), relaunching on a crash so each launch is its own evidence file
(`runs/<chain>/<start>T<time>Z.jsonl`) that scans from where the previous one stopped; builds the
report with `build_report.py`; records it with `add_run.py`; verifies; then commits on
`readiness/<chain>-<run-id>`, pushes and opens a pull request when `gh` is authenticated, otherwise
prints the exact commands. The PR is the review step: edit the bottom line if you want, re-run
`add_run.py --force` so the checksums follow, merge. Every archive is per run and chain
(`archive/bodies-<ver>/<run-id>-<chain>/`), because a shared manifest would change under the earlier
runs' fingerprints.

`tools/monthly.json` holds the per-chain settings (solver URL, our settlement addresses, the command
that prints the engine build sha); RPC URLs come from the environment variable it names and are never
committed. A restarted watch may replay a few auctions twice; the report keeps the first replay and
says how many blocks were scanned twice.

Reports here are signal, not guarantee: a replay quotes live liquidity against archived auctions.
Each report says so in its own words.

## License

MIT. The reports describe one solver's own measurements and may be quoted with attribution.
