from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import pandas as pd
import numpy as np

from .config import ROOT
from .db import _connection_args, _run_sql, initialize
from .data import fetch_daily
from .stock_analysis import analyze_bars, fetch_intraday


WEB_ROOT = ROOT / "web"
SYMBOL_RE = re.compile(r"^\d{6}$")
CHAT_MODEL = os.getenv("QUANT_CHAT_MODEL", "gpt-5.6")
AI_CHAT_ENABLED = os.getenv("QUANT_ENABLE_AI_CHAT", "0") == "1"


def query_rows(sql: str) -> list[dict]:
    wrapped = f"SELECT COALESCE(json_agg(row_to_json(q)),'[]'::json) FROM ({sql}) q"
    args, env = _connection_args()
    result = subprocess.run(args + ["-q", "-A", "-t", "-c", wrapped], check=True, env=env, capture_output=True, text=True, encoding="utf-8")
    return json.loads(result.stdout.strip() or "[]")


QUERIES = {
    "summary": """
      SELECT mr.run_id, to_char(mr.created_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS') AS created_at,
        mr.threshold, mr.top_k, (mr.metrics->>'transaction_cost_bps')::float AS transaction_cost_bps,
        (mr.metrics->'test'->>'total_return')::float AS total_return,
        (mr.metrics->'test'->>'annual_return')::float AS annual_return,
        (mr.metrics->'test'->>'sharpe')::float AS sharpe,
        (mr.metrics->'test'->>'max_drawdown')::float AS max_drawdown,
        (mr.metrics->'test'->>'direction_accuracy')::float AS direction_accuracy,
        (mr.metrics->'test'->>'majority_baseline_accuracy')::float AS baseline_accuracy,
        (SELECT count(*) FROM v_latest_signals WHERE action='BUY') AS buy_count,
        (SELECT count(*) FROM v_latest_signals WHERE action='HOLD') AS hold_count,
        (SELECT count(*) FROM v_latest_signals WHERE action='SELL') AS sell_count,
        (SELECT count(*) FROM bars_daily) AS bar_count,
        (SELECT count(DISTINCT symbol) FROM bars_daily) AS symbol_count,
        dq.latest_date, dq.stale_symbol_count, dq.download_error_count
      FROM v_latest_model_run mr LEFT JOIN data_quality_snapshots dq USING(run_id)
    """,
    "signals": """
      SELECT s.hot_rank, s.symbol,
        COALESCE(sno.name_cn,CASE WHEN octet_length(sm.name_cn)>length(sm.name_cn) THEN sm.name_cn END,CASE WHEN octet_length(bn.name)>length(bn.name) THEN bn.name END,s.name) AS name,
        sm.industry, s.action, s.probability, s.confidence, s.price_date,
        to_char(s.signal_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS') AS signal_at,
        COALESCE(s.latest_price,b.close) AS current_price,
        COALESCE(s.latest_pct_change,b.pct_change) AS pct_change, s.rank_change,
        c.previous_action, c.change_type, s.raw_action, s.risk_flags,
        COALESCE(s.signal_price,b.close) AS signal_price, COALESCE(s.signal_pct_change,b.pct_change) AS signal_pct_change
      FROM v_latest_signals s
      LEFT JOIN analysis_universe au ON au.symbol=s.symbol
      LEFT JOIN stock_master sm ON sm.symbol=s.symbol
      LEFT JOIN stock_name_overrides sno ON sno.symbol=s.symbol
      LEFT JOIN LATERAL (SELECT name FROM bars_daily WHERE symbol=s.symbol AND name IS NOT NULL ORDER BY trade_date DESC LIMIT 1) bn ON true
      LEFT JOIN LATERAL (SELECT close,pct_change FROM bars_daily WHERE symbol=s.symbol ORDER BY trade_date DESC LIMIT 1) b ON true
      LEFT JOIN signal_changes c ON c.run_id=s.run_id AND c.symbol=s.symbol
      WHERE COALESCE(au.included,true)
      ORDER BY CASE s.action WHEN 'BUY' THEN 1 WHEN 'HOLD' THEN 2 WHEN 'SELL' THEN 3 ELSE 4 END,s.probability DESC
    """,
    "analysis-universe": """
      SELECT au.symbol,au.included,au.purchase_price,au.pinned,au.source,
        COALESCE(sm.name_cn,b.name,au.symbol) AS name,
        latest.close AS current_price,
        CASE WHEN au.purchase_price>0 AND latest.close IS NOT NULL THEN latest.close/au.purchase_price-1 END AS holding_return,
        to_char(au.updated_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI') AS updated_at
      FROM analysis_universe au
      LEFT JOIN stock_master sm ON sm.symbol=au.symbol
      LEFT JOIN LATERAL (SELECT name FROM bars_daily WHERE symbol=au.symbol ORDER BY trade_date DESC LIMIT 1) b ON true
      LEFT JOIN LATERAL (SELECT close FROM bars_daily WHERE symbol=au.symbol ORDER BY trade_date DESC LIMIT 1) latest ON true
      WHERE au.included
      ORDER BY au.pinned DESC,au.updated_at DESC
    """,
    "equity": """
      SELECT trade_date,strategy_return,equity FROM backtest_equity
      WHERE run_id=(SELECT run_id FROM v_latest_model_run) ORDER BY trade_date
    """,
    "runs": """
      SELECT run_id,to_char(created_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI') AS created_at,
        threshold,top_k,(metrics->'test'->>'annual_return')::float AS annual_return,
        (metrics->'test'->>'sharpe')::float AS sharpe,
        (metrics->'test'->>'max_drawdown')::float AS max_drawdown,
        (metrics->'test'->>'direction_accuracy')::float AS accuracy
      FROM model_runs ORDER BY created_at DESC LIMIT 30
    """,
    "weights": """
      SELECT feature_name,coefficient FROM model_feature_weights
      WHERE run_id=(SELECT run_id FROM v_latest_model_run) ORDER BY abs(coefficient) DESC
    """,
    "quality": """
      SELECT dq.*,to_char(dq.checked_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS') AS checked_local,
        (SELECT min(trade_date) FROM bars_daily) AS database_earliest,
        (SELECT max(trade_date) FROM bars_daily) AS database_latest,
        (SELECT count(*) FROM universe_snapshots) AS universe_snapshot_rows,
        (SELECT count(*) FROM signals) AS signal_rows,
        (SELECT count(*) FROM model_runs) AS model_run_rows
      FROM data_quality_snapshots dq ORDER BY checked_at DESC LIMIT 1
    """,
    "changes": """
      SELECT to_char(changed_at AT TIME ZONE 'Asia/Shanghai','MM-DD HH24:MI') AS changed_at,
        c.symbol || ' ' || COALESCE(sm.name_cn,s.name,'') AS symbol,
        previous_action,current_action,previous_probability,current_probability,change_type
      FROM signal_changes c LEFT JOIN stock_master sm ON sm.symbol=c.symbol
      LEFT JOIN signals s ON s.run_id=c.run_id AND s.symbol=c.symbol
      ORDER BY changed_at DESC,id DESC LIMIT 50
    """,
    "evaluation-summary": """
      SELECT count(*) AS evaluated_count,
        round((100.0*avg(direction_correct::int))::numeric,1) AS direction_accuracy,
        round((100.0*avg(executable_correct::int))::numeric,1) AS executable_accuracy,
        round((100.0*avg(median_correct::int))::numeric,1) AS median_accuracy,
        max(evaluation_date) AS latest_evaluation_date
      FROM (SELECT DISTINCT ON(e.evaluation_date,e.symbol) e.* FROM signal_evaluations e
        JOIN signals s USING(run_id,symbol)
        WHERE e.evaluation_date=(SELECT max(evaluation_date) FROM signal_evaluations)
        ORDER BY e.evaluation_date,e.symbol,s.signal_at DESC) latest
    """,
    "evaluations-legacy": """
      SELECT e.signal_date,e.evaluation_date,to_char(e.signal_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS') AS signal_at,
        e.symbol,COALESCE(sm.name_cn,e.signal_name) AS name,e.signal_action,e.raw_action,e.confidence,
        e.signal_median_price,e.evaluation_median_price,e.median_return,e.median_correct,e.evaluation_method,
        e.close_to_close_return,e.executable_return,e.direction_correct,e.executable_correct,
        CASE WHEN e.median_correct THEN U&'\\5224\\65AD\\6B63\\786E'
          WHEN e.signal_action='BUY' AND e.median_return<0 THEN U&'\\8D8B\\52BF\\53CD\\8F6C\\FF1A\\590D\\6838\\77ED\\671F\\52A8\\91CF\\3001\\8D44\\91D1\\6D41\\4E0E\\98CE\\9669\\95E8\\63A7'
          WHEN e.signal_action='SELL' AND e.median_return>0 THEN U&'\\8D85\\8DCC\\53CD\\8F6C\\FF1A\\590D\\6838\\653E\\91CF\\7A81\\7834\\3001\\884C\\4E1A\\50AC\\5316\\4E0E\\60C5\\7EEA\\56E0\\5B50'
          ELSE U&'\\9700\\590D\\6838\\9608\\503C\\4E0E\\4EA4\\6613\\6210\\672C' END AS optimization_diagnostic
      FROM (SELECT DISTINCT ON(ev.evaluation_date,ev.symbol) ev.*,s.name AS signal_name,s.signal_at,s.confidence
        FROM signal_evaluations ev JOIN signals s USING(run_id,symbol)
        WHERE ev.evaluation_date=(SELECT max(evaluation_date) FROM signal_evaluations)
        ORDER BY ev.evaluation_date,ev.symbol,s.signal_at DESC) e
      LEFT JOIN stock_master sm ON sm.symbol=e.symbol
      ORDER BY e.median_correct,e.symbol LIMIT 300
    """,
    "evaluations": """
      WITH base AS (
      SELECT s.price_date AS signal_date,e.evaluation_date,
        to_char(s.signal_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS') AS signal_at,
        s.symbol,COALESCE(CASE WHEN octet_length(sm.name_cn)>length(sm.name_cn) THEN sm.name_cn END,CASE WHEN octet_length(bn.name)>length(bn.name) THEN bn.name END,s.name) AS name,s.action AS signal_action,
        CASE WHEN elapsed.trading_days=0 THEN NULL ELSE s.raw_action END AS raw_action,
        s.confidence,elapsed.trading_days AS trading_days_elapsed,
        CASE WHEN e.evaluation_date IS NOT NULL THEN s.action END AS execution_action,
        e.signal_median_price,e.evaluation_median_price,e.median_return,e.median_correct,e.evaluation_method,
        e.close_to_close_return,e.executable_return,
        CASE WHEN e.evaluation_date IS NOT NULL THEN e.direction_correct END AS direction_correct,
        CASE WHEN e.evaluation_date IS NOT NULL THEN e.executable_correct END AS executable_correct
      FROM signals s LEFT JOIN signal_evaluations e USING(run_id,symbol)
      LEFT JOIN stock_master sm ON sm.symbol=s.symbol
      LEFT JOIN LATERAL (SELECT name FROM bars_daily WHERE symbol=s.symbol AND name IS NOT NULL ORDER BY trade_date DESC LIMIT 1) bn ON true
      LEFT JOIN LATERAL (SELECT count(*)::int AS trading_days FROM bars_daily b
        WHERE b.symbol=s.symbol AND b.trade_date>s.price_date) elapsed ON true
      ), pending AS (
        SELECT * FROM base WHERE evaluation_date IS NULL ORDER BY signal_at DESC,symbol LIMIT 500
      ), mature AS (
        SELECT * FROM base WHERE evaluation_date IS NOT NULL ORDER BY signal_at DESC,symbol LIMIT 500
      )
      SELECT * FROM pending UNION ALL SELECT * FROM mature ORDER BY signal_at DESC,symbol
    """,
    "global-markets": """
      SELECT a.symbol,a.name,a.category,a.country,a.currency,s.price,s.previous_close,s.change_pct,s.market_state,
        to_char(s.observed_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI') AS observed_at
      FROM global_market_assets a LEFT JOIN LATERAL (
        SELECT * FROM global_market_snapshots x WHERE x.symbol=a.symbol ORDER BY observed_at DESC LIMIT 1
      ) s ON true WHERE s.price IS NOT NULL ORDER BY a.category,a.country,a.symbol
    """,
    "macro-series": """
      SELECT DISTINCT ON(series_code,country) series_code,country,name,category,observation_date,value,unit,source
      FROM macro_series ORDER BY series_code,country,observation_date DESC
    """,
    "cycles": """
      SELECT as_of,model_name,phase,score,confidence,evidence,guidance FROM cycle_assessments
      WHERE as_of=(SELECT max(as_of) FROM cycle_assessments) ORDER BY confidence DESC
    """,
    "trade-opportunities": """
      SELECT as_of,reporter_name,hs_code,hs_name,flow_code,latest_value_usd,yoy_growth,
        opportunity_score,beneficiary_industries,risk_note FROM trade_opportunities
      WHERE as_of=(SELECT max(as_of) FROM trade_opportunities) ORDER BY opportunity_score DESC
    """,
    "fund-flows": """
      SELECT fs.as_of,fs.symbol,COALESCE(sm.name_cn,fs.name) AS name,fs.follow_score,fs.reliability,
        fs.recommendation,fs.factors,s.action AS quant_action,s.raw_action,s.risk_flags
      FROM fund_flow_scores fs LEFT JOIN stock_master sm ON sm.symbol=fs.symbol
      LEFT JOIN v_latest_signals s ON s.symbol=fs.symbol
      WHERE fs.as_of=(SELECT max(as_of) FROM fund_flow_scores) ORDER BY fs.follow_score DESC
    """,
    "data-status": """
      SELECT
        (SELECT count(*) FROM global_market_snapshots) AS global_snapshot_rows,
        (SELECT max(observed_at) FROM global_market_snapshots) AS global_updated_at,
        (SELECT count(*) FROM cycle_assessments) AS cycle_rows,
        (SELECT max(as_of) FROM cycle_assessments) AS cycle_updated_at,
        (SELECT count(*) FROM trade_opportunities) AS trade_rows,
        (SELECT max(as_of) FROM trade_opportunities) AS trade_updated_at,
        (SELECT count(*) FROM fund_flow_scores) AS fund_flow_rows,
        (SELECT max(as_of) FROM fund_flow_scores) AS fund_flow_updated_at
    """,
    "research-summary": """
      SELECT max(publish_date) AS latest_date,count(*) AS report_count,count(DISTINCT broker_name) AS broker_count,
        count(DISTINCT industry_name) AS industry_count,
        round((100.0*count(*) FILTER (WHERE a.stance=U&'\\504F\\591A')/NULLIF(count(*),0))::numeric,1) AS bullish_ratio,
        (SELECT to_char(completed_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI:SS') FROM research_ingestion_runs WHERE status='success' ORDER BY completed_at DESC LIMIT 1) AS last_ingestion
      FROM industry_research_reports r JOIN industry_research_analysis a USING(report_id)
      WHERE publish_date=(SELECT max(publish_date) FROM industry_research_reports)
    """,
    "research-digests": """
      SELECT digest_date,industry_name,report_count,broker_count,sentiment_score,stance,themes,summary
      FROM industry_daily_digest WHERE digest_date=(SELECT max(digest_date) FROM industry_daily_digest)
      ORDER BY sentiment_score DESC,report_count DESC
    """,
    "research-reports": """
      SELECT r.report_id,r.publish_date,r.title,r.industry_name,r.broker_name,r.rating,r.researcher,r.source_url,
        a.sentiment_score,a.stance,a.themes,a.catalysts,a.risks,a.summary
      FROM industry_research_reports r JOIN industry_research_analysis a USING(report_id)
      ORDER BY r.publish_date DESC,r.fetched_at DESC LIMIT 300
    """,
    "research-ratings": """
      WITH categories(sort_order,rating) AS (VALUES
        (1,U&'\\4E70\\5165'),(2,U&'\\589E\\6301'),(3,U&'\\6301\\6709'),
        (4,U&'\\6301\\6709/\\4E2D\\6027'),(5,U&'\\770B\\597D/\\63A8\\8350'),
        (6,U&'\\51CF\\6301'),(7,U&'\\5356\\51FA'),(8,U&'\\672A\\8BC4\\7EA7')
      ), counts AS (
        SELECT COALESCE(rating_category,rating,U&'\\672A\\8BC4\\7EA7') AS rating,count(*) AS report_count
        FROM industry_research_reports WHERE publish_date=(SELECT max(publish_date) FROM industry_research_reports)
        GROUP BY COALESCE(rating_category,rating,U&'\\672A\\8BC4\\7EA7')
      ) SELECT c.rating,COALESCE(x.report_count,0) AS report_count
      FROM categories c LEFT JOIN counts x USING(rating) ORDER BY c.sort_order
    """,
    "research-industries": """
      SELECT industry_name,count(*) AS report_count,count(DISTINCT broker_name) AS broker_count,
        avg(a.sentiment_score) AS sentiment_score,
        count(*) FILTER (WHERE a.sentiment_score>=.3) AS bullish_count,
        count(*) FILTER (WHERE a.sentiment_score<=-.2) AS bearish_count
      FROM industry_research_reports r JOIN industry_research_analysis a USING(report_id)
      WHERE publish_date=(SELECT max(publish_date) FROM industry_research_reports)
      GROUP BY industry_name ORDER BY sentiment_score DESC,report_count DESC LIMIT 20
    """,
    "research-stocks": """
      SELECT s.symbol,COALESCE(sm.name_cn,s.name) AS name,sm.industry,s.action AS quant_action,s.probability,
        EXISTS(SELECT 1 FROM analysis_universe au WHERE au.symbol=s.symbol AND au.included) AS in_analysis_list,
        count(r.report_id) AS related_reports,count(DISTINCT r.broker_name) AS broker_count,
        avg(a.sentiment_score) AS research_score,
        CASE WHEN avg(a.sentiment_score)>=.3 THEN U&'\\504F\\591A' WHEN avg(a.sentiment_score)<=-.2 THEN U&'\\504F\\7A7A' ELSE U&'\\4E2D\\6027' END AS research_stance,
        string_agg(DISTINCT COALESCE(r.rating_category,r.rating),', ') AS ratings,
        json_agg(DISTINCT jsonb_build_object('report_id',r.report_id,'title',r.title,'source_url',r.source_url,'broker',r.broker_name)) AS reports
      FROM v_latest_signals s JOIN stock_master sm ON sm.symbol=s.symbol
      JOIN industry_research_reports r ON r.industry_key=sm.industry_key AND r.publish_date=(SELECT max(publish_date) FROM industry_research_reports)
      JOIN industry_research_analysis a ON a.report_id=r.report_id
      GROUP BY s.symbol,sm.name_cn,s.name,sm.industry,s.action,s.probability
      ORDER BY research_score DESC,related_reports DESC
    """,
    "news-impact": """
      WITH news AS (
        SELECT r.industry_key,max(r.publish_date) AS news_date,count(*) AS news_count,
          avg(a.sentiment_score) AS sentiment_score
        FROM industry_research_reports r JOIN industry_research_analysis a USING(report_id)
        WHERE r.publish_date >= (SELECT max(publish_date)-6 FROM industry_research_reports)
        GROUP BY r.industry_key
      )
      SELECT s.symbol,COALESCE(sm.name_cn,s.name) AS name,sm.industry,n.news_date,n.news_count,
        n.sentiment_score,s.confidence,s.action,
        CASE WHEN p0.close IS NOT NULL AND p5.close IS NOT NULL THEN p5.close/p0.close-1 END AS five_day_return,
        n.sentiment_score*ln(1+n.news_count)*s.confidence AS impact_score
      FROM v_latest_signals s JOIN stock_master sm ON sm.symbol=s.symbol
      JOIN news n ON n.industry_key=sm.industry_key
      LEFT JOIN LATERAL (SELECT close FROM bars_daily b WHERE b.symbol=s.symbol AND b.trade_date>=n.news_date ORDER BY trade_date LIMIT 1) p0 ON true
      LEFT JOIN LATERAL (SELECT close FROM bars_daily b WHERE b.symbol=s.symbol AND b.trade_date>n.news_date ORDER BY trade_date OFFSET 4 LIMIT 1) p5 ON true
      ORDER BY abs(n.sentiment_score*ln(1+n.news_count)*s.confidence) DESC LIMIT 80
    """,
}


