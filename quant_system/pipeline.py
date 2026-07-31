from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .backtest import optimize, run_backtest
from .config import DATA_DIR, REPORT_DIR, SETTINGS
from .data import fetch_hot_rank, fetch_universe_history
from .db import _run_sql, copy_frame, initialize
from .features import FEATURES, build_features
from .model import fit_logistic


BAR_COLUMNS = ["symbol", "name", "trade_date", "open", "close", "high", "low", "volume", "amount", "amplitude", "pct_change", "change", "turnover", "source"]


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _date_splits(frame: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    dates = np.array(sorted(frame["trade_date"].dropna().unique()))
    if len(dates) < 300:
        raise RuntimeError("有效交易日不足 300，不能进行可靠的时间切分")
    return pd.Timestamp(dates[int(len(dates) * 0.70)]), pd.Timestamp(dates[int(len(dates) * 0.85)])


def _apply_execution_risk_gate(latest: pd.DataFrame) -> pd.DataFrame:
    """Keep the model view auditable, but do not turn a crash into an immediate BUY."""
    result = latest.copy()
    result["raw_action"] = result["action"]
    flags: list[str] = []
    all_flags: list[str] = []
    for _, row in result.iterrows():
        flags = []
        if row["raw_action"] == "BUY" and row["pct_change"] <= -9.5:
            flags.append("跌停或接近跌停")
        if row["raw_action"] == "BUY" and row["pct_change"] <= -5:
            flags.append("单日急跌超过5%")
        if row["raw_action"] == "BUY" and row["ret_1"] <= -0.04 and row["trend_20"] < -0.05:
            flags.append("短期趋势破位")
        if row["raw_action"] == "SELL" and row["pct_change"] >= 9.5:
            flags.append("涨停附近无法可靠卖出")
        all_flags.append(json.dumps(flags, ensure_ascii=False))
    result["risk_flags"] = all_flags
    blocked = (result["raw_action"] == "BUY") & result["risk_flags"].ne("[]")
    result.loc[blocked, "action"] = "HOLD"
    result.loc[blocked, "confidence"] *= 0.5
    return result


def evaluate_matured_signals() -> None:
    """Evaluate every frozen signal on its fifth following trading day."""
    cost = SETTINGS.transaction_cost_bps / 10_000
    _run_sql(
        "INSERT INTO signal_evaluations(run_id,symbol,signal_date,evaluation_date,signal_action,raw_action,signal_price,next_open,next_close,close_to_close_return,executable_return,direction_correct,executable_correct) "
        "SELECT s.run_id,s.symbol,s.price_date,n.trade_date,s.action,COALESCE(s.raw_action,s.action),s.signal_price,n.open,n.close,"
        "n.close/s.signal_price-1,n.close/n.open-1,"
        f"CASE WHEN s.action='BUY' THEN n.close/s.signal_price-1>{cost} WHEN s.action='SELL' THEN n.close/s.signal_price-1<{-cost} ELSE abs(n.close/s.signal_price-1)<=0.01 END,"
        f"CASE WHEN s.action='BUY' THEN n.close/n.open-1>{cost} WHEN s.action='SELL' THEN n.close/n.open-1<{-cost} ELSE abs(n.close/n.open-1)<=0.01 END "
        "FROM signals s JOIN LATERAL (SELECT trade_date,open,close FROM bars_daily b WHERE b.symbol=s.symbol AND b.trade_date>s.price_date ORDER BY trade_date OFFSET 4 LIMIT 1) n ON true "
        "WHERE s.signal_price IS NOT NULL ON CONFLICT (run_id,symbol) DO NOTHING"
    )
    _run_sql(
        "UPDATE signal_evaluations e SET "
        "signal_median_price=x.signal_median,evaluation_median_price=x.evaluation_median,"
        "median_return=x.evaluation_median/NULLIF(x.signal_median,0)-1,"
        f"median_correct=CASE WHEN e.signal_action='BUY' THEN x.evaluation_median/NULLIF(x.signal_median,0)-1>{cost} "
        f"WHEN e.signal_action='SELL' THEN x.evaluation_median/NULLIF(x.signal_median,0)-1<{-cost} "
        "ELSE abs(x.evaluation_median/NULLIF(x.signal_median,0)-1)<=0.01 END,"
        "evaluation_method='FIFTH_TRADING_DAY_OHLC_MEDIAN' "
        "FROM (SELECT e2.run_id,e2.symbol,"
        "(p.open+p.high+p.low+p.close-greatest(p.open,p.high,p.low,p.close)-least(p.open,p.high,p.low,p.close))/2 AS signal_median,"
        "(n.open+n.high+n.low+n.close-greatest(n.open,n.high,n.low,n.close)-least(n.open,n.high,n.low,n.close))/2 AS evaluation_median "
        "FROM signal_evaluations e2 JOIN bars_daily p ON p.symbol=e2.symbol AND p.trade_date=e2.signal_date "
        "JOIN bars_daily n ON n.symbol=e2.symbol AND n.trade_date=e2.evaluation_date) x "
        "WHERE e.run_id=x.run_id AND e.symbol=x.symbol"
    )


def run_pipeline(persist: bool = True, reuse_data: bool = False) -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)
    if reuse_data:
        universe = pd.read_csv(DATA_DIR / "hot_rank_latest.csv", dtype={"symbol": str}, parse_dates=["snapshot_at"])
        bars = pd.read_csv(DATA_DIR / "bars_daily_latest.csv.gz", dtype={"symbol": str}, parse_dates=["trade_date"])
        download_errors = {}
    else:
        universe = fetch_hot_rank(50)
        bars, download_errors = fetch_universe_history(universe)
        if "name" in universe.columns:
            bars["name"] = bars["symbol"].map(universe.set_index("symbol")["name"]).fillna(bars["name"])
        universe.to_csv(DATA_DIR / "hot_rank_latest.csv", index=False, encoding="utf-8-sig")
        bars.to_csv(DATA_DIR / "bars_daily_latest.csv.gz", index=False, compression="gzip")

    dataset = build_features(bars, SETTINGS.transaction_cost_bps)
    complete = dataset.dropna(subset=FEATURES + ["target", "forward_return"]).copy()
    train_end, validation_end = _date_splits(complete)
    train = complete[complete["trade_date"] <= train_end]
    validation = complete[(complete["trade_date"] > train_end) & (complete["trade_date"] <= validation_end)]
    test = complete[complete["trade_date"] > validation_end]
    model = fit_logistic(train, SETTINGS.random_seed)
    validation = validation.copy()
    test = test.copy()
    validation["probability"] = model.predict_proba(validation)
    test["probability"] = model.predict_proba(test)
    threshold, top_k, validation_metrics = optimize(validation, SETTINGS.transaction_cost_bps)
    test_metrics, equity = run_backtest(test, threshold, top_k, SETTINGS.transaction_cost_bps)
    test_accuracy = float(((test["probability"] >= 0.5).astype(float) == test["target"]).mean())
    baseline_accuracy = float(max(test["target"].mean(), 1 - test["target"].mean()))

    # Refit on every labelled observation only after all model choices are frozen.
    final_model = fit_logistic(complete, SETTINGS.random_seed)
    latest = dataset.sort_values("trade_date").dropna(subset=FEATURES).groupby("symbol", as_index=False).tail(1).copy()
    latest["probability"] = final_model.predict_proba(latest)
    rank_map = universe.set_index("symbol")["rank"]
    latest["hot_rank"] = latest["symbol"].map(rank_map).astype(int)
    # The daily decision set is frozen to the public popularity top 10. Manually
    # pinned stocks are handled separately by analysis_universe/stock_analysis.
    latest = latest[latest["hot_rank"] <= SETTINGS.top_k].copy()
    latest["action"] = np.select(
        [latest["probability"] >= threshold, latest["probability"] <= 1 - threshold],
        ["BUY", "SELL"], default="HOLD",
    )
    # Directional confidence is the probability assigned to the predicted side.
    latest["confidence"] = np.maximum(latest["probability"], 1 - latest["probability"])
    latest["signal_price"] = latest["close"]
    latest["signal_pct_change"] = latest["pct_change"]
    latest = _apply_execution_risk_gate(latest)
    latest = latest.sort_values(["action", "probability"], ascending=[True, False])
    if "name" in universe.columns:
        latest["name"] = latest["symbol"].map(universe.set_index("symbol")["name"]).fillna(latest["name"])
    latest[["trade_date", "symbol", "name", "hot_rank", "probability", "action", "confidence"]].to_csv(REPORT_DIR / "latest_signals.csv", index=False, encoding="utf-8-sig")
    equity.to_csv(REPORT_DIR / "test_equity.csv", index=False)

    run_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc)
    report = {
        "run_id": run_id,
        "created_at": created_at.isoformat(),
        "universe_size": int(len(universe)),
        "symbols_with_history": int(bars["symbol"].nunique()),
        "download_errors": download_errors,
        "history_rows": int(len(bars)),
        "train_end": str(train_end.date()),
        "validation_end": str(validation_end.date()),
        "test_start": str((validation_end + pd.Timedelta(days=1)).date()),
        "threshold": threshold,
        "top_k": top_k,
        "transaction_cost_bps": SETTINGS.transaction_cost_bps,
        "validation": asdict(validation_metrics),
        "test": {**asdict(test_metrics), "direction_accuracy": test_accuracy, "majority_baseline_accuracy": baseline_accuracy},
        "signal_counts": latest["action"].value_counts().to_dict(),
    }
    (REPORT_DIR / "latest_run.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if persist:
        initialize()
        universe_columns = ["snapshot_at", "rank", "symbol", "market", "rank_change", "source"]
        universe_columns += [c for c in ("name", "latest_price", "latest_pct_change") if c in universe.columns]
        copy_frame(universe, "universe_snapshots", universe_columns, conflict=["snapshot_at", "symbol"])
        copy_frame(bars, "bars_daily", BAR_COLUMNS, conflict=["symbol", "trade_date"])
        metrics_json = json.dumps(report, ensure_ascii=False).replace("'", "''")
        features_json = json.dumps(FEATURES).replace("'", "''")
        _run_sql(
            "INSERT INTO model_runs(run_id,created_at,train_end,validation_end,test_start,threshold,top_k,metrics,feature_names) VALUES ("
            f"'{run_id}', '{created_at.isoformat()}', '{train_end.date()}', '{validation_end.date()}', '{report['test_start']}', {threshold}, {top_k}, '{metrics_json}'::jsonb, '{features_json}'::jsonb)"
        )
        signal_rows = latest[["trade_date", "symbol", "name", "hot_rank", "probability", "action", "confidence", "signal_price", "signal_pct_change", "raw_action", "risk_flags"]].copy().rename(columns={"trade_date": "price_date"})
        signal_rows.insert(0, "signal_at", created_at)
        signal_rows.insert(0, "run_id", run_id)
        copy_frame(signal_rows, "signals", ["run_id", "signal_at", "price_date", "symbol", "name", "hot_rank", "probability", "action", "confidence", "signal_price", "signal_pct_change", "raw_action", "risk_flags"])
        equity_rows = equity[["trade_date", "strategy_return", "equity"]].copy()
        equity_rows.insert(0, "run_id", run_id)
        copy_frame(equity_rows, "backtest_equity", ["run_id", "trade_date", "strategy_return", "equity"])
        weight_rows = pd.DataFrame({
            "run_id": run_id,
            "feature_name": FEATURES,
            "coefficient": final_model.weights[:-1],
        })
        copy_frame(weight_rows, "model_feature_weights", ["run_id", "feature_name", "coefficient"])
        previous_run_sql = f"(SELECT run_id FROM model_runs WHERE run_id <> '{run_id}' ORDER BY created_at DESC LIMIT 1)"
        _run_sql(
            "INSERT INTO signal_changes(run_id,previous_run_id,changed_at,symbol,previous_action,current_action,previous_probability,current_probability,change_type) "
            f"SELECT '{run_id}', p.run_id, '{created_at.isoformat()}', COALESCE(c.symbol,p.symbol), p.action, c.action, p.probability, c.probability, "
            "CASE WHEN p.symbol IS NULL THEN 'ENTERED' WHEN c.symbol IS NULL THEN 'EXITED' ELSE 'ACTION_CHANGED' END "
            f"FROM (SELECT * FROM signals WHERE run_id='{run_id}') c FULL OUTER JOIN (SELECT * FROM signals WHERE run_id={previous_run_sql}) p USING(symbol) "
            "WHERE p.symbol IS NULL OR c.symbol IS NULL OR p.action IS DISTINCT FROM c.action ON CONFLICT (run_id,symbol) DO NOTHING"
        )
        evaluate_matured_signals()
        latest_dates = bars.groupby("symbol")["trade_date"].max()
        stale_count = int((latest_dates < bars["trade_date"].max()).sum())
        details = json.dumps({"download_errors": download_errors}, ensure_ascii=False).replace("'", "''")
        _run_sql(
            "INSERT INTO data_quality_snapshots(run_id,checked_at,symbol_count,bar_count,earliest_date,latest_date,stale_symbol_count,download_error_count,details) VALUES ("
            f"'{run_id}','{created_at.isoformat()}',{bars['symbol'].nunique()},{len(bars)},'{bars['trade_date'].min().date()}','{bars['trade_date'].max().date()}',{stale_count},{len(download_errors)},'{details}'::jsonb) "
            "ON CONFLICT (run_id) DO NOTHING"
        )
    return report
