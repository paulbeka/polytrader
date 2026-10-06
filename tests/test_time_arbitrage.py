import asyncio
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import random
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from polytrader.data import DataError
from polytrader.orderbook import BookSnapshot, Level, OrderBookService, resolve_books
from polytrader.bot.time_arbitrage.config import load_config
from polytrader.bot.time_arbitrage.costs import Metadata, interpret, refresh_metadata
from polytrader.bot.time_arbitrage.detector import evaluate
from polytrader.bot.time_arbitrage.discovery import reference, resolve_entry, resolve_universe
from polytrader.bot.time_arbitrage.models import (
    D, Chain, Config, CostSettings, Entry, Evaluation, Pair, ResolvedMarket, ScannerSettings,
)
from polytrader.bot.time_arbitrage.reporting import Session, Tracker, read_events
from polytrader.bot.time_arbitrage.runner import run
from polytrader.bot.time_arbitrage.__main__ import main


NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)
ZERO = CostSettings(execution_buffer_per_pair=D(0))
SETTINGS = ScannerSettings(min_edge_per_pair=D(0), min_total_profit=D(0), min_shares=D('.01'))


def raw_market(slug):
    return {"id": slug, "slug": slug, "question": f"Event by {slug}?", "conditionId": "c-" + slug,
            "description": "Synthetic fixture: shared start, source, threshold; nested deadlines.",
            "outcomes": '["No","Yes"]', "clobTokenIds": json.dumps(["n-" + slug, "y-" + slug]),
            "active": True, "closed": False, "acceptingOrders": True, "enableOrderBook": True,
            "orderMinSize": "5", "orderPriceMinTickSize": ".01", "feesEnabled": False}


def market(slug):
    return ResolvedMarket(slug, "c-" + slug, slug, f"Event by {slug}?", "y-" + slug,
                          "n-" + slug, raw_market(slug), NOW)


def pair():
    return Pair("test", market("nov"), market("dec"), Entry("nov"), Entry("dec"))


def meta(market_id, **kwargs):
    return Metadata("c-" + market_id, NOW, True, "supported", "confirmed-zero", **kwargs)


def metadata():
    return {"c-nov": meta("nov"), "c-dec": meta("dec")}


def book(token, levels, **kwargs):
    return BookSnapshot(token, asks=tuple(Level(D(p), D(q)) for p, q in levels),
                        status="live", updated_at=NOW - timedelta(days=2), received_at=NOW, **kwargs)


def books(left=((".48", "80"),), right=((".50", "50"),)):
    return (book("n-nov", left), book("y-dec", right))


def config(root, chains=None, **settings):
    return Config(Path(root) / "config.toml", replace(SETTINGS, output_dir=Path(root) / "out", **settings),
                  ZERO, chains or (Chain("test", (Entry("nov"), Entry("dec"))),), "fixture-hash")


def missing(value):
    cause = HTTPError(value, 404, "not found", {}, None)
    cause.close()
    raise DataError("not found") from cause


def client_fixture():
    client = Mock()
    client.get_market.side_effect = lambda slug: raw_market(slug)
    client.get_event.side_effect = missing
    return client