def stock_analysis(symbol: str) -> dict:
    rows = query_rows(
        "SELECT trade_date,open,high,low,close,volume,amount,pct_change FROM bars_daily "
        f"WHERE symbol='{symbol}' ORDER BY trade_date DESC LIMIT 90"
    )
    bars = pd.DataFrame(list(reversed(rows)))
    source = "database"
    if len(bars) < 30:
        start = (date.today() - timedelta(days=180)).strftime("%Y%m%d")
        bars = fetch_daily(symbol, start=start)
        source = "on_demand_market_data"
    result = analyze_bars(bars)
    profile = query_rows(
        "SELECT COALESCE(sm.symbol,sno.symbol) AS symbol,COALESCE(sno.name_cn,sm.name_cn) AS name,sm.industry "
        f"FROM stock_master sm FULL JOIN stock_name_overrides sno USING(symbol) WHERE COALESCE(sm.symbol,sno.symbol)='{symbol}' LIMIT 1"
    )
    latest_signal = query_rows(
        "SELECT action,probability,confidence,price_date,risk_flags FROM v_latest_signals "
        f"WHERE symbol='{symbol}' LIMIT 1"
    )
    result.update({
        "symbol": symbol,
        "name": (profile[0].get("name") if profile else None) or (bars.iloc[-1].get("name") if "name" in bars else symbol),
        "industry": profile[0].get("industry") if profile else None,
        "data_source": source,
        "model_signal": latest_signal[0] if latest_signal else None,
        "bars": bars.tail(30)[["trade_date", "open", "high", "low", "close", "volume"]].assign(
            trade_date=lambda x: pd.to_datetime(x["trade_date"]).dt.strftime("%Y-%m-%d")
        ).to_dict("records"),
    })
    included = query_rows(
        "SELECT included,purchase_price,pinned FROM analysis_universe "
        f"WHERE symbol='{symbol}' LIMIT 1"
    )
    if included and included[0].get("included") and included[0].get("purchase_price"):
        purchase_price = float(included[0]["purchase_price"])
        current_price = float(result["latest_close"])
        holding_return = current_price / purchase_price - 1
        technical_score = float(result["score"])
        position_adjustment = 0.0
        position_reasons: list[str] = []
        position_risks: list[str] = []
        if holding_return >= 0 and current_price >= float(result["sma20"]):
            position_adjustment += 4
            position_reasons.append("当前价格高于购入价且站上20日均线")
        if holding_return <= -0.08 and current_price < float(result["sma20"]):
            position_adjustment -= 10
            position_risks.append("相对购入价回撤超过8%，且价格位于20日均线下方")
        if holding_return >= 0.15 and (result.get("rsi14") or 0) >= 70:
            position_adjustment -= 6
            position_risks.append("持仓浮盈超过15%，同时RSI进入偏热区间")
        position_score = float(np.clip(technical_score + position_adjustment, 0, 100))
        position_action = "BUY" if position_score >= 62 else "SELL" if position_score <= 38 else "HOLD"
        result["position_analysis"] = {
            "purchase_price": purchase_price,
            "current_price": current_price,
            "holding_return": holding_return,
            "break_even_change": purchase_price / current_price - 1,
            "technical_score": technical_score,
            "position_adjustment": position_adjustment,
            "position_score": position_score,
            "action": position_action,
            "reasons": position_reasons,
            "risks": position_risks,
            "basis": "以购入价计算持仓收益，并结合20日均线、RSI和原始技术评分形成持仓视角信号",
        }
        result["action"] = position_action
        result["score"] = position_score
    try:
        result["intraday"] = fetch_intraday(symbol)
    except Exception as exc:
        result["intraday"] = {"points": [], "trade_date": None, "market_state": "UNAVAILABLE", "detail": str(exc)[:200]}
    return result


