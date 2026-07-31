from __future__ import annotations

import json
import time
import random
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import numpy as np

from .config import SETTINGS

HOT_PAGE_URL = "https://guba.eastmoney.com/rank/"
HOT_URL = "https://emappdata.eastmoney.com/stockrank/getAllCurrentList"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
GLOBAL_ID = "786e4c21-70dc-435a-93bb-38"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0 Safari/537.36"


def _json(url: str, payload: dict | None = None, retries: int = 5) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": HOT_PAGE_URL,
        "Content-Type": "application/json",
        "Connection": "close",
    }
    request = urllib.request.Request(url, data=body, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=SETTINGS.request_timeout) as response:
                return json.load(response)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(1.0 * (2**attempt) + random.uniform(0.1, 0.8))
    raise RuntimeError("unreachable")


def fetch_hot_rank(limit: int = 50) -> pd.DataFrame:
    payload = {
        "appId": "appId01",
        "globalId": GLOBAL_ID,
        "marketType": "",
        "pageNo": 1,
        "pageSize": limit,
    }
    response = _json(HOT_URL, payload)
    rows = response.get("data") or []
    if not isinstance(rows, list):
        raise RuntimeError("东方财富人气榜返回格式异常：data 不是列表")
    if len(rows) < limit:
        raise RuntimeError(f"东方财富人气榜只返回 {len(rows)} 条，预期 {limit} 条")
    valid_rows = []
    for row in rows[:limit]:
        if not isinstance(row, dict) or row.get("rk") is None or not row.get("sc"):
            continue
        code = str(row["sc"])
        if len(code) == 8 and code[:2] in {"SH", "SZ", "BJ"} and code[2:].isdigit():
            valid_rows.append((row, code))
    if len(valid_rows) < limit:
        raise RuntimeError(f"东方财富人气榜有效股票仅 {len(valid_rows)} 条，预期 {limit} 条")
    rows = [row for row, _ in valid_rows]
    now = datetime.now(timezone.utc)
    frame = pd.DataFrame(
        {
            "snapshot_at": now,
            "rank": [int(x["rk"]) for x in rows[:limit]],
            "symbol": [x["sc"][2:] for x in rows[:limit]],
            "market": [x["sc"][:2] for x in rows[:limit]],
            "rank_change": [int(x.get("rc") or 0) for x in rows[:limit]],
            "source": "eastmoney_hot_rank",
        }
    )
    secids = ",".join(_secid(symbol) for symbol in frame["symbol"])
    quote_params = {
        "ut": "f057cbcbce2a86e2866ab8877db1d059", "fltt": "2", "invt": "2",
        "fields": "f14,f3,f12,f2", "secids": secids,
    }
    try:
        quotes = _json("https://push2.eastmoney.com/api/qt/ulist.np/get?" + urllib.parse.urlencode(quote_params), retries=2)["data"]["diff"]
        quote_frame = pd.DataFrame({
            "symbol": [x["f12"] for x in quotes], "name": [x["f14"] for x in quotes],
            "latest_price": [x.get("f2") for x in quotes], "latest_pct_change": [x.get("f3") for x in quotes],
        })
        return frame.merge(quote_frame, on="symbol", how="left")
    except Exception:
        # Ranking remains authoritative; quote enrichment must not stop the pipeline.
        return frame


def _secid(symbol: str) -> str:
    return ("1." if symbol.startswith(("5", "6", "9")) else "0.") + symbol


def fetch_daily(symbol: str, start: str = SETTINGS.history_start) -> pd.DataFrame:
    suffix = ".SS" if symbol.startswith(("5", "6", "9")) else ".SZ"
    start_ts = int(pd.Timestamp(start).timestamp())
    params = {
        "period1": start_ts,
        "period2": int(time.time()) + 86400,
        "interval": "1d",
        "events": "history",
        "includeAdjustedClose": "true",
    }
    result = _json(YAHOO_URL.format(symbol + suffix) + "?" + urllib.parse.urlencode(params)).get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"{symbol} 无 Yahoo 历史行情")
    result = result[0]
    quote = result["indicators"]["quote"][0]
    adjusted = result["indicators"].get("adjclose", [{}])[0].get("adjclose", quote["close"])
    raw_close = np.asarray(quote["close"], dtype=float)
    factor = np.divide(np.asarray(adjusted, dtype=float), raw_close, out=np.ones_like(raw_close), where=raw_close != 0)
    frame = pd.DataFrame({
        "trade_date": pd.to_datetime(result["timestamp"], unit="s", utc=True).tz_convert("Asia/Shanghai").normalize().tz_localize(None),
        "open": np.asarray(quote["open"], dtype=float) * factor,
        "close": np.asarray(adjusted, dtype=float),
        "high": np.asarray(quote["high"], dtype=float) * factor,
        "low": np.asarray(quote["low"], dtype=float) * factor,
        "volume": quote["volume"],
    })
    frame.insert(0, "symbol", symbol)
    frame.insert(1, "name", result.get("meta", {}).get("longName") or result.get("meta", {}).get("shortName") or symbol)
    frame["amount"] = frame["close"] * frame["volume"]
    frame["amplitude"] = (frame["high"] - frame["low"]) / frame["close"].shift(1) * 100
    frame["pct_change"] = frame["close"].pct_change() * 100
    frame["change"] = frame["close"].diff()
    frame["turnover"] = np.nan
    frame["source"] = "yahoo_adjusted_daily"
    return frame.dropna(subset=["open", "close", "high", "low", "volume"]).sort_values("trade_date")


def fetch_universe_history(universe: pd.DataFrame, start: str = SETTINGS.history_start) -> tuple[pd.DataFrame, dict[str, str]]:
    frames: list[pd.DataFrame] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=SETTINGS.workers) as pool:
        jobs = {pool.submit(fetch_daily, symbol, start): symbol for symbol in universe["symbol"]}
        for job in as_completed(jobs):
            symbol = jobs[job]
            try:
                frames.append(job.result())
            except Exception as exc:
                errors[symbol] = str(exc)
    if not frames:
        raise RuntimeError(f"全部历史行情下载失败: {errors}")
    return pd.concat(frames, ignore_index=True), errors
