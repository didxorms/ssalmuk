from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd

from .config import BotConfig
from .indicators import add_indicators


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class StrategyDecision:
    symbol: str
    signal: Signal
    reason: str
    score: float
    last_close: float
    rsi: float
    sma_fast: float
    sma_slow: float


def decide(symbol: str, bars: pd.DataFrame, cfg: BotConfig, has_position: bool) -> StrategyDecision:
    df = add_indicators(bars, cfg.fast_sma, cfg.slow_sma, cfg.rsi_period).dropna()
    if len(df) < 3:
        return StrategyDecision(symbol, Signal.HOLD, "not enough indicator-ready bars", 0.0, 0.0, 50.0, 0.0, 0.0)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    close = float(last["close"])
    sma_fast = float(last["sma_fast"])
    sma_slow = float(last["sma_slow"])
    rsi_value = float(last["rsi"])
    momentum = float(last.get("momentum_20d", 0.0) or 0.0)
    volatility = float(last.get("volatility_20d", 0.0) or 0.0)

    trend_ok = close > sma_fast > sma_slow
    rsi_ok = cfg.rsi_buy_min <= rsi_value <= cfg.rsi_buy_max
    recent_breakout = float(prev["close"]) <= float(prev["sma_fast"]) and close > sma_fast

    # Higher score = stronger candidate. Penalize very high short-term volatility.
    score = (close / sma_slow - 1.0) + 0.40 * momentum - 0.25 * max(volatility, 0.0)

    if has_position:
        if close < sma_fast:
            return StrategyDecision(symbol, Signal.SELL, "close below fast SMA", score, close, rsi_value, sma_fast, sma_slow)
        if rsi_value < cfg.rsi_sell_below:
            return StrategyDecision(symbol, Signal.SELL, "RSI weakened below sell threshold", score, close, rsi_value, sma_fast, sma_slow)
        return StrategyDecision(symbol, Signal.HOLD, "position trend still intact", score, close, rsi_value, sma_fast, sma_slow)

    if trend_ok and rsi_ok:
        reason = "uptrend with RSI filter passed"
        if recent_breakout:
            reason += "; fresh fast-SMA reclaim"
        return StrategyDecision(symbol, Signal.BUY, reason, score, close, rsi_value, sma_fast, sma_slow)

    if not trend_ok:
        return StrategyDecision(symbol, Signal.HOLD, "trend filter failed", score, close, rsi_value, sma_fast, sma_slow)
    return StrategyDecision(symbol, Signal.HOLD, "RSI filter failed", score, close, rsi_value, sma_fast, sma_slow)
