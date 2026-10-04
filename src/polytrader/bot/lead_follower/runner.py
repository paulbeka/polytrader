"""Own one shared subscription for all configured lead/follower families."""

import asyncio

from polytrader.data import DataError
from polytrader.orderbook.client import timestamp
from polytrader.orderbook.service import OrderBookService
from .engine import Engine
from .feed import parse_trade, utc_now
from .reporting import Session


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
            trade = parse_trade(message, engine.leaders, utc, config.settings.max_feed_delay_seconds)
            if message and message.get("timestamp") is not None:
                delay = abs((utc - timestamp(message["timestamp"])).total_seconds())
                if delay > config.settings.max_feed_delay_seconds:
                    raise DataError("Wire message outside feed delay tolerance")
        except DataError as exc:
            healthy, reason, trade = False, str(exc), None
        changed = {t: b.to_dict() for t, b in collection.books.items() if previous.get(t) is not b}
        previous.update(collection.books)
        # Persist before processing; replay observes the identical state and time.
        row = dict(elapsed_seconds=now, utc=utc.isoformat(), books=changed,
                   trade=trade, healthy=healthy, reason=reason)
        session.input(row)
        engine.observe(now, row["utc"], collection.books, trade=trade, healthy=healthy, reason=reason)
        last_health = healthy
        if not healthy and reason != "transport_gap":
            # Cause the service to reconnect and require fresh book snapshots.
            raise DataError(reason)

    try:
        print(f"Session: {session.directory}", flush=True)
        async with OrderBookService(collection, client=client, on_event=observe) as service:
            while duration is None or loop.time() - started < duration:
                if service._task.done():
                    if service._error:
                        raise service._error
                    termination = "feed_ended"
                    break
                # Timers handle sell availability, latency, timeouts and quiet books.
                # No observation replaces or invents a quote: current healthy depth persists.
                observe(None, service)
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
            print(f"Closed paper P&L (fees excluded): {final['closed_pnl']}; "
                  f"open positions: {len(final['open_positions'])}", flush=True)
        finally:
            session.close()
    return session.directory
