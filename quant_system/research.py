from __future__ import annotations

import json
import subprocess
import time
import uuid
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from .config import DATA_DIR, SETTINGS
from .db import _run_sql, copy_frame, initialize
from .data import _json, _secid

API_URL = "https://reportapi.eastmoney.com/report/list"
STOCK_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
SOURCE = "eastmoney_research_center"
MODEL_VERSION = "rules-v1"
THEMES = {
    "人工智能": ("AI", "人工智能", "算力", "大模型", "智能体", "数据中心"),
    "半导体": ("半导体", "芯片", "晶圆", "存储", "光刻"),
    "新能源": ("新能源", "光伏", "风电", "储能", "锂电", "电池"),
    "医药": ("医药", "创新药", "医疗", "制药", "临床", "医保"),
    "消费": ("消费", "食品", "白酒", "零售", "旅游", "家电"),
    "金融地产": ("银行", "证券", "保险", "地产", "房地产"),
    "高端制造": ("机器人", "军工", "航空", "设备", "汽车", "机械"),
    "资源周期": ("有色", "煤炭", "钢铁", "化工", "原油", "黄金"),
    "政策宏观": ("政策", "财政", "货币", "利率", "经济数据", "央行"),
}
POSITIVE = ("增长", "回暖", "改善", "景气", "向好", "突破", "加速", "上行", "机遇", "受益", "看好", "超预期", "拐点")
NEGATIVE = ("下滑", "承压", "风险", "放缓", "下降", "低迷", "恶化", "不及预期", "调整", "压力", "亏损")
CATALYSTS = ("政策", "需求", "涨价", "订单", "创新", "出口", "降息", "并购", "国产替代", "业绩")
RISKS = ("风险", "承压", "下滑", "竞争", "波动", "库存", "监管", "不确定", "低于预期", "需求不足")
RATING_SCORE = {"强烈推荐": .8, "买入": .7, "推荐": .6, "增持": .4, "看好": .4, "持有": .1, "中性": 0., "减持": -.5, "卖出": -.8}


def _sql_text(value: object) -> str:
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def _ascii_error(value: object) -> str:
    return str(value).encode("ascii", "backslashreplace").decode("ascii")


def normalize_industry(value: object) -> str:
    text = str(value or "").strip()
    for suffix in ("行业", "Ⅱ", "II", "概念"):
        text = text.replace(suffix, "")
    return text.strip()


def rating_category(value: object) -> str:
    text = str(value or "")
    for key in ("卖出", "减持", "买入", "增持", "持有"):
        if key in text:
            return key
    if "中性" in text:
        return "持有/中性"
    if "看好" in text or "推荐" in text:
        return "看好/推荐"
    return "未评级"


def fetch_stock_master() -> pd.DataFrame:
    hot_path = DATA_DIR / "hot_rank_latest.csv"
    if not hot_path.exists():
        raise RuntimeError("hot_rank_latest.csv is required before research enrichment")
    symbols = pd.read_csv(hot_path, dtype={"symbol": str})["symbol"].dropna().astype(str).tolist()
    params = {"fltt": 2, "secids": ",".join(_secid(x) for x in symbols), "fields": "f12,f14,f100"}
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get?" + urlencode(params)
    try:
        rows = ((_json(url, retries=5).get("data") or {}).get("diff") or [])
    except Exception:
        completed = subprocess.run(["curl.exe", "--silent", "--show-error", "--retry", "5", "-A", "Mozilla/5.0", url], check=True, capture_output=True)
        rows = ((json.loads(completed.stdout.decode("utf-8")).get("data") or {}).get("diff") or [])
    now = datetime.now().astimezone().isoformat()
    return pd.DataFrame([{"symbol": str(x.get("f12") or ""), "name_cn": x.get("f14") or "",
        "industry": x.get("f100"), "industry_key": normalize_industry(x.get("f100")),
        "updated_at": now, "source": "eastmoney_a_share_list"} for x in rows if len(str(x.get("f12") or "")) == 6 and x.get("f14")])


