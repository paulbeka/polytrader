"""Allowlisted managed worker; all outputs live under its instance volume."""

import asyncio
from dataclasses import replace
import importlib
from pathlib import Path

from .runtime import until_stopped

STRATEGIES = ("lead_follower", "time_arbitrage")


def configuration(strategy, path, output):
    if strategy not in STRATEGIES:
        raise ValueError("Unknown strategy")
    module = importlib.import_module(f"polytrader.bot.{strategy}.config")
    config = module.load_config(Path(path))
    if strategy == "lead_follower":
        return replace(config, output_dir=Path(output))
    return replace(config, scanner=replace(config.scanner, output_dir=Path(output)))


def worker(strategy, path, output, *, validate=False, offline=False):
    config = configuration(strategy, path, output)
    if offline:
        return
    runner = importlib.import_module(f"polytrader.bot.{strategy}.runner")
    if strategy == "lead_follower":
        from polytrader.bot.lead_follower.discovery import prepare
        first, second = prepare(config)
        runner.describe(first)
    else:
        first, second, errors = runner.prepare(config)
        runner.describe(config, first, second)
        if errors or any(not m.supported for m in second.values()):
            raise ValueError(f"Discovery metadata incomplete: {errors}")
    if not validate:
        try:
            asyncio.run(until_stopped(runner.run(config, first, second)))
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
