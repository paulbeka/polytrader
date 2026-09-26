"""Immutable views of outcome-token order books."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal

    def to_dict(self) -> dict:
        return {"price": str(self.price), "size": str(self.size)}


@dataclass(frozen=True)
class MarketReference:
    token_id: str
    market_id: str | None = None
    market_slug: str | None = None
    question: str | None = None
    outcome: str | None = None
    label: str | None = None
    deadline: str | None = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {**self.__dict__, "raw": dict(self.raw)}


@dataclass(frozen=True)
class BookSnapshot:
    token_id: str
    bids: tuple[Level, ...] = ()
    asks: tuple[Level, ...] = ()
    updated_at: datetime | None = None
    received_at: datetime | None = None
    status: str = "initializing"
    reason: str | None = None

    @property
    def best_bid(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask.price - self.best_bid.price
        return None

    @property
    def midpoint(self) -> Decimal | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid.price + self.best_ask.price) / 2
        return None

    def to_dict(self, *, depth: bool = True) -> dict:
        result = {
            "token_id": self.token_id, "status": self.status, "reason": self.reason,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "best_bid": self.best_bid.to_dict() if self.best_bid else None,
            "best_ask": self.best_ask.to_dict() if self.best_ask else None,
            "spread": str(self.spread) if self.spread is not None else None,
            "midpoint": str(self.midpoint) if self.midpoint is not None else None,
        }
        if depth:
            result.update(bids=[level.to_dict() for level in self.bids],
                          asks=[level.to_dict() for level in self.asks])
        return result


@dataclass
class OrderBooks:
    """Latest snapshots indexed by token ID, plus event/market metadata."""

    event: dict
    markets: dict[str, MarketReference]
    books: dict[str, BookSnapshot]
    excluded: list[dict] = field(default_factory=list)

    def summary(self) -> list[dict]:
        return [{**ref.to_dict(), **self.books[token].to_dict(depth=False)}
                for token, ref in self.markets.items()]

    def to_dict(self) -> dict:
        return {"event": self.event, "excluded": self.excluded,
                "books": [{"market": self.markets[token].to_dict(), **book.to_dict()}
                          for token, book in self.books.items()]}


@dataclass(frozen=True)
class BookUpdate:
    market: MarketReference
    book: BookSnapshot

    @property
    def market_slug(self) -> str | None:
        return self.market.market_slug

    @property
    def outcome(self) -> str | None:
        return self.market.outcome

    def to_dict(self) -> dict:
        return {"market": self.market.to_dict(), **self.book.to_dict()}
