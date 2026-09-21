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
tools/              add_run.py (record a run), verify.py (check the record), tests
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

1. Run `cow-backtester --readiness` (with `--json-out` and `--archive-bodies`) and write the public
   report from its rows.
2. `tools/add_run.py --chain <chain> --run-id <YYYY-MM-DD> --report <report.md> --evidence-root <dir> --evidence <rel>[:kind] ...`
   copies the report in, fingerprints the evidence, writes `run.json` + `SHA256SUMS` +
   `EVIDENCE.sha256`, and regenerates `index.json`, the table above and the root `SHA256SUMS`.
   Verdict, window, checks, budget, tool and engine versions are parsed from the report's readiness
   screen; `--figures` attaches the generator's figures and `--meta` merges anything else.
3. `python3 tools/verify.py --evidence-root <dir>` then commit.

Reports here are signal, not guarantee: a replay quotes live liquidity against archived auctions.
Each report says so in its own words.

## License

MIT. The reports describe one solver's own measurements and may be quoted with attribution.