class MathTests(unittest.TestCase):
    def evaluate(self, bs=None, ms=None, settings=SETTINGS, costs=ZERO, **kwargs):
        return evaluate(pair(), bs or books(), ms or metadata(), settings, costs, NOW, **kwargs)

    def test_payoff_and_example_smaller_size(self):
        self.assertEqual([(1 - a) + b for a, b in ((1, 1), (0, 1), (0, 0))], [1, 2, 1])
        self.assertEqual((1 - 1) + 0, 0)  # Why the implication review is required.
        result = self.evaluate()
        self.assertEqual(result.state, "qualified")
        c = result.calculation
        self.assertEqual((c['shares'], c['purchase_cost'], c['conservative_profit']), (D(50), D(49), D(1)))
        self.assertEqual(c['conservative_edge_per_pair'], D('.02'))
        self.assertEqual(c['legs'][0]['vwap'], D('.48'))

    def test_exact_one_or_more_is_not_profitable(self):
        for price in ('.52', '.53'):
            self.assertEqual(self.evaluate(books(right=((price, '50'),))).state, 'rejected')

    def test_each_cost_can_remove_inversion_and_fixed_cost_once(self):
        for costs in (replace(ZERO, execution_buffer_per_pair=D('.02')),
                      replace(ZERO, extra_cost_per_pair=D('.02')),
                      replace(ZERO, fixed_cost_per_opportunity=D('1'))):
            result = self.evaluate(costs=costs)
            self.assertEqual(result.state, 'rejected')
            self.assertEqual(result.calculation['conservative_profit'], D(0))
        ms = {k: replace(m, adapter='v2-cash-price-curve', rate=D('.07')) for k, m in metadata().items()}
        self.assertEqual(self.evaluate(ms=ms).state, 'rejected')

    def test_budget_includes_all_costs_and_floors_size(self):
        costs = replace(ZERO, fixed_cost_per_opportunity=D('.1'), execution_buffer_per_pair=D('.001'))
        s = replace(SETTINGS, max_cost_per_opportunity=D('10'), max_shares=D('15'))
        c = self.evaluate(settings=s, costs=costs).calculation
        self.assertEqual(c['shares'], D('10.09'))
        self.assertLessEqual(c['total_cost_with_buffer'], D(10))
        self.assertGreater(c['total_cost_with_buffer'] + D('.00981'), D(10))
        c = self.evaluate(books(left=(('.48', '12.349'),)), settings=replace(SETTINGS, max_shares=D('12.345'))).calculation
        self.assertEqual(c['shares'], D('12.34'))

    def test_full_depth_fixture_and_top_ignores_deeper_liquidity(self):
        bs = books(left=(('.48', '10'), ('.49', '20')), right=(('.50', '15'), ('.53', '20')))
        c = self.evaluate(bs, settings=replace(SETTINGS, depth_mode='full')).calculation
        self.assertEqual((c['shares'], c['purchase_cost'], c['conservative_profit']), (D(15), D('14.75'), D('.25')))
        self.assertEqual(c['legs'][0]['worst_price'], D('.49'))
        self.assertEqual(len(c['legs'][0]['consumed_levels']), 2)
        self.assertEqual(self.evaluate(bs).calculation['shares'], D(10))

    def test_full_depth_fixed_cost_and_budget(self):
        bs = books(left=(('.48', '10'), ('.49', '20')), right=(('.50', '15'), ('.53', '20')))
        result = self.evaluate(bs, settings=replace(SETTINGS, depth_mode='full'),
                               costs=replace(ZERO, fixed_cost_per_opportunity=D('.20')))
        self.assertEqual(result.calculation['conservative_profit'], D('.05'))
        self.assertEqual(result.calculation['other_cost'], D('.20'))
        c = self.evaluate(bs, settings=replace(SETTINGS, depth_mode='full', max_cost_per_opportunity=D('12'))).calculation
        self.assertEqual(c['shares'], D('12.22'))
        self.assertLessEqual(c['total_cost_with_buffer'], D(12))

    def test_thresholds_and_minimum_notional_units(self):
        self.assertEqual(self.evaluate(settings=replace(SETTINGS, min_total_profit=D(1), min_edge_per_pair=D('.02'))).state, 'qualified')
        self.assertEqual(self.evaluate(settings=replace(SETTINGS, min_total_profit=D('1.00000001'))).state, 'rejected')
        ms = {k: replace(m, min_notional=D(5)) for k, m in metadata().items()}
        result = self.evaluate(books(left=(('.48', '10'),)), ms=ms)
        self.assertEqual(result.reason, 'below_minimum_notional')  # 10 shares, only $4.80.
        self.assertEqual(self.evaluate(books(left=(('.48', '11'),)), ms=ms).state, 'qualified')

    def test_live_validity_and_quiet_book(self):
        self.assertEqual(self.evaluate().state, 'qualified')  # Old quote, healthy live connection.
        self.assertEqual(self.evaluate(healthy=False).reason, 'feed_not_healthy')
        for status in ('initializing', 'snapshot', 'stale', 'unavailable'):
            bs = (replace(books()[0], status=status), books()[1])
            self.assertEqual(self.evaluate(bs).state, 'blocked')
        self.assertEqual(self.evaluate((replace(books()[0], asks=()), books()[1])).reason, 'missing_asks')
        self.assertEqual(self.evaluate((replace(books()[0], token_id='y-nov'), books()[1])).reason, 'wrong_outcome_token')
        expired = {k: replace(m, fetched_at=NOW-timedelta(seconds=901)) for k,m in metadata().items()}
        self.assertEqual(self.evaluate(ms=expired).reason, 'metadata_expired')
        unsupported = {k: replace(m, supported=False, reason='unsupported') for k,m in metadata().items()}
        self.assertEqual(self.evaluate(ms=unsupported).reason, 'unsupported')
        p = replace(pair(), earlier_entry=Entry('nov', deadline=NOW))
        self.assertEqual(evaluate(p, books(), metadata(), SETTINGS, ZERO, NOW).reason, 'configured_deadline_passed')

    def test_invalid_levels(self):
        for price, size in (('NaN','1'), ('.48','Infinity'), ('0','1'), ('1','1'), ('.485','1'), ('.48','-1')):
            self.assertEqual(self.evaluate(books(left=((price,size),))).state, 'blocked')

    def test_randomized_independent_share_grid_reference(self):
        """Independent centishare expansion, no production ladder/budget helpers."""
        rng = random.Random(260926)
        for mode in ('top', 'full'):
            for _ in range(100):
                ladders = []
                for leg in range(2):
                    prices = sorted(rng.sample(range(30, 66), 3))
                    ladders.append(tuple((str(D(p)/100), str(D(rng.randint(1, 35))/100)) for p in prices))
                bs = books(*ladders)
                fee_rate = D(rng.choice(('0', '.04', '.07')))
                ms = {k: replace(v, rate=fee_rate) for k,v in metadata().items()}
                costs = replace(ZERO, fixed_cost_per_opportunity=D('.002'), execution_buffer_per_pair=D('.001'))
                settings = replace(SETTINGS, depth_mode=mode, min_edge_per_pair=D('.002'),
                                   max_shares=D('.65'), max_cost_per_opportunity=D('.28'))
                expanded = []
                for ladder in ladders:
                    selected = ladder[:1] if mode == 'top' else ladder
                    expanded.append([D(p) for p,q in selected for __ in range(int(D(q)*100))])
                prefix = []
                for p,r in zip(*expanded):
                    marginal = 1-p-r-fee_rate*p*(1-p)-fee_rate*r*(1-r)-D('.001')
                    if mode == 'full' and (marginal <= 0 or marginal < D('.002')):
                        break
                    prefix.append((p,r))
                expected = D(0)
                expected_cost = D('.002')
                for count in range(1, min(len(prefix), 65)+1):
                    q = D(count)/100
                    purchase = sum((p+r for p,r in prefix[:count]), D(0))/100
                    fee = D(0)
                    for leg in (0,1):
                        by_price = {}
                        for prices in prefix[:count]:
                            by_price[prices[leg]] = by_price.get(prices[leg], D(0)) + D('.01')
                        for p,shares in by_price.items():
                            fee += (shares*fee_rate*p*(1-p)).quantize(D('.00001'), rounding='ROUND_CEILING')
                    total = purchase + fee + D('.002') + q*D('.001')
                    if total <= D('.28'):
                        expected, expected_cost = q, total
                c = self.evaluate(bs, ms, settings, costs).calculation
                self.assertEqual(c['shares'], expected)
                self.assertEqual(c['total_cost_with_buffer'], expected_cost)


