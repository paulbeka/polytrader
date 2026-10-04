"""Strict, offline configuration for paper trading."""

from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
import math
import tomllib


@dataclass(frozen=True)
class Settings:
    lookback_seconds: float = 30
    baseline_seconds: float = 3600
    warmup_seconds: float = 60
    candidate_min_trades: int = 2
    min_trades: int = 3
    min_burst_score: Decimal = Decimal("3")
    min_move_pp: Decimal = Decimal("0")  # Optional aligned confirmation; zero disables.
    min_volume: Decimal = Decimal("100")
    min_imbalance: Decimal = Decimal("0.6")
    max_follower_fraction: Decimal = Decimal("0.25")
    min_price_age_seconds: float = 10
    min_age_gap_seconds: float = 5
    max_spread: Decimal = Decimal("0.04")
    shares: Decimal = Decimal("10")
    cash_per_position: Decimal = Decimal("20")
    total_cash: Decimal = Decimal("100")
    slippage_per_share: Decimal = Decimal("0.001")
    min_profit: Decimal = Decimal("0.01")
    stop_loss: Decimal = Decimal("1")
    max_hold_seconds: float = 300
    entry_latency_seconds: float = 0.25
    exit_latency_seconds: float = 0.25
    sell_delay_seconds: float = 1
    cooldown_seconds: float = 60
    entry_timeout_seconds: float = 10
    max_feed_delay_seconds: float = 10

    def __post_init__(self):
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(f.default, Decimal):
                try:
                    if isinstance(v, bool):
                        raise ValueError(f"Invalid {f.name}")
                    v = Decimal(str(v))
                except InvalidOperation as exc:
                    raise ValueError(f"Invalid {f.name}") from exc
                valid = v.is_finite() and v >= 0
            else:
                valid = type(v) in (int, float) and math.isfinite(v) and v >= 0
            if not valid:
                raise ValueError(f"{f.name} must be finite and nonnegative")
            object.__setattr__(self, f.name, v)
        positive = ("lookback_seconds", "baseline_seconds", "min_volume",
                    "shares", "cash_per_position", "total_cash", "min_profit", "stop_loss",
                    "max_hold_seconds", "entry_timeout_seconds", "max_feed_delay_seconds")
        if any(getattr(self, n) <= 0 for n in positive):
            raise ValueError(f"These settings must be positive: {', '.join(positive)}")
        if self.baseline_seconds < self.lookback_seconds or self.min_burst_score < 1:
            raise ValueError("baseline must cover lookback; min_burst_score must be >= 1")
        if self.warmup_seconds <= self.lookback_seconds:
            raise ValueError("warmup_seconds must exceed lookback_seconds")
        if (type(self.min_trades) is not int or type(self.candidate_min_trades) is not int
                or not 2 <= self.candidate_min_trades <= self.min_trades):
            raise ValueError("Require integer 2 <= candidate_min_trades <= min_trades")
        if not 0 < self.min_imbalance <= 1 or not 0 <= self.max_follower_fraction < 1:
            raise ValueError("Require 0 < min_imbalance <= 1 and 0 <= max_follower_fraction < 1")
        if self.max_spread > 1 or self.slippage_per_share >= 1:
            raise ValueError("Spread/slippage are prices in [0, 1]")
        if self.entry_timeout_seconds <= self.entry_latency_seconds:
            raise ValueError("entry_timeout_seconds must exceed entry_latency_seconds")
        if self.max_hold_seconds <= self.sell_delay_seconds:
            raise ValueError("max_hold_seconds must exceed sell_delay_seconds")


@dataclass(frozen=True)
class Group:
    id: str
    event: str
    leader: str
    followers: tuple[str, ...] = ()

    def __post_init__(self):
        if any(not isinstance(x, str) or not x.strip() for x in (self.id, self.event, self.leader)):
            raise ValueError("Group id, event and leader must be nonempty strings")
        if not isinstance(self.followers, (list, tuple)) or any(
                not isinstance(x, str) or not x.strip() for x in self.followers):
            raise ValueError("followers must be a list of market slugs or IDs")
        if len(set(self.followers)) != len(self.followers) or self.leader in self.followers:
            raise ValueError("Followers must be distinct and exclude the leader")
        object.__setattr__(self, "followers", tuple(self.followers))


@dataclass(frozen=True)
class Config:
    groups: tuple[Group, ...]
    settings: Settings = Settings()
    output_dir: Path = Path("data/lead_follower")

    def __post_init__(self):
        if not self.groups or len({g.id for g in self.groups}) != len(self.groups):
            raise ValueError("Provide at least one group, with unique IDs")


def load_config(path):
    path = Path(path).resolve()
    with path.open("rb") as f:
        raw = tomllib.load(f)
    if (set(raw) - {"version", "output_dir", "settings", "groups"}
            or type(raw.get("version")) is not int or raw["version"] != 1):
        raise ValueError("Require version=1 and known configuration keys")
    try:
        if "volume_ratio" in raw.get("settings", {}):
            raise ValueError("volume_ratio is retired; set min_burst_score (trade-count acceleration), "
                             "and set min_move_pp=0 to disable price confirmation")
        settings = Settings(**raw.get("settings", {}))
        groups = tuple(Group(**g) for g in raw.get("groups", []))
        out = Path(raw.get("output_dir", "data/lead_follower"))
        return Config(groups, settings, out if out.is_absolute() else path.parent / out)
    except (TypeError, KeyError) as exc:
        raise ValueError(f"Invalid configuration: {exc}") from exc
