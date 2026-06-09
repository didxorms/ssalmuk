from __future__ import annotations

import logging
import time
from pathlib import Path

from .broker_alpaca import AlpacaBroker
from .config import BotConfig, load_env, require_live_trading_confirmation
from .risk import (
    AccountSnapshot,
    PositionSnapshot,
    cooldown_guard,
    daily_loss_guard,
    exit_reason_from_pnl,
    next_notional,
    portfolio_exposure,
)
from .state import StateStore
from .strategy import Signal, StrategyDecision, decide


class AutoTrader:
    def __init__(self, cfg: BotConfig, logger: logging.Logger | None = None):
        self.cfg = cfg
        self.logger = logger or setup_logger(cfg.log_file)
        api_key, secret_key = load_env()
        self.broker = AlpacaBroker(api_key, secret_key, paper=cfg.paper, data_feed=cfg.data_feed)
        self.state = StateStore(cfg.state_file)

    def run_loop(self, interval_seconds: int) -> None:
        while True:
            try:
                self.run_once(scan_only=False)
            except KeyboardInterrupt:
                raise
            except Exception:
                self.logger.exception("run_once failed")
            time.sleep(max(10, interval_seconds))

    def run_once(self, scan_only: bool = False, held_only: bool = False) -> list[StrategyDecision]:
        cfg = self.cfg
        account = self.broker.get_account()
        state = self.state.ensure_market_day(account.equity)
        positions = self.broker.get_positions()
        positions_by_symbol = {p.symbol: p for p in positions}
        symbols_to_scan = sorted(positions_by_symbol) if held_only else cfg.watchlist

        self.logger.info(
            "account equity=%.2f cash=%.2f exposure=%.2f%% paper=%s place_orders=%s",
            account.equity,
            account.cash,
            portfolio_exposure(positions, account.equity) * 100,
            cfg.paper,
            cfg.place_orders,
        )

        if held_only and not symbols_to_scan:
            self.logger.info("held scan: no open positions")
            return []

        bars_by_symbol = self.broker.get_daily_bars(symbols_to_scan, cfg.lookback_days)
        decisions: list[StrategyDecision] = []
        for symbol in symbols_to_scan:
            bars = bars_by_symbol.get(symbol.upper())
            if bars is None or bars.empty:
                self.logger.warning("%s: no bars returned", symbol)
                continue
            decisions.append(decide(symbol, bars, cfg, has_position=symbol.upper() in positions_by_symbol))
        decisions = self._apply_risk_exit_signals(decisions, positions_by_symbol)

        for d in sorted(decisions, key=lambda x: x.score, reverse=True):
            self.logger.info(
                "signal %-5s %-5s close=%8.2f rsi=%5.1f sma_fast=%8.2f sma_slow=%8.2f score=%+.4f reason=%s",
                d.symbol,
                d.signal.value,
                d.last_close,
                d.rsi,
                d.sma_fast,
                d.sma_slow,
                d.score,
                d.reason,
            )

        if scan_only or not cfg.place_orders:
            self.logger.info("scan/dry mode: no orders submitted")
            return decisions

        if Path("kill_switch.txt").exists():
            self.logger.error("kill_switch.txt found: no orders submitted")
            return decisions

        require_live_trading_confirmation(cfg.paper)

        if cfg.trade_only_when_market_open and not self.broker.is_market_open():
            self.logger.warning("market is closed: no orders submitted")
            return decisions

        risk = daily_loss_guard(account, state, cfg)
        if not risk.allowed:
            self.logger.error("%s", risk.reason)
            return decisions

        submitted_sell_symbols = self._execute_sells(decisions, positions_by_symbol)

        # Refresh account/positions after possible sells.
        account, positions = self._refresh_after_sells(submitted_sell_symbols)
        positions_by_symbol = {p.symbol: p for p in positions}
        self._execute_buys(decisions, account, positions, positions_by_symbol)
        return decisions

    def _apply_risk_exit_signals(
        self,
        decisions: list[StrategyDecision],
        positions_by_symbol: dict[str, PositionSnapshot],
    ) -> list[StrategyDecision]:
        result: list[StrategyDecision] = []
        for decision in decisions:
            position = positions_by_symbol.get(decision.symbol.upper())
            risk_exit_reason = exit_reason_from_pnl(position, self.cfg) if position else None
            if risk_exit_reason:
                result.append(
                    StrategyDecision(
                        symbol=decision.symbol,
                        signal=Signal.SELL,
                        reason=risk_exit_reason,
                        score=decision.score,
                        last_close=decision.last_close,
                        rsi=decision.rsi,
                        sma_fast=decision.sma_fast,
                        sma_slow=decision.sma_slow,
                    )
                )
            else:
                result.append(decision)
        return result

    def _execute_sells(self, decisions: list[StrategyDecision], positions_by_symbol: dict[str, PositionSnapshot]) -> set[str]:
        cfg = self.cfg
        decision_by_symbol = {d.symbol.upper(): d for d in decisions}
        submitted_sell_symbols: set[str] = set()

        for symbol, position in positions_by_symbol.items():
            if position.qty <= 0:
                continue

            strategy_decision = decision_by_symbol.get(symbol)
            risk_exit_reason = exit_reason_from_pnl(position, cfg)
            should_sell = False
            reason = ""

            if risk_exit_reason:
                should_sell = True
                reason = risk_exit_reason
            elif strategy_decision and strategy_decision.signal == Signal.SELL:
                should_sell = True
                reason = strategy_decision.reason

            if not should_sell:
                continue

            cooldown = cooldown_guard(symbol, self.state.load(), cfg)
            if not cooldown.allowed:
                self.logger.info("SELL skipped %s: %s", symbol, cooldown.reason)
                continue

            self.logger.warning("SELL %s qty=%.6f reason=%s", symbol, position.qty, reason)
            order = self.broker.submit_market_sell_qty(symbol, position.qty)
            self.state.mark_trade(symbol, side="sell")
            submitted_sell_symbols.add(symbol)
            self.logger.warning("SELL order submitted %s: %s", symbol, getattr(order, "id", order))

        return submitted_sell_symbols

    def _refresh_after_sells(self, submitted_sell_symbols: set[str]) -> tuple[AccountSnapshot, list[PositionSnapshot]]:
        account = self.broker.get_account()
        positions = self.broker.get_positions()
        if not submitted_sell_symbols:
            return account, positions

        deadline = time.monotonic() + 8
        while self._positions_include_symbols(positions, submitted_sell_symbols):
            if time.monotonic() >= deadline:
                still_open = sorted(
                    p.symbol for p in positions if p.symbol in submitted_sell_symbols and p.qty > 0 and p.market_value > 0
                )
                self.logger.info("SELL positions still reflected after refresh: %s", ", ".join(still_open))
                break
            time.sleep(1)
            account = self.broker.get_account()
            positions = self.broker.get_positions()

        return account, positions

    def _positions_include_symbols(self, positions: list[PositionSnapshot], symbols: set[str]) -> bool:
        return any(p.symbol in symbols and p.qty > 0 and p.market_value > 0 for p in positions)

    def _execute_buys(
        self,
        decisions: list[StrategyDecision],
        account: AccountSnapshot,
        positions: list[PositionSnapshot],
        positions_by_symbol: dict[str, PositionSnapshot],
    ) -> None:
        cfg = self.cfg
        state = self.state.load()
        last_trade_at = state.get("last_trade_at", {})
        if not isinstance(last_trade_at, dict):
            last_trade_at = {}
        last_trade_side = state.get("last_trade_side", {})
        if not isinstance(last_trade_side, dict):
            last_trade_side = {}
        recent_buy_symbols = {
            str(symbol).upper()
            for symbol in last_trade_at
            if str(last_trade_side.get(symbol, "")).lower() == "buy"
        }
        reserved_symbols = recent_buy_symbols - set(positions_by_symbol)
        open_position_count = sum(1 for p in positions if p.qty > 0 and p.market_value > 0) + len(reserved_symbols)
        available_slots = max(0, cfg.max_open_positions - open_position_count)
        if available_slots <= 0:
            self.logger.info("BUY skipped: max_open_positions reached")
            return

        if reserved_symbols:
            self.logger.info("BUY reserved slots for recent submitted orders: %s", ", ".join(sorted(reserved_symbols)))

        candidates = [
            d
            for d in decisions
            if (
                d.signal == Signal.BUY
                and d.symbol.upper() not in positions_by_symbol
                and d.symbol.upper() not in reserved_symbols
            )
        ]
        candidates.sort(key=lambda x: x.score, reverse=True)

        submitted_buys = 0
        for d in candidates:
            if submitted_buys >= available_slots:
                break

            state = self.state.load()
            cooldown = cooldown_guard(d.symbol, state, cfg)
            if not cooldown.allowed:
                self.logger.info("BUY skipped %s: %s", d.symbol, cooldown.reason)
                continue

            notional = next_notional(account, positions, cfg)
            if notional < cfg.min_trade_notional:
                self.logger.info("BUY skipped %s: notional %.2f below minimum %.2f", d.symbol, notional, cfg.min_trade_notional)
                break

            self.logger.warning("BUY %s notional=%.2f reason=%s", d.symbol, notional, d.reason)
            order = self.broker.submit_market_buy_notional(d.symbol, notional)
            self.state.mark_trade(d.symbol, side="buy")
            self.logger.warning("BUY order submitted %s: %s", d.symbol, getattr(order, "id", order))
            submitted_buys += 1

            # Conservative local update so the next candidate respects caps even before broker refresh.
            positions.append(PositionSnapshot(d.symbol.upper(), qty=1.0, market_value=notional, avg_entry_price=d.last_close))
            account = AccountSnapshot(
                equity=account.equity,
                cash=max(0.0, account.cash - notional),
                buying_power=max(0.0, account.buying_power - notional),
            )


def setup_logger(log_file: str, console_level: int = logging.WARNING) -> logging.Logger:
    logger = logging.getLogger("gpt_autotrader")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    path = Path(log_file)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
