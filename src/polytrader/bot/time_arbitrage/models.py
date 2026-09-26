"""Configuration and resolved identities shared by the scanner components."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

D = Decimal


@dataclass(frozen=True)
class ScannerSettings:
    output_dir: Path = Path("data/time_arbitrage")
    depth_mode: str = "top"
    min_edge_per_pair: Decimal = D("0.001")
    min_total_profit: Decimal = D("0.10")
    min_shares: Decimal = D("1")
    max_shares: Decimal = D("1000")
    max_cost_per_opportunity: Decimal = D("500")
    health_check_seconds: float = 1
    summary_seconds: float = 60
    update_log_seconds: float = 5


@dataclass(frozen=True)
class CostSettings:
    fee_mode: str = "auto"
    unknown_fee_policy: str = "skip"
    extra_cost_per_pair: Decimal = D("0")
    fixed_cost_per_opportunity: Decimal = D("0")
    execution_buffer_per_pair: Decimal = D("0.001")
    refresh_seconds: float = 300
    max_metadata_age_seconds: float = 900


@dataclass(frozen=True)
class Entry:
    ref: str
    market: str | None = None
    label: str | None = None
    deadline: datetime | None = None


@dataclass(frozen=True)
class Chain:
    id: str
    markets: tuple[Entry, ...]
    enabled: bool = True
    relation: str = "yes_implies_next_yes"


@dataclass(frozen=True)
class Config:
    path: Path
    scanner: ScannerSettings
    costs: CostSettings
    chains: tuple[Chain, ...]
    source_hash: str
    version: int = 1


@dataclass(frozen=True)
class ResolvedMarket:
    id: str
    condition_id: str
    slug: str
    question: str
    yes: str
    no: str
    raw: dict
    resolved_at: datetime


@dataclass(frozen=True)
class Pair:
    chain_id: str
    earlier: ResolvedMarket
    later: ResolvedMarket
    earlier_entry: Entry
    later_entry: Entry

    @property
    def key(self):
        return f"{self.chain_id}:{self.earlier.condition_id}:{self.later.condition_id}"

    @property
    def tokens(self):
        return self.earlier.no, self.later.yes


@dataclass
class Universe:
    markets: dict[str, ResolvedMarket]
    chains: dict[str, tuple[ResolvedMarket, ...]]
    pairs: tuple[Pair, ...]
    token_ids: tuple[str, ...]
    reverse: dict[str, tuple[Pair, ...]]


@dataclass(frozen=True)
class Evaluation:
    state: str  # qualified, rejected (observable), blocked (unobservable)
    reason: str
    calculation: dict = field(default_factory=dict)
