# Readiness report: kaisersolver on bnb — 2026-10-12 → 13

The Readiness Standard, applied to ourselves, with cow-backtester **0.11.2**. This run uses the observed per-request budget for the chain and the standard's 500-auction evidence floor: `--readiness --watch 60`, every settled auction in the window replayed against the production BNB instance, serving the CoW driver's live traffic during the run.

- **Window:** blocks 127207259–127305544 (fixed), 2026-10-12T12:01:21Z → 2026-10-13T00:18:38Z (12.3 h of chain time)
- **Sample:** 3072 auctions attempted (floor 500), 3072 answered, 1780 with ≥1 solution
- **Budget:** 2.35 s (observed)
- **Replay lag:** median 0.6 min between an auction settling and our replay of it — live liquidity at replay time is close to auction-time liquidity
- **Fairness:** the CIP-67 filter was applied to our solutions on every bidding auction with a competition record (1780 of 1780); 1 of our solutions were filtered, exactly as the protocol would have filtered them
- **Capture:** 0.03 % of the on-chain winners' surplus, coverage-adjusted (the screen below rounds to whole percent)
- **Own wins:** 45 of the 3072 attempted auctions were settled on-chain by kaisersolver's own account (counted per auction)
- **Per bid:** on the 1665 unflagged auctions we entered, our surplus was 0.864× the winner's at the median. The capture figure is sum-weighted: the five largest auctions by winner surplus hold 99 % of the window's winner surplus, and we entered 2 of them
- **Verdict:** **REVIEW** — no failing check; 3 warn-level: competitive vs winners, prices look plausible, field coverage

```
====================================================================
  READINESS — kaisersolver   [REVIEW]
  bnb · prod · blocks 127207259..127305544
====================================================================
  window  : blocks 127207259..127305544 | attempted auctions span 2026-10-12T12:01:16Z → 2026-10-13T00:18:34Z (12.29 h, auctionStartBlock timestamps; 249.99 attempted/h)
            settlements found 4431 / auctions formed 3568 / attempted 3072 / replayed 3072 / returned 1780
            excluded 1403 — top: [('tx_uid_mismatch', 908), ('body_uid_mismatch', 494), ('rpc_fetch_failed', 1)]
  budget  : 2.350 s (observed, BUDGETS_S[bnb].settle) | original-deadline upper bound p50 3.279 s / p95 3.858 s over 3072 attempted
  thresholds: profile 'default' — answer_rate >=90/50% · transport 0/<=1% · deadline_miss 0/<=1% · latency p95 <=0.5x/1x budget · validity >=90/50% · capture >=50/>0% · min_evidence 500
  [PASS] reached auctions         3072 auctions attempted
  [PASS] no transport errors      0 transport errors
  [PASS] answers reliably         100% returned a parseable response inside the budget (incl. legitimate empty solutions)
  [PASS] bid coverage             58% of answered auctions carried >=1 solution
  [PASS] inside the deadline      0 deadline misses
  [PASS] latency headroom         p95 1098 ms of a 2350 ms budget (observed); PASS <= 0.5x, WARN <= 1x
  [PASS] solutions are valid      100% of bid auctions had >=1 valid solution [feasibility+eligibility+udcp+fairness; fairness_filtered 1, zero_surplus 0, udcp 4648 checked / 0 violations]
  [WARN] competitive vs winners   0% of winner surplus captured (coverage-adjusted; 0% conditional on answering; basis mix {'exact_uniform': 2885, 'mixed': 111, 'wrapper_lower_bound': 76})
  [WARN] prices look plausible    115 auction(s) flagged implausible_surplus
  [PASS] scan coverage            every block in the window was scanned
  [WARN] field coverage           4431 settlements, 3568 auctions formed, 1403 excluded (32% of field; top reasons [('tx_uid_mismatch', 908), ('body_uid_mismatch', 494), ('rpc_fetch_failed', 1)])
  ----------------------------------------------------------------
  answered            : 3072/3072  (100%)   bids: 1780   transport: 0   deadline misses: 0
  latency             : p50 641 ms / p95 1098 ms / max 1850 ms   (budget 2350 ms; answered-only incl. late answers)
  surplus vs winners  : 0% captured (coverage-adjusted) / 0% conditional   (1780/3072 valid)
  per-bid median      : 0.864x ours/winner over 1665 unflagged bid auction(s)
  winner basis mix    : {'exact_uniform': 2885, 'mixed': 111, 'wrapper_lower_bound': 76}
  validity basis      : feasibility+eligibility+udcp+fairness

  Reproduce this run:
    cow-backtester --chain bnb --env prod --from-block 127207259 --to-block 127305544 --bodies-dir <archive> --solve-timeout 2.35 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url <solver-url> --solver-name kaisersolver --readiness
    # cow-backtester 0.11.2 · engine build sha: ec1e86504

  Note: replays live liquidity against archived auctions — a readiness
  signal, not a settlement guarantee. Pair with self-hosted shadow before prod.
```

