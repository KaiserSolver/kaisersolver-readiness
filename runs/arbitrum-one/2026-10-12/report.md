# Readiness report: kaisersolver on arbitrum-one — 2026-10-12 → 13

The Readiness Standard, applied to ourselves, with cow-backtester **0.11.2**. This run uses the observed per-request budget for the chain and the standard's 500-auction evidence floor: `--readiness --watch 60`, every settled auction in the window replayed against the production Arbitrum instance, serving the CoW driver's live traffic during the run.

- **Window:** blocks 514106940–514272751 (fixed), 2026-10-12T12:07:41Z → 2026-10-13T00:18:17Z (12.2 h of chain time)
- **Sample:** 769 auctions attempted (floor 500), 769 answered, 367 with ≥1 solution
- **Budget:** 4.84 s (observed)
- **Replay lag:** median 0.6 min between an auction settling and our replay of it — live liquidity at replay time is close to auction-time liquidity
- **Fairness:** the CIP-67 filter was applied to our solutions on every bidding auction with a competition record (367 of 367); 9 of our solutions were filtered, exactly as the protocol would have filtered them
- **Capture:** 57.75 % of the on-chain winners' surplus, coverage-adjusted (the screen below rounds to whole percent)
- **Own wins:** 18 of the 769 attempted auctions were settled on-chain by kaisersolver's own account (counted per auction)
- **Per bid:** on the 356 unflagged auctions we entered, our surplus was 0.956× the winner's at the median. The capture figure is sum-weighted: the five largest auctions by winner surplus hold 28 % of the window's winner surplus, and we entered 3 of them
- **Verdict:** **REVIEW** — no failing check; 2 warn-level: bid coverage, prices look plausible

```
====================================================================
  READINESS — kaisersolver   [REVIEW]
  arbitrum-one · prod · blocks 514106940..514272751
====================================================================
  window  : blocks 514106940..514272751 | attempted auctions span 2026-10-12T12:07:34Z → 2026-10-13T00:18:10Z (12.18 h, auctionStartBlock timestamps; 63.15 attempted/h)
            settlements found 825 / auctions formed 774 / attempted 769 / replayed 769 / returned 367
            excluded 20 — top: [('tx_uid_mismatch', 6), ('rpc_fetch_failed', 5), ('body_uid_mismatch', 5)]
  budget  : 4.840 s (observed, BUDGETS_S[arbitrum-one].settle) | original-deadline upper bound p50 5.961 s / p95 6.449 s over 768 attempted
  thresholds: profile 'default' — answer_rate >=90/50% · transport 0/<=1% · deadline_miss 0/<=1% · latency p95 <=0.5x/1x budget · validity >=90/50% · capture >=50/>0% · min_evidence 500
  [PASS] reached auctions         769 auctions attempted
  [PASS] no transport errors      0 transport errors
  [PASS] answers reliably         100% returned a parseable response inside the budget (incl. legitimate empty solutions)
  [WARN] bid coverage             48% of answered auctions carried >=1 solution
  [PASS] inside the deadline      0 deadline misses
  [PASS] latency headroom         p95 2018 ms of a 4840 ms budget (observed); PASS <= 0.5x, WARN <= 1x
  [PASS] solutions are valid      99% of bid auctions had >=1 valid solution [feasibility+eligibility+udcp+fairness; fairness_filtered 9, zero_surplus 0, udcp 1174 checked / 0 violations]
  [PASS] competitive vs winners   58% of winner surplus captured (coverage-adjusted; 58% conditional on answering; basis mix {'exact_uniform': 692, 'wrapper_lower_bound': 66, 'mixed': 11})
  [WARN] prices look plausible    11 auction(s) flagged implausible_surplus
  [PASS] scan coverage            every block in the window was scanned
  [PASS] field coverage           825 settlements, 774 auctions formed, 20 excluded (2% of field; top reasons [('tx_uid_mismatch', 6), ('rpc_fetch_failed', 5), ('body_uid_mismatch', 5)])
  ----------------------------------------------------------------
  answered            : 769/769  (100%)   bids: 367   transport: 0   deadline misses: 0
  latency             : p50 1025 ms / p95 2018 ms / max 3654 ms   (budget 4840 ms; answered-only incl. late answers)
  surplus vs winners  : 58% captured (coverage-adjusted) / 58% conditional   (364/769 valid)
  per-bid median      : 0.956x ours/winner over 356 unflagged bid auction(s)
  winner basis mix    : {'exact_uniform': 692, 'wrapper_lower_bound': 66, 'mixed': 11}
  validity basis      : feasibility+eligibility+udcp+fairness

  Reproduce this run:
    cow-backtester --chain arbitrum-one --env prod --from-block 514106940 --to-block 514272751 --bodies-dir <archive> --solve-timeout 4.84 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url <solver-url> --solver-name kaisersolver --readiness
    # cow-backtester 0.11.2 · engine build sha: 571b7e8fc

  Note: replays live liquidity against archived auctions — a readiness
  signal, not a settlement guarantee. Pair with self-hosted shadow before prod.
```

