# Lead/follower paper trader

Detect directional trade bursts in a leader and delayed responses in related deadline
contracts. Leader price movement is a logged feature, not a required trigger. Measure hypothetical
positions against observed bid/ask depth. No wallet, orders or private credentials.
Fees are zero by design. Spread, depth consumption and an additional adverse
slippage allowance are included on entry and exit.

## Run

Install the existing live extra, discover market slugs, and select a leader:

```powershell
.venv/Scripts/python.exe -m pip install -e ".[live]"
.venv/Scripts/python.exe -m polytrader markets "<event-url-or-slug>"
.venv/Scripts/python.exe -m polytrader.bot.lead_follower "<event-url-or-slug>" --leader "<market-slug>" --validate
.venv/Scripts/python.exe -m polytrader.bot.lead_follower "<event-url-or-slug>" --leader "<market-slug>" --follower "<other-market-slug>" --duration 3600
```

Repeat `--follower`, or omit it to use other eligible YES/NO markets in that event.
The leader is explicit; it is not selected by deadline or assumed to be more liquid.
Check that the printed selection has comparable resolution rules. Auto-selection
does not parse deadlines or prove that markets share the same underlying event.

All settings are CLI flags in quick mode, for example `--shares 20 --min-trades 4
--slippage-per-share 0.002 --sell-delay-seconds 2`. Use `--help` for defaults. Omit
`--duration` to run until Ctrl+C. `--output` changes the session root.

For multiple events, copy and edit
[`lead_follower.example.toml`](../config/lead_follower.example.toml), then run:

```powershell
.venv/Scripts/python.exe -m polytrader.bot.lead_follower --config path/to/lead_follower.toml --duration 3600
```

Config-relative output paths are resolved from the file's directory. Quick-mode
paths are relative to the working directory. Config mode does not accept selection
or settings overrides. Validation performs public discovery but creates no session.
Exit codes: 0 normal/interrupted, 2 config/discovery/replay error, 3 runtime failure.

`time_arbitrage` can run alongside this bot in a separate terminal. This bot's
configured families share one subscription; the two strategies in separate
processes use independent subscriptions. Neither places orders.

## Signals

Defaults allow paper entries after 60 seconds of uninterrupted feed observation.
The activity window is 30 seconds; the trailing baseline covers up to 3,600 seconds
strictly before that window. A paper signal requires:

- At least 3 leader YES trades and 100 total traded shares in the activity window.
- Absolute BUY-minus-SELL share imbalance of at least 0.6. Positive flow buys
  follower YES; negative flow buys follower NO, regardless of leader price direction.
- Trade-count acceleration score of at least 3, defined below.
- If the leader midpoint moved, absolute follower YES movement at most 25% as large.
  With zero leader movement this relative-movement filter is skipped.
- Follower YES bid/ask prices unchanged for at least 10 seconds, and the latest
  leader trade at least 5 seconds after that price change. Size changes do not
  reset price age. No leader price change is required for this age comparison.

`expected_count = prior_trade_count / observed_prior_seconds * activity_window_seconds`

`burst_score = recent_trade_count / max(1, expected_count)`

The one-trade floor keeps the score finite on sparse or empty baselines: three
recent trades after an observed quiet period score 3. A steady active market whose
recent frequency matches its preceding frequency scores about 1. This is a research
heuristic, not statistical significance. Unknown pre-session history is never
counted as observed silence: baseline duration/completeness is logged and the rate
uses only the time actually observed. Candidates during warm-up are still recorded.

`--min-move-pp` defaults to **0 (disabled)**. An explicitly positive value requires
an aligned leader price move of that size in addition to the burst. The former
`volume_ratio` setting/flag is retired; migrate configs to `min_burst_score`, whose
meaning is trade-count acceleration, and set `min_move_pp = "0"` to disable old
price confirmation. See the updated example config.

The leader spread and book reaction are features only. The selected entry token's
spread limit remains an execution filter (four cents by default), as do cash and
depth limits. No equality between leader and follower probabilities is assumed.

