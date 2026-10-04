import asyncio
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from polytrader.data import DataError
from polytrader.orderbook.models import BookSnapshot, Level, MarketReference, OrderBooks
from polytrader.orderbook.service import OrderBookService
from polytrader.bot.lead_follower.config import Settings, Group, Config, load_config
from polytrader.bot.lead_follower.discovery import Market, Family, prepare
from polytrader.bot.lead_follower.engine import Engine, sweep
from polytrader.bot.lead_follower.feed import parse_trade
from polytrader.bot.lead_follower.reporting import Session
from polytrader.bot.lead_follower.replay import replay
from polytrader.bot.lead_follower.runner import run
from polytrader.bot.lead_follower.__main__ import main

UTC = datetime(2026, 10, 4, tzinfo=timezone.utc)
FAMILY = Family("g", "event", Market("L", "leader", "ly", "ln"),
                (Market("F", "follower", "fy", "fn"),))


def book(token, bid=".28", ask=".30", size="100", **kwargs):
    return BookSnapshot(token, (Level(D(bid), D(size)),), (Level(D(ask), D(size)),),
                        UTC, UTC, "live", **kwargs)


def settings(**kwargs):
    defaults = dict(lookback_seconds=10, baseline_seconds=20, warmup_seconds=30,
                    min_volume="10", min_burst_score="2", min_price_age_seconds=5,
                    min_age_gap_seconds=5, entry_latency_seconds=0, exit_latency_seconds=0,
                    sell_delay_seconds=1, cooldown_seconds=10, max_hold_seconds=50)
    defaults.update(kwargs)
    return Settings(**defaults)


def trade(size="1", side="BUY", token="ly"):
    return dict(token=token, size=D(size), signed_size=D(size) if side == "BUY" else -D(size),
                price=D(".4"), side=side)


