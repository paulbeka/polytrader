"""Fee-excluded hypothetical position P&L, not real trading returns."""

from decimal import Decimal

def event(row):
    kind = row.get("type", "unknown")
    amount = Decimal(str(row["pnl"])) if kind == "exit" else None
    if amount is not None and not amount.is_finite():
        raise ValueError("Non-finite paper P&L")
    return kind, str(row.get("follower", "")), str(amount) if amount is not None else None
