from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class StateStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True) if self.path.parent != Path(".") else None
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        tmp_path.replace(self.path)

    def ensure_market_day(self, equity: float) -> dict[str, Any]:
        data = self.load()
        today = market_day_key()
        if data.get("market_day") != today:
            data = {
                "market_day": today,
                "start_equity": float(equity),
                "last_trade_at": {},
                "last_trade_side": {},
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.save(data)
        return data

    def mark_trade(self, symbol: str, side: str | None = None) -> None:
        data = self.load()
        clean_symbol = symbol.upper()
        data.setdefault("last_trade_at", {})[clean_symbol] = datetime.now(timezone.utc).isoformat()
        if side:
            data.setdefault("last_trade_side", {})[clean_symbol] = side.lower()
        self.save(data)


def market_day_key() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
