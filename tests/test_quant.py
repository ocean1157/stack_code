import unittest

import numpy as np
import pandas as pd

from quant_system.backtest import run_backtest
from quant_system.features import FEATURES, build_features
from quant_system.model import fit_logistic
from quant_system.monitor import is_trading_day, is_trading_session
from quant_system.research import analyze_report
from quant_system.stock_analysis import analyze_bars, ohlc_median
from datetime import datetime
from zoneinfo import ZoneInfo


class QuantTests(unittest.TestCase):
    def test_exchange_holiday_is_not_trading_day(self):
        self.assertFalse(is_trading_day(pd.Timestamp("2026-10-02").date()))
        self.assertTrue(is_trading_day(pd.Timestamp("2026-07-16").date()))

    def test_monitor_window_starts_one_hour_before_open(self):
        tz = ZoneInfo("Asia/Shanghai")
        self.assertTrue(is_trading_session(datetime(2026, 7, 16, 8, 30, tzinfo=tz)))
        self.assertFalse(is_trading_session(datetime(2026, 7, 16, 15, 1, tzinfo=tz)))

    def test_research_analysis_is_bounded_and_auditable(self):
        result = analyze_report({"title": "半导体需求回暖，国产替代加速", "sRatingName": "买入", "industryName": "半导体", "orgSName": "测试券商"})
        self.assertGreaterEqual(result["score"], -1.0)
        self.assertLessEqual(result["score"], 1.0)
        self.assertEqual(result["stance"], "偏多")
        self.assertIn("半导体", result["themes"])

    def _bars(self, n=180):
        dates = pd.bdate_range("2024-01-01", periods=n)
        close = 10 * np.cumprod(1 + 0.001 + 0.01 * np.sin(np.arange(n) / 7))
        return pd.DataFrame({
            "symbol": "000001", "name": "测试", "trade_date": dates,
            "open": close * 0.999, "close": close, "high": close * 1.01,
            "low": close * 0.99, "volume": 1e6 + np.arange(n) * 100,
            "amount": 1e7, "amplitude": 2.0, "pct_change": 0.1,
            "change": 0.01, "turnover": 1.5, "source": "test",
        })

    def test_target_uses_next_open_to_close(self):
        featured = build_features(self._bars(), 15)
        row = featured.dropna(subset=FEATURES + ["forward_return"]).iloc[-1]
        bars = self._bars()
        position = bars.index[bars["trade_date"] == row["trade_date"]][0]
        expected = bars.loc[position + 1, "close"] / bars.loc[position + 1, "open"] - 1
        self.assertAlmostEqual(row["forward_return"], expected)

    def test_model_probabilities_are_bounded(self):
        data = build_features(self._bars(), 15).dropna(subset=FEATURES + ["target"])
        model = fit_logistic(data)
        probability = model.predict_proba(data)
        self.assertTrue(((probability >= 0) & (probability <= 1)).all())

    def test_cost_is_deducted(self):
        scored = pd.DataFrame({"trade_date": pd.to_datetime(["2025-01-01"]), "probability": [0.8], "forward_return": [0.01]})
        metrics, _ = run_backtest(scored, 0.6, 1, 20)
        self.assertAlmostEqual(metrics.total_return, 0.008)

    def test_stock_analysis_returns_auditable_decision(self):
        result = analyze_bars(self._bars(60))
        self.assertIn(result["action"], {"BUY", "HOLD", "SELL"})
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)
        self.assertEqual(result["sessions"], 30)
        self.assertTrue(result["reasons"] or result["risks"])

    def test_ohlc_median_uses_middle_two_prices(self):
        self.assertAlmostEqual(ohlc_median(10, 14, 8, 12), 11)

    def test_stock_analysis_exposes_common_technical_indicators(self):
        indicators = analyze_bars(self._bars(180))["indicators"]
        expected = {"sma5", "sma10", "sma20", "sma60", "ema12", "ema26", "macd",
                    "macd_signal", "macd_histogram", "bollinger_upper", "bollinger_middle",
                    "bollinger_lower", "rsi14", "kdj_k", "kdj_d", "kdj_j", "atr14",
                    "atr14_pct", "annualized_volatility", "volume_ratio_5_20"}
        self.assertEqual(set(indicators), expected)

    def test_cn_trading_sessions(self):
        tz = ZoneInfo("Asia/Shanghai")
        self.assertTrue(is_trading_session(datetime(2026, 7, 16, 10, 0, tzinfo=tz)))
        self.assertTrue(is_trading_session(datetime(2026, 7, 16, 14, 30, tzinfo=tz)))
        self.assertTrue(is_trading_session(datetime(2026, 7, 16, 12, 0, tzinfo=tz)))
        self.assertFalse(is_trading_session(datetime(2026, 7, 18, 10, 0, tzinfo=tz)))


if __name__ == "__main__":
    unittest.main()
