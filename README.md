# kaisersolver readiness record

Every published readiness run of **kaisersolver**, a solver on [CoW Protocol](https://cow.fi), with a
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
  report.md         the report, copied in unchanged
  run.json          window, verdict, every check, tool + engine versions, provenance, evidence fingerprints
  SHA256SUMS        sha256 of report.md and run.json
  EVIDENCE.sha256   sha256 / size / line count of the evidence files (not in this repository)
index.json          every run's summary, regenerated from the run.json files
SHA256SUMS          sha256 of every file in the repository
tools/              add_run.py (record a run), verify.py (check the record), regenerate.py (rebuild the manifests),
                    build_report.py (rows → report.md + figures), run_month.py (the monthly run, end to end),
                    monthly.json + monthly.local.example.json (its config), make_fixture.py + fixtures/ (synthetic: the tool's real code over stand-in inputs, for the tests), tests
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
   screen; `--figures` attaches the generator's figures and `--meta` merges build_report's own facts (the
   `generator` block names `tools/build_report.py`, its sha256 and the commit it ran from; only the
   keys `generator`, `rows_tool_version`, `build`, `body_archive_manifest` and `bottom_line` are taken,
   and paths are relative). `bottom_line` in `run.json` says whether the report's bottom line is still
   the generated one or was edited afterwards.
3. `python3 tools/verify.py --evidence-root <dir>` then commit.

A commit that only changes `tools/` has no run to add; `python3 tools/regenerate.py` rebuilds the root
`SHA256SUMS` (and `index.json` + the table) so `verify.py` passes.

## Monthly runs

From October 2026 a report per chain is published every month. `tools/run_month.py` does the whole
thing on the engine host — one ~20 h `--readiness --watch 60 --compete` run per chain, reported over its
own block window (the same shape as the 2026-09-14 reports):

```
python3 tools/run_month.py --chain base --dry-run   # replay + build only; no worktree, no commit, nothing sent
python3 tools/run_month.py --chain base             # build, record and verify locally; prints the publish commands
python3 tools/run_month.py --chain base --publish   # ... and publish the branch and open the pull request
python3 tools/run_month.py --chain base --skip-run --run-id 2026-10-01   # rebuild from the evidence on disk
```

**Host settings.** `tools/monthly.json` is public and holds only generic settings (chain, our
settlement addresses, thresholds). Everything about the host goes in `tools/monthly.local.json`, which
is git-ignored and merged over it; `tools/monthly.local.example.json` shows the keys: `evidence_root`,
each chain's `solver_url` (the replay target) and `engine_sha_cmd`. A live run refuses to start when
one is missing; `--dry-run` says which and stops. RPC URLs come from the environment variable
`rpc_env` names. The tool takes the URL on its command line, so it is visible in `ps` for the length of
the run: use a key made for this job. The runner never prints it.

**Replay target.** Use a non-production instance of the engine build under test. Before the long run
the runner POSTs one small real `/solve` to it and requires HTTP 200; a refusing ingress (403) would
otherwise score every replay as a transport error. The public report names the target only as
`<solver-url>`.

**Load and disk.** The tool runs under `ionice -c3 nice -n 19` in its own session, and a lock in the
evidence root means only one run executes at a time on the host, so schedule the chains on separate
days (`--wait-lock` queues instead of failing). The runner needs `min_free_gb` (default 30) free under
the evidence root to start, re-checks it between launches and while the tool runs, and stops early
(partial run, not published) when it is crossed. The tool's cache is off, so nothing but the body
archive (about 6 GB for a month) and the rows grows. Archived bodies are the reproducibility
evidence and are never deleted by the runner: move closed months to cheaper storage by hand once their
pull request is merged. If the tool reports an archive write error the run fails and nothing is built.

**Stopping.** At the deadline the runner sends the tool SIGINT and repeats it every 30 s (the tool
finishes the cycle in flight with partial data on the first and only stops on a later one), then
SIGTERM after the grace, then SIGKILL. A stop the runner sent is clean whatever the exit code, and the
cycle in flight is left out of the window. A crash is relaunched; the budget counts consecutive failures
and resets whenever a launch wrote a cycle. The relaunch asks for a few hundred blocks more than the
gap, so launches overlap (disclosed in the report) instead of leaving blocks unscanned, and a gap is
detected afterwards. Running out of restarts or disk after at least one cycle builds a partial run.
What happened is in `runs/<chain>/<run-id>.plan.json` beside the evidence, which `--skip-run` reads;
the output of every launch is in `logs/<chain>/` under the evidence root. SIGINT or SIGTERM to the
runner stops the tool and exits 130 with the state written.

**Recording and publishing.** The record is written in a fresh `git worktree` cut from `origin/main`
under the build root, so the checkout the runner lives in is never switched, dirtied or stacked
across chains; `tools/` in that checkout must equal `origin/main` (the code that runs is the code that
is recorded). The commit is authored and committed as `kaisersolver` with no trailers. Nothing leaves
the host unless `--publish` is given and the gate passes: bids returned, transport plus deadline-miss
share at most `max_bad_share_pct` (default 5), no unscanned gap, one engine build (the sha is re-read at
every launch and at the end), not a partial run. A refused run is still recorded and committed locally
(exit 3). `--publish` authenticates with a token limited to this repository (fine-grained, Contents
and Pull requests write) in the environment variable `publish_token_env` names (default `GH_TOKEN`),
passed to `git` and `gh` through the environment, never the host's global `gh` login. The pull request
is the review step: edit the bottom line if you want, re-run `add_run.py --force` so the checksums
follow, merge. Every archive is per run and chain (`archive/bodies-<ver>/<run-id>-<chain>/`), because
a shared manifest would change under the earlier runs' fingerprints.

**Months that are not published.** Only a run that passes the gate and is merged enters the record. A
refused, partial or thin (fewer than 500 attempted auctions) run is recorded locally with the reason in
its `run.json` notes and is not published unless the owner decides to; a month with no run here was
not published, not hidden. A hand-edited bottom line is marked `edited` in `run.json`.

`engine_sha_cmd` is an argv list run without a shell, on the host, and is read only from the
git-ignored overlay; as root the runner wants `--allow-root` before it runs it. `--start` must carry a
timezone. A restarted watch may replay a few auctions twice; the report keeps the first replay and
says how many blocks were scanned twice.

**End-to-end test.** `tools/e2e/run_e2e.py` runs the real `run_month.py`, the real `cow-backtester`
CLI doing a real watch over HTTP, the real `add_run.py` / `verify.py` and a real publish — against
a stand-in world: `tools/e2e/mock_world.py` serves the chain's JSON-RPC, the S3 bucket and the CoW API
on one local port and advances 4 blocks/s in wall-clock time; the tool's own `mock_solver.py` is the
engine; a `gh` shim logs instead of reaching GitHub; a bare repository is the remote. It needs the
tool checkout (`COW_BACKTESTER_SRC`) and takes as long as `--duration` (default 150 s):

```
COW_BACKTESTER_SRC=~/cow-backtester python3 tools/e2e/run_e2e.py --duration 150s
COW_BACKTESTER_SRC=~/cow-backtester python3 tools/e2e/run_e2e.py --long-cycle   # the deadline lands inside a cycle
```

It passes only when the published branch, cloned back from the bare remote, verifies and this run's
`EVIDENCE.sha256` checks out against the evidence on disk, the checkout is back on a clean `main`, and
the commit carries the owner identity and no trailer. CI installs the tool at a pinned full commit,
fails (rather than skips) when it cannot be imported, and runs this end to end test on every push.

**Mutation smoke.** `tools/mutation_smoke.py` applies one deliberately wrong edit at a time to a scratch
copy (a figure mapped from the wrong field, a dropped counter, a changed constant) and checks that the
test suite fails each time. It is slow and is not part of CI; run it after changing `render_report.py`
or `rebuild_state.py`.

Reports here are signal, not guarantee: a replay quotes live liquidity against archived auctions.
Each report says so in its own words.

## License

MIT. The reports describe one solver's own measurements and may be quoted with attribution.
