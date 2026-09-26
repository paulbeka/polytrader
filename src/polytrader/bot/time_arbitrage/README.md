# Time-arbitrage scanner

A read-only strategy under `polytrader.bot`, reusing `polytrader.orderbook` for live
depth. It compares **every earlier/later pair within each configured chain**, buys
the earlier NO and later YES hypothetically, and reports equal-share batches that
meet net-profit, edge, size, trading-minimum and budget constraints. No private key,
wallet, orders or positions are involved. Imports perform no network work.

## Run from the repository root

```powershell
.venv/Scripts/python.exe -m pip install -e ".[live]"
Copy-Item src/polytrader/bot/config/time_arbitrage.example.toml src/polytrader/bot/config/time_arbitrage.toml
# Edit the copied file: replace market references and order the chain.
.venv/Scripts/python.exe -m polytrader.bot.time_arbitrage --config src/polytrader/bot/config/time_arbitrage.toml --validate
.venv/Scripts/python.exe -m polytrader.bot.time_arbitrage --config src/polytrader/bot/config/time_arbitrage.toml --duration 600
# Omit --duration to run until Ctrl+C.
```

The [template](../config/time_arbitrage.example.toml) contains placeholders, so it
cannot resolve as shipped. `--validate` resolves
public metadata, prints exact YES/NO tokens and all selected pairs, and opens no
WebSocket or output session. It checks supported fees/constraints as well as config
structure; **it does not prove the logical implication**. Exit codes: `0` clean
completion/valid supported metadata; `2` invalid config/discovery or unsupported
metadata during validation; `3` runtime/feed/persistence failure. Known blocked
markets do not prevent live mode from scanning unrelated eligible pairs.

Live mode uses the configured chain directly, with no review flag or note required.
The payoff calculation assumes that earlier YES implies later YES; the scanner
does not establish that relationship automatically.

## Payoff and scope

If earlier YES implies later YES, one earlier NO plus one later YES pays:

| Earlier YES | Later YES | Pair payout |
|---|---|---|
| Yes | Yes | 1 |
| No | Yes | 2 |
| No | No | 1 |

Earlier YES / later NO would pay zero and **must be impossible under the actual
rules**. Separate "during November" and "during December" contracts generally
do not satisfy this requirement. These are different markets; the positions cannot
be assumed mergeable or redeemable together before settlement.

All results are quoted hypothetical batches conditional on both legs filling and
normal settlement. An execution buffer is an estimate, not a fill guarantee.
Different opportunities may share liquidity; never sum their profits or quantities
as simultaneously executable capacity. The scanner does not model the probability
of the extra payout between deadlines or perform portfolio allocation/backtesting.

## Configuration

See the commented template for every setting. All monetary and quantity values
must be **quoted Decimal strings**. Timer values are numeric seconds. Unknown keys,
nonfinite/negative costs, nonpositive limits, invalid relations and duplicate chain
IDs are errors. Only `version=1`, `fee_mode="auto"` and
`unknown_fee_policy="skip"` are supported.

- An entry is a slug/URL string or `{ref, market?, label?, deadline?}`. Supported
  URLs are `https://polymarket.com/market/<slug>`, `/event/<slug>`, and
  `/event/<event>/<child>`. `www.polymarket.com` is also accepted.
- Events with multiple eligible markets require a child slug/ID in `market` or
  the child URL. Bare slugs are checked as both market and event; conflicting
  interpretations fail. Unknown references and duplicate aliases fail at startup.
- YES/NO tokens come from outcome labels, never array position. The union of both
  outcomes for all enabled markets is owned by one service, including tokens reused
  by multiple chains. Chains are never compared to one another.
- Array order defines the implication chain. API `endDate`, titles and creation
  dates do not reorder it. All supplied deadlines must be timezone-aware; if every
  entry has one, they must strictly increase. Partial deadlines do not establish
  an ordering. Any supplied elapsed deadline blocks pairs containing that entry.
