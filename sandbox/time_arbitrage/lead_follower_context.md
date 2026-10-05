# lead_follower: development and operating context

Updated: 4 October 2026. Repository: `C:/Workspace/polytrader`.
This is a handoff for continuing work on the bot. Check current source before
changing behavior; do not assume a previous process is still running.

## Purpose and current status

`polytrader.bot.lead_follower` is a public-data detector and **paper trader** for
related Polymarket deadline contracts. Its research question is whether concentrated
directional trading in a normally quiet leader precedes a response in less-liquid
followers. It is not guaranteed arbitrage and does not assume equal probabilities
across deadlines.

The bot is implemented. Strategy v2 uses **trade bursts**, replacing v1's mandatory
leader price movement. It places no orders, needs no wallet, excludes transaction
fees by user request, and includes spread, depth consumption and extra slippage.
The former live-feed timestamp issue is fixed as described below. Passing tests
does not establish profitable trading or reliable operation for every live market.

The separate `time_arbitrage` bot detects earlier-NO/later-YES opportunities.
Both can run concurrently in separate processes. Each process owns its own feed;
multiple lead/follower families within one session share one subscription.

## Signal rules and research defaults

The leader is selected explicitly. Only its YES-token trade tape contributes to
activity, avoiding aggregation of complementary YES/NO reports. BUY/SELL is treated
as feed-reported token direction, not certified aggressor-side information. Identical
messages are retained: a transaction hash is not a unique fill identifier.

Default entry qualification requires:

- 60 seconds of uninterrupted feed observation.
- At least 3 leader trades and 100 traded shares within 30 seconds.
- Absolute `(BUY shares - SELL shares) / total shares` of at least 0.6.
- Activity acceleration score of at least 3.
- Follower YES bid/ask prices unchanged for at least 10 seconds, and the latest
  leader trade at least 5 seconds after that price change.
- If leader midpoint movement is nonzero, absolute follower movement no greater
  than 25% of the leader's movement.
- Valid selected books, available entry depth, spread and position/cash limits.

The baseline covers up to 3,600 seconds **before** the current activity window:

```text
expected_count = preceding_trade_count / observed_preceding_seconds * window_seconds
burst_score = current_trade_count / max(1, expected_count)
```

This recognizes bursts after observed silence without infinite ratios. Partial
baseline coverage is logged; missing pre-session history is not treated as silence.
The score is a heuristic, not statistical significance.

Positive imbalance buys follower YES; negative imbalance buys follower NO.
Leader midpoint changes over 5/10/30 seconds, spread and book reactions are logged
features, not mandatory triggers. `min_move_pp` defaults to 0; setting it positive
explicitly requires an aligned price move as additional confirmation. The former
`volume_ratio` config setting is retired; use `min_burst_score` instead.

## Candidate collection

On each new leader trade, once there are at least 2 trades in the activity window,
write a `burst_candidate` per follower **before entry filtering**. Include rejected
candidates during warm-up, weak flow, low volume, insufficient acceleration,
follower movement, fresh quotes, invalid books, wide entry spreads, inadequate
depth, occupied positions and cooldowns. Log all rejection reasons, qualification,
and IDs linking accepted candidates to signals and positions.

Features include 10s/30s counts and volume, BUY/SELL shares, imbalance, trades per
second, inter-arrival statistics, previous-trade and pre-cluster gaps, baseline
coverage, price movements, follower quotes/age and leader depth changes. Depth
reductions can be cancellations; they are not proof of consumed liquidity.
Unavailable historical features remain null. Repeated cluster updates are correlated
research rows, not independent opportunities. Timers do not repeat candidate rows.

## Paper execution

Defaults: 10 target shares, $20 per-position cash cap, $100 concurrent cash cap,
maximum entry spread $0.04, and an extra $0.001 per-share slippage allowance on each
leg. Entry/exit latency is 0.25 seconds; sell availability is delayed 1 second.
These delays are modeling assumptions, not verified exchange settlement guarantees.

Buy through observed ask depth. After sell availability, request the first full-size
bid-side exit producing at least $0.01 total profit after slippage. Recheck after
exit latency; cancel a profit exit if the opportunity disappears. Also request exits
at a $1 loss or 300-second holding limit. A stop-loss request stays committed through
latency even if prices recover. Insufficient exit depth leaves an unresolved position.

There are no partial fills or assumed resting-limit fills. One pending/open position
per follower applies across groups; cooldown is 60 seconds and restarts on exit.
Do not subtract spread/depth slippage twice. Closed P&L excludes open liquidation
marks, and unresolved positions do not become wins. Sessions do not resume positions
after a process restart. Actual fills, order minimums and queue priority are not certified.

## Running and outputs

From the repository root, this command selects the user's December leader and
November/October followers for a four-hour run, with price confirmation disabled:

