"""Public-data scanner orchestration; no wallets, orders or positions."""

import asyncio
from contextlib import AsyncExitStack, suppress
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import subprocess

from polytrader.data import DataError
from polytrader.orderbook import OrderBookService, resolve_books
from .costs import refresh_metadata, rules_hash
from .detector import evaluate
from .discovery import ScannerClient, resolve_universe
from .models import Evaluation
from .reporting import Session, Tracker, encode


def prepare(config, client=None):
    client = client or ScannerClient()
    universe = resolve_universe(config, client)
    metadata, errors = refresh_metadata(universe, client)
    return universe, metadata, errors


def manifest(config, universe, metadata):
    try:
        app_version = version("polytrader")
    except PackageNotFoundError:
        app_version = "unknown"
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=config.path.parent,
                                capture_output=True, text=True, timeout=3, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {"application_version": app_version, "git_commit": commit,
            "config": config, "config_sha256": config.source_hash,
            "chains": {key: [m.condition_id for m in markets] for key, markets in universe.chains.items()},
            "markets": {key: {"market": market, "rules_sha256": rules_hash(market.raw),
                               "rules_source": f"https://gamma-api.polymarket.com/markets/slug/{market.slug}"}
                        for key, market in universe.markets.items()},
            "pairs": [{"key": p.key, "chain": p.chain_id, "earlier": p.earlier.condition_id,
                       "later": p.later.condition_id, "tokens": p.tokens} for p in universe.pairs],
            "unique_tokens": universe.token_ids, "fee_and_constraint_metadata": metadata,
            "assumptions": {"minimum_payout": "1 per equal net share pair, conditional on reviewed implication",
                            "capture": "coalesced current state; can miss intermediate opportunities",
                            "fees": "estimates; per-level upward rounding plus configured execution buffer",
                            "execution": "both legs must fill; no orders placed; no shared-liquidity aggregation",
                            "durability": "flush each event; fsync manifest and clean shutdown; crashes may lose OS-cached writes"}}


def describe(config, universe, metadata, *, printer=print):
    printer(f"Config: {config.path}\nDetection only; depth={config.scanner.depth_mode}; "
            f"chains={len(universe.chains)} markets={len(universe.markets)} "
            f"pairs={len(universe.pairs)} books={len(universe.token_ids)}")
    printer(f"Costs: buffer/pair={config.costs.execution_buffer_per_pair}; "
            f"extra/pair={config.costs.extra_cost_per_pair}; fixed/batch={config.costs.fixed_cost_per_opportunity}")
    for chain in config.chains:
        if not chain.enabled:
            continue
        printer(f"Chain {chain.id}:")
        for index, market in enumerate(universe.chains[chain.id]):
            meta = metadata[market.condition_id]
            printer(f"  [{index}] {market.slug} market={market.id} condition={market.condition_id}\n"
                    f"      YES={market.yes} NO={market.no}\n"
                    f"      fee={meta.adapter} min_notional={meta.min_notional} "
                    f"status={meta.reason}")
    for pair in universe.pairs:
        printer(f"PAIR {pair.chain_id}: NO({pair.earlier.slug}) + YES({pair.later.slug})")


async def run(config, universe, metadata, *, duration=None, client=None,
              service_factory=OrderBookService, session_factory=Session, printer=print):
    client = client or ScannerClient()
    loop = asyncio.get_running_loop()
    started = loop.time()
    session = session_factory(config.scanner.output_dir, manifest(config, universe, metadata),
                              start_mono=started, printer=printer)
    tracker = Tracker(session, universe.pairs, config.scanner.update_log_seconds)
    wake = asyncio.Event()
    dirty = set(universe.token_ids)
    consumer = refreshing = None
    termination = "completed"
    service = None
    printer(f"Manifest: {session.directory / 'manifest.json'}")

    async def consume():
        try:
            async for update in service.updates():
                dirty.add(update.book.token_id)
                wake.set()
        finally:
            wake.set()

    try:
        async with AsyncExitStack() as stack:
            # Public supported API owns exactly one book per unique outcome token.
            collection = resolve_books(token_ids=list(universe.token_ids))
            service = await stack.enter_async_context(service_factory(
                collection, client=client, allow_missing_snapshots=True))
            consumer = asyncio.create_task(consume(), name="time-arbitrage-updates")
            previous_health, continuity = None, service.continuity
            next_health = next_summary = started
            next_refresh = started + config.costs.refresh_seconds
            stop_at = loop.time() + duration if duration is not None else float("inf")
            while True:
                mono, now = loop.time(), datetime.now(timezone.utc)
                if mono >= stop_at:
                    termination = "duration_elapsed"
                    break
                if consumer.done():
                    consumer.result()  # Preserve the transport's useful error.
                    raise DataError("Orderbook service ended")
                rescan_all = mono >= next_health
                if rescan_all:
                    next_health = mono + config.scanner.health_check_seconds
                if service.continuity != continuity:
                    # Even stale->live coalesced into one notification must censor the episode.
                    for pair in universe.pairs:
                        tracker.accept(pair, Evaluation("blocked", "feed_continuity_lost"), now, mono)
                    continuity = service.continuity
                    rescan_all = True
                health = (service.healthy, continuity)
                if health != previous_health:
                    session.emit("connection_status", now, mono, healthy=health[0], continuity=continuity)
                    printer(f"{now.isoformat()} feed healthy={health[0]} continuity={continuity}")
                    previous_health, rescan_all = health, True
                if refreshing is not None and refreshing.done():
                    metadata, errors = refreshing.result()
                    session.emit("metadata_refresh", now, mono, metadata=metadata, errors=errors)
                    refreshing, rescan_all = None, True
                    next_refresh = mono + config.costs.refresh_seconds
                if refreshing is None and mono >= next_refresh:
                    refreshing = asyncio.create_task(asyncio.to_thread(refresh_metadata, universe, client, metadata))
                    refreshing.add_done_callback(lambda _: wake.set())
                selected = (universe.pairs if rescan_all else
                            {p.key: p for t in dirty for p in universe.reverse[t]}.values())
                dirty.clear()
                for pair in selected:
                    # No await between these reads or during pure evaluation.
                    books = tuple(service.collection.books[t] for t in pair.tokens)
                    tracker.accept(pair, evaluate(pair, books, metadata, config.scanner,
                                                 config.costs, now, healthy=service.healthy), now, mono)
                tracker.flush(now, mono)
                if mono >= next_summary:
                    summary = {**tracker.summary(), "connection_healthy": service.healthy,
                               "continuity": continuity}
                    session.emit("health_summary", now, mono, summary=summary)
                    printer("HEALTH " + encode(summary))
                    next_summary = mono + config.scanner.summary_seconds
                wake.clear()
                timeout = min(next_health, next_summary, stop_at,
                              tracker.next_flush(),
                              next_refresh if refreshing is None else float("inf")) - loop.time()
                with suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout=max(0, timeout))
    except asyncio.CancelledError:
        termination = "interrupted"
    except Exception as exc:
        termination = "runtime_error"
        session.emit("error", datetime.now(timezone.utc), loop.time(), message=str(exc))
        raise
    finally:
        for task in (consumer, refreshing):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        try:
            now, mono = datetime.now(timezone.utc), loop.time()
            tracker.close_all(now, mono, "runtime_error" if termination == "runtime_error" else "shutdown")
            session.finish({**tracker.summary(), "termination_reason": termination,
                            "elapsed_seconds": mono - started}, now, mono)
        finally:
            session.close()
    return session.directory