- Relative output paths resolve against the **config file's directory**. The
  template writes to the repository's ignored `data/time_arbitrage/`. If using
  `../data/time_arbitrage` in `bot/config/`, output instead lands in `bot/data/`.
  Local configs and both generated-data locations are ignored by git.
- Restart to change markets or config; hot reload is not implemented.

## Fees, minimums and sizing

The adapter fetches fresh Gamma market details, CLOB `/clob-markets/{condition}`
and `/fee-rate?token_id=...` for **both exact tokens**. It verifies identity, rules
content, trade flags, ticks and fee metadata; category/title never determines fees.
The raw responses, endpoints, retrieval time and adapter interpretation are audited.
Metadata refresh runs in a worker thread. Transport failures keep the last result
only within `max_metadata_age_seconds`; explicit unsupported responses block
immediately. A changed rules hash requires restart and another rule review.

Supported adapters:

- **Confirmed zero:** explicit `feesEnabled=false` and zero token fee responses,
  with no conflicting taker fee. Missing metadata is never interpreted as zero.
- **V2 cash fees:** explicit CLOB `v2`, matching Gamma/CLOB rate, exponent 1 or 2,
  taker-only flags and compatible nonzero token rates. Fee at each price is
  `shares × rate × [price × (1-price)]^exponent`. The rate/exponent must support
  monotonic purchase-plus-fee cost (`rate × exponent <= 1`). Cash fees are added
  to purchase cost; equal gross shares are equal net delivered shares in this
  adapter. No rebates are credited.
- **Fee-bearing V1 and unknown versions/schedules are blocked.** This build does
  not certify share-deducted buy fees. Live examples checked during development
  included fee-bearing V1 markets; these correctly remain ineligible.