def chat_context() -> dict:
    return {
        "model_run": query_rows(QUERIES["summary"])[0],
        "top_signals": query_rows(QUERIES["signals"] + " LIMIT 15"),
        "research_summary": query_rows(QUERIES["research-summary"])[0],
        "bullish_industries": query_rows(QUERIES["research-industries"])[:10],
        "related_hot_stocks": query_rows(QUERIES["research-stocks"] + " LIMIT 15"),
        "rating_distribution": query_rows(QUERIES["research-ratings"]),
    }


def ask_model(message: str, history: list[dict], page_context: dict) -> str:
    snapshot = chat_context()
    if not AI_CHAT_ENABLED:
        return local_data_answer(message, snapshot)
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    instructions = (
        "你是 Quant Scope 研究助手。只依据提供的数据库快照回答，使用简洁中文。"
        "必须区分量化信号、券商直接评级和行业研报映射；行业映射不能表述为券商直接推荐个股。"
        "回答应说明数据截至日期，指出不确定性，不承诺收益，不提供自动下单指令。"
        "若问题超出快照范围，明确说明当前数据不足。"
    )
    messages = [{"role": "developer", "content": instructions}]
    for item in history[-6:]:
        role = item.get("role")
        content = str(item.get("content") or "")[:2000]
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message + "\n\n当前页面上下文：" + json.dumps(page_context, ensure_ascii=False)[:3000] + "\n\n数据库快照：" + json.dumps(snapshot, ensure_ascii=False)[:14000]})
    payload = json.dumps({"model": CHAT_MODEL, "reasoning": {"effort": "low"}, "input": messages}, ensure_ascii=False).encode("utf-8")
    request = Request("https://api.openai.com/v1/responses", data=payload, method="POST", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))
    parts = [content.get("text", "") for item in result.get("output", []) if item.get("type") == "message" for content in item.get("content", []) if content.get("type") == "output_text"]
    answer = "\n".join(x for x in parts if x).strip()
    if not answer:
        raise RuntimeError("model returned no text")
    return answer


