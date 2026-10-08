"""Signal revisions used to reject incompatible historical backtest priors."""

SIGNAL_VERSIONS = {"Breakout": 2}


def signal_version(strategy: str) -> int:
    return SIGNAL_VERSIONS.get(strategy, 1)
