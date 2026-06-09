from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import BotConfig
from .state import parse_dt


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    qty: float
    market_value: float
    avg_entry_price: float
    unrealized_plpc: float | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float
    cash: float
    buying_power: float


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str


def portfolio_exposure(positions: list[PositionSnapshot], equity: float) -> float:
    if equity <= 0:
        return 0.0
    long_value = sum(max(p.market_value, 0.0) for p in positions)
    return long_value / equity


def daily_loss_guard(account: AccountSnapshot, state: dict, cfg: BotConfig) -> RiskDecision:
    start_equity = float(state.get("start_equity") or account.equity)
    if start_equity <= 0:
        return RiskDecision(False, "start equity is invalid")
    drawdown = (start_equity - account.equity) / start_equity
    if drawdown >= cfg.max_daily_loss_pct:
        return RiskDecision(False, f"daily loss guard triggered: {drawdown:.2%}")
    return RiskDecision(True, f"daily drawdown OK: {drawdown:.2%}")


def cooldown_guard(symbol: str, state: dict, cfg: BotConfig) -> RiskDecision:
    last_trade_raw = state.get("last_trade_at", {}).get(symbol.upper())
    last_trade_at = parse_dt(last_trade_raw)
    if last_trade_at is None:
        return RiskDecision(True, "no cooldown history")
    elapsed = datetime.now(timezone.utc) - last_trade_at
    elapsed_minutes = elapsed.total_seconds() / 60
    if elapsed_minutes < cfg.cooldown_minutes:
        return RiskDecision(False, f"cooldown active: {elapsed_minutes:.1f}/{cfg.cooldown_minutes} minutes")
    return RiskDecision(True, "cooldown passed")


def exit_reason_from_pnl(position: PositionSnapshot, cfg: BotConfig) -> str | None:
    if position.avg_entry_price <= 0 or position.qty <= 0:
        return None
    pnl_pct = (position.market_value / (position.avg_entry_price * position.qty)) - 1.0
    if pnl_pct <= -cfg.stop_loss_pct:
        return f"stop loss triggered: {pnl_pct:.2%}"
    if pnl_pct >= cfg.take_profit_pct:
        return f"take profit triggered: {pnl_pct:.2%}"
    return None


def next_notional(account: AccountSnapshot, positions: list[PositionSnapshot], cfg: BotConfig) -> float:
    equity_cap = account.equity * cfg.max_position_pct
    current_exposure_value = sum(max(p.market_value, 0.0) for p in positions)
    max_exposure_value = account.equity * cfg.max_portfolio_exposure_pct
    remaining_exposure_value = max(0.0, max_exposure_value - current_exposure_value)
    cash_cap = max(0.0, account.cash * 0.95)
    return max(0.0, min(equity_cap, remaining_exposure_value, cash_cap))