The public [fee guide](https://docs.polymarket.com/trading/fees) documents five-place
fee rounding. Estimates here round **up** to five USD decimal places at each
consumed price level. Aggregate depth does not reveal fill fragmentation, so this
is not an upper bound on every possible fill sequence; the configured execution
buffer supplies an additional estimate and the records identify that assumption.
Set funding/redemption or other allocations explicitly if relevant: zero defaults
mean excluded, not proven absent.

The [market-details guide](https://docs.polymarket.com/market-data/market-details)
defines Gamma `orderMinSize` as minimum USD notional **per order/leg**. This is the
field checked, against purchase notional before fees. CLOB rewards `r.mi` is never
used as an order minimum. Quantities are rounded down to `.01` shares, following
the supported limit-order precision. Unknown tick sizes or inconsistent metadata
block evaluation. These constraints describe quoted sizing, not order submission.

Sizing uses Decimal throughout (including HTTP decimal decoding):

```text
purchase = cost of matched shares across both ask ladders
fees = sum of supported fee estimates at consumed price levels
other = shares * extra_cost_per_pair + fixed_cost_per_opportunity
buffer = shares * execution_buffer_per_pair
estimated_net_profit = shares - purchase - fees - other
conservative_profit = estimated_net_profit - buffer
conservative_edge_per_pair = conservative_profit / shares
```

The budget includes purchase, fees, other costs and buffer exactly once. Asks
already include crossing the spread. Bids, midpoint, last trade and complemented
YES prices never substitute for executable NO asks.

`top` uses the smaller displayed best-ask size, capped by `max_shares` and the
maximum affordable `.01`-share quantity. `full` walks the two ascending ladders,
consumes matched tranches, and stops at the first nonpositive/below-threshold
marginal edge. It never skips an expensive tranche. Final qualification also
includes fixed batch cost and rounded fees. Each leg must meet its notional
minimum, and conservative profit must be strictly positive and meet both configured
profit and edge thresholds. Consumed levels, available/capped sizes, VWAP, worst
prices and all cost components are recorded.

## Feed health, lifecycle and files

Detection runs on maintained book/status notifications, including size/depth
changes, and a health timer. Both immutable snapshots are captured without an
intervening await; this is a coherent local view, not an exchange-wide atomic quote.
The service exposes transport `healthy` and an incrementing `continuity` counter.
A disconnect closes active episodes as **unobservable**, even when stale/live
notifications coalesce before the scanner consumes them. Full reconnect snapshots
are required before a pair is eligible again. Quiet books remain valid on a healthy
connection regardless of last quote-change time.

The scanner enables `allow_missing_snapshots` on the shared service: a token with
no initial snapshot becomes stale when the timeout is checked on incoming
heartbeats, while other live tokens continue. A later full snapshot recovers it.
The transport's existing PING/PONG watchdog still detects a broken connection.
The default service behavior for other callers remains unchanged.

Each pair has one episode at a time. Opens/closes are immediate. Material economic
updates are throttled by `update_log_seconds`; pending updates flush on a timer
even if quotes go quiet. Timestamp-only changes do not create updates. Latest
qualifying state and peaks update internally during throttling. Closure reasons
distinguish an observed threshold/constraint failure, unobservable data, shutdown
and runtime failure. First/last qualifying times, close time, observed qualifying
window and censored duration are distinct fields.

Each exclusive UTC-plus-random session directory contains:

- `manifest.json`: effective config/hash, ordered references/mappings, source rules
  and hashes, raw fee/constraint metadata, application version/commit, assumptions.
- `events.jsonl`: ordered sequence numbers, connection/metadata/pair status,
  opportunity open/update/close calculations, health summaries, errors and session end.
- `summary.json`: written at shutdown, with evaluation/episode counts, peaks,
  blocked/closure reasons and termination cause. Profits are never aggregated.

Console startup prints all mappings and assumptions. Periodic `HEALTH` records
distinguish quiet healthy feeds from blocked pairs. One synchronous local writer
flushes each emitted record; disk failures stop the scanner. Manifest and shutdown
records are fsynced. OS-cached writes can be lost on a crash; a crashed run may
lack a summary/end record. `reporting.read_events(path)` reads complete JSONL lines
and ignores only a truncated final line. There is no cross-process episode resume.
Monetary quantities serialize as strings; time durations may be JSON numbers.

The orderbook notifications coalesce current state, so short intermediate
opportunities can be missed. These logs are not tick-complete captures, fill
evidence, realised profits or historical full-depth replay.

## Code map and verification

`config.py` validates TOML; `discovery.py` resolves identities and builds pairs;
`costs.py` interprets metadata; `detector.py` is pure sizing; `runner.py` owns the
service/timers; `reporting.py` owns lifecycle/files. The module CLI is `__main__.py`.

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

Tests cover the payoff table, documented fee examples, threshold/budget/quantity
boundaries, the handoff's full-depth fixture, 200 independent randomized sizing
cross-checks, discovery/config, shared ownership, quiet books, reconnect censoring,
missing-market isolation, lifecycle throttling, writer failures and real-service
integration with an injected public-feed fixture. They place no trades.

Additional adapter references:

- [Official V2 cash-fee calculations](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/fees.py)
- [Official fee test vectors](https://github.com/Polymarket/py-clob-client-v2/blob/main/tests/test_fee_calculations.py)
- [Official quantity/tick precision](https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/order_builder/builder.py)
- [CLOB market-info endpoint](https://docs.polymarket.com/api-reference/markets/get-clob-market-info)

Sources were checked during implementation on 2026-09-26. Unsupported or changed
schemas must be reviewed and added to the adapter/tests before they qualify.

### Public verification performed on 2026-09-26

Metadata validation resolved November market `3501951` and December market
`2176270` for Hormuz traffic normalisation, with four unique outcome tokens.
Both returned supported confirmed-zero fees and a Gamma minimum notional of 5.
The local, git-ignored config is `data/time_arbitrage/hormuz.validation.toml`
and can be used with `--validate` immediately.

A separate **transport-only** 35-second diagnostic maintained all four live books,
consumed 46 notifications, and finished healthy with continuity counter 0. It
performed zero opportunity evaluations. Its manifest/events/summary are in:

```text
data/time_arbitrage/public_feed_checks/20260926T211655.732621Z-7e95d90b7d56/
```

This verifies public connectivity and metadata, not a reviewed live strategy or
profitable execution. The full scanner lifecycle is exercised by offline integration
tests.
