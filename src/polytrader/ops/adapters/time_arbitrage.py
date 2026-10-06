"""Quoted opportunity estimates must never be summed as portfolio P&L."""

from decimal import Decimal

def event(row):
    kind = row.get("event", "unknown")
    calc = row.get("calculation", {})
    amount = Decimal(str(calc["conservative_profit"])) if "conservative_profit" in calc else None
    if amount is not None and not amount.is_finite():
        raise ValueError("Non-finite quoted profit")
    return kind, str(row.get("pair_id", "")), str(amount) if amount is not None else None