Trades come from public `last_trade_price` messages described in Polymarket's
[market-stream documentation](https://docs.polymarket.com/market-data/realtime-data).
We interpret the message's reported BUY/SELL as token-direction flow; the public
schema does not explicitly certify that field as the aggressor's side. Logs retain
that distinction. Only the leader's YES token contributes to volume and imbalance,
avoiding aggregation of complementary YES/NO reports. NO-only activity is therefore
not a signal input in this version. Separate identical messages are retained because
transaction hashes do not uniquely identify fills. The feed is an observed trade
sample, not an independently reconciled accounting of exchange-wide volume.

Signals use local monotonic receipt times. Source timestamps are logged and checked
against a configurable delay tolerance. Disconnects, malformed leader trades and
late wire messages reset warm-up; delayed/invalid messages request resynchronization.
All selected books must be valid before new paper entries resume. With a healthy
transport, bursts are still logged if a selected book is unavailable, with an explicit
rejection and null unavailable book features. Open positions remain tracked and can
exit once their own books and the transport are healthy. Quiet prices alone do not
make a healthy book invalid.

## Positions and exits

Positive-imbalance signals buy follower YES; negative-imbalance signals buy follower NO. Entry uses the
current ask ladder after entry latency, with a 10-share target and $20 per-position
cap by default. Insufficient target-size ask depth rejects entry. Budget-limited
size can be reduced, rounded down to 0.01 shares. $100 caps concurrent committed
and reserved cash; realized gains do not raise that cap. A follower can have only
one pending/open position across all groups.

The default extra allowance is $0.001 per share on each leg. Depth already captures
spread and market impact, so these are not subtracted again. Entries and exits use
full observed size; exchange order minimums, queue priority, settlement guarantees
and actual fills are not certified by this paper simulation.

After the one-second sell-availability delay, check each processed book event and
timer observation. Trigger a full-size marketable bid exit when total proceeds
after slippage exceed entry cost by at least $0.01. Wait exit latency and recheck
profitability: a vanished profit cancels that exit attempt. A $1 loss or 300-second
holding limit also requests an exit. Stop-loss requests remain committed through
latency even if the price recovers. Insufficient exit depth leaves the position
unresolved. There are no resting-limit fill assumptions or partial fills.

Positions can remain open at shutdown; their liquidation marks are separate from
closed P&L. Marks on a normal shutdown are the last healthy observation, not fills.
Interrupted/disconnected shutdowns can have unavailable marks. Sessions do not
resume open paper positions across process restarts. Cooldowns suppress repeated
signals and start again on exit.

## Logs and replay

Each unique `data/lead_follower/<timestamp>-<id>/` session contains:

- `metadata.json`: settings, resolved families/token IDs, raw market metadata,
  assumptions and source provenance.
- `events.jsonl`: burst candidates, signals, rejected entries, entries, exit requests/cancellations,
  exits, feed pauses and warm-up events. Includes features, fills, depth used,
  timestamps, holding time and fee-excluded paper P&L.
- `inputs.jsonl`: every normalized observation, individual leader trade, full
  changed book depth and feed health. Quiet books are carried forward in replay.
- `summary.json`: counts, wins/losses, closed P&L and mean holding time by group,
  event and follower, plus candidate qualification/rejection counts, rejection-reason
  counts, unresolved positions and liquidation marks.

Research candidates are logged on **each new leader trade** once at least two trades
are present in the activity window (`candidate_min_trades`). Each follower gets a
`burst_candidate` row before entry filtering. Low volume, weak/mixed flow, insufficient
acceleration, warm-up, follower movement, fresh prices, invalid books, wide entry
spreads, insufficient depth, existing positions and cooldowns remain in the research
sample. A row includes `qualified_for_paper_trade`, primary `rejection_reason`, and
all `rejection_reasons`. Qualification is at detection time, not a fill guarantee;
later entry rejection or P&L is linked by `position_id` and the signal's `candidate_id`.
Timers/book updates do not duplicate candidate rows. Successive rows from a cluster
are correlated updates, not independent opportunities.

Every candidate includes nested `trade_10s`/`trade_30s` count, shares, BUY/SELL volume,
imbalance, trades/second and inter-arrival statistics; current-window and baseline
statistics; previous-trade and pre-cluster gaps; score and baseline coverage; leader
midpoint changes over 5/10/30 seconds; follower movement, bid/ask, quote age and
spreads; leader best quotes/current depth and 30-second book-change counts/reductions.
Book reductions may be cancellations, not executed trades: levels are labelled
changed/removed, never asserted to have been consumed. Null features mean unavailable
history; they are not invented zero observations. Windows use `(now-window, now]`
in monotonic receipt time, and baseline observations end before the current window.

```powershell
.venv/Scripts/python.exe -m polytrader.bot.lead_follower --replay data/lead_follower/<session>
```

New metadata records `strategy_version=2`. Replay uses the burst detector for v2
sessions and a frozen original price detector for v1 sessions (metadata without
`strategy_version`), preserving historical experiments instead of silently changing
their signal rules. Replay is offline and prints a JSON summary without modifying the session. An
incomplete JSON line fails visibly. Input recording is continuous and unbounded;
use `--duration` to bound a run. Complete input lines survive abrupt termination,
but a final summary requires orderly shutdown. Lifecycle output is flushed per
line. Logging errors fail the run; they are never silently ignored.

The event hook executes synchronously after complete wire-message processing, so
trades and book changes are not lost to notification coalescing. Processing/logging
must keep up with the feed; delay checks trigger resynchronization if it falls
behind. There is no claim of lossless exchange delivery or millisecond execution.

Tests: `python -m unittest discover -s tests -p test_lead_follower.py -v`.
