from __future__ import annotations

import hashlib
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from .risk import AccountSnapshot, PositionSnapshot

BAR_CACHE_VERSION = "daily-bars-v3-symbol-incremental"


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


class AlpacaBroker:
    def __init__(self, api_key: str, secret_key: str, paper: bool = True, data_feed: str = "iex"):
        # Imports are kept here so `python -m compileall` works even before dependencies are installed.
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self.paper = paper
        self.data_feed = data_feed.lower().strip()
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.data = StockHistoricalDataClient(api_key, secret_key)

    def get_account(self) -> AccountSnapshot:
        account = self.trading.get_account()
        return AccountSnapshot(
            equity=_float(account.equity),
            cash=_float(account.cash),
            buying_power=_float(account.buying_power),
        )

    def get_positions(self) -> list[PositionSnapshot]:
        positions = []
        for p in self.trading.get_all_positions():
            positions.append(
                PositionSnapshot(
                    symbol=str(p.symbol).upper(),
                    qty=_float(p.qty),
                    market_value=_float(p.market_value),
                    avg_entry_price=_float(p.avg_entry_price),
                    unrealized_plpc=_float(getattr(p, "unrealized_plpc", None), default=None),
                )
            )
        return positions

    def get_symbols_sold_today(self) -> set[str]:
        from alpaca.trading.enums import OrderSide, QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        market_now = datetime.now(ZoneInfo("America/New_York"))
        after = market_now.replace(hour=0, minute=0, second=0, microsecond=0)
        request = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            after=after,
            side=OrderSide.SELL,
            limit=500,
        )
        orders = self.trading.get_orders(request)

        ignored_statuses = {"canceled", "expired", "rejected"}
        result: set[str] = set()
        for order in orders:
            status = str(getattr(getattr(order, "status", ""), "value", getattr(order, "status", ""))).lower()
            if status in ignored_statuses:
                continue
            symbol = str(getattr(order, "symbol", "")).upper().strip()
            if symbol:
                result.add(symbol)
        return result

    def is_market_open(self) -> bool:
        clock = self.trading.get_clock()
        return bool(clock.is_open)

    def get_daily_bars(self, symbols: Iterable[str], lookback_days: int) -> dict[str, pd.DataFrame]:
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        clean_symbols = sorted({s.strip().upper() for s in symbols if s.strip()})
        if not clean_symbols:
            return {}

        end = datetime.now(timezone.utc)
        # Calendar days are not trading days. Pull extra history, then trim per symbol.
        full_start = end - timedelta(days=max(lookback_days * 3, 120))
        current_market_day = datetime.now(ZoneInfo("America/New_York")).date()

        feed = DataFeed.IEX if self.data_feed == "iex" else DataFeed.SIP
        result: dict[str, pd.DataFrame] = {}
        cached_by_symbol: dict[str, pd.DataFrame] = {}
        fetch_groups: dict[datetime, list[str]] = {}

        for symbol in clean_symbols:
            cached = self._load_symbol_bar_cache(symbol)
            if cached is None:
                fetch_groups.setdefault(full_start, []).append(symbol)
                continue

            checked_market_day = str(cached.get("checked_market_day") or "")
            cached_bars = self._normalize_cached_bars(cached.get("bars"), current_market_day, lookback_days)
            cached_by_symbol[symbol] = cached_bars
            if checked_market_day == current_market_day.isoformat():
                if not cached_bars.empty:
                    result[symbol] = cached_bars
                continue

            last_timestamp = self._last_bar_timestamp(cached_bars)
            if last_timestamp is None:
                fetch_groups.setdefault(full_start, []).append(symbol)
            else:
                fetch_groups.setdefault(last_timestamp + timedelta(days=1), []).append(symbol)

        for start, group_symbols in fetch_groups.items():
            request = StockBarsRequest(
                symbol_or_symbols=group_symbols,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                feed=feed,
                adjustment=Adjustment.SPLIT,
            )
            bars = self.data.get_stock_bars(request)
            fetched = self._daily_bars_from_response(bars.df, current_market_day, lookback_days)
            for symbol in group_symbols:
                combined = self._merge_daily_bars(cached_by_symbol.get(symbol), fetched.get(symbol))
                combined = self._trim_complete_bars(combined, current_market_day, lookback_days)
                self._save_symbol_bar_cache(symbol, current_market_day.isoformat(), combined)
                if not combined.empty:
                    result[symbol] = combined

        return result

    def _daily_bars_from_response(
        self,
        df: pd.DataFrame | None,
        current_market_day,
        lookback_days: int,
    ) -> dict[str, pd.DataFrame]:
        if df is None or df.empty:
            return {}

        df = df.reset_index()
        result: dict[str, pd.DataFrame] = {}
        for symbol, group in df.groupby("symbol"):
            group = group.rename(columns={"trade_count": "trades"})
            group = group[["timestamp", "open", "high", "low", "close", "volume"]].copy()
            group = self._trim_complete_bars(group, current_market_day, lookback_days)
            if not group.empty:
                result[str(symbol).upper()] = group
        return result

    def _trim_complete_bars(self, bars: pd.DataFrame | None, current_market_day, lookback_days: int) -> pd.DataFrame:
        if bars is None or bars.empty:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

        group = bars.copy()
        group["timestamp"] = pd.to_datetime(group["timestamp"], utc=True)
        group = group.sort_values("timestamp")
        market_dates = group["timestamp"].dt.tz_convert("America/New_York").dt.date
        group = group.loc[market_dates < current_market_day]
        group = group.drop_duplicates(subset=["timestamp"], keep="last").tail(lookback_days).copy()
        return group[["timestamp", "open", "high", "low", "close", "volume"]]

    def _merge_daily_bars(self, cached: pd.DataFrame | None, fetched: pd.DataFrame | None) -> pd.DataFrame:
        frames = [df for df in [cached, fetched] if df is not None and not df.empty]
        if not frames:
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        return pd.concat(frames, ignore_index=True)

    def _last_bar_timestamp(self, bars: pd.DataFrame | None) -> datetime | None:
        if bars is None or bars.empty:
            return None
        timestamp = pd.to_datetime(bars["timestamp"], utc=True).max()
        return timestamp.to_pydatetime()

    def _symbol_bar_cache_path(self, symbol: str) -> Path:
        safe_symbol = symbol.upper().replace("/", "_").replace("\\", "_")
        key = "|".join([BAR_CACHE_VERSION, self.data_feed, safe_symbol])
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return Path(".cache") / "daily_bars" / "symbols" / f"{safe_symbol}-{digest}.pkl"

    def _load_symbol_bar_cache(self, symbol: str) -> dict | None:
        path = self._symbol_bar_cache_path(symbol)
        if not path.exists():
            return None
        try:
            with path.open("rb") as f:
                cached = pickle.load(f)
        except Exception:
            return None
        if isinstance(cached, dict):
            return cached
        return None

    def _normalize_cached_bars(self, bars, current_market_day, lookback_days: int) -> pd.DataFrame:
        if not isinstance(bars, pd.DataFrame):
            return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        return self._trim_complete_bars(bars, current_market_day, lookback_days)

    def _save_symbol_bar_cache(self, symbol: str, checked_market_day: str, bars: pd.DataFrame) -> None:
        path = self._symbol_bar_cache_path(symbol)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as f:
                pickle.dump(
                    {
                        "version": BAR_CACHE_VERSION,
                        "checked_market_day": checked_market_day,
                        "bars": bars,
                    },
                    f,
                    protocol=pickle.HIGHEST_PROTOCOL,
                )
        except Exception:
            return

    def submit_market_buy_notional(self, symbol: str, notional: float):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(
            symbol=symbol.upper(),
            notional=round(float(notional), 2),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
        )
        return self.trading.submit_order(req)

    def submit_market_sell_qty(self, symbol: str, qty: float):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=abs(float(qty)),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        return self.trading.submit_order(req)
