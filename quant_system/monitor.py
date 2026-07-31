from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime, time as clock_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import DATA_DIR, REPORT_DIR
from .data import fetch_hot_rank, fetch_universe_history
from .pipeline import run_pipeline


SHANGHAI = ZoneInfo("Asia/Shanghai")
STATE_FILE = DATA_DIR / "last_notified_signals.csv"
NOTIFICATION_LOG = DATA_DIR / "notifications.log"

EXCHANGE_HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 2),
    *[date(2026, 2, day) for day in range(16, 24)],
    date(2026, 4, 6),
    *[date(2026, 5, day) for day in range(1, 6)],
    date(2026, 6, 19),
    date(2026, 9, 25),
    *[date(2026, 10, day) for day in range(1, 8)],
}


def is_trading_day(day: date) -> bool:
    if day.weekday() >= 5:
        return False
    if day.year == 2026 and day in EXCHANGE_HOLIDAYS_2026:
        return False
    return True


def is_trading_session(now: datetime | None = None) -> bool:
    now = now or datetime.now(SHANGHAI)
    if not is_trading_day(now.date()):
        return False
    current = now.time().replace(tzinfo=None)
    return clock_time(8, 30) <= current <= clock_time(15, 0)


def _windows_popup(title: str, message: str) -> None:
    timestamp = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    DATA_DIR.mkdir(exist_ok=True)
    with NOTIFICATION_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp}\t{title}\t{message}\n")
    if os.name != "nt":
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            ["msg.exe", "*", "/TIME:30", f"{title}\n{message}"],
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def _changes(previous: pd.DataFrame, current: pd.DataFrame) -> list[str]:
    old = previous.set_index("symbol")["action"].to_dict() if not previous.empty else {}
    new = current.set_index("symbol")["action"].to_dict()
    rank = current.set_index("symbol")["hot_rank"].to_dict()
    changed = []
    for symbol, action in new.items():
        before = old.get(symbol)
        if before is not None and before != action:
            changed.append(f"#{int(rank[symbol])} {symbol}: {before}→{action}")
        elif before is None and old:
            changed.append(f"#{int(rank[symbol])} {symbol}: 新入榜→{action}")
    for symbol in old.keys() - new.keys():
        changed.append(f"{symbol}: 已移出热榜")
    return changed


def refresh_once(force: bool = False) -> dict:
    now = datetime.now(SHANGHAI)
    if not force and not is_trading_session(now):
        return {"status": "skipped", "reason": "outside_trading_session", "time": now.isoformat()}

    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)
    universe = fetch_hot_rank(50)
    cached_path = DATA_DIR / "bars_daily_latest.csv.gz"
    cached = pd.read_csv(cached_path, dtype={"symbol": str}, parse_dates=["trade_date"]) if cached_path.exists() else pd.DataFrame()
    symbols = set(universe["symbol"])
    known = set(cached["symbol"]) if not cached.empty else set()
    recent_start = (now.date() - timedelta(days=220)).strftime("%Y%m%d")
    recent, errors = fetch_universe_history(universe, start=recent_start)
    missing_symbols = symbols - known
    if missing_symbols:
        missing_universe = universe[universe["symbol"].isin(missing_symbols)]
        full, full_errors = fetch_universe_history(missing_universe)
        errors.update(full_errors)
        recent = pd.concat([recent[~recent["symbol"].isin(missing_symbols)], full], ignore_index=True)

    retained = cached[cached["symbol"].isin(symbols)] if not cached.empty else cached
    bars = pd.concat([retained, recent], ignore_index=True).drop_duplicates(["symbol", "trade_date"], keep="last")
    if "name" in universe.columns:
        bars["name"] = bars["symbol"].map(universe.set_index("symbol")["name"]).fillna(bars["name"])
    universe.to_csv(DATA_DIR / "hot_rank_latest.csv", index=False, encoding="utf-8-sig")
    bars.to_csv(cached_path, index=False, compression="gzip")

    previous = pd.read_csv(STATE_FILE, dtype={"symbol": str}) if STATE_FILE.exists() else pd.DataFrame()
    report = run_pipeline(persist=True, reuse_data=True)
    current = pd.read_csv(REPORT_DIR / "latest_signals.csv", dtype={"symbol": str})
    changed = _changes(previous, current)
    current.to_csv(STATE_FILE, index=False, encoding="utf-8-sig")
    if changed:
        preview = "；".join(changed[:8])
        if len(changed) > 8:
            preview += f"；另有 {len(changed) - 8} 项"
        _windows_popup("量化信号发生变化", preview)
    return {
        "status": "changed" if changed else "no_changes",
        "time": now.isoformat(),
        "changes": changed,
        "download_errors": errors,
        "run_id": report["run_id"],
    }