class EngineTests(unittest.TestCase):
    def setup_engine(self, **kwargs):
        self.rows = []
        self.engine = Engine((FAMILY,), settings(**kwargs), self.rows.append)
        self.books = {t: book(t) for t in ("ly", "ln", "fy", "fn")}

    def observe(self, t, **kwargs):
        self.engine.observe(t, (UTC + timedelta(seconds=t)).isoformat(), self.books, **kwargs)

    def signal(self, down=False):
        self.observe(0)
        self.observe(5, trade=trade())
        self.observe(15, trade=trade())
        self.observe(28, trade=trade("5", "SELL" if down else "BUY"))
        self.observe(29, trade=trade("5", "SELL" if down else "BUY"))
        self.books["ly"] = book("ly", ".25" if down else ".31", ".27" if down else ".33")
        self.observe(30, trade=trade("20", "SELL" if down else "BUY"))

    def kinds(self, kind):
        return [r for r in self.rows if r["type"] == kind]

    def test_up_signal_profit_after_sell_delay_and_exact_slippage(self):
        self.setup_engine(sell_delay_seconds=2)
        self.signal()
        p = self.engine.positions["F"]
        self.assertEqual(p.token, "fy")
        self.assertEqual(p.cost, D("3.01"))
        self.books["fy"] = book("fy", ".33", ".35")
        self.observe(31)
        self.assertFalse(self.kinds("exit"))
        self.observe(32)
        self.assertEqual(self.kinds("exit")[0]["pnl"], D(".28"))
        self.assertEqual(self.kinds("exit")[0]["holding_seconds"], 2)

    def test_down_signal_buys_no_and_exits(self):
        self.setup_engine()
        self.signal(down=True)
        self.assertEqual(self.engine.positions["F"].token, "fn")
        self.books["fn"] = book("fn", ".33", ".35")
        self.observe(31)
        self.assertEqual(self.kinds("exit")[0]["direction"], "down")

    def test_disappearing_exit_profit_rechecks_after_latency(self):
        self.setup_engine(exit_latency_seconds=2)
        self.signal()
        self.books["fy"] = book("fy", ".33", ".35")
        self.observe(31)
        self.books["fy"] = book("fy")
        self.observe(33)
        self.assertEqual(len(self.kinds("exit_cancelled")), 1)
        self.assertFalse(self.kinds("exit"))

    def test_entry_latency_uses_later_depth_not_signal_price(self):
        self.setup_engine(entry_latency_seconds=2)
        self.signal()
        self.assertFalse(self.kinds("entry"))
        self.books["fy"] = book("fy", ".30", ".32")
        self.observe(32)
        self.assertEqual(self.kinds("entry")[0]["cost"], D("3.21"))

    def test_missed_entry_expires_instead_of_filling_late(self):
        self.setup_engine(entry_latency_seconds=2, entry_timeout_seconds=5)
        self.signal()
        self.observe(36)
        self.assertFalse(self.kinds("entry"))
        self.assertEqual(self.kinds("entry_rejected")[0]["reason"], "entry_timeout")

    def test_shutdown_rejects_pending_and_keeps_open_pnl_separate(self):
        self.setup_engine(entry_latency_seconds=2)
        self.signal()
        self.engine.finish(31, UTC.isoformat())
        summary = self.engine.summary(self.books)
        self.assertEqual(summary["rejected"], 1)
        self.assertEqual(summary["opened"], 0)
        self.assertEqual(summary["closed_pnl"], 0)

    def test_invalid_book_does_not_trigger_a_staleness_signal(self):
        self.setup_engine()
        self.observe(0)
        self.observe(5, trade=trade())
        self.books["fy"] = replace(self.books["fy"], status="stale")
        self.books["ly"] = book("ly", ".31", ".33")
        self.observe(30, trade=trade("100"))
        self.assertFalse(self.kinds("signal"))
        self.assertFalse(self.kinds("entry"))

    def test_invalid_leader_cancels_pending_but_allows_existing_exit(self):
        self.setup_engine(entry_latency_seconds=2)
        self.signal()
        self.books["ly"] = replace(self.books["ly"], status="unavailable")
        self.observe(32)
        self.assertFalse(self.kinds("entry"))
        self.assertEqual(len(self.kinds("entry_rejected")), 1)

        self.setup_engine(exit_latency_seconds=2)
        self.signal()
        self.books["ly"] = replace(self.books["ly"], status="unavailable")
        self.books["fy"] = book("fy", ".33", ".35")
        self.observe(31)
        self.observe(33)
        self.assertEqual(len(self.kinds("exit")), 1)

    def test_stop_loss_and_max_hold_log_losing_exits(self):
        for options, t, reason in [({"stop_loss": ".1"}, 31, "stop_loss"), ({}, 80, "max_hold")]:
            with self.subTest(reason=reason):
                self.setup_engine(**options)
                self.signal()
                self.observe(t)
                row = self.kinds("exit")[0]
                self.assertEqual(row["reason"], reason)
                self.assertLess(row["pnl"], 0)

    def test_missing_exit_depth_remains_unresolved_and_unmarked(self):
        self.setup_engine()
        self.signal()
        self.books["fy"] = book("fy", size="1")
        self.observe(100)
        summary = self.engine.summary(self.books)
        self.assertEqual(summary["by_follower"][0]["unresolved"], 1)
        self.assertIsNone(summary["open_positions"][0]["liquidation_pnl"])

    def test_gap_cancels_pending_and_restarts_warmup(self):
        self.setup_engine(entry_latency_seconds=2)
        self.signal()
        self.observe(31, healthy=False)
        self.assertFalse(self.engine.positions)
        self.assertEqual(self.kinds("entry_rejected")[0]["reason"], "feed_gap")
        self.observe(32, trade=trade("500"))
        self.assertEqual(self.engine.started, 32)
        self.assertEqual(len(self.kinds("signal")), 1)

    def test_gap_retains_open_position_and_blocks_exit_until_healthy(self):
        self.setup_engine()
        self.signal()
        self.books["fy"] = book("fy", ".33", ".35")
        self.observe(31, healthy=False)
        self.assertFalse(self.kinds("exit"))
        self.observe(32)
        self.assertEqual(len(self.kinds("exit")), 1)

    def test_duplicate_signals_and_overlapping_groups_do_not_duplicate_positions(self):
        self.setup_engine()
        self.engine.families = (FAMILY, replace(FAMILY, id="other"))
        self.signal()
        self.observe(31, trade=trade("200"))
        self.assertEqual(len(self.kinds("signal")), 1)

    def test_single_large_trade_is_not_a_burst(self):
        for no_baseline, wrong_side in [(True, False), (False, True)]:
            self.setup_engine()
            self.observe(0)
            if not no_baseline:
                self.observe(5, trade=trade())
            self.books["ly"] = book("ly", ".31", ".33")
            self.observe(30, trade=trade("100", "SELL" if wrong_side else "BUY"))
            self.assertFalse(self.kinds("signal"))

    def test_follower_reaction_filters_signal(self):
        self.setup_engine()
        self.observe(0)
        self.observe(5, trade=trade())
        self.books["fy"] = book("fy", ".30", ".32")
        self.observe(21)
        self.books["ly"] = book("ly", ".31", ".33")
        self.observe(30, trade=trade("100"))
        self.assertFalse(self.kinds("signal"))

    def test_size_changes_do_not_reset_price_age(self):
        self.setup_engine()
        self.observe(0)
        self.books["fy"] = book("fy", size="200")
        self.observe(5)
        self.assertEqual(self.engine.price_changes["fy"], 0)

    def test_depth_and_cash_rejections_are_logged_after_signal(self):
        for opts, reason in [({"shares": "1000"}, "insufficient_ask_depth"),
                             ({"total_cash": "1"}, "portfolio_cash_limit")]:
            self.setup_engine(**opts)
            self.signal()
            self.assertFalse(self.kinds("signal"))
            self.assertIn(reason, self.kinds("burst_candidate")[-1]["rejection_reasons"])

    def test_entry_budget_reduces_size_and_summary_separates_open_marks(self):
        self.setup_engine(shares="20", cash_per_position="3")
        self.signal()
        p = self.engine.positions["F"]
        self.assertLess(p.shares, 20)
        self.assertLessEqual(p.cost, 3)
        summary = self.engine.summary(self.books)
        self.assertEqual(summary["closed_pnl"], 0)
        self.assertLess(summary["open_positions"][0]["liquidation_pnl"], 0)

    def test_depth_sweep_consumes_multiple_levels(self):
        levels = (Level(D(".3"), D(4)), Level(D(".4"), D(6)))
        cash, used = sweep(levels, D(10))
        self.assertEqual(cash, D("3.6"))
        self.assertEqual(len(used), 2)
        self.assertIsNone(sweep(levels, D(11)))


