"""Single ordered local writer and one observed-opportunity state machine per pair."""

from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import uuid
from polytrader.ops.files import open_journal, read_journal


def json_value(value):
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if isinstance(value, (Decimal, Path)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def encode(value):
    return json.dumps(json_value(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def read_events(path):
    """Read complete JSONL records; ignore only an incomplete final line after a crash."""
    if not Path(path).exists() and Path(path).parent.joinpath("events", "segments.json").exists():
        yield from read_journal(Path(path).parent, "events", strict=False)
        return
    with Path(path).open("rb") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                break
            yield json.loads(line)


class Session:
    def __init__(self, output_dir, manifest, *, start_mono, printer=print):
        self.started = start_mono
        self.printer = printer
        self.sequence = 0
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:12]
        self.directory = Path(output_dir) / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        with (self.directory / "manifest.json").open("x", encoding="utf-8") as stream:
            stream.write(encode({"schema_version": 1, "run_id": self.run_id,
                                 "started_at": datetime.now(timezone.utc),
                                 "detection_only": True, **manifest}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.stream = open_journal(self.directory, "events")

    def emit(self, event, now, mono, **fields):
        self.sequence += 1
        row = {"schema_version": 1, "sequence": self.sequence, "run_id": self.run_id,
               "event": event, "utc": now, "elapsed_seconds": mono - self.started,
               "detection_only": True, **fields}
        self.stream.write(encode(row) + "\n")
        self.stream.flush()  # Writer errors propagate; never silently discard records.
        if event.startswith("opportunity_"):
            calc = fields["calculation"]
            self.printer(f"{now.isoformat()} {event.removeprefix('opportunity_').upper()} "
                         f"{fields['chain_id']} {fields['earlier']['label']} NO + "
                         f"{fields['later']['label']} YES shares={calc['shares']} "
                         f"vwap={calc['legs'][0]['vwap']}+{calc['legs'][1]['vwap']} "
                         f"cost={calc['total_cost_with_buffer']} fees={calc['fee_cost']} "
                         f"min_payout={calc['minimum_payout']} "
                         f"net_est={calc['estimated_net_profit']} "
                         f"conservative_profit={calc['conservative_profit']} "
                         f"edge={calc['conservative_edge_per_pair']} "
                         f"mode={calc['depth_mode']} reason={fields.get('reason', 'qualifies')}")

    def finish(self, summary, now, mono):
        self.emit("session_end", now, mono, summary=summary)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        with (self.directory / "summary.json").open("x", encoding="utf-8") as stream:
            stream.write(encode({"schema_version": 1, "run_id": self.run_id, **summary}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def close(self):
        self.stream.close()


def material(calculation):
    """Economic state; ignore quote age, source timestamps and metadata fetch time."""
    value = json_value(calculation)
    value.pop("quotes", None)
    for leg in value.get("legs", []):
        fee = leg.pop("fee_metadata", {})
        leg["fee_model"] = {k: fee.get(k) for k in
                            ("adapter", "rate", "exponent", "min_notional", "tick", "share_step")}
    return encode(value)


@dataclass
class Episode:
    identity: str
    first_utc: datetime
    first_mono: float
    last_utc: datetime
    last_mono: float
    last_emitted: float
    latest: object
    logged_signature: str
    peak_edge: Decimal
    peak_profit: Decimal


class Tracker:
    def __init__(self, session, pairs, update_seconds):
        self.session = session
        self.pairs = {p.key: p for p in pairs}
        self.update_seconds = update_seconds
        self.active = {}
        self.states = {}
        self.evaluated = self.opened = self.closed = 0
        self.peak_profit = self.peak_edge = Decimal(0)
        self.close_reasons = Counter()

    def fields(self, key, episode):
        pair = self.pairs[key]
        def identity(market, entry, outcome, token):
            return {"market_id": market.id, "condition_id": market.condition_id,
                    "slug": market.slug, "question": market.question,
                    "label": entry.label or market.slug, "outcome": outcome,
                    "token_id": token, "original_ref": entry.ref, "deadline": entry.deadline}
        return {"pair_id": key, "chain_id": pair.chain_id, "episode_id": episode.identity,
                "earlier": identity(pair.earlier, pair.earlier_entry, "No", pair.earlier.no),
                "later": identity(pair.later, pair.later_entry, "Yes", pair.later.yes),
                "first_qualifying_at": episode.first_utc,
                "last_qualifying_at": episode.last_utc,
                "observed_qualifying_seconds": episode.last_mono - episode.first_mono,
                "peak_edge": episode.peak_edge, "peak_profit": episode.peak_profit,
                "depth_mode": episode.latest.calculation["depth_mode"],
                "calculation": episode.latest.calculation}

    def accept(self, pair, evaluation, now, mono):
        key = pair.key
        self.evaluated += 1
        state = (evaluation.state, evaluation.reason)
        if self.states.get(key) != state:
            self.states[key] = state
            self.session.emit("pair_status", now, mono, pair_id=key,
                              state=evaluation.state, reason=evaluation.reason)
        episode = self.active.get(key)
        if evaluation.state != "qualified":
            if episode:
                self.close_episode(key, now, mono,
                                   "unobservable" if evaluation.state == "blocked" else evaluation.reason,
                                   cause=evaluation.reason, failing=evaluation)
            return
        calc = evaluation.calculation
        edge, profit = calc["conservative_edge_per_pair"], calc["conservative_profit"]
        self.peak_edge, self.peak_profit = max(self.peak_edge, edge), max(self.peak_profit, profit)
        signature = material(calc)
        if episode is None:
            episode = Episode(uuid.uuid4().hex, now, mono, now, mono, mono,
                              evaluation, signature, edge, profit)
            self.active[key] = episode
            self.opened += 1
            self.session.emit("opportunity_open", now, mono, **self.fields(key, episode))
        else:
            episode.latest, episode.last_utc, episode.last_mono = evaluation, now, mono
            episode.peak_edge, episode.peak_profit = max(episode.peak_edge, edge), max(episode.peak_profit, profit)
            self.flush(now, mono, keys=(key,))

    def flush(self, now, mono, *, keys=None):
        for key in keys if keys is not None else tuple(self.active):
            episode = self.active[key]
            signature = material(episode.latest.calculation)
            if signature != episode.logged_signature and mono - episode.last_emitted >= self.update_seconds:
                self.session.emit("opportunity_update", now, mono, **self.fields(key, episode))
                episode.last_emitted, episode.logged_signature = mono, signature

    def next_flush(self):
        return min((e.last_emitted + self.update_seconds for e in self.active.values()
                    if material(e.latest.calculation) != e.logged_signature), default=float("inf"))

    def close_episode(self, key, now, mono, reason, *, cause=None, failing=None):
        episode = self.active[key]
        self.session.emit("opportunity_close", now, mono, **self.fields(key, episode),
                          reason=reason, cause=cause, first_failing_or_unobservable_at=now,
                          evaluation_window_seconds=mono - episode.first_mono,
                          duration_censored=reason in {"unobservable", "shutdown", "runtime_error"},
                          closing_evaluation=failing)
        del self.active[key]
        self.closed += 1
        self.close_reasons[reason] += 1

    def close_all(self, now, mono, reason):
        for key in tuple(self.active):
            self.close_episode(key, now, mono, reason)

    def summary(self):
        return {"active_opportunities": len(self.active), "episodes_opened": self.opened,
                "episodes_closed": self.closed, "pair_evaluations": self.evaluated,
                "blocked_by_reason": dict(Counter(reason for state, reason in self.states.values() if state == "blocked")),
                "close_reasons": dict(self.close_reasons),
                "peak_observed_edge": self.peak_edge, "peak_observed_profit": self.peak_profit,
                "capacity_note": "Standalone quoted batches; shared liquidity/profits must not be summed."}