def local_data_answer(message: str, snapshot: dict) -> str:
    latest = snapshot["research_summary"].get("latest_date") or snapshot["model_run"].get("latest_date") or "未知"
    if any(word in message for word in ("行业", "板块", "偏多")):
        rows = snapshot["bullish_industries"][:5]
        detail = "；".join(f"{x['industry_name']}（评分{float(x['sentiment_score']):.2f}，{x['report_count']}篇）" for x in rows)
        return f"截至 {latest}，研报综合评分靠前的行业为：{detail}。这是行业研报聚合观点，不代表券商直接推荐其中每只股票。"
    if any(word in message for word in ("股票", "个股", "标的")):
        rows = snapshot["related_hot_stocks"][:8]
        detail = "；".join(f"{x['symbol']} {x['name']}（{x['industry']}，研报映射{x['research_stance']}，量化{x['quant_action']}）" for x in rows)
        return f"截至 {latest}，当前热榜中研报行业映射较强的股票包括：{detail}。其中研报观点属于行业映射，不是券商对个股的直接评级。"
    if any(word in message for word in ("评级", "买入", "增持", "持有", "卖出")):
        rows = snapshot["rating_distribution"]
        return f"截至 {latest}，最新评级分布为：" + "；".join(f"{x['rating']} {x['report_count']}篇" for x in rows) + "。"
    if any(word in message for word in ("回测", "风险", "回撤", "夏普", "年化")):
        m = snapshot["model_run"]
        return f"当前样本外年化收益为 {float(m.get('annual_return') or 0)*100:.2f}%，Sharpe 为 {float(m.get('sharpe') or 0):.2f}，最大回撤为 {float(m.get('max_drawdown') or 0)*100:.2f}%，方向准确率为 {float(m.get('direction_accuracy') or 0)*100:.2f}%。历史回测不代表未来收益。"
    return "当前为本地数据问答模式。你可以询问：偏多行业、研报关联股票、买入/增持/持有/卖出评级分布，或回测风险指标。启用外部 AI 后可进行更自由的分析对话。"


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "QuantDashboard/1.0"

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._api(parsed.path[5:], parse_qs(parsed.query))
            return
        relative = parsed.path.lstrip("/") or "index.html"
        target = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not target.is_file():
            target = WEB_ROOT / "index.html"
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", (mimetypes.guess_type(target.name)[0] or "application/octet-stream") + ("; charset=utf-8" if target.suffix in {".html", ".css", ".js"} else ""))
        if target.suffix in {".html", ".css", ".js"}:
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/analysis-universe":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                symbol = str(payload.get("symbol") or "")
                if not SYMBOL_RE.fullmatch(symbol):
                    self._json({"error": "invalid symbol"}, 400)
                    return
                included = bool(payload.get("included"))
                purchase_price = payload.get("purchase_price")
                if included:
                    try:
                        purchase_price = float(purchase_price)
                    except (TypeError, ValueError):
                        self._json({"error": "purchase_price must be a positive number"}, 400)
                        return
                    if purchase_price <= 0:
                        self._json({"error": "purchase_price must be a positive number"}, 400)
                        return
                price_sql = str(purchase_price) if included else "NULL"
                _run_sql(
                    "INSERT INTO analysis_universe(symbol,included,purchase_price,pinned,source,updated_at) "
                    f"VALUES ('{symbol}',{'true' if included else 'false'},{price_sql},true,'manual',now()) "
                    "ON CONFLICT(symbol) DO UPDATE SET included=excluded.included,"
                    "purchase_price=CASE WHEN excluded.included THEN excluded.purchase_price ELSE analysis_universe.purchase_price END,"
                    "pinned=true,source='manual',updated_at=now()"
                )
                self._json({"symbol": symbol, "included": included, "purchase_price": purchase_price})
            except (json.JSONDecodeError, subprocess.CalledProcessError) as exc:
                self._json({"error": "unable to update analysis list", "detail": str(exc)[:300]}, 500)
            return
        if path != "/api/chat":
            self._json({"error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 50000:
                self._json({"error": "invalid request size"}, 400)
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            message = str(payload.get("message") or "").strip()
            if not message or len(message) > 2000:
                self._json({"error": "message must be 1-2000 characters"}, 400)
                return
            answer = ask_model(message, payload.get("history") or [], payload.get("context") or {})
            self._json({"answer": answer, "model": CHAT_MODEL})
        except HTTPError as exc:
            self._json({"error": "model request failed", "detail": exc.read().decode("utf-8", "replace")[:500]}, 502)
        except (URLError, RuntimeError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
            self._json({"error": "chat unavailable", "detail": str(exc)[:500]}, 503)

    def _api(self, name: str, params: dict[str, list[str]]) -> None:
        try:
            if name == "health":
                self._json({"status": "ok", "database": bool(query_rows("SELECT true AS connected")[0]["connected"])})
            elif name in QUERIES:
                self._json(query_rows(QUERIES[name]))
            elif name == "stock-analysis":
                symbol = (params.get("symbol") or [""])[0]
                if not SYMBOL_RE.fullmatch(symbol):
                    self._json({"error": "股票代码必须是6位数字"}, 400)
                    return
                self._json(stock_analysis(symbol))
            elif name == "stock-profile":
                symbol = (params.get("symbol") or [""])[0]
                if not SYMBOL_RE.fullmatch(symbol):
                    self._json({"error": "invalid symbol"}, 400)
                    return
                rows = query_rows(
                    "SELECT COALESCE(sm.symbol,'" + symbol + "') AS symbol,COALESCE(sno.name_cn,CASE WHEN octet_length(sm.name_cn)>length(sm.name_cn) THEN sm.name_cn END,"
                    "CASE WHEN b.name IS NOT NULL AND octet_length(b.name)>length(b.name) THEN b.name END,sm.name_cn,b.name,sm.symbol) AS name,sm.industry "
                    "FROM (SELECT 1) seed LEFT JOIN stock_master sm ON sm.symbol='" + symbol + "' LEFT JOIN stock_name_overrides sno ON sno.symbol='" + symbol + "' "
                    "LEFT JOIN LATERAL (SELECT name FROM bars_daily WHERE symbol='" + symbol + "' ORDER BY trade_date DESC LIMIT 1) b ON true"
                )
                self._json(rows[0] if rows else {"symbol": symbol, "name": symbol, "industry": None})
            elif name == "symbol":
                symbol = (params.get("symbol") or [""])[0]
                if not SYMBOL_RE.fullmatch(symbol):
                    self._json({"error": "invalid symbol"}, 400)
                    return
                bars = query_rows(f"SELECT trade_date,open,high,low,close,volume,pct_change FROM bars_daily WHERE symbol='{symbol}' ORDER BY trade_date DESC LIMIT 180")
                history = query_rows(f"SELECT to_char(signal_at AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD HH24:MI') AS signal_at,action,probability,confidence,hot_rank FROM signals WHERE symbol='{symbol}' ORDER BY signal_at DESC LIMIT 30")
                self._json({"bars": list(reversed(bars)), "signals": history})
            else:
                self._json({"error": "not found"}, 404)
        except ValueError as exc:
            self._json({"error": "analysis unavailable", "detail": str(exc)}, 422)
        except (subprocess.CalledProcessError, json.JSONDecodeError, IndexError) as exc:
            self._json({"error": "database query failed", "detail": str(exc)}, 500)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[web] {self.address_string()} {fmt % args}")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    initialize()
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Quant dashboard: http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Quant dashboard web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
