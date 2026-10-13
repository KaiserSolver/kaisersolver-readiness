# Readiness report: kaisersolver on base — 2026-10-12 → 13

The Readiness Standard, applied to ourselves, with cow-backtester **0.11.2**. This run uses the observed per-request budget for the chain and the standard's 500-auction evidence floor: `--readiness --watch 60`, every settled auction in the window replayed against the production Base instance, serving the CoW driver's live traffic during the run.

- **Window:** blocks 52507014–52530697 (fixed), 2026-10-12T11:10:09Z → 2026-10-13T00:18:55Z (13.1 h of chain time)
- **Sample:** 1210 auctions attempted (floor 500), 1210 answered, 871 with ≥1 solution
- **Budget:** 4.62 s (observed)
- **Replay lag:** median 0.6 min between an auction settling and our replay of it — live liquidity at replay time is close to auction-time liquidity
- **Fairness:** the CIP-67 filter was applied to our solutions on every bidding auction with a competition record (859 of 871); 30 of our solutions were filtered, exactly as the protocol would have filtered them
- **Capture:** 83.12 % of the on-chain winners' surplus, coverage-adjusted (the screen below rounds to whole percent)
- **Own wins:** 70 of the 1210 attempted auctions were settled on-chain by kaisersolver's own account (counted per auction)
- **Per bid:** on the 775 unflagged auctions we entered, our surplus was 0.969× the winner's at the median. The capture figure is sum-weighted: the five largest auctions by winner surplus hold 19 % of the window's winner surplus, and we entered 3 of them
- **Verdict:** **REVIEW** — no failing check; 2 warn-level: latency headroom, prices look plausible

```
====================================================================
  READINESS — kaisersolver   [REVIEW]
  base · prod · blocks 52507014..52530697
====================================================================
  window  : blocks 52507014..52530697 | attempted auctions span 2026-10-12T11:10:01Z → 2026-10-13T00:18:33Z (13.14 h, auctionStartBlock timestamps; 92.07 attempted/h)
            settlements found 1363 / auctions formed 1218 / attempted 1210 / replayed 1210 / returned 871
            excluded 37 — top: [('wrapper_unattributed', 20), ('tx_uid_mismatch', 10), ('body_uid_mismatch', 6)]
  budget  : 4.620 s (observed, BUDGETS_S[base].settle) | original-deadline upper bound p50 5.655 s / p95 9.730 s over 1194 attempted
  thresholds: profile 'default' — answer_rate >=90/50% · transport 0/<=1% · deadline_miss 0/<=1% · latency p95 <=0.5x/1x budget · validity >=90/50% · capture >=50/>0% · min_evidence 500
  [PASS] reached auctions         1210 auctions attempted
  [PASS] no transport errors      0 transport errors
  [PASS] answers reliably         100% returned a parseable response inside the budget (incl. legitimate empty solutions)
  [PASS] bid coverage             72% of answered auctions carried >=1 solution
  [PASS] inside the deadline      0 deadline misses
  [WARN] latency headroom         p95 2968 ms of a 4620 ms budget (observed); PASS <= 0.5x, WARN <= 1x
  [PASS] solutions are valid      100% of bid auctions had >=1 valid solution [feasibility+eligibility+udcp+fairness:partial(859/871 evaluated); fairness_filtered 30, zero_surplus 0, udcp 2995 checked / 0 violations]
  [PASS] competitive vs winners   83% of winner surplus captured (coverage-adjusted; 83% conditional on answering; basis mix {'mixed': 53, 'exact_uniform': 919, 'wrapper_lower_bound': 238})
  [WARN] prices look plausible    96 auction(s) flagged implausible_surplus
  [PASS] scan coverage            every block in the window was scanned
  [PASS] field coverage           1363 settlements, 1218 auctions formed, 37 excluded (3% of field; top reasons [('wrapper_unattributed', 20), ('tx_uid_mismatch', 10), ('body_uid_mismatch', 6)])
  ----------------------------------------------------------------
  answered            : 1210/1210  (100%)   bids: 871   transport: 0   deadline misses: 0
  latency             : p50 1615 ms / p95 2968 ms / max 3607 ms   (budget 4620 ms; answered-only incl. late answers)
  surplus vs winners  : 83% captured (coverage-adjusted) / 83% conditional   (871/1210 valid)
  per-bid median      : 0.969x ours/winner over 775 unflagged bid auction(s)
  winner basis mix    : {'mixed': 53, 'exact_uniform': 919, 'wrapper_lower_bound': 238}
  validity basis      : feasibility+eligibility+udcp+fairness:partial(859/871 evaluated)

  Reproduce this run:
    cow-backtester --chain base --env prod --from-block 52507014 --to-block 52530697 --bodies-dir <archive> --solve-timeout 4.62 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url <solver-url> --solver-name kaisersolver --readiness
    # cow-backtester 0.11.2 · engine build sha: cf57bd616

  Note: replays live liquidity against archived auctions — a readiness
  signal, not a settlement guarantee. Pair with self-hosted shadow before prod.
```