class FeedConfigTests(unittest.TestCase):
    def test_trade_scope_direction_and_identical_fills_preserved(self):
        raw = dict(event_type="last_trade_price", asset_id="ly", price=".4", size="10",
                   side="SELL", timestamp=str(int(UTC.timestamp() * 1000)))
        row = parse_trade(raw, {"ly"}, UTC, 10)
        self.assertEqual(row["signed_size"], -10)
        self.assertEqual(parse_trade(raw, {"ly"}, UTC, 10), row)
        self.assertIsNone(parse_trade(dict(raw, asset_id="ln"), {"ly"}, UTC, 10))
        for changed in [dict(raw, size=None), dict(raw, side="?"), dict(raw, timestamp="0")]:
            with self.assertRaises(DataError):
                parse_trade(changed, {"ly"}, UTC, 10)

    def test_bad_settings_rejected(self):
        for kwargs in [dict(shares="NaN"), dict(lookback_seconds=-1), dict(min_burst_score=".1"),
                       dict(min_imbalance="2"), dict(min_profit="0"), dict(sell_delay_seconds=float("inf"))]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Settings(**kwargs)

    def test_config_paths_groups_and_unknown_keys(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "test.toml"
            path.write_text('version=1\noutput_dir="out"\n[[groups]]\nid="g"\nevent="e"\nleader="l"\n')
            c = load_config(path)
            self.assertEqual(c.output_dir, Path(root) / "out")
            with path.open("a") as f:
                f.write('unknown="x"\n')
            with self.assertRaises(ValueError):
                load_config(path)

    def test_cli_invalid_args_do_not_discover(self):
        with patch("polytrader.bot.lead_follower.__main__.prepare") as discover, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["event"]), 2)
            self.assertEqual(main(["event", "--leader", "l", "--shares", "NaN"]), 2)
            discover.assert_not_called()

    def test_discovery_tokens_use_labels_and_support_default_followers(self):
        refs = {}
        for m in (FAMILY.leader, *FAMILY.followers):
            for outcome, t in [("No", m.no), ("YES", m.yes)]:
                refs[t] = MarketReference(t, m.id, m.slug, outcome=outcome)
        collection = OrderBooks({"slug": "event"}, refs, {t: BookSnapshot(t) for t in refs})
        with patch("polytrader.bot.lead_follower.discovery.resolve_books", return_value=collection):
            families, found = prepare(Config((Group("g", "event", "leader"),)))
            self.assertEqual(families, (FAMILY,))
            self.assertEqual(set(found.books), set(refs))
            with self.assertRaises(ValueError):
                prepare(Config((Group("g", "event", "missing"),)))


