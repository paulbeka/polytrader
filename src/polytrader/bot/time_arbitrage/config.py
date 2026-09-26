"""Strict TOML configuration: array order defines the implication chain."""

from dataclasses import fields
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path
import tomllib

from .models import Chain, Config, CostSettings, Entry, ScannerSettings


def known(table, names, location):
    if not isinstance(table, dict):
        raise ValueError(f"{location} must be a table")
    unknown = table.keys() - set(names)
    if unknown:
        raise ValueError(f"Unknown keys in {location}: {', '.join(sorted(unknown))}")


def decimal_string(value, name, *, positive=False):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a quoted decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal for {name}") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def duration(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number of seconds")
    return float(value)


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def parse_entry(value):
    if isinstance(value, str):
        value = {"ref": value}
    known(value, {"ref", "market", "label", "deadline"}, "market entry")
    ref = text(value.get("ref"), "ref")
    market = text(value["market"], "market") if "market" in value else None
    label = text(value["label"], "label") if "label" in value else None
    deadline = value.get("deadline")
    if deadline is not None:
        if isinstance(deadline, str):
            deadline = datetime.fromisoformat(deadline)
        if not isinstance(deadline, datetime) or deadline.tzinfo is None:
            raise ValueError("deadline must be a timezone-aware ISO timestamp")
    return Entry(ref, market, label, deadline)


def load_config(path):
    path = Path(path).resolve()
    content = path.read_bytes()
    raw = tomllib.loads(content.decode("utf-8-sig"))
    known(raw, {"version", "scanner", "costs", "chains"}, "root")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise ValueError("Supported config version is 1")
    scanner_values, cost_values = {}, {}
    for name, cls, target in (("scanner", ScannerSettings, scanner_values), ("costs", CostSettings, cost_values)):
        table = raw.get(name, {})
        known(table, {f.name for f in fields(cls)}, name)
        defaults = cls()
        for key, value in table.items():
            if isinstance(getattr(defaults, key), Decimal):
                value = decimal_string(value, f"{name}.{key}", positive=key in {
                    "min_shares", "max_shares", "max_cost_per_opportunity"})
            elif key.endswith("_seconds"):
                value = duration(value, key)
            else:
                value = text(value, key)
            target[key] = value
    output = text(scanner_values.get("output_dir", "data/time_arbitrage"), "output_dir")
    scanner_values["output_dir"] = (path.parent / output).resolve()
    scanner, costs = ScannerSettings(**scanner_values), CostSettings(**cost_values)
    if scanner.depth_mode not in {"top", "full"}:
        raise ValueError("depth_mode must be top or full")
    if scanner.min_shares > scanner.max_shares:
        raise ValueError("min_shares exceeds max_shares")
    if costs.fee_mode != "auto" or costs.unknown_fee_policy != "skip":
        raise ValueError("Only fee_mode=auto and unknown_fee_policy=skip are supported")
    if costs.refresh_seconds > costs.max_metadata_age_seconds:
        raise ValueError("refresh_seconds exceeds max_metadata_age_seconds")
    if not isinstance(raw.get("chains"), list) or not raw["chains"]:
        raise ValueError("At least one chain is required")
    chains, ids = [], set()
    for row in raw["chains"]:
        known(row, {f.name for f in fields(Chain)}, "chain")
        identity = text(row.get("id"), "chain id")
        if identity in ids:
            raise ValueError(f"Duplicate chain ID: {identity}")
        ids.add(identity)
        if "enabled" in row and type(row["enabled"]) is not bool:
            raise ValueError("enabled must be a boolean")
        if row.get("relation", "yes_implies_next_yes") != "yes_implies_next_yes":
            raise ValueError("Unsupported chain relation")
        if not isinstance(row.get("markets"), list):
            raise ValueError("markets must be an ordered array")
        entries = tuple(parse_entry(v) for v in row["markets"])
        chain = Chain(**{**row, "id": identity, "markets": entries})
        if chain.enabled and len(entries) < 2:
            raise ValueError("An enabled chain needs at least two markets")
        if entries and all(e.deadline is not None for e in entries):
            if any(a.deadline >= b.deadline for a, b in zip(entries, entries[1:])):
                raise ValueError(f"Deadlines must strictly increase in chain {identity}")
        chains.append(chain)
    if not any(c.enabled for c in chains):
        raise ValueError("No enabled chains")
    return Config(path, scanner, costs, tuple(chains), hashlib.sha256(content).hexdigest())
