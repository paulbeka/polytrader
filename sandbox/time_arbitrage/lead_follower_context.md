# lead_follower: implementation context

## Objective and scope

Build `polytrader.bot.lead_follower`, a live detector and paper-trading tracker for delayed price adjustment between related deadline contracts. A sudden directional trade burst in a normally quiet leader may precede a less-liquid follower's response, even without a leader price move. This is a statistical hypothesis, not a guaranteed deadline arbitrage.

Version one places no orders. It logs signals, simulates executable entries and exits, and reports hypothetical profit or loss. Transaction fees are explicitly zero; include spread, depth slippage and a configurable adverse slippage allowance on both legs. Implementation authorized and completed; see `src/polytrader/bot/lead_follower/README.md` for the implemented behavior and defaults.

## Subscription and coexistence

Quick-start command:

```powershell
python -m polytrader.bot.lead_follower "<event-url-or-slug>" --leader "<market-slug>" --duration 3600
```

Discover the event's eligible YES/NO markets. Require an explicit leader initially; default followers to the other eligible deadline markets, with repeatable `--follower` filters. Print resolved markets and tokens before scanning. Membership in one event does not establish comparable rules: users select a compatible deadline family. Later deadlines need not be leaders, and followers need not converge to the leader's absolute price.

Also support `--config <file.toml>` for multiple event groups and thresholds, plus `--validate` for discovery without streaming. Reuse `OrderBookService` and Decimal-based depth calculations. Keep strategy logic independent of connection ownership so a future combined runner can fan out one feed to multiple strategies. Initially, `lead_follower` and `time_arbitrage` can run concurrently as separate processes with separate logs and subscriptions.

## Signal and data requirements

Maintain rolling histories per market. After a configurable warm-up, evaluate:

- Trade counts, shares, BUY/SELL volumes and inter-arrival statistics over 10/30 seconds; leader YES midpoint changes over 5/10/30 seconds are features, not mandatory triggers.
- Actual trade share volume relative to a preceding baseline, and signed buying/selling imbalance. Version one uses the leader's YES-token tape only, avoiding double-counting complementary YES/NO reports. NO-only activity is excluded. BUY/SELL is interpreted as feed-reported direction; the public schema does not explicitly certify aggressor-side semantics.
- Follower movement over the same window: when leader movement is nonzero, it must be at most 25% as large in absolute terms.
- Time since the follower's best bid/ask prices changed, relative to the latest leader trade, plus spread and available depth. Track size changes separately so replenishment does not reset price age.

Strategy v2 defaults require three trades and 100 shares in 30 seconds, absolute imbalance >= 0.6, score >= 3 and stale follower prices. The score is current count divided by max(1, count expected from preceding observed trade frequency), so a zero-volume baseline can still identify a burst. Use up to one hour of preceding history, with 60 seconds of warm-up and explicit partial-coverage metadata. `min_move_pp=0` disables price confirmation; positive values explicitly opt in. Direction comes from imbalance. Record a research candidate per new leader trade/follower once the window contains two trades, including all rejected candidates, feature values and exact reasons. Use only information received by decision time. Disconnected or stale-status books are invalid data, not opportunities. Preserve v1 replay through its frozen detector; new sessions record strategy version 2.

The orderbook service now has an opt-in synchronous event hook after each complete wire message. The bot normalizes individual trade events without coalescing them like book notifications. Invalid trade inputs, delayed messages or reconnect gaps suspend signals and restart warm-up; quote updates are never presented as traded volume. This remains an observed feed, not independently reconciled exchange-wide activity.

## Hypothetical entry and immediate profitable exit

A positive leader imbalance buys follower YES; a negative imbalance buys follower NO. This allows both directions without assuming an existing position to sell short.

At detection, size within configured share and cash limits. Simulate buying against current ask depth, using volume-weighted cost plus adverse slippage. Reject insufficient depth or invalid books. Log the signal even if entry is rejected. Use configurable entry/exit latency, taking the first observed valid book after each delay; no retrospective fill at a missed quote.

Once the simulated entry is filled and the configured sell-availability delay has elapsed, evaluate every subsequent book update. Sell at the first opportunity where available bid depth would close the entire position above its total entry cost plus a configurable minimum profit. Recheck profitability after exit latency; if it disappears, keep monitoring. Use marketable bid-side exits rather than assuming a resting ask will fill. Version one closes the full position, with no partial exits.

`paper P&L = bid-depth sale proceeds - ask-depth purchase cost - entry/exit slippage allowances`

Depth consumption already captures spread and depth slippage; do not subtract them twice. A leader move is not a reliable estimate of follower upside. Version one records that hypothesis and measures subsequent outcomes, rather than claiming a profitable exit is known at entry.

Add configurable maximum holding time and stop-loss. Attempt a depth-based exit when either triggers, recording losses as well as wins. If exit liquidity is unavailable, retain an unresolved position. At shutdown, report open positions and their available liquidation marks separately from closed P&L. Allow one open position per follower market and a signal cooldown; never count repeated triggers as independent profits.

## Logs, reporting and verification

Write unique sessions under `data/lead_follower/`: resolved metadata/configuration, append-only lifecycle JSONL, and a final summary. Record UTC/source/receipt times, signal and position IDs, tokens, direction, features, depth used, size, modeled fills, slippage, rejection reasons, exit reason, holding time, P&L and feed interruptions. Label all results hypothetical and fee-excluded.

Summaries include detections, rejected entries, opened/closed/unresolved positions, wins/losses, closed P&L and time to exit, grouped by event and follower. Preserve signals that never became profitable to avoid winner-only reporting.

Use deterministic offline tests for both directions, time alignment, warm-up and gaps, depth/slippage arithmetic, delayed sell availability, disappearing exit opportunities, losing/time-limited exits and duplicate-signal suppression. Existing compact quote recordings lack trades and full depth; this bot records normalized trades, full changed depth and health/timing observations in `inputs.jsonl`, with offline `--replay` support.
