"""Adapters preserve the separate meanings of each strategy's results."""

from . import lead_follower, time_arbitrage

ADAPTERS = {"lead_follower": lead_follower, "time_arbitrage": time_arbitrage}
