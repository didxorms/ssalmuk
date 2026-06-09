from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class BotConfig:
    paper: bool = True
    place_orders: bool = False
    watchlist: list[str] = field(default_factory=lambda: ["SPY", "QQQ"])
    data_feed: str = "iex"  # Alpaca free/paper accounts commonly use IEX data.

    lookback_days: int = 260
    fast_sma: int = 20
    slow_sma: int = 50
    rsi_period: int = 14
    rsi_buy_min: float = 35.0
    rsi_buy_max: float = 72.0
    rsi_sell_below: float = 45.0

    max_open_positions: int = 4
    max_position_pct: float = 0.10
    max_portfolio_exposure_pct: float = 0.60
    min_trade_notional: float = 25.0
    max_daily_loss_pct: float = 0.02
    stop_loss_pct: float = 0.08
    take_profit_pct: float = 0.25
    cooldown_minutes: int = 60

    trade_only_when_market_open: bool = True
    state_file: str = "state.json"
    log_file: str = "logs/gpt_autotrader.log"


def _coerce_watchlist(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("watchlist must be a list of ticker symbols")
    result: list[str] = []
    for item in raw:
        symbol = str(item).strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def load_config(path: str | Path) -> BotConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError("config.yaml must contain a YAML mapping/object")

    if "watchlist" in raw:
        raw["watchlist"] = _coerce_watchlist(raw["watchlist"])

    cfg = BotConfig(**raw)
    validate_config(cfg)
    return cfg


def validate_config(cfg: BotConfig) -> None:
    if not cfg.watchlist:
        raise ValueError("watchlist is empty")
    if cfg.fast_sma <= 1 or cfg.slow_sma <= 1:
        raise ValueError("SMA periods must be greater than 1")
    if cfg.fast_sma >= cfg.slow_sma:
        raise ValueError("fast_sma should be smaller than slow_sma")
    if cfg.lookback_days < cfg.slow_sma + cfg.rsi_period + 5:
        raise ValueError("lookback_days is too small for the selected indicators")
    for name in [
        "max_position_pct",
        "max_portfolio_exposure_pct",
        "max_daily_loss_pct",
        "stop_loss_pct",
        "take_profit_pct",
    ]:
        value = getattr(cfg, name)
        if value <= 0 or value > 1:
            raise ValueError(f"{name} must be between 0 and 1")
    if cfg.max_open_positions < 1:
        raise ValueError("max_open_positions must be at least 1")


def load_env() -> tuple[str, str]:
    load_dotenv()
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    secret_key = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
    return api_key, secret_key


def require_live_trading_confirmation(paper: bool) -> None:
    if paper:
        return
    confirm = os.getenv("CONFIRM_LIVE_TRADING_I_ACCEPT_RISK", "").strip().lower()
    if confirm != "yes":
        raise RuntimeError(
            "Live trading blocked. Set CONFIRM_LIVE_TRADING_I_ACCEPT_RISK=yes "
            "only after you fully understand the risk."
        )
