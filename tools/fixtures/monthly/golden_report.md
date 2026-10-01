# Readiness report: kaisersolver on arbitrum-one — 2026-03-02

The Readiness Standard, applied to ourselves, with cow-backtester **0.11.3**. This run uses the observed per-request budget for the chain and the standard's 500-auction evidence floor: `--readiness --watch 60`, every settled auction in the window replayed against the production Arbitrum instance.

- **Window:** blocks 491743288–491977488 (fixed), 2026-03-02T00:00:00Z → 2026-03-02T16:15:00Z (16.2 h of chain time)
- **Sample:** 40 auctions attempted (floor 500), 37 answered, 31 with ≥1 solution
- **Budget:** 4.84 s (observed)
- **Replay lag:** median 101.4 min between an auction settling and our replay of it — live liquidity at replay time is close to auction-time liquidity
- **Fairness:** the CIP-67 filter was applied to our solutions on every bidding auction with a competition record (31 of 31); 0 of our solutions were filtered, exactly as the protocol would have filtered them
- **Capture:** 96344.31 % as the tool computes it (screen below), **9917191.78 %** with the 1 valuation artefact(s) described under *Reading the verdict* excluded — both coverage-adjusted
- **Own wins:** 14 of the 40 attempted auctions were settled on-chain by kaisersolver's own account (counted per auction)
- **Per bid:** on the 28 unflagged auctions we entered, our surplus was 1.000× the winner's at the median. The capture figure is sum-weighted: the five largest auctions by winner surplus hold 20 % of the window's winner surplus (artefact excluded), and we entered 4 of them
- **Verdict:** **NOT READY** — 2 failing: no transport errors, inside the deadline; 2 warn-level: prices look plausible, winner surplus plausible

```
====
  READINESS — kaisersolver   [NOT READY]
  arbitrum-one · prod · blocks 1..2
====
```

## Reading the verdict

- **FAIL — no transport errors:** 1/40 (2.50%) transport errors [SolverHttpError]
- **FAIL — inside the deadline:** 2/40 (5.00%) deadline misses [DeadlineExceeded: {'late': 1, 'timeout': 1}]
- **WARN — prices look plausible:** 2 auction(s) flagged implausible_surplus
- **WARN — winner surplus plausible:** 1 auction(s) excluded from the capture verdict as valuation artefact(s) — auction 8339062: 0.007680 ETH winner surplus, 102x the rest of the attempted window combined (competition referenceScore 0) [rule: > 100x the rest, >= 20 attempted]
- **Valuation artefact — auction 8339062:** decoded winner surplus 0.007680 ETH (102× the rest of the attempted window combined) against a competition reference score of 0. The tool excludes it from the capture its verdict reads; both figures print on the screen.

**On "prices look plausible":** the tool flags an auction when our *claimed* surplus exceeds ten times what the on-chain winner actually delivered. 2 auctions are flagged here ({'exact_uniform': 2} by winner-baseline type; largest ratio 1,619,797×). Claimed prices are not simulated, so these rows are treated as suspect rather than as evidence of out-competing the winner. 0 of the 2 had a winner surplus below a tenth of the window's median winner surplus (flagged-row median 0.0000023 ETH vs window median 0.0000023 ETH). The capture figure above includes them; a reader who wants a conservative number can subtract them.

**Bottom line for Arbitrum:** NOT READY: 2 failing: no transport errors, inside the deadline; 2 warn-level: prices look plausible, winner surplus plausible. Answered 92 % of 40 attempted auctions, bid on 84 % of them, captured 9917191.8 % of the winners' surplus (coverage-adjusted), p95 latency 770 ms against a 4840 ms budget.

## Method notes
- Regenerated 2026-10-02T06:00:00Z over a fixed block window (491743288–491977488) from the watch run's rows: every settled auction whose settlement block lies inside it, one row per auction id (`tools/build_report.py`).
- Counters are cumulative over the watch run (the tool's per-cycle screens are summed; each auction counted once). The verdict is the tool's own `readiness_report` over those counters.
- The watch ran in 1 launch(es). 0 auction(s) seen more than once keep their first replay; 0 block(s) were scanned more than once; 0 cycle(s) lay wholly inside blocks already scanned and were left out.
- A "deadline miss" is a timeout OR an answer that arrived after the budget, mirroring the driver; 2 occurred. "transport" is every other failure; 1 occurred.
- Capture is coverage-adjusted: auctions we failed to answer would keep the winner in the denominator.
- Signal, not guarantee: replays quote live liquidity against archived auctions.

## Reproduce

```
    cow-backtester --chain arbitrum-one --env prod --from-block 491743288 --to-block 491977488 --bodies-dir tools/fixtures/monthly/archive/bodies-0.11 --solve-timeout 4.84 --min-evidence 500 --compete \
        --rpc-url <rpc> --solver-url http://solver.invalid/prod/arbitrum-one --solver-name kaisersolver --readiness
    # cow-backtester 0.11.3 · engine build sha: <fill in: the endpoint's boot-line git_sha (not exposed over /solve)>
```