class FeeTests(unittest.TestCase):
    def inputs(self):
        m = market('nov')
        gamma = raw_market('nov')
        clob = {'c': m.condition_id, 'v': 'v1', 'cbos': True, 'mts': '.01',
                't': [{'t': m.no, 'o': 'No'}, {'t': m.yes, 'o': 'Yes'}]}
        rates = {t: {'base_fee': 0} for t in (m.yes,m.no)}
        return m, gamma, clob, rates

    def test_zero_requires_confirmation_and_correct_constraint_field(self):
        m,g,c,f = self.inputs()
        c['r'] = {'mi': 200}  # Reward minimum is unrelated.
        result = interpret(m,g,c,f,NOW)
        self.assertTrue(result.supported)
        self.assertEqual(result.min_notional, D(5))
        self.assertEqual(result.share_step, D('.01'))
        for field in ('feesEnabled','orderMinSize','orderPriceMinTickSize','acceptingOrders'):
            bad = dict(g); bad.pop(field)
            self.assertFalse(interpret(m,bad,c,f,NOW).supported)
        f[m.yes]['base_fee'] = 1
        self.assertFalse(interpret(m,g,c,f,NOW).supported)

    def test_contradictory_zero_schedule_and_unknown_version_block(self):
        m,g,c,f = self.inputs()
        c['fd'] = {'r':'.07','e':1,'to':True}
        self.assertFalse(interpret(m,g,c,f,NOW).supported)
        del c['fd']; c['v'] = 'future'
        self.assertEqual(interpret(m,g,c,f,NOW).reason,'unknown_clob_version')

    def test_v2_fee_reference_examples_and_v1_fail_closed(self):
        m,g,c,f = self.inputs()
        g.update(feesEnabled=True,feeSchedule={'rate': '.07','exponent':1,'takerOnly':True})
        c.update(v='v2',fd={'r':'.07','e':1,'to':True})
        f = {t: {'base_fee': 1000} for t in f}
        result = interpret(m,g,c,f,NOW)
        self.assertTrue(result.supported)
        self.assertEqual(result.fee(D('.5'),D(100)), D('1.75'))
        self.assertEqual(result.fee(D('.3'),D(100)), D('1.47'))
        self.assertEqual(result.fee(D('.01'),D('.01')), D('.00001'))
        g['feeSchedule'].update(rate='.25',exponent=2)
        c['fd'].update(r='.25',e=2)
        result = interpret(m,g,c,f,NOW)
        self.assertEqual(result.fee(D('.5'),D(100)), D('1.56250'))
        self.assertEqual(result.fee(D('.3'),D(100)), D('1.10250'))
        self.assertEqual(result.fee(D('.1'),D(100)), D('.20250'))
        c['v'] = 'v1'
        self.assertIn('net_shares_unverified', interpret(m,g,c,f,NOW).reason)
        c['v'] = 'v2'; c['fd']['e'] = 3
        self.assertFalse(interpret(m,g,c,f,NOW).supported)

    def test_rules_identity_and_tradability_changes(self):
        for mutate, reason in ((lambda g: g.update(description='Changed rules'), 'rules_changed'),
                               (lambda g: g.update(conditionId='wrong'), 'identity_changed'),
                               (lambda g: g.update(closed=True), 'not_tradable')):
            m,g,c,f = self.inputs(); mutate(g)
            self.assertIn(reason, interpret(m,g,c,f,NOW).reason)

    def test_refresh_error_retains_original_fetch_time(self):
        universe = resolve_universe(config('.'), client_fixture())
        client = Mock(); client.get_market.side_effect = DataError('offline')
        old = metadata()
        result, errors = refresh_metadata(universe,client,old)
        self.assertEqual(result,old)
        self.assertEqual(len(errors),2)


class ConfigDiscoveryTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'test.toml'; path.write_text(text,encoding='utf-8')
            return load_config(path)

    @property
    def valid(self):
        return 'version=1\n[[chains]]\nid="test"\nmarkets=["nov","dec"]\n'

    def test_config_decimal_typing_and_relative_path(self):
        c = self.load('version=1\n[scanner]\noutput_dir="../sessions"\nmax_shares="15.5"\n'+self.valid.split('\n',1)[1])
        self.assertEqual(c.scanner.max_shares,D('15.5'))
        self.assertEqual(c.scanner.output_dir,c.path.parent.parent/'sessions')
        self.load(self.valid)
        for value in ('1.5','"NaN"','"-1"','"Infinity"'):
            with self.assertRaises(ValueError):
                self.load('version=1\n[scanner]\nmax_shares='+value+'\n'+self.valid.split('\n',1)[1])

    def test_invalid_config_fields_and_deadlines(self):
        for change in (self.valid.replace('version=1','version=2'), self.valid+'wat=1\n',
                       self.valid+'relation="during_month"\n', self.valid+self.valid.split('\n',1)[1],
                       self.valid.replace('["nov","dec"]','["nov"]'),
                       self.valid.replace('markets=["nov","dec"]',
                           'markets=[{ref="nov",deadline="2027-01-01T00:00:00Z"},{ref="dec",deadline="2026-12-01T00:00:00Z"}]')):
            with self.assertRaises(ValueError):
                self.load(change)
        for value in ('[]','4','false'):
            with self.assertRaises(ValueError):
                self.load('version=1\n[scanner]\ndepth_mode='+value+'\n'+self.valid.split('\n',1)[1])

    def test_reference_paths_resolution_ambiguity_and_cache(self):
        client = client_fixture(); cache = {}
        a = resolve_entry(client,Entry('nov'),cache)
        self.assertEqual((a.no,a.yes),('n-nov','y-nov'))
        self.assertIs(resolve_entry(client,Entry(' nov '),cache),a)
        self.assertEqual(client.get_market.call_count,1)
        client.get_event.side_effect = lambda slug: {'markets':[raw_market('nov')]}
        self.assertEqual(resolve_entry(client,Entry('https://polymarket.com/event/e'),{}).id,'nov')
        client.get_event.side_effect = lambda slug: {'markets':[raw_market('nov'),raw_market('dec')]}
        self.assertEqual(resolve_entry(client,Entry('https://polymarket.com/event/e/dec'),{}).id,'dec')
        self.assertEqual(resolve_entry(client,Entry('e',market='nov'),{}).id,'nov')
        with self.assertRaisesRegex(ValueError,'selector'):
            resolve_entry(client,Entry('https://polymarket.com/event/e'),{})
        client.get_event.side_effect = lambda slug: {'markets':[raw_market('dec')]}
        with self.assertRaisesRegex(ValueError,'Ambiguous'):
            resolve_entry(client,Entry('nov'),{})
        self.assertEqual(resolve_entry(client,Entry('https://polymarket.com/market/nov'),{}).id,'nov')
        for ref in ('https://evil.com/event/x','https://polymarket.com/a/b/c/d','https://polymarket.com/event/a/b/c'):
            with self.assertRaises(ValueError): reference(Entry(ref))

    def test_pairs_union_and_duplicate_aliases(self):
        chains = (Chain('a',(Entry('nov'),Entry('dec'),Entry('jan'))),Chain('b',(Entry('nov'),Entry('jan'))))
        universe = resolve_universe(config('.',chains),client_fixture())
        self.assertEqual(len(universe.token_ids),6)
        self.assertEqual([(p.earlier.slug,p.later.slug) for p in universe.pairs[:3]], [('nov','dec'),('nov','jan'),('dec','jan')])
        self.assertEqual(len(universe.pairs),4)
        duplicate = (Chain('a',(Entry('nov'),Entry('https://polymarket.com/market/nov'))),)
        with self.assertRaisesRegex(ValueError,'Duplicate'):
            resolve_universe(config('.',duplicate),client_fixture())

    def test_only_nonadjacent_pair_qualifies(self):
        chain = Chain('a',(Entry('nov'),Entry('dec'),Entry('jan')))
        universe = resolve_universe(config('.',(chain,)),client_fixture())
        prices = {'n-nov':'.48','y-dec':'.60','n-dec':'.60','y-jan':'.50'}
        ms = {m.condition_id:meta(m.slug) for m in universe.markets.values()}
        hits = []
        for p in universe.pairs:
            bs = tuple(book(t,((prices[t],'50'),)) for t in p.tokens)
            if evaluate(p,bs,ms,SETTINGS,ZERO,NOW).state == 'qualified': hits.append((p.earlier.slug,p.later.slug))
        self.assertEqual(hits,[('nov','jan')])


