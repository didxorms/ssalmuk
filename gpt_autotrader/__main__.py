from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import replace

from .backtest import backtest_single_symbol
from .bot import AutoTrader, setup_logger
from .config import load_config

SIGNAL_CHOICES = ("all", "active", "buy", "sell", "hold")
TRADE_PHASE_CHOICES = ("both", "sell", "buy")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gpt_autotrader", description="Alpaca Paper Trading auto-trading bot")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan symbols and print signals without placing orders")
    scan.add_argument("--config", default="config.yaml")
    scan.add_argument("--held", action="store_true", help="scan only currently held positions")
    scan.add_argument("--symbols", nargs="+", help="scan only these symbols; accepts spaces or commas")
    add_output_args(scan)

    trade = sub.add_parser("trade", help="run trading bot")
    trade.add_argument("--config", default="config.yaml")
    trade.add_argument("--once", action="store_true", help="run once and exit")
    trade.add_argument("--interval-seconds", type=int, default=900, help="loop interval when --once is not set")
    trade.add_argument(
        "--phase",
        choices=TRADE_PHASE_CHOICES,
        default="both",
        help="which side of trading to execute: both, sell only, or buy only",
    )
    add_output_args(trade)

    bt = sub.add_parser("backtest", help="simple single-symbol backtest using Alpaca historical bars")
    bt.add_argument("--config", default="config.yaml")
    bt.add_argument("--symbol", required=True)
    bt.add_argument("--start-cash", type=float, default=100_000.0)
    bt.add_argument("--plain", action="store_true", help="disable color terminal output")
    bt.add_argument("--verbose", action="store_true", help="show detailed runtime logs")

    return parser


def add_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--top", type=int, default=40, help="number of signal rows to print; 0 prints all")
    parser.add_argument("--signals", choices=SIGNAL_CHOICES, default="all", help="filter printed signals")
    parser.add_argument("--plain", action="store_true", help="disable color terminal output")
    parser.add_argument("--verbose", action="store_true", help="show detailed runtime logs")


def _signal_name(decision) -> str:
    value = getattr(decision.signal, "value", decision.signal)
    return str(value).upper()


def _parse_symbols(raw_symbols: list[str] | None) -> list[str]:
    if not raw_symbols:
        return []
    symbols: list[str] = []
    for raw in raw_symbols:
        for part in raw.split(","):
            symbol = part.strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    if not symbols:
        raise ValueError("--symbols did not include any valid ticker symbols")
    return symbols


def _apply_scan_symbol_override(cfg, args):
    if getattr(args, "held", False) and getattr(args, "symbols", None):
        raise ValueError("Use either --held or --symbols, not both")
    symbols = _parse_symbols(getattr(args, "symbols", None))
    if not symbols:
        return cfg
    return replace(cfg, watchlist=symbols)


def _filtered_decisions(decisions, signal_filter: str, top: int):
    rows = sorted(decisions, key=lambda x: x.score, reverse=True)
    if signal_filter == "active":
        rows = [d for d in rows if _signal_name(d) in {"BUY", "SELL"}]
    elif signal_filter != "all":
        rows = [d for d in rows if _signal_name(d) == signal_filter.upper()]
    if top > 0:
        rows = rows[:top]
    return rows


def _signal_counts(decisions) -> dict[str, int]:
    counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    for d in decisions:
        counts[_signal_name(d)] = counts.get(_signal_name(d), 0) + 1
    return counts


def _rich_console(plain: bool):
    if plain:
        return None
    try:
        from rich.console import Console

        return Console()
    except Exception:
        return None