## Reading the verdict

- **WARN — bid coverage:** 48% of answered auctions carried >=1 solution
- **WARN — prices look plausible:** 11 auction(s) flagged implausible_surplus

**On "prices look plausible":** the tool flags an auction when our *claimed* surplus exceeds ten times what the on-chain winner actually delivered. 11 auctions are flagged here ({'exact_uniform': 10, 'mixed': 1} by winner-baseline type; largest ratio 7,701×). Claimed prices are not simulated, so these rows are treated as suspect rather than as evidence of out-competing the winner. 7 of the 11 had a winner surplus below a tenth of the window's median winner surplus (flagged-row median 0.0000088 ETH vs window median 0.0001113 ETH). The capture figure above includes them; a reader who wants a conservative number can subtract them.

**Bottom line for Arbitrum:** REVIEW: no failing check; 2 warn-level: bid coverage, prices look plausible. Answered 100 % of 769 attempted auctions, bid on 48 % of them, captured 57.7 % of the winners' surplus (coverage-adjusted), p95 latency 2018 ms against a 4840 ms budget.

## Method notes
- Regenerated 2026-10-13T10:46:18Z over a fixed block window (514106940–514272751) from the watch run's rows: every settled auction whose settlement block lies inside it, one row per auction id (`tools/build_report.py`).
- Counters are cumulative over the watch run (the tool's per-cycle screens are summed; each auction counted once). The verdict is the tool's own `readiness_report` over those counters.
- The watch ran in 1 launch(es). 0 auction(s) seen more than once keep their first replay; 0 block(s) were scanned more than once; 0 cycle(s) lay wholly inside blocks already scanned and were left out.
- A "deadline miss" is a timeout OR an answer that arrived after the budget, mirroring the driver; none occurred. "transport" is every other failure; none occurred.
- Capture is coverage-adjusted: auctions we failed to answer would keep the winner in the denominator.
- Bodies were archived (`--archive-bodies`, SHA-256 per body) so the run is reproducible after the public instance bucket evicts them; the archive is ours, hence `<archive>` in the reproduce line.
- Signal, not guarantee: replays quote live liquidity against archived auctions.

## Reproduce

```
    cow-backtester --chain arbitrum-one --env prod --from-block 514106940 --to-block 514272751 --bodies-dir <archive> --solve-timeout 4.84 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url <solver-url> --solver-name kaisersolver --readiness
    # cow-backtester 0.11.2 · engine build sha: 571b7e8fc
```

Body archive manifest (root `manifest.jsonl` of the `--archive-bodies` directory, one line per archived body with its SHA-256):
SHA-256 `997f41b7a8ab919eb5d2edcfc856924ed470facd709e64e0184ad6ea5c11ed03` · 774 lines · 2026-10-13 00:18:32Z.
