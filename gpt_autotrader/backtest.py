from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .config import BotConfig
from .strategy import Signal, decide


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    start_cash: float
    end_value: float
    total_return: float
    buy_and_hold_return: float
    trades: int


def backtest_single_symbol(symbol: str, bars: pd.DataFrame, cfg: BotConfig, start_cash: float = 100_000.0) -> BacktestResult:
    if len(bars) < cfg.slow_sma + cfg.rsi_period + 5:
        raise ValueError("Not enough bars for backtest")

    cash = float(start_cash)
    qty = 0.0
    trades = 0
    start_price = float(bars.iloc[0]["close"])

    for i in range(cfg.slow_sma + cfg.rsi_period + 2, len(bars)):
        window = bars.iloc[: i + 1].copy()
        has_position = qty > 0
        decision = decide(symbol, window, cfg, has_position=has_position)
        close = float(window.iloc[-1]["close"])

        if decision.signal == Signal.BUY and qty == 0:
            qty = cash / close
            cash = 0.0
            trades += 1
        elif decision.signal == Signal.SELL and qty > 0:
            cash = qty * close
            qty = 0.0
            trades += 1

    final_close = float(bars.iloc[-1]["close"])
    end_value = cash + qty * final_close
    buy_and_hold_value = start_cash * (final_close / start_price)
    return BacktestResult(
        symbol=symbol.upper(),
        start_cash=start_cash,
        end_value=end_value,
        total_return=(end_value / start_cash) - 1.0,
        buy_and_hold_return=(buy_and_hold_value / start_cash) - 1.0,
        trades=trades,
    )