class LifecycleTests(unittest.TestCase):
    def test_lifecycle_throttle_pending_peaks_and_censored_closure(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root,{},start_mono=0,printer=lambda _:None)
            tracker = Tracker(session,[pair()],5)
            first = evaluate(pair(),books(),metadata(),SETTINGS,ZERO,NOW)
            changed = evaluate(pair(),books(right=(('.49','50'),)),metadata(),SETTINGS,ZERO,NOW)
            tracker.accept(pair(),first,NOW,0)
            tracker.accept(pair(),first,NOW+timedelta(seconds=1),1)
            tracker.accept(pair(),changed,NOW+timedelta(seconds=2),2)
            self.assertEqual(tracker.opened,1)
            self.assertEqual(tracker.peak_profit,D('1.50'))
            tracker.flush(NOW+timedelta(seconds=5),5)  # Quiet feed still flushes pending change.
            tracker.accept(pair(),Evaluation('blocked','feed_not_healthy'),NOW+timedelta(seconds=6),6)
            tracker.accept(pair(),first,NOW+timedelta(seconds=7),7)
            tracker.close_all(NOW+timedelta(seconds=8),8,'shutdown')
            session.finish(tracker.summary(),NOW,9); session.close()
            rows = list(read_events(session.directory/'events.jsonl'))
            ops = [r for r in rows if r['event'].startswith('opportunity_')]
            self.assertEqual([r['event'] for r in ops],['opportunity_open','opportunity_update','opportunity_close','opportunity_open','opportunity_close'])
            self.assertNotEqual(ops[0]['episode_id'],ops[3]['episode_id'])
            self.assertEqual(ops[2]['reason'],'unobservable')
            self.assertTrue(ops[2]['duration_censored'])
            self.assertEqual(ops[2]['observed_qualifying_seconds'],2)
            self.assertEqual(ops[2]['evaluation_window_seconds'],6)
            self.assertEqual(ops[-1]['reason'],'shutdown')
            self.assertIsInstance(ops[0]['calculation']['shares'],str)

    def test_price_deterioration_is_observed_not_censored(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root,{},start_mono=0,printer=lambda _:None)
            tracker = Tracker(session,[pair()],5)
            tracker.accept(pair(),evaluate(pair(),books(),metadata(),SETTINGS,ZERO,NOW),NOW,0)
            bad = evaluate(pair(),books(right=(('.60','50'),)),metadata(),SETTINGS,ZERO,NOW)
            tracker.accept(pair(),bad,NOW,1); session.close()
            close = list(read_events(session.directory/'events.jsonl'))[-1]
            self.assertEqual(close['reason'],'nonpositive_conservative_profit')
            self.assertFalse(close['duration_censored'])

    def test_truncated_last_line_corrupt_complete_line_and_collision(self):
        with tempfile.TemporaryDirectory() as root:
            a = Session(root,{},start_mono=0,printer=lambda _:None)
            b = Session(root,{},start_mono=0,printer=lambda _:None)
            self.assertNotEqual(a.directory,b.directory)
            a.emit('test',NOW,1,price=D('.48')); a.close(); b.close()
            path = a.directory/'events.jsonl'
            with path.open('ab') as out: out.write(b'{"event":')
            self.assertEqual(len(list(read_events(path))),1)
            with path.open('ab') as out: out.write(b'\n')
            with self.assertRaises(json.JSONDecodeError): list(read_events(path))
            class FrozenDate(datetime):
                @classmethod
                def now(cls, tz=None): return cls(2026,9,26,tzinfo=timezone.utc)
            with patch('polytrader.bot.time_arbitrage.reporting.datetime',FrozenDate), patch('polytrader.bot.time_arbitrage.reporting.uuid.uuid4') as uid:
                uid.return_value.hex = 'same'
                c = Session(root,{},start_mono=0,printer=lambda _:None); c.close()
                with self.assertRaises(FileExistsError): Session(root,{},start_mono=0,printer=lambda _:None)


