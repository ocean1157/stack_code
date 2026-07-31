from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BacktestMetrics:
    total_return: float
    annual_return: float
    annual_volatility: float
    sharpe: float
    max_drawdown: float
    hit_rate: float
    trades: int
    active_days: int


def run_backtest(scored: pd.DataFrame, threshold: float, top_k: int, cost_bps: float) -> tuple[BacktestMetrics, pd.DataFrame]:
    selected = scored[scored["probability"] >= threshold].copy()
    selected["rank"] = selected.groupby("trade_date")["probability"].rank(method="first", ascending=False)
    selected = selected[selected["rank"] <= top_k]
    selected["net_return"] = selected["forward_return"] - cost_bps / 10_000
    daily = selected.groupby("trade_date")["net_return"].mean().rename("strategy_return").to_frame()
    all_dates = pd.Index(sorted(scored["trade_date"].unique()), name="trade_date")
    daily = daily.reindex(all_dates, fill_value=0.0)
    daily["equity"] = (1 + daily["strategy_return"]).cumprod()
    drawdown = daily["equity"] / daily["equity"].cummax() - 1
    n = len(daily)
    total = daily["equity"].iloc[-1] - 1 if n else 0.0
    vol = daily["strategy_return"].std(ddof=1) * np.sqrt(252) if n > 1 else 0.0
    annual = (1 + total) ** (252 / max(n, 1)) - 1 if total > -1 else -1.0
    sharpe = daily["strategy_return"].mean() / daily["strategy_return"].std(ddof=1) * np.sqrt(252) if vol > 0 else 0.0
    metrics = BacktestMetrics(total, annual, vol, sharpe, float(drawdown.min()), float((selected["net_return"] > 0).mean()) if len(selected) else 0.0, len(selected), int((daily["strategy_return"] != 0).sum()))
    return metrics, daily.reset_index()


def optimize(validation: pd.DataFrame, cost_bps: float) -> tuple[float, int, BacktestMetrics]:
    candidates = []
    # Keep a genuine neutral zone; 0.50 would force every stock into BUY/SELL.
    for threshold in (0.52, 0.54, 0.56, 0.58, 0.60):
        for top_k in (3, 5, 10):
            metrics, _ = run_backtest(validation, threshold, top_k, cost_bps)
            score = metrics.sharpe + 0.5 * metrics.annual_return + 2.0 * metrics.max_drawdown
            candidates.append((score, threshold, top_k, metrics))
    _, threshold, top_k, metrics = max(candidates, key=lambda x: x[0])
    return threshold, top_k, metrics
