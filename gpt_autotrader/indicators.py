from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    # Wilder-style smoothing via exponential moving average.
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def add_indicators(df: pd.DataFrame, fast_sma: int, slow_sma: int, rsi_period: int) -> pd.DataFrame:
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")

    result = df.copy()
    result["sma_fast"] = sma(result["close"], fast_sma)
    result["sma_slow"] = sma(result["close"], slow_sma)
    result["rsi"] = rsi(result["close"], rsi_period)
    result["momentum_20d"] = result["close"].pct_change(20)
    result["volatility_20d"] = result["close"].pct_change().rolling(20).std()
    return result