def fetch_reports(begin: date, end: date, page_size: int = 100, max_pages: int = 10) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        params = {"industryCode": "*", "pageSize": page_size, "industry": "*", "rating": "*",
                  "ratingChange": "*", "beginTime": begin.isoformat(), "endTime": end.isoformat(),
                  "pageNo": page, "fields": "", "qType": 1, "orgCode": "", "rcode": "",
                  "p": page, "pageNum": page, "pageNumber": page}
        request = Request(API_URL + "?" + urlencode(params), headers={"User-Agent": "Mozilla/5.0 QuantScope/1.0"})
        with urlopen(request, timeout=SETTINGS.request_timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        batch = payload.get("data") or []
        rows.extend(batch)
        if page >= int(payload.get("TotalPage") or 1) or len(batch) < page_size:
            break
    return rows


def analyze_report(row: dict) -> dict:
    title = str(row.get("title") or "")
    rating = str(row.get("sRatingName") or row.get("emRatingName") or "")
    score = next((v for k, v in RATING_SCORE.items() if k in rating), 0.0)
    score += .12 * sum(word in title for word in POSITIVE)
    score -= .12 * sum(word in title for word in NEGATIVE)
    score = max(-1., min(1., score))
    themes = [name for name, words in THEMES.items() if any(word.lower() in title.lower() for word in words)]
    catalysts, risks = [w for w in CATALYSTS if w in title], [w for w in RISKS if w in title]
    stance = "偏多" if score >= .3 else "偏空" if score <= -.2 else "中性"
    industry, broker = row.get("industryName") or "综合研究", row.get("orgSName") or row.get("orgName") or "机构"
    summary = f"{broker}对{industry}观点{stance}"
    if themes: summary += "，重点涉及" + "、".join(themes[:3])
    if catalysts: summary += "；潜在催化为" + "、".join(catalysts[:3])
    if risks: summary += "；需关注" + "、".join(risks[:3])
    return {"score": score, "stance": stance, "themes": themes, "catalysts": catalysts, "risks": risks, "summary": summary + "。"}


def _normalize(rows: list[dict], fetched_at: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    reports, analyses = [], []
    for row in rows:
        report_id = row.get("infoCode")
        if not report_id or not row.get("title"): continue
        a = analyze_report(row)
        reports.append({
            "report_id": report_id, "title": row["title"], "industry_code": row.get("industryCode"),
            "industry_name": row.get("industryName") or "综合研究", "industry_key": normalize_industry(row.get("industryName") or "综合研究"), "broker_code": row.get("orgCode"),
            "broker_name": row.get("orgSName") or row.get("orgName") or "未知机构",
            "rating": row.get("sRatingName") or row.get("emRatingName"), "rating_category": rating_category(row.get("sRatingName") or row.get("emRatingName")), "rating_change": row.get("ratingChange"),
            "researcher": row.get("researcher"), "publish_date": str(row.get("publishDate") or "")[:10],
            "source_url": f"https://data.eastmoney.com/report/zw_industry.jshtml?infocode={report_id}",
            "source": SOURCE, "fetched_at": fetched_at.isoformat(),
            "raw_metadata": json.dumps(row, ensure_ascii=False, separators=(",", ":"))})
        analyses.append({"report_id": report_id, "analyzed_at": fetched_at.isoformat(),
            "sentiment_score": a["score"], "stance": a["stance"],
            "themes": json.dumps(a["themes"], ensure_ascii=False), "catalysts": json.dumps(a["catalysts"], ensure_ascii=False),
            "risks": json.dumps(a["risks"], ensure_ascii=False), "summary": a["summary"], "model_version": MODEL_VERSION})
    return pd.DataFrame(reports), pd.DataFrame(analyses)


def _rebuild_digest(begin: date, end: date) -> None:
    _run_sql(f"""DELETE FROM industry_daily_digest WHERE digest_date BETWEEN {_sql_text(begin)} AND {_sql_text(end)};
    INSERT INTO industry_daily_digest
    SELECT r.publish_date,r.industry_name,count(*)::int,count(DISTINCT r.broker_name)::int,avg(a.sentiment_score),
      CASE WHEN avg(a.sentiment_score)>=.3 THEN U&'\\504F\\591A' WHEN avg(a.sentiment_score)<=-.2 THEN U&'\\504F\\7A7A' ELSE U&'\\4E2D\\6027' END,
      COALESCE((SELECT jsonb_agg(x.theme) FROM (SELECT DISTINCT jsonb_array_elements_text(a2.themes) theme
        FROM industry_research_reports r2 JOIN industry_research_analysis a2 USING(report_id)
        WHERE r2.publish_date=r.publish_date AND r2.industry_name=r.industry_name LIMIT 8) x),'[]'::jsonb),
      r.industry_name||': '||count(*)||' reports / '||count(DISTINCT r.broker_name)||' brokers',
      jsonb_agg(jsonb_build_object('report_id',r.report_id,'title',r.title,'broker',r.broker_name,'url',r.source_url)
        ORDER BY a.sentiment_score DESC),now()
    FROM industry_research_reports r JOIN industry_research_analysis a USING(report_id)
    WHERE r.publish_date BETWEEN {_sql_text(begin)} AND {_sql_text(end)} GROUP BY r.publish_date,r.industry_name;""")


def run_research(days: int = 3) -> dict:
    initialize()
    run_id, started = str(uuid.uuid4()), datetime.now().astimezone()
    begin, end = date.today() - timedelta(days=max(days - 1, 0)), date.today()
    _run_sql(f"INSERT INTO research_ingestion_runs(run_id,started_at,status,source) VALUES('{run_id}',now(),'running','{SOURCE}')")
    try:
        rows = fetch_reports(begin, end)
        stocks = fetch_stock_master()
        reports, analyses = _normalize(rows, started)
        copy_frame(stocks, "stock_master", list(stocks.columns), ["symbol"])
        copy_frame(reports, "industry_research_reports", list(reports.columns), ["report_id"])
        copy_frame(analyses, "industry_research_analysis", list(analyses.columns), ["report_id"])
        _rebuild_digest(begin, end)
        _run_sql(f"UPDATE research_ingestion_runs SET completed_at=now(),status='success',fetched_count={len(rows)},saved_count={len(reports)},analyzed_count={len(analyses)} WHERE run_id='{run_id}'")
        return {"run_id": run_id, "status": "success", "period": [begin.isoformat(), end.isoformat()], "fetched": len(rows), "saved": len(reports), "analyzed": len(analyses), "stock_master": len(stocks)}
    except Exception as exc:
        _run_sql(f"UPDATE research_ingestion_runs SET completed_at=now(),status='failed',error_message={_sql_text(_ascii_error(exc)[:2000])} WHERE run_id='{run_id}'")
        raise