class BurstTests(unittest.TestCase):
    setup_engine = EngineTests.setup_engine
    observe = EngineTests.observe
    kinds = EngineTests.kinds

    def quiet_burst(self, side="BUY", sizes=("40", "40", "40")):
        self.observe(0)
        for t, size in zip((60, 62, 64), sizes):
            self.observe(t, trade=trade(size, side))

    def test_flat_leader_zero_baseline_qualifies_in_both_directions(self):
        for side, token in [("BUY", "fy"), ("SELL", "fn")]:
            with self.subTest(side=side):
                self.setup_engine(lookback_seconds=30, baseline_seconds=3600, warmup_seconds=60,
                                  min_volume="100", min_burst_score="3")
                self.quiet_burst(side)
                p = self.engine.positions["F"]
                self.assertEqual(p.token, token)
                c = self.kinds("burst_candidate")[-1]
                self.assertTrue(c["qualified_for_paper_trade"])
                self.assertEqual(c["leader_move_pp"], 0)
                self.assertEqual(c["trade_10s"]["count"], 3)
                self.assertEqual(c["trade_30s"]["volume"], 120)
                self.assertEqual(c["burst_score"], 3)
                self.assertEqual(c["expected_trade_count"], 0)
                self.assertEqual(c["time_since_previous_trade_seconds"], 2)
                self.assertFalse(c["baseline_complete"])

    def test_full_hour_sparse_example_records_quiet_gap(self):
        self.setup_engine(lookback_seconds=30, baseline_seconds=3600, warmup_seconds=60,
                          min_volume="100", min_burst_score="3")
        self.observe(0)
        for t in (10, 200, 500, 1000):
            self.observe(t, trade=trade())
        for t, size in zip((3630, 3632, 3634, 3637, 3641), (300, 500, 250, 700, 400)):
            self.observe(t, trade=trade(str(size)))
        rows = self.kinds("burst_candidate")
        self.assertEqual(len(self.kinds("entry")), 1)
        qualified = next(r for r in rows if r["qualified_for_paper_trade"])
        self.assertTrue(qualified["baseline_complete"])
        self.assertEqual(qualified["gap_before_cluster_seconds"], 2630)
        self.assertIn("position_already_open_or_pending", rows[-1]["rejection_reasons"])

    def test_sustained_activity_has_candidates_but_no_acceleration(self):
        self.setup_engine(lookback_seconds=30, baseline_seconds=300, warmup_seconds=60,
                          min_volume="100", min_burst_score="3")
        self.observe(0)
        for t in range(1, 121):
            self.observe(t, trade=trade("40"))
        last = self.kinds("burst_candidate")[-1]
        self.assertEqual(last["burst_score"], 1)
        self.assertIn("insufficient_activity_acceleration", last["rejection_reasons"])
        self.assertFalse(self.kinds("entry"))

    def test_mixed_flow_and_tiny_volume_are_logged_not_entered(self):
        self.setup_engine(min_volume="100")
        self.observe(0)
        self.observe(60, trade=trade("1", "BUY"))
        self.observe(62, trade=trade("1", "SELL"))
        self.observe(64, trade=trade("1", "BUY"))
        row = self.kinds("burst_candidate")[-1]
        self.assertIn("insufficient_volume", row["rejection_reasons"])
        self.assertIn("insufficient_directional_imbalance", row["rejection_reasons"])
        self.assertFalse(self.kinds("entry"))

    def test_optional_price_filter_only_when_enabled(self):
        self.setup_engine(min_move_pp=".5")
        self.quiet_burst()
        self.assertIn("optional_price_confirmation", self.kinds("burst_candidate")[-1]["rejection_reasons"])
        self.assertFalse(self.kinds("entry"))

    def test_burst_direction_overrides_opposite_price_movement(self):
        self.setup_engine()
        self.observe(0)
        self.observe(60, trade=trade("40", "SELL"))
        self.observe(62, trade=trade("40", "SELL"))
        self.books["ly"] = book("ly", ".31", ".33")
        self.observe(64, trade=trade("40", "SELL"))
        self.assertEqual(self.engine.positions["F"].token, "fn")

    def test_follower_moved_rejection_and_spread_features(self):
        self.setup_engine(min_price_age_seconds=0, min_age_gap_seconds=0)
        self.observe(0)
        self.observe(60, trade=trade("40"))
        self.observe(62, trade=trade("40"))
        self.books["ly"] = book("ly", ".30", ".36")
        self.books["fy"] = book("fy", ".31", ".33")
        self.observe(64, trade=trade("40"))
        row = self.kinds("burst_candidate")[-1]
        self.assertIn("follower_already_moved", row["rejection_reasons"])
        self.assertEqual(row["leader_spread"], D(".06"))
        self.assertNotIn("spread_limit", row["rejection_reasons"])

    def test_book_reductions_are_features_not_required_execution_claims(self):
        self.setup_engine()
        self.observe(0)
        self.observe(60, trade=trade("40"))
        self.books["ly"] = replace(self.books["ly"], asks=(Level(D(".30"), D(20)),))
        self.observe(61)
        self.observe(62, trade=trade("40"))
        self.observe(64, trade=trade("40"))
        row = self.kinds("burst_candidate")[-1]
        changes = row["leader_book_changes_30s"]
        self.assertEqual(changes["asks_shares_reduced"], 80)
        self.assertEqual(changes["asks_levels_changed"], 1)
        self.assertEqual(row["leader_midpoint_moves_pp"], {"5": D(0), "10": D(0), "30": D(0)})
        self.assertTrue(row["qualified_for_paper_trade"])

    def test_warmup_invalid_follower_and_cooldown_are_research_rows(self):
        self.setup_engine()
        self.observe(0)
        self.observe(1, trade=trade("40"))
        self.observe(2, trade=trade("40"))
        self.assertIn("warmup", self.kinds("burst_candidate")[-1]["rejection_reasons"])
        self.books["fy"] = replace(self.books["fy"], status="stale")
        for t in (60, 62, 64):
            self.observe(t, trade=trade("40"))
        self.assertIn("incomplete_or_invalid_books", self.kinds("burst_candidate")[-1]["rejection_reasons"])
        self.assertFalse(self.kinds("entry"))

    def test_timers_do_not_duplicate_candidates_and_disconnect_clears_tape(self):
        self.setup_engine()
        self.quiet_burst()
        count = len(self.kinds("burst_candidate"))
        self.observe(65)
        self.observe(66)
        self.assertEqual(len(self.kinds("burst_candidate")), count)
        self.observe(67, healthy=False)
        self.observe(68, trade=trade("100"))
        self.assertEqual(len(self.kinds("burst_candidate")), count)
        self.assertIsNone(self.engine.previous_gap["ly"])

    def test_window_boundaries_no_lookahead_and_candidate_summary(self):
        self.setup_engine(lookback_seconds=30, baseline_seconds=300, warmup_seconds=60)
        self.observe(0)
        self.observe(50, trade=trade("40"))
        self.observe(60, trade=trade("40"))
        self.observe(80, trade=trade("40"))
        row = self.kinds("burst_candidate")[-1]
        self.assertEqual(row["trade_30s"]["count"], 2)  # t=50 excluded
        self.assertEqual(row["trade_10s"]["count"], 1)
        self.assertIn("insufficient_trade_count", row["rejection_reasons"])
        summary = self.engine.summary(self.books)
        self.assertEqual(summary["candidate_count"], 2)
        self.assertEqual(summary["qualified_candidate_count"], 0)


