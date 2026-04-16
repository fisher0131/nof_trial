from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BacktestResult:
    final_cash: float
    final_position_qty: float
    final_position_value: float
    final_equity: float
    trades: int
    win_rate: float


def run_backtest(ohlcv: list[list[float]], initial_usdc: float) -> BacktestResult:
    """Simple momentum backtester (interface kept for future use; not called by web UI)."""
    cash = initial_usdc
    position = 0.0
    entry = 0.0
    wins = 0
    trades = 0

    for i in range(5, len(ohlcv)):
        price = ohlcv[i][4]
        prev = ohlcv[i - 5][4]

        if position == 0 and price > prev:
            position = cash / price
            entry = price
            cash = 0
        elif position > 0 and price < prev:
            cash = position * price
            trades += 1
            if price > entry:
                wins += 1
            position = 0

    if position > 0:
        cash = position * ohlcv[-1][4]
        trades += 1
        if ohlcv[-1][4] > entry:
            wins += 1
        position = 0

    win_rate = wins / trades if trades > 0 else 0.0
    final_position_qty = position
    final_position_value = final_position_qty * ohlcv[-1][4] if ohlcv else 0.0
    final_equity = cash + final_position_value
    return BacktestResult(
        final_cash=cash,
        final_position_qty=final_position_qty,
        final_position_value=final_position_value,
        final_equity=final_equity,
        trades=trades,
        win_rate=win_rate,
    )
