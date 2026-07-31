from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

import numpy as np
import pandas as pd

from .data import _json, _secid


@dataclass(frozen=True)
class StockDecision:
    action: str
    score: float
    confidence: float
    reasons: list[str]
    risks: list[str]


def _rsi(close: pd.Series, window: int = 14) -> float | None:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(window).mean()
    loss = (-delta.clip(upper=0)).rolling(window).mean()
    value = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    return None if value.empty or pd.isna(value.iloc[-1]) else float(value.iloc[-1])


def _max_drawdown(close: pd.Series) -> float:
    equity = close / close.iloc[0]
    return float((equity / equity.cummax() - 1).min())


def analyze_bars(bars: pd.DataFrame) -> dict:
    """Create a transparent 30-session decision score from daily OHLCV bars."""
    if bars.empty or len(bars) < 20:
        raise ValueError("至少需要 20 个交易日行情才能分析")
    frame = bars.sort_values("trade_date").tail(60).copy()
    close = pd.to_numeric(frame["close"], errors="coerce")
    volume = pd.to_numeric(frame.get("volume"), errors="coerce")
    last30 = frame.tail(30)
    close30 = close.tail(30)
    latest = float(close.iloc[-1])
    sma5 = float(close.tail(5).mean())
    sma10 = float(close.tail(10).mean())
    sma20 = float(close.tail(20).mean())
    sma60 = float(close.tail(60).mean()) if len(close) >= 60 else None
    ema12_series = close.ewm(span=12, adjust=False).mean()
    ema26_series = close.ewm(span=26, adjust=False).mean()
    macd_series = ema12_series - ema26_series
    macd_signal_series = macd_series.ewm(span=9, adjust=False).mean()
    macd = float(macd_series.iloc[-1])
    macd_signal = float(macd_signal_series.iloc[-1])
    std20 = float(close.tail(20).std(ddof=0))
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    rsv = (close-low.rolling(9).min())/(high.rolling(9).max()-low.rolling(9).min()).replace(0,np.nan)*100
    k_series = rsv.ewm(com=2,adjust=False).mean(); d_series = k_series.ewm(com=2,adjust=False).mean()
    kdj_k = None if pd.isna(k_series.iloc[-1]) else float(k_series.iloc[-1])
    kdj_d = None if pd.isna(d_series.iloc[-1]) else float(d_series.iloc[-1])
    previous_close = close.shift(1)
    true_range = pd.concat([(high-low),(high-previous_close).abs(),(low-previous_close).abs()],axis=1).max(axis=1)
    atr14 = float(true_range.rolling(14).mean().iloc[-1])
    ret30 = float(latest / close30.iloc[0] - 1)
    volatility = float(close.pct_change().tail(30).std() * np.sqrt(252))
    rsi14 = _rsi(close)
    drawdown = _max_drawdown(close30)
    recent_volume = float(volume.tail(5).mean()) if volume.notna().any() else 0.0
    base_volume = float(volume.tail(20).mean()) if volume.notna().any() else 0.0
    volume_ratio = recent_volume / base_volume if base_volume else None

    score = 50.0
    reasons: list[str] = []
    risks: list[str] = []
    if latest > sma20:
        score += 12
        reasons.append("收盘价位于20日均线上方")
    else:
        score -= 12
        risks.append("收盘价位于20日均线下方")
    if sma5 > sma10 > sma20:
        score += 12
        reasons.append("5/10/20日均线呈多头排列")
    elif sma5 < sma10 < sma20:
        score -= 12
        risks.append("5/10/20日均线呈空头排列")
    momentum_points = float(np.clip(ret30 * 100, -12, 12))
    score += momentum_points
    (reasons if ret30 >= 0 else risks).append(f"近30日收益 {ret30 * 100:.2f}%")
    if rsi14 is not None:
        if 45 <= rsi14 <= 70:
            score += 6
            reasons.append(f"RSI(14) {rsi14:.1f}，动能处于健康区间")
        elif rsi14 > 75:
            score -= 5
            risks.append(f"RSI(14) {rsi14:.1f}，存在短期过热风险")
        elif rsi14 < 35:
            score -= 5
            risks.append(f"RSI(14) {rsi14:.1f}，弱势尚未确认反转")
    if volatility > 0.45:
        score -= 6
        risks.append(f"年化波动率 {volatility * 100:.1f}% 偏高")
    if drawdown < -0.12:
        score -= 6
        risks.append(f"近30日最大回撤 {drawdown * 100:.1f}%")
    if volume_ratio is not None and volume_ratio > 1.25 and ret30 > 0:
        score += 4
        reasons.append(f"近5日成交量为20日均量的 {volume_ratio:.2f} 倍")

    score = float(np.clip(score, 0, 100))
    action = "BUY" if score >= 62 else "SELL" if score <= 38 else "HOLD"
    confidence = float(min(abs(score - 50) / 35, 1.0))
    decision = StockDecision(action, score, confidence, reasons[:5], risks[:5])
    return {
        "as_of": str(pd.to_datetime(frame["trade_date"].iloc[-1]).date()),
        "window_start": str(pd.to_datetime(last30["trade_date"].iloc[0]).date()),
        "sessions": int(len(last30)),
        "latest_close": latest,
        "return_30d": ret30,
        "sma5": sma5,
        "sma10": sma10,
        "sma20": sma20,
        "rsi14": rsi14,
        "annualized_volatility": volatility,
        "max_drawdown_30d": drawdown,
        "volume_ratio_5_20": volume_ratio,
        "indicators": {
            "sma5": sma5, "sma10": sma10, "sma20": sma20, "sma60": sma60,
            "ema12": float(ema12_series.iloc[-1]), "ema26": float(ema26_series.iloc[-1]),
            "macd": macd, "macd_signal": macd_signal, "macd_histogram": macd-macd_signal,
            "bollinger_upper": sma20+2*std20, "bollinger_middle": sma20, "bollinger_lower": sma20-2*std20,
            "rsi14": rsi14, "kdj_k": kdj_k, "kdj_d": kdj_d,
            "kdj_j": None if kdj_k is None or kdj_d is None else 3*kdj_k-2*kdj_d,
            "atr14": atr14, "atr14_pct": atr14/latest if latest else None,
            "annualized_volatility": volatility, "volume_ratio_5_20": volume_ratio,
        },
        "action": decision.action,
        "score": decision.score,
        "confidence": decision.confidence,
        "reasons": decision.reasons,
        "risks": decision.risks,
        "method": "30个交易日价格动量、均线趋势、RSI、波动率、回撤与量能的透明加权评分",
        "disclaimer": "研究提示，不构成投资建议或自动交易指令。",
    }


def ohlc_median(open_: float, high: float, low: float, close: float) -> float:
    """Median of four OHLC observations (mean of the two middle values)."""
    values = sorted(float(x) for x in (open_, high, low, close))
    return (values[1] + values[2]) / 2


def fetch_intraday(symbol: str) -> dict:
    """Fetch up to five days of minute trends for an on-demand chart; never writes the DB."""
    params = {
        "secid": _secid(symbol),
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "ndays": 5,
        "iscr": 0,
    }
    url = "https://push2his.eastmoney.com/api/qt/stock/trends2/get?" + urlencode(params)
    payload = _json(url, retries=2).get("data") or {}
    points = []
    for row in payload.get("trends") or []:
        fields = row.split(",")
        if len(fields) < 3:
            continue
        try:
            points.append({"time": fields[0], "price": float(fields[2])})
        except (TypeError, ValueError):
            continue
    if not points:
        return {"points": [], "trade_date": None, "market_state": "NO_INTRADAY_DATA"}
    latest_day = points[-1]["time"][:10]
    latest = [x for x in points if x["time"].startswith(latest_day)]
    state = "REALTIME" if latest_day == date.today().isoformat() else "LAST_TRADING_DAY"
    return {"points": latest, "trade_date": latest_day, "market_state": state}