def ws_snapshot(token, price='.48', size='50'):
    return {'event_type':'book','asset_id':token,'timestamp':str(int(NOW.timestamp()*1000)),
            'bids':[], 'asks':[{'price':price,'size':size}]}


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_coalesced_reconnect_is_censored_even_with_live_snapshots(self):
        opened, reopened = asyncio.Event(), asyncio.Event()
        episode_ids = []
        class ObservedSession(Session):
            def emit(self, event, now, mono, **fields):
                super().emit(event, now, mono, **fields)
                if event == 'opportunity_open':
                    episode_ids.append(fields['episode_id'])
                    (opened if len(episode_ids) == 1 else reopened).set()
        class CoalescedService:
            def __init__(self,collection,**kwargs):
                self.collection, self.continuity, self.healthy = collection, 0, True
            async def __aenter__(self):
                for t in self.collection.books:
                    self.collection.books[t] = book(t,(('.48','50'),))
                return self
            async def __aexit__(self,*args): self.healthy = False
            async def updates(self):
                yield SimpleNamespace(book=self.collection.books['n-nov'])
                await opened.wait()
                self.continuity += 1  # Stale->live happened before consumer could read.
                yield SimpleNamespace(book=self.collection.books['n-nov'])
                await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as root:
            c = config(root,health_check_seconds=.005)
            ms = {k:replace(v,fetched_at=datetime.now(timezone.utc)) for k,v in metadata().items()}
            task = asyncio.create_task(run(c,resolve_universe(c,client_fixture()),ms,
                                       service_factory=CoalescedService,session_factory=ObservedSession,
                                       printer=lambda _:None))
            try:
                # Observe the behavior under test, rather than racing durable
                # heartbeat fsyncs against a 60 ms wall-clock deadline.
                await asyncio.wait_for(reopened.wait(), timeout=5)
            finally:
                task.cancel()
                directory = await task
            rows = list(read_events(directory/'events.jsonl'))
            self.assertEqual(sum(r['event']=='opportunity_open' for r in rows),2)
            self.assertTrue(any(r.get('cause')=='feed_continuity_lost' and r['duration_censored'] for r in rows))

    async def test_metadata_expiry_closes_quiet_episode_during_slow_refresh(self):
        clock = [NOW]
        refresh_started, release_refresh = threading.Event(), threading.Event()
        class ClockDate(datetime):
            @classmethod
            def now(cls,tz=None): return clock[0]
        class Client:
            async def stream(self,tokens):
                for t in tokens: yield ws_snapshot(t)
                await asyncio.to_thread(refresh_started.wait, 1)
                clock[0] = NOW + timedelta(seconds=31)
                while True:
                    await asyncio.sleep(.005)
                    yield None
        class ObservedSession(Session):
            def emit(self,event,*args,**kwargs):
                super().emit(event,*args,**kwargs)
                if event == 'opportunity_close': release_refresh.set()
        with tempfile.TemporaryDirectory() as root:
            c = config(root,health_check_seconds=.005)
            c = replace(c,costs=replace(ZERO,refresh_seconds=.01,max_metadata_age_seconds=30))
            ms = metadata()
            def slow_refresh(*args):
                refresh_started.set()
                release_refresh.wait(1)
                return ms, {'fixture':'offline; retained data expires'}
            with patch('polytrader.bot.time_arbitrage.runner.refresh_metadata',side_effect=slow_refresh), \
                 patch('polytrader.bot.time_arbitrage.runner.datetime',ClockDate):
                directory = await run(c,resolve_universe(c,client_fixture()),ms,duration=.09,
                                      client=Client(),session_factory=ObservedSession,printer=lambda _:None)
            rows = list(read_events(directory/'events.jsonl'))
            self.assertTrue(any(r['event']=='opportunity_open' for r in rows))
            close = next(r for r in rows if r['event']=='opportunity_close')
            self.assertEqual(close['cause'],'metadata_expired')
            self.assertTrue(close['duration_censored'])

    async def test_live_service_end_to_end_reconnect_and_quiet_shutdown(self):
        class Client:
            calls = 0
            async def stream(self, tokens):
                self.calls += 1
                for token in tokens:
                    yield ws_snapshot(token,'.50' if token.startswith('y-') else '.48')
                await asyncio.sleep(.025)
                if self.calls == 1:
                    raise DataError('injected_disconnect')
                while True:
                    await asyncio.sleep(.01)
                    yield None
        with tempfile.TemporaryDirectory() as root:
            c = config(root,health_check_seconds=.005,update_log_seconds=.01,summary_seconds=.025)
            universe = resolve_universe(c,client_fixture())
            ms = {k:replace(v,fetched_at=datetime.now(timezone.utc)) for k,v in metadata().items()}
            output = []
            def factory(collection,**kwargs): return OrderBookService(collection,retry_delay=0,**kwargs)
            directory = await run(c,universe,ms,duration=.10,client=Client(),service_factory=factory,printer=output.append)
            rows = list(read_events(directory/'events.jsonl'))
            ops = [r for r in rows if r['event'].startswith('opportunity_')]
            self.assertGreaterEqual(sum(r['event']=='opportunity_open' for r in ops),2)
            self.assertTrue(any(r.get('reason')=='unobservable' for r in ops))
            self.assertEqual(ops[-1]['reason'],'shutdown')
            self.assertEqual(rows[-1]['event'],'session_end')
            summary = json.loads((directory/'summary.json').read_text())
            self.assertEqual(summary['termination_reason'],'duration_elapsed')
            self.assertGreater(summary['pair_evaluations'],2)
            self.assertTrue(any('OPEN' in s for s in output))
            self.assertEqual([r['sequence'] for r in rows],list(range(1,len(rows)+1)))

    async def test_missing_market_snapshot_does_not_break_other_live_books(self):
        class Client:
            async def stream(self,tokens):
                yield ws_snapshot('a')
                while True:
                    await asyncio.sleep(.005)
                    yield None
        async with OrderBookService(resolve_books(token_ids=['a','missing']),client=Client(),
                                    snapshot_timeout=.01,allow_missing_snapshots=True) as service:
            await asyncio.sleep(.03)
            self.assertTrue(service.healthy)
            self.assertEqual(service.collection.books['a'].status,'live')
            self.assertEqual(service.collection.books['missing'].status,'stale')

    async def test_writer_error_stops_and_closes_feed(self):
        closed = []
        class Client:
            async def stream(self,tokens):
                try:
                    for token in tokens: yield ws_snapshot(token)
                    await asyncio.Event().wait()
                finally: closed.append(True)
        class BrokenSession(Session):
            def emit(self,event,*args,**kwargs):
                if event == 'opportunity_open': raise OSError('injected disk full')
                return super().emit(event,*args,**kwargs)
        with tempfile.TemporaryDirectory() as root:
            c = config(root,health_check_seconds=.01)
            ms = {k:replace(v,fetched_at=datetime.now(timezone.utc)) for k,v in metadata().items()}
            with self.assertRaisesRegex(OSError,'disk full'):
                await run(c,resolve_universe(c,client_fixture()),ms,duration=.2,client=Client(),
                          session_factory=BrokenSession,printer=lambda _:None)
            self.assertTrue(closed)

class CliTests(unittest.TestCase):
    def test_validate_no_websocket_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'c.toml'
            path.write_text('version=1\n[[chains]]\nid="test"\nmarkets=["nov","dec"]\n')
            c = load_config(path)
            universe = resolve_universe(c,client_fixture())
            with patch('polytrader.bot.time_arbitrage.__main__.prepare',return_value=(universe,metadata(),{})), \
                 patch('polytrader.bot.time_arbitrage.__main__.run') as live, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(['--config',str(path),'--validate']),0)
                live.assert_not_called()
                self.assertEqual(main(['--config',str(path)]),0)
                live.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