## Reading the verdict

- **WARN — competitive vs winners:** 0% of winner surplus captured (coverage-adjusted; 0% conditional on answering; basis mix {'exact_uniform': 2885, 'mixed': 111, 'wrapper_lower_bound': 76})
- **WARN — prices look plausible:** 115 auction(s) flagged implausible_surplus
- **WARN — field coverage:** 4431 settlements, 3568 auctions formed, 1403 excluded (32% of field; top reasons [('tx_uid_mismatch', 908), ('body_uid_mismatch', 494), ('rpc_fetch_failed', 1)])

**On "prices look plausible":** the tool flags an auction when our *claimed* surplus exceeds ten times what the on-chain winner actually delivered. 115 auctions are flagged here ({'exact_uniform': 102, 'wrapper_lower_bound': 7, 'mixed': 6} by winner-baseline type; largest ratio 138,089×). Claimed prices are not simulated, so these rows are treated as suspect rather than as evidence of out-competing the winner. 24 of the 115 had a winner surplus below a tenth of the window's median winner surplus (flagged-row median 0.0000361 BNB vs window median 0.0000777 BNB). The capture figure above includes them; a reader who wants a conservative number can subtract them.

**Bottom line for BNB:** REVIEW: no failing check; 3 warn-level: competitive vs winners, prices look plausible, field coverage. Answered 100 % of 3072 attempted auctions, bid on 58 % of them, captured 0.0 % of the winners' surplus (coverage-adjusted), p95 latency 1098 ms against a 2350 ms budget.

## Method notes
- Regenerated 2026-10-13T10:46:19Z over a fixed block window (127207259–127305544) from the watch run's rows: every settled auction whose settlement block lies inside it, one row per auction id (`tools/build_report.py`).
- Counters are cumulative over the watch run (the tool's per-cycle screens are summed; each auction counted once). The verdict is the tool's own `readiness_report` over those counters.
- The watch ran in 1 launch(es). 2 auction(s) seen more than once keep their first replay; 0 block(s) were scanned more than once; 0 cycle(s) lay wholly inside blocks already scanned and were left out.
- A "deadline miss" is a timeout OR an answer that arrived after the budget, mirroring the driver; none occurred. "transport" is every other failure; none occurred.
- Capture is coverage-adjusted: auctions we failed to answer would keep the winner in the denominator.
- Bodies were archived (`--archive-bodies`, SHA-256 per body) so the run is reproducible after the public instance bucket evicts them; the archive is ours, hence `<archive>` in the reproduce line.
- Signal, not guarantee: replays quote live liquidity against archived auctions.

## Reproduce

```
    cow-backtester --chain bnb --env prod --from-block 127207259 --to-block 127305544 --bodies-dir <archive> --solve-timeout 2.35 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url <solver-url> --solver-name kaisersolver --readiness
    # cow-backtester 0.11.2 · engine build sha: ec1e86504
```

Body archive manifest (root `manifest.jsonl` of the `--archive-bodies` directory, one line per archived body with its SHA-256):
SHA-256 `1a55781c4b74d85030f2098e48b509e7ebe222c35af0407364dcb1a0af0cb518` · 3,568 lines · 2026-10-13 00:18:57Z.
