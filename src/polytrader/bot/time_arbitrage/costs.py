"""Fail-closed fee/constraint adapters. No category/title-based fee inference."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json

from polytrader.data.discovery import parse_market
from polytrader.data import DataError
from .models import D

FEE_SOURCE = "https://docs.polymarket.com/trading/fees"
CONSTRAINT_SOURCE = "https://docs.polymarket.com/market-data/market-details"
SDK_SOURCE = "https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/fees.py"
SHARE_STEP = D("0.01")  # Official V2 limit-order builder size precision.
TICKS = {D(x) for x in (".1", ".01", ".005", ".0025", ".001", ".0001")}


def number(value, name, *, positive=False):
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Unknown {name}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid {name}") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"Invalid {name}")
    return result


def rules_hash(raw):
    rules = {key: raw.get(key) for key in ("description", "resolutionSource")}
    return hashlib.sha256(json.dumps(rules, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Metadata:
    condition_id: str
    fetched_at: datetime
    supported: bool
    reason: str
    adapter: str = "unsupported"
    rate: Decimal = D("0")
    exponent: int = 1
    min_notional: Decimal = D("0")
    tick: Decimal = D("0.01")
    share_step: Decimal = SHARE_STEP
    raw: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=lambda: {
        "fee_source": FEE_SOURCE, "buy_cash_fee_source": SDK_SOURCE,
        "minimum_source": CONSTRAINT_SOURCE, "minimum_unit": "USD notional per leg",
        "rounding": "ceil to 5 decimal USD per consumed price level; fragmentation estimated",
        "share_precision_source": "https://github.com/Polymarket/py-clob-client-v2/blob/main/py_clob_client_v2/order_builder/builder.py",
        "delivered_shares": "equal order shares; zero fees or supported V2 cash fees",
    })

    def unit_fee(self, price):
        return self.rate * (price * (1 - price)) ** self.exponent

    def fee(self, price, shares):
        return (shares * self.unit_fee(price)).quantize(D(".00001"), rounding=ROUND_CEILING)


def interpret(market, gamma, clob, fees, fetched_at):
    """Interpret exact-market metadata; unsupported is a visible blocking state."""
    raw = {"gamma": gamma, "clob": clob, "token_fee_rates": fees,
           "endpoints": [f"https://gamma-api.polymarket.com/markets/slug/{market.slug}",
                         f"https://clob.polymarket.com/clob-markets/{market.condition_id}",
                         *[f"https://clob.polymarket.com/fee-rate?token_id={t}" for t in fees]]}
    try:
        parsed = parse_market(gamma)
        tokens = {k.casefold(): v for k, v in parsed.tokens.items()}
        if (parsed.id != market.id or gamma.get("conditionId") != market.condition_id
                or tokens != {"yes": market.yes, "no": market.no}
                or clob.get("c") != market.condition_id):
            raise ValueError("market_identity_changed")
        if rules_hash(gamma) != rules_hash(market.raw):
            raise ValueError("resolution_rules_changed_restart_and_review")
        if not str(gamma.get("description", "")).strip():
            raise ValueError("missing_resolution_rules")
        clob_tokens = {t.get("o", "").casefold(): t.get("t") for t in clob.get("t", [])}
        if clob_tokens != tokens:
            raise ValueError("clob_token_mapping_mismatch")
        if clob.get("v") not in {"v1", "v2"}:
            raise ValueError("unknown_clob_version")
        if (gamma.get("active") is not True or gamma.get("closed") is not False
                or gamma.get("acceptingOrders") is not True
                or gamma.get("enableOrderBook") is not True
                or clob.get("cbos") is not True or clob.get("ao", True) is not True):
            raise ValueError("market_not_tradable")
        minimum = number(gamma.get("orderMinSize"), "minimum notional", positive=True)
        tick = number(gamma.get("orderPriceMinTickSize"), "price tick", positive=True)
        if tick not in TICKS or number(clob.get("mts"), "CLOB tick") != tick:
            raise ValueError("unknown_or_inconsistent_tick_size")
        bases = [number(fees[t].get("base_fee"), "token fee rate") for t in (market.yes, market.no)]
        if gamma.get("feesEnabled") is False:
            if any(bases) or number(clob.get("tbf", 0), "taker base fee") != 0:
                raise ValueError("conflicting_zero_fee_metadata")
            for schedule, key in ((gamma.get("feeSchedule"), "rate"), (clob.get("fd"), "r")):
                if schedule is not None and (not isinstance(schedule, dict)
                                             or number(schedule.get(key), "fee schedule rate") != 0):
                    raise ValueError("conflicting_zero_fee_schedule")
            adapter, rate, exponent = "confirmed-zero", D(0), 1
        elif gamma.get("feesEnabled") is True:
            if clob.get("v") != "v2":
                raise ValueError("unsupported_fee_bearing_version_net_shares_unverified")
            schedule, fd = gamma.get("feeSchedule"), clob.get("fd")
            if not isinstance(schedule, dict) or not isinstance(fd, dict):
                raise ValueError("missing_fee_schedule")
            rate = number(schedule.get("rate"), "fee rate", positive=True)
            exponent_value = number(schedule.get("exponent"), "fee exponent", positive=True)
            if exponent_value not in (D(1), D(2)):
                raise ValueError("unsupported_fee_exponent")
            exponent = int(exponent_value)
            if (rate * exponent > 1 or number(fd.get("r"), "CLOB fee rate") != rate
                    or number(fd.get("e"), "CLOB exponent") != exponent_value
                    or schedule.get("takerOnly") is not True or fd.get("to") is not True
                    or not all(bases) or bases[0] != bases[1]):
                raise ValueError("unsupported_or_conflicting_fee_schedule")
            adapter = "v2-cash-price-curve"
        else:
            raise ValueError("unknown_fees_enabled")
        return Metadata(market.condition_id, fetched_at, True, "supported", adapter,
                        rate, exponent, minimum, tick, raw=raw)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        return Metadata(market.condition_id, fetched_at, False, str(exc), raw=raw)


def fetch_metadata(market, client):
    gamma = client.get_market(market.slug)
    clob = client.get_clob_info(market.condition_id)
    fees = {t: client.get_fee(t) for t in (market.yes, market.no)}
    return interpret(market, gamma, clob, fees, datetime.now(timezone.utc))


def refresh_metadata(universe, client, previous=None):
    """Blocking HTTP batch, called in a worker thread. Errors retain last-known data.

    Retained records keep their original fetch time: the detector enforces TTL.
    An explicit unsupported response immediately replaces a previous good record.
    """
    result, errors = dict(previous or {}), {}
    for key, market in universe.markets.items():
        try:
            result[key] = fetch_metadata(market, client)
        except (OSError, ValueError, DataError) as exc:
            # Public client wraps transport errors in DataError; preserve last good.
            errors[key] = str(exc)
            if key not in result:
                result[key] = Metadata(key, datetime.now(timezone.utc), False,
                                       "metadata_fetch_failed: " + str(exc))
    return result, errors