```text
.venv\Scripts\python.exe -m polytrader.bot.lead_follower "https://polymarket.com/event/russia-x-ukraine-ceasefire-agreement-by" --leader "russia-x-ukraine-ceasefire-agreement-by-december-31-2026" --follower "russia-x-ukraine-ceasefire-agreement-by-november-30-2026" --follower "russia-x-ukraine-ceasefire-agreement-by-october-31-2026" --min-move-pp 0 --duration 14400
```

Use `--validate` for discovery without streaming; `--config <file.toml>` supports
multiple groups. Config-relative output paths use the config directory. Omitting
followers selects the other eligible binary markets; it does not verify comparable
resolution rules. Rediscover markets if eligibility changes.

Unique sessions under `data/lead_follower/` contain:

- `metadata.json`: settings, selected markets, assumptions and strategy version.
- `inputs.jsonl`: normalized trades, full changed depth, observation times and health.
- `events.jsonl`: research candidates and position/feed lifecycle events.
- `summary.json`: candidate/rejection counts, closed P&L, holding times and open marks.

`python -m polytrader.bot.lead_follower --replay <session-directory>` is offline.
New sessions record `strategy_version=2`; old metadata without it uses the frozen
v1 price detector. Do not silently reinterpret old sessions with new signal rules.
Recording is unbounded; use duration limits and monitor storage.

## Feed health: quiet snapshot timestamp issue fixed

The user's session `data/lead_follower/20261004T205041Z-37113c18` repeatedly reported:

```text
signals_paused   Wire message outside feed delay tolerance
```

Previously, `runner.py` compared every timestamped wire message against local UTC
with a 10-second tolerance. The log showed December book state timestamped
`20:50:06.739 UTC` arriving at `20:50:42.119 UTC`, about 35 seconds later. November
also arrived with an older timestamp. These triggered reconnects and warm-up resets;
repeated failures can exhaust retries.

**Fixed:** source-timestamp age checks have been removed for both book messages and
trade messages. No maximum age applies to trades, books or quote changes. A quiet
market remains connected while its WebSocket transport is healthy. The old
`max_feed_delay_seconds` setting/flag is accepted but ignored for compatibility.

Only transport closure/errors, heartbeat failure or malformed/unrecoverable stream
state cause resynchronization. Retry attempts continue with bounded backoff until
shutdown. Missing initial snapshots do not force reconnects on a healthy transport;
those books remain ineligible until a snapshot arrives. Out-of-order book updates
remain consistency errors; this is sequence validation, not wall-clock age checking.

Input logs preserve wire event type/source timestamp and UTC receipt time. Trade
records include source-to-receipt seconds for analysis. Quote age and time since
previous trades remain strategy features. Regression coverage includes three
simulated hours without market activity, arbitrarily old snapshots/trades, healthy
ping/pong without market messages, real disconnect/resubscription and feature ages.

Guiding rule: **Market inactivity is not connection inactivity.** Do not restore
timestamp-age reconnection logic. Restart existing bot processes to load this fix.

## Code map and verification

Paths below are under `src/polytrader/bot/lead_follower/` unless specified.

| File | Responsibility |
|---|---|
| `config.py`, `__main__.py` | Settings validation, TOML and CLI |
| `discovery.py` | Resolve explicit leader/follower families and YES/NO tokens |
| `engine.py` | v2 candidates, filters, positions, P&L and summaries |
| `features.py` | Burst statistics and observable book changes |
| `feed.py` | Normalize leader YES trade messages |
| `runner.py` | Subscription ownership, timestamps, timers and input recording |
| `reporting.py` | Session files and lifecycle logging |
| `replay.py`, `legacy_price_engine.py` | Version-aware replay and frozen v1 behavior |
| `src/polytrader/orderbook/service.py` | Shared depth service with synchronous event hook |
| `tests/test_lead_follower.py` | Detector, execution, feed and replay tests |

The event hook runs after complete wire-message processing, before book notifications
can coalesce. Disconnects reset trade history/warm-up and cancel pending entries;
existing positions remain tracked. On healthy transport, invalid selected books
block entries but still permit research candidates and exits on healthy own books.

Last completed verification: **159 offline tests passed**, including the new
feed-health regressions. Earlier CLI/example-config checks and a 10-second
December/November live book smoke test passed; that earlier live test did not
evaluate returns or expose the subsequently fixed quiet-snapshot issue.

```text
.venv\Scripts\python.exe -m unittest discover -s tests -q
```

Preserve research transparency: rejected candidates, losses, missing data and feed
gaps must remain visible. Do not tune implementation to manufacture positive P&L.
See [the usage guide](../../src/polytrader/bot/lead_follower/README.md) and
[example configuration](../../src/polytrader/bot/config/lead_follower.example.toml).