class ReplayTests(unittest.TestCase):
    setup_engine = EngineTests.setup_engine
    observe = EngineTests.observe
    signal = EngineTests.signal

    def test_recorded_inputs_reproduce_events_and_pnl(self):
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            self.setup_engine()
            config = Config((Group("g", "event", "leader"),), self.engine.s, Path(root))
            collection = OrderBooks({}, {}, self.books)
            session = Session(root, config, (FAMILY,), collection)
            original = self.observe
            def observed(t, **kwargs):
                session.input(dict(elapsed_seconds=t, utc=(UTC + timedelta(seconds=t)).isoformat(),
                                   books={k: b.to_dict() for k, b in self.books.items()},
                                   trade=kwargs.get("trade"), healthy=kwargs.get("healthy", True), reason="feed_gap"))
                original(t, **kwargs)
            self.observe = observed
            self.signal()
            self.books["fy"] = book("fy", ".33", ".35")
            self.observe(31)
            session.close()
            rows = []
            summary = replay(session.directory, rows.append)
            self.assertEqual(summary["closed_pnl"], D(".28"))
            self.assertEqual(rows, self.rows)

    def test_legacy_recording_replays_with_original_price_detector(self):
        from polytrader.bot.lead_follower.legacy_price_engine import Engine as LegacyEngine
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            self.setup_engine()
            config = Config((Group("g", "event", "leader"),), self.engine.s, Path(root))
            session = Session(root, config, (FAMILY,), OrderBooks({}, {}, self.books))
            path = session.directory / "metadata.json"
            metadata = json.loads(path.read_text())
            metadata.pop("strategy_version")
            old = metadata["config"]["settings"]
            old["volume_ratio"], old["min_move_pp"] = "2", "1"
            path.write_text(json.dumps(metadata))
            legacy = LegacyEngine((FAMILY,), SimpleNamespace(**{
                k: D(v) if isinstance(v, str) else v for k, v in old.items()}), lambda row: None)
            for t in (0, 5, 15, 30, 31):
                tape = trade("20" if t == 30 else "1") if t in (5, 15, 30) else None
                if t == 30:
                    self.books["ly"] = book("ly", ".31", ".33")
                if t == 31:
                    self.books["fy"] = book("fy", ".33", ".35")
                row = dict(elapsed_seconds=t, utc=UTC.isoformat(), books={k: b.to_dict() for k, b in self.books.items()},
                           trade=tape, healthy=True, reason="feed_gap")
                session.input(row)
                legacy.observe(t, UTC.isoformat(), self.books, trade=tape)
            session.close()
            self.assertEqual(replay(session.directory), legacy.summary(self.books))
            self.assertEqual(replay(session.directory)["closed_pnl"], D(".28"))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_failure_is_exposed_to_consumers(self):
        class Feed:
            async def stream(self, tokens):
                yield None
        collection = OrderBooks({}, {"ly": MarketReference("ly")}, {"ly": BookSnapshot("ly")})
        def broken(msg, service):
            raise RuntimeError("logger failed")
        service = OrderBookService(collection, client=Feed(), on_event=broken, max_retries=0)
        with self.assertRaisesRegex(RuntimeError, "logger failed"):
            async with service:
                async with asyncio.timeout(2):
                    while not service._task.done():
                        await asyncio.sleep(.001)
                self.assertIsInstance(service._error, RuntimeError)
                async for _ in service.updates():
                    pass

    async def test_observer_sees_each_trade_and_completed_batch_then_gap(self):
        stamp = str(int(UTC.timestamp() * 1000))
        snapshot = dict(event_type="book", asset_id="ly", timestamp=stamp,
                        bids=[dict(price=".3", size="10")], asks=[dict(price=".4", size="10")])
        raw_trade = dict(event_type="last_trade_price", asset_id="ly", timestamp=stamp,
                         price=".4", size="1", side="BUY")
        class Feed:
            async def stream(self, tokens):
                for row in (snapshot, raw_trade, raw_trade):
                    yield row
                raise DataError("gap")
        collection = OrderBooks({}, {}, {"ly": BookSnapshot("ly")})
        collection.markets["ly"] = MarketReference("ly")
        seen = []
        def observe(msg, service):
            seen.append((msg, service.healthy, collection.books["ly"].status))
        async with OrderBookService(collection, client=Feed(), on_event=observe, max_retries=0) as service:
            async with asyncio.timeout(2):
                while not service._task.done():
                    await asyncio.sleep(.001)
        self.assertEqual([r[0] for r in seen if r[0] is not None], [snapshot, raw_trade, raw_trade])
        self.assertEqual(seen[0][2], "live")
        self.assertTrue(any(not healthy and status == "stale" for _, healthy, status in seen))

    async def test_bounded_runner_records_and_replays_without_network(self):
        class Feed:
            async def stream(self, tokens):
                for token in tokens:
                    yield dict(event_type="book", asset_id=token,
                               timestamp=str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                               bids=[dict(price=".28", size="100")], asks=[dict(price=".30", size="100")])
                await asyncio.Event().wait()
        tokens = ("ly", "ln", "fy", "fn")
        collection = OrderBooks({}, {t: MarketReference(t) for t in tokens},
                                {t: BookSnapshot(t) for t in tokens})
        with tempfile.TemporaryDirectory() as root, redirect_stdout(io.StringIO()):
            config = Config((Group("g", "event", "leader"),), settings(), Path(root))
            path = await run(config, (FAMILY,), collection, duration=.15, client=Feed())
            summary = json.loads((path / "summary.json").read_text())
            self.assertEqual(summary["termination"], "duration")
            self.assertEqual(replay(path)["closed_pnl"], 0)


if __name__ == "__main__":
    unittest.main()
