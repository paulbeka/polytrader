"""Own one shared subscription for all configured lead/follower families."""

import asyncio

from polytrader.data import DataError
from polytrader.orderbook.service import OrderBookService
from .engine import Engine
from .feed import parse_trade, utc_now
from .reporting import Session
from polytrader.ops.runtime import Runtime


def describe(families):
    for f in families:
        print(f"{f.id}: {f.event}")
        for role, markets in (("leader", (f.leader,)), ("follower", f.followers)):
            for m in markets:
                print(f"  {role}: {m.slug}\n    YES {m.yes}\n    NO  {m.no}")
    print("Paper trading only; fees excluded. Selected families must have comparable rules.")


async def run(config, families, collection, *, duration=None, client=None):
    loop = asyncio.get_running_loop()
    started = loop.time()
    session = Session(config.output_dir, config, families, collection)
    engine = Engine(families, config.settings, session.emit)
    runtime = Runtime(session.directory, "lead_follower", config)
    previous = {}
    continuity = 0
    termination = "duration"
    service = None
    last_health = False

    def observe(message, source):
        nonlocal continuity, last_health
        now, utc = loop.time() - started, utc_now()
        healthy = source.healthy
        reason, trade = "transport_gap", None
        if source.continuity != continuity:
            continuity = source.continuity
            healthy = False
        try:
            trade = parse_trade(message, engine.leaders, utc)
        except DataError as exc:
            healthy, reason, trade = False, str(exc), None
        changed = {t: b.to_dict() for t, b in collection.books.items() if previous.get(t) is not b}
        previous.update(collection.books)
        # Persist before processing; replay observes the identical state and time.
        row = dict(elapsed_seconds=now, utc=utc.isoformat(), books=changed,
                   trade=trade, healthy=healthy, reason=reason,
                   wire_event_type=message.get("event_type") if message else None,
                   wire_source_timestamp=message.get("timestamp") if message else None)
        session.input(row)
        engine.observe(now, row["utc"], collection.books, trade=trade, healthy=healthy, reason=reason)
        last_health = healthy
        if not healthy and reason != "transport_gap":
            # Cause the service to reconnect and require fresh book snapshots.
            raise DataError(reason)

    try:
        print(f"Session: {session.directory}", flush=True)
        # Heartbeats determine transport liveness. A healthy subscription can wait
        # indefinitely for quiet/missing books; those books cannot qualify entries.
        async with OrderBookService(collection, client=client, on_event=observe,
                                    max_retries=None, allow_missing_snapshots=True) as service:
            while duration is None or loop.time() - started < duration:
                if service._task.done():
                    if service._error:
                        raise service._error
                    termination = "feed_ended"
                    break
                # Timers handle sell availability, latency, timeouts and quiet books.
                # No observation replaces or invents a quote: current healthy depth persists.
                observe(None, service)
                if loop.time() >= runtime.next_status:
                    runtime.publish(engine.summary(collection.books, healthy=last_health), service,
                                    warming_up=engine.started is None or
                                    loop.time() - started - engine.started < config.settings.warmup_seconds)
                await asyncio.sleep(min(.1, max(0, duration - (loop.time() - started)))
                                    if duration is not None else .1)
            # Capture marks before normal context shutdown invalidates the feed.
            observe(None, service)
            engine.finish(engine.last_now, engine.utc)
            final_summary = engine.summary(collection.books, healthy=last_health)
            service.on_event = None
    except asyncio.CancelledError:
        termination = "interrupted"
        final_summary = engine.summary(collection.books, healthy=False)
        raise
    except BaseException:
        termination = "error"
        final_summary = engine.summary(collection.books, healthy=False)
        raise
    finally:
        try:
            engine.finish(loop.time() - started, utc_now().isoformat())
            # Refresh counts after pending entries are rejected on shutdown.
            final = engine.summary(collection.books, healthy=False)
            if "final_summary" in locals():
                marks = {p["id"]: p["liquidation_pnl"] for p in final_summary["open_positions"]}
                for p in final["open_positions"]:
                    p["liquidation_pnl"] = marks.get(p["id"])
            session.finish({**final, "termination": termination, "elapsed_seconds": loop.time() - started})
            runtime.publish(final, force=True)
            runtime.finish(termination)
            print(f"Closed paper P&L (fees excluded): {final['closed_pnl']}; "
                  f"open positions: {len(final['open_positions'])}", flush=True)
        finally:
            session.close()
    return session.directory
