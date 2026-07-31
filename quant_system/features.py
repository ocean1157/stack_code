from __future__ import annotations

import numpy as np
import pandas as pd


FEATURES = [
    "ret_1", "mom_5", "mom_20", "mom_60", "trend_20", "trend_60",
    "vol_20", "atr_14", "rsi_14", "range_1", "volume_z20", "liquidity_z20",
]


def _one(group: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    g = group.sort_values("trade_date").copy()
    close = g["close"]
    ret = close.pct_change()
    g["ret_1"] = ret
    for n in (5, 20, 60):
        g[f"mom_{n}"] = close.pct_change(n)
    g["trend_20"] = close / close.rolling(20).mean() - 1
    g["trend_60"] = close / close.rolling(60).mean() - 1
    g["vol_20"] = ret.rolling(20).std() * np.sqrt(252)
    previous = close.shift(1)
    true_range = pd.concat([(g["high"] - g["low"]), (g["high"] - previous).abs(), (g["low"] - previous).abs()], axis=1).max(axis=1)
    g["atr_14"] = true_range.rolling(14).mean() / close
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    g["rsi_14"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    g["range_1"] = (g["high"] - g["low"]) / previous
    log_volume = np.log1p(g["volume"])
    g["volume_z20"] = ((log_volume - log_volume.rolling(20).mean()) / log_volume.rolling(20).std().replace(0, np.nan)).fillna(0)
    log_amount = np.log1p(g["amount"])
    g["liquidity_z20"] = ((log_amount - log_amount.rolling(20).mean()) / log_amount.rolling(20).std().replace(0, np.nan)).fillna(0)
    # Signal is formed after close t and executed at next open; this is the realizable target.
    g["forward_return"] = g["close"].shift(-1) / g["open"].shift(-1) - 1
    g["target"] = (g["forward_return"] > cost_bps / 10_000).astype(float)
    g.loc[g["forward_return"].isna(), "target"] = np.nan
    return g


def build_features(bars: pd.DataFrame, cost_bps: float) -> pd.DataFrame:
    frames = []
    for symbol, group in bars.groupby("symbol", sort=False):
        result = _one(group.drop(columns=["symbol"]), cost_bps=cost_bps)
        result.insert(0, "symbol", symbol)
        frames.append(result)
    result = pd.concat(frames, ignore_index=True)
    return result.replace([np.inf, -np.inf], np.nan)