## Reading the verdict

- **WARN — latency headroom:** p95 2968 ms of a 4620 ms budget (observed); PASS <= 0.5x, WARN <= 1x
- **WARN — prices look plausible:** 96 auction(s) flagged implausible_surplus

**On "prices look plausible":** the tool flags an auction when our *claimed* surplus exceeds ten times what the on-chain winner actually delivered. 96 auctions are flagged here ({'wrapper_lower_bound': 7, 'exact_uniform': 87, 'mixed': 2} by winner-baseline type; largest ratio 295,244,847×). Claimed prices are not simulated, so these rows are treated as suspect rather than as evidence of out-competing the winner. 69 of the 96 had a winner surplus below a tenth of the window's median winner surplus (flagged-row median 0.0000111 ETH vs window median 0.0001860 ETH). The capture figure above includes them; a reader who wants a conservative number can subtract them.

**Bottom line for Base:** REVIEW: no failing check; 2 warn-level: latency headroom, prices look plausible. Answered 100 % of 1210 attempted auctions, bid on 72 % of them, captured 83.1 % of the winners' surplus (coverage-adjusted), p95 latency 2968 ms against a 4620 ms budget.

## Method notes
- Regenerated 2026-10-13T10:46:19Z over a fixed block window (52507014–52530697) from the watch run's rows: every settled auction whose settlement block lies inside it, one row per auction id (`tools/build_report.py`).
- Counters are cumulative over the watch run (the tool's per-cycle screens are summed; each auction counted once). The verdict is the tool's own `readiness_report` over those counters.
- The watch ran in 1 launch(es). 1 auction(s) seen more than once keep their first replay; 0 block(s) were scanned more than once; 0 cycle(s) lay wholly inside blocks already scanned and were left out.
- A "deadline miss" is a timeout OR an answer that arrived after the budget, mirroring the driver; none occurred. "transport" is every other failure; none occurred.
- Capture is coverage-adjusted: auctions we failed to answer would keep the winner in the denominator.
- Bodies were archived (`--archive-bodies`, SHA-256 per body) so the run is reproducible after the public instance bucket evicts them; the archive is ours, hence `<archive>` in the reproduce line.
- Signal, not guarantee: replays quote live liquidity against archived auctions.

## Reproduce

```
    cow-backtester --chain base --env prod --from-block 52507014 --to-block 52530697 --bodies-dir <archive> --solve-timeout 4.62 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url <solver-url> --solver-name kaisersolver --readiness
    # cow-backtester 0.11.2 · engine build sha: cf57bd616
```

Body archive manifest (root `manifest.jsonl` of the `--archive-bodies` directory, one line per archived body with its SHA-256):
SHA-256 `b9d3cf3a8b93d2aa0bd9cb58b76ae7c07bc927443454bfb16702f26d702751f4` · 1,217 lines · 2026-10-13 00:19:05Z.