def _ansi_enabled(plain: bool) -> bool:
    return not plain and sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _ansi(text: str, code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\033[{code}m{text}\033[0m"


def _signal_style(signal: str) -> str:
    if signal == "BUY":
        return "1;32"
    if signal == "SELL":
        return "1;31"
    return "2"


def print_signal_table(decisions, *, top: int = 40, signal_filter: str = "all", plain: bool = False) -> None:
    rows = _filtered_decisions(decisions, signal_filter, top)
    counts = _signal_counts(decisions)

    console = _rich_console(plain)
    if console is not None:
        from rich import box
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        summary = (
            f"BUY {counts.get('BUY', 0)}   "
            f"SELL {counts.get('SELL', 0)}   "
            f"HOLD {counts.get('HOLD', 0)}   "
            f"showing {len(rows)}/{len(decisions)}"
        )
        console.print(Panel(summary, title="Signal Summary", border_style="cyan"))

        table = Table(title="Signals", box=box.SIMPLE_HEAVY, show_lines=False)
        table.add_column("Symbol", style="bold", no_wrap=True)
        table.add_column("Signal", justify="center", no_wrap=True)
        table.add_column("Close", justify="right")
        table.add_column("RSI", justify="right")
        table.add_column("Fast", justify="right")
        table.add_column("Slow", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("Reason", overflow="fold")

        for d in rows:
            signal = _signal_name(d)
            if signal == "BUY":
                signal_text = Text("BUY", style="bold green")
            elif signal == "SELL":
                signal_text = Text("SELL", style="bold red")
            else:
                signal_text = Text("HOLD", style="dim")
            score_style = "green" if d.score >= 0 else "red"
            table.add_row(
                d.symbol,
                signal_text,
                f"{d.last_close:,.2f}",
                f"{d.rsi:,.1f}",
                f"{d.sma_fast:,.2f}",
                f"{d.sma_slow:,.2f}",
                Text(f"{d.score:+.4f}", style=score_style),
                d.reason,
            )
        console.print(table)
        if top > 0 and len(rows) < len(_filtered_decisions(decisions, signal_filter, 0)):
            console.print(f"[dim]Use --top 0 to show every matching row.[/dim]")
        return

    use_ansi = _ansi_enabled(plain)
    print(
        "\n"
        + _ansi("SIGNALS", "1;36", use_ansi)
        + "  "
        + _ansi(f"BUY={counts.get('BUY', 0)}", "1;32", use_ansi)
        + " "
        + _ansi(f"SELL={counts.get('SELL', 0)}", "1;31", use_ansi)
        + " "
        + _ansi(f"HOLD={counts.get('HOLD', 0)}", "2", use_ansi)
        + f"  showing={len(rows)}/{len(decisions)}"
    )
    print("\n" + _ansi("SYMBOL  SIGNAL  CLOSE      RSI   SMA_FAST  SMA_SLOW  SCORE    REASON", "1", use_ansi))
    print("-" * 96)
    for d in rows:
        signal = _signal_name(d)
        signal_cell = _ansi(f"{signal:<6}", _signal_style(signal), use_ansi)
        score_style = "32" if d.score >= 0 else "31"
        score_cell = _ansi(f"{d.score:>+8.4f}", score_style, use_ansi)
        print(
            f"{d.symbol:<7} {signal_cell} {d.last_close:>9.2f} "
            f"{d.rsi:>6.1f} {d.sma_fast:>9.2f} {d.sma_slow:>9.2f} "
            f"{score_cell}  {d.reason}"
        )
    if top > 0 and len(rows) < len(_filtered_decisions(decisions, signal_filter, 0)):
        print("\n" + _ansi("Use --top 0 to show every matching row.", "2", use_ansi))


def print_backtest_result(result, *, plain: bool = False) -> None:
    console = _rich_console(plain)
    if console is not None:
        from rich import box
        from rich.table import Table

        table = Table(title="Backtest", box=box.ROUNDED)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")
        table.add_row("Symbol", result.symbol)
        table.add_row("Start cash", f"${result.start_cash:,.2f}")
        table.add_row("End value", f"${result.end_value:,.2f}")
        table.add_row("Strategy return", f"{result.total_return:.2%}")
        table.add_row("Buy and hold return", f"{result.buy_and_hold_return:.2%}")
        table.add_row("Trades", str(result.trades))
        console.print(table)
        return

    use_ansi = _ansi_enabled(plain)
    print("\n" + _ansi("BACKTEST", "1;36", use_ansi))
    print("-" * 40)
    print(f"symbol              : {result.symbol}")
    print(f"start_cash          : {result.start_cash:,.2f}")
    print(f"end_value           : {result.end_value:,.2f}")
    print(f"strategy_return     : {_ansi(f'{result.total_return:.2%}', '32' if result.total_return >= 0 else '31', use_ansi)}")
    print(f"buy_hold_return     : {_ansi(f'{result.buy_and_hold_return:.2%}', '32' if result.buy_and_hold_return >= 0 else '31', use_ansi)}")
    print(f"trades              : {result.trades}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if args.command == "scan":
        cfg = _apply_scan_symbol_override(cfg, args)
    console_level = logging.INFO if args.verbose else logging.WARNING
    logger = setup_logger(cfg.log_file, console_level=console_level)

    if args.command == "scan":
        bot = AutoTrader(cfg, logger=logger)
        decisions = bot.run_once(scan_only=True, held_only=args.held)
        print_signal_table(decisions, top=args.top, signal_filter=args.signals, plain=args.plain)
        return 0

    if args.command == "trade":
        bot = AutoTrader(cfg, logger=logger)
        if args.once:
            decisions = bot.run_once(scan_only=False, phase=args.phase)
            print_signal_table(decisions, top=args.top, signal_filter=args.signals, plain=args.plain)
        else:
            bot.run_loop(args.interval_seconds, phase=args.phase)
        return 0

    if args.command == "backtest":
        bot = AutoTrader(cfg, logger=logger)
        bars_by_symbol = bot.broker.get_daily_bars([args.symbol], cfg.lookback_days)
        bars = bars_by_symbol.get(args.symbol.upper())
        if bars is None or bars.empty:
            raise RuntimeError(f"No bars returned for {args.symbol}")
        result = backtest_single_symbol(args.symbol, bars, cfg, start_cash=args.start_cash)
        print_backtest_result(result, plain=args.plain)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
