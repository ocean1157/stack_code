from __future__ import annotations

import json
import math
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from .config import DATA_DIR, SETTINGS
from .data import _json, _secid
from .db import copy_frame, initialize

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
NYFED = "https://markets.newyorkfed.org/api/rates/{}/{}/last/20.json"
WORLD_BANK = "https://api.worldbank.org/v2/country/{}/indicator/{}?format=json&per_page=10"
COMTRADE = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
SINA_QUOTES = "https://hq.sinajs.cn/list={}"
SINA_SYMBOLS = {
    "000001.SS":"s_sh000001","399001.SZ":"s_sz399001",
    "^GSPC":"gb_inx","^IXIC":"gb_ixic","^DJI":"gb_dji",
    "GC=F":"hf_GC","SI=F":"hf_SI","CL=F":"hf_CL","NG=F":"hf_NG",
    "ZC=F":"hf_C","ZW=F":"hf_W",
    "USDCNY=X":"fx_susdcny","EURUSD=X":"fx_seurusd","USDJPY=X":"fx_susdjpy",
    "GBPUSD=X":"fx_sgbpusd","USDHKD=X":"fx_susdhkd",
}

ASSETS = [
    ("000001.SS","上证指数","index","中国","CNY","Asia/Shanghai"),("399001.SZ","深证成指","index","中国","CNY","Asia/Shanghai"),
    ("^HSI","恒生指数","index","中国香港","HKD","Asia/Hong_Kong"),("^N225","日经225","index","日本","JPY","Asia/Tokyo"),
    ("^GSPC","标普500","index","美国","USD","America/New_York"),("^IXIC","纳斯达克综合","index","美国","USD","America/New_York"),
    ("^DJI","道琼斯工业","index","美国","USD","America/New_York"),("^FTSE","英国富时100","index","英国","GBP","Europe/London"),
    ("^GDAXI","德国DAX","index","德国","EUR","Europe/Berlin"),("^FCHI","法国CAC40","index","法国","EUR","Europe/Paris"),
    ("GC=F","黄金期货","commodity","全球","USD","America/New_York"),("SI=F","白银期货","commodity","全球","USD","America/New_York"),
    ("CL=F","WTI原油","commodity","全球","USD","America/New_York"),("BZ=F","布伦特原油","commodity","全球","USD","America/New_York"),
    ("HG=F","铜期货","commodity","全球","USD","America/New_York"),("NG=F","天然气期货","commodity","全球","USD","America/New_York"),
    ("ZC=F","玉米期货","commodity","全球","USD","America/Chicago"),("ZW=F","小麦期货","commodity","全球","USD","America/Chicago"),
    ("DX-Y.NYB","美元指数","fx_index","美国","USD","America/New_York"),("USDCNY=X","美元/人民币","fx","中国","CNY","Etc/UTC"),
    ("EURUSD=X","欧元/美元","fx","欧元区","USD","Etc/UTC"),("USDJPY=X","美元/日元","fx","日本","JPY","Etc/UTC"),
    ("GBPUSD=X","英镑/美元","fx","英国","USD","Etc/UTC"),("USDHKD=X","美元/港元","fx","中国香港","HKD","Etc/UTC"),
    ("^IRX","美国3月国债收益率","rate","美国","%","America/New_York"),("^TNX","美国10年国债收益率","rate","美国","%","America/New_York"),
]
HS = {"27":("能源矿物",["石油石化","煤炭"]),"30":("医药产品",["化学制药","中药","医疗器械"]),"39":("塑料及制品",["塑料制品","化学制品"]),
      "72":("钢铁",["钢铁"]),"84":("机械设备",["通用设备","专用设备","工程机械"]),"85":("电机电气设备",["电网设备","消费电子","电子元件"]),
      "87":("车辆及零部件",["汽车整车","汽车零部件"]),"90":("光学医疗仪器",["光学光电子","医疗器械","仪器仪表"]),"94":("家具",["家用轻工"])}
COUNTRIES = {"CHN":"中国","USA":"美国","DEU":"德国","JPN":"日本","KOR":"韩国","VNM":"越南","IND":"印度","BRA":"巴西","MEX":"墨西哥"}

def _curl_json(url: str, timeout: int = 35) -> dict | list:
    out = subprocess.run(["curl.exe","--silent","--show-error","--retry","1","--max-time",str(timeout),"-A","Mozilla/5.0 QuantScope/1.0",url],check=True,capture_output=True,timeout=timeout+8).stdout
    return json.loads(out.decode("utf-8"))

def _sina_market_snapshots(now: datetime) -> list[dict]:
    codes = ",".join(SINA_SYMBOLS.values())
    out = subprocess.run(
        ["curl.exe","--silent","--show-error","--retry","1","--max-time","20",
         "-H","Referer: https://finance.sina.com.cn",SINA_QUOTES.format(codes)],
        check=True,capture_output=True,timeout=28,
    ).stdout.decode("gb18030","replace")
    reverse = {code:symbol for symbol,code in SINA_SYMBOLS.items()}
    rows = []
    for code, payload in re.findall(r'var hq_str_([^=]+)="([^"]*)"', out):
        if code not in reverse or not payload:
            continue
        values = payload.split(",")
        try:
            if code.startswith("s_"):
                price, change, pct = float(values[1]), float(values[2]), float(values[3])
                previous = price - change
            elif code.startswith("gb_"):
                price, pct, change = float(values[1]), float(values[2]), float(values[4])
                previous = price - change
            elif code.startswith("hf_"):
                price, previous = float(values[0]), float(values[7])
                change = price - previous
                pct = (price / previous - 1) * 100 if previous else 0.0
            else:
                price, pct, change = float(values[1]), float(values[10]), float(values[11])
                previous = price - change
        except (IndexError, TypeError, ValueError):
            continue
        rows.append({
            "symbol":reverse[code],"observed_at":now.isoformat(),"price":price,
            "previous_close":previous,"change":change,"change_pct":pct,
            "market_state":"OPEN","source":"sina_market_fallback",
        })
    return rows

def _asset_frame() -> pd.DataFrame:
    now = datetime.now(timezone.utc).isoformat()
    return pd.DataFrame([{"symbol":s,"name":n,"category":c,"country":co,"currency":cu,"timezone":tz,"source":"yahoo_chart","updated_at":now} for s,n,c,co,cu,tz in ASSETS])

def _yahoo(symbol: str, range_: str, interval: str) -> dict:
    params = urlencode({"range":range_,"interval":interval,"includePrePost":"true","events":"div,splits"})
    return _curl_json(YAHOO.format(quote(symbol,safe=""))+"?"+params,12)["chart"]["result"][0]

def collect_markets() -> tuple[pd.DataFrame,pd.DataFrame]:
    now = datetime.now(timezone.utc)
    bars,snaps=[],[]
    def one(symbol: str):
        local_bars=[]
        try:
            daily=_yahoo(symbol,"2y","1d"); q=daily["indicators"]["quote"][0]
            for i,ts in enumerate(daily.get("timestamp") or []):
                close=q["close"][i]
                if close is None: continue
                local_bars.append({"symbol":symbol,"interval":"1d","bar_time":datetime.fromtimestamp(ts,timezone.utc).isoformat(),"open":q["open"][i],"high":q["high"][i],"low":q["low"][i],"close":close,"volume":q.get("volume",[None]*len(q["close"]))[i],"source":"yahoo_chart"})
            intra=_yahoo(symbol,"1d","5m"); iq=intra["indicators"]["quote"][0]; times=intra.get("timestamp") or []
            for i,ts in enumerate(times):
                close=iq["close"][i]
                if close is None: continue
                local_bars.append({"symbol":symbol,"interval":"5m","bar_time":datetime.fromtimestamp(ts,timezone.utc).isoformat(),"open":iq["open"][i],"high":iq["high"][i],"low":iq["low"][i],"close":close,"volume":iq.get("volume",[None]*len(iq["close"]))[i],"source":"yahoo_chart"})
            meta=intra["meta"]; price=float(meta.get("regularMarketPrice") or iq["close"][-1]); prev=float(meta.get("chartPreviousClose") or meta.get("previousClose") or price)
            last=datetime.fromtimestamp(times[-1],timezone.utc) if times else now; state="OPEN" if abs((now-last).total_seconds())<1800 else "CLOSED"
            snap={"symbol":symbol,"observed_at":now.isoformat(),"price":price,"previous_close":prev,"change":price-prev,"change_pct":((price/prev)-1)*100 if prev else None,"market_state":state,"source":"yahoo_chart"}
            return local_bars,snap
        except Exception:
            return [],None
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs=[pool.submit(one,s) for s,*_ in ASSETS]
        for job in as_completed(jobs):
            b,s=job.result();bars.extend(b)
            if s:snaps.append(s)
    existing = {row["symbol"] for row in snaps}
    try:
        snaps.extend(row for row in _sina_market_snapshots(now) if row["symbol"] not in existing)
    except Exception:
        pass
    return (pd.DataFrame(bars,columns=["symbol","interval","bar_time","open","high","low","close","volume","source"]),
            pd.DataFrame(snaps,columns=["symbol","observed_at","price","previous_close","change","change_pct","market_state","source"]))

def collect_rates() -> pd.DataFrame:
    rows=[]
    for path,code,name in (("unsecured","effr","EFFR"),("secured","sofr","SOFR")):
        payload=_curl_json(NYFED.format(path,code))
        for x in payload.get("refRates",[]): rows.append({"series_code":name,"country":"美国","name":name,"category":"policy_rate","observation_date":x["effectiveDate"],"value":x["percentRate"],"unit":"%","source":"new_york_fed"})
    return pd.DataFrame(rows)

def collect_world_trade() -> pd.DataFrame:
    rows=[]
    jobspec=[(iso,country,code,name) for iso,country in COUNTRIES.items() for code,name in (("NE.EXP.GNFS.CD","商品与服务出口"),("NE.IMP.GNFS.CD","商品与服务进口"),("NE.TRD.GNFS.ZS","贸易占GDP比重"))]
    def one(spec):
        iso,country,code,name=spec;payload=_curl_json(WORLD_BANK.format(iso,code));return [{"series_code":code,"country":country,"name":name,"category":"trade","observation_date":f"{x['date']}-12-31","value":x["value"],"unit":"%" if code.endswith("ZS") else "USD","source":"world_bank"} for x in (payload[1] if isinstance(payload,list) and len(payload)>1 else []) if x.get("value") is not None]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures=[pool.submit(one,x) for x in jobspec]
        for f in as_completed(futures):
            try:rows.extend(f.result())
            except Exception:pass
    return pd.DataFrame(rows)

def collect_comtrade() -> pd.DataFrame:
    rows=[]
    specs=[(year,flow,hs,name) for year in (2023,2024) for flow in ("X","M") for hs,(name,_) in HS.items()]
    def one(spec):
        year,flow,hs,name=spec;params=urlencode({"flowCode":flow,"reporterCode":156,"period":year,"partnerCode":0,"cmdCode":hs,"maxRecords":50});data=_curl_json(COMTRADE+"?"+params).get("data") or []
        return {"period":year,"reporter_code":"156","reporter_name":"中国","flow_code":flow,"hs_code":hs,"hs_name":name,"value_usd":float(data[0].get("primaryValue") or 0),"source":"un_comtrade_preview"} if data else None
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures=[pool.submit(one,x) for x in specs]
        for f in as_completed(futures):
            try:
                x=f.result()
                if x:rows.append(x)
            except Exception:pass
    return pd.DataFrame(rows)

def score_trade(flows: pd.DataFrame) -> pd.DataFrame:
    if flows.empty:return pd.DataFrame()
    current=flows[flows.period==flows.period.max()].copy(); prev=flows[flows.period==flows.period.max()-1][["flow_code","hs_code","value_usd"]].rename(columns={"value_usd":"previous"})
    current=current.merge(prev,on=["flow_code","hs_code"],how="left"); current["yoy_growth"]=current["value_usd"]/current["previous"]-1
    growth_rank=current["yoy_growth"].rank(pct=True).fillna(.5); scale_rank=np.log1p(current["value_usd"]).rank(pct=True); current["opportunity_score"]=(growth_rank*.6+scale_rank*.4)*100
    now=date.today(); out=[]
    for _,x in current.iterrows(): out.append({"as_of":now,"reporter_name":"中国","hs_code":x.hs_code,"hs_name":x.hs_name,"flow_code":x.flow_code,"latest_value_usd":x.value_usd,"yoy_growth":x.yoy_growth,"opportunity_score":x.opportunity_score,"beneficiary_industries":json.dumps(HS[x.hs_code][1],ensure_ascii=False),"risk_note":"分类贸易增长仅用于行业线索，需结合利润率、汇率和公司海外收入验证。"})
    return pd.DataFrame(out)

def collect_fund_flows() -> tuple[pd.DataFrame,pd.DataFrame]:
    hot=pd.read_csv(DATA_DIR/"hot_rank_latest.csv",dtype={"symbol":str}); symbols=hot.symbol.tolist()
    fields="f12,f14,f2,f3,f6,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"
    def fetch_chunk(chunk):
        secids=",".join(_secid(x) for x in chunk); url="https://push2.eastmoney.com/api/qt/ulist.np/get?"+urlencode({"fltt":2,"secids":secids,"fields":fields})
        try:return ((_curl_json(url,15).get("data") or {}).get("diff") or [])
        except Exception:return []
    rows=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(fetch_chunk,[symbols[i:i+10] for i in range(0,len(symbols),10)]):rows.extend(result)
    now=datetime.now(timezone.utc); mapping={"f2":"price","f3":"pct_change","f6":"amount","f62":"main_net","f184":"main_ratio","f66":"super_net","f69":"super_ratio","f72":"large_net","f75":"large_ratio","f78":"medium_net","f81":"medium_ratio","f84":"small_net","f87":"small_ratio"}
    frame=pd.DataFrame([{**{"observed_at":now.isoformat(),"symbol":str(x.get("f12")),"name":x.get("f14") or str(x.get("f12")),"source":"eastmoney_fund_flow"},**{v:x.get(k) for k,v in mapping.items()}} for x in rows])
    if frame.empty:return frame,pd.DataFrame()
    for c in mapping.values(): frame[c]=pd.to_numeric(frame[c],errors="coerce")
    p=lambda col:frame[col].rank(pct=True).fillna(.5)
    confirmation=((np.sign(frame["main_ratio"].fillna(0))==np.sign(frame["pct_change"].fillna(0))).astype(float))
    frame["follow_score"]=(.45*p("main_ratio")+.2*p("super_ratio")+.15*p("large_ratio")+.1*p("amount")+.1*confirmation)*100
    completeness=frame[["main_ratio","super_ratio","large_ratio","amount","pct_change"]].notna().mean(axis=1); reliability=.6+.4*completeness
    rec=np.where((frame.follow_score>=70)&(reliability>=.6),"可跟买",np.where((frame.follow_score<=30)&(reliability>=.6),"可跟卖","观察"))
    scores=pd.DataFrame({"as_of":now.isoformat(),"symbol":frame.symbol,"name":frame.name,"follow_score":frame.follow_score,"reliability":reliability,"recommendation":rec,"factors":[json.dumps({"main_ratio":r.main_ratio,"super_ratio":r.super_ratio,"large_ratio":r.large_ratio,"price_change":r.pct_change,"history_days":1},ensure_ascii=False) for r in frame.itertuples()]})
    return frame.drop(columns=["follow_score"]),scores

def assess_cycles(bars: pd.DataFrame,rates: pd.DataFrame) -> pd.DataFrame:
    daily=bars[bars["interval"]=="1d"].sort_values("bar_time"); mom={}
    for symbol,g in daily.groupby("symbol"):
        if len(g)>=120:mom[symbol]=float(g.close.iloc[-1]/g.close.iloc[-120]-1)
    equity_values=[mom[x] for x in ("^GSPC","^IXIC","^GDAXI","^N225") if x in mom]
    commodity_values=[mom[x] for x in ("CL=F","HG=F","GC=F") if x in mom]
    equity=float(np.mean(equity_values)) if equity_values else 0.0
    commodity=float(np.mean(commodity_values)) if commodity_values else 0.0
    dollar=float(mom.get("DX-Y.NYB",0)); curve=float(daily[daily.symbol=="^TNX"].close.iloc[-1]-daily[daily.symbol=="^IRX"].close.iloc[-1]) if not daily.empty and all((daily.symbol==x).any() for x in ("^TNX","^IRX")) else 0.0
    business=float(np.nan_to_num(equity)*45+np.nan_to_num(commodity)*25+np.clip(curve/3,-1,1)*30); liquidity=float(-np.nan_to_num(dollar)*60+np.clip(curve/3,-1,1)*40); risk=float(np.nan_to_num(equity)*70-np.nan_to_num(dollar)*30)
    phase="复苏" if business>8 and liquidity>0 else "过热" if business>8 and commodity>0.12 else "衰退" if business<-8 else "滞胀/减速"
    evidence={"equity_6m":equity,"commodity_6m":commodity,"dollar_6m":dollar,"yield_curve_proxy":curve}
    guidance={"复苏":["均衡配置权益与周期制造","保留回撤预算"],"过热":["降低高估值久期暴露","关注资源与通胀对冲"],"衰退":["提高现金和高质量资产比重","避免高杠杆周期股"],"滞胀/减速":["偏向现金流与定价权","分散商品和防御行业"]}[phase]
    today=date.today(); rows=[("景气周期",phase,business,.62),("流动性周期","宽松" if liquidity>0 else "偏紧",liquidity,.58),("风险偏好","风险偏好上升" if risk>0 else "风险偏好下降",risk,.65),("康波周期代理","第五长波后段/第六长波孕育期",business*.35,.3)]
    return pd.DataFrame([{"as_of":today,"model_name":n,"phase":p,"score":s,"confidence":c,"evidence":json.dumps(evidence,ensure_ascii=False),"guidance":json.dumps(guidance,ensure_ascii=False)} for n,p,s,c in rows])

def run_macro_collection() -> dict:
    initialize(); assets=_asset_frame(); copy_frame(assets,"global_market_assets",list(assets.columns),["symbol"])
    bars,snaps=collect_markets(); rates=collect_rates(); wb=collect_world_trade(); trade=collect_comtrade(); opportunities=score_trade(trade); flows,flow_scores=collect_fund_flows(); cycles=assess_cycles(bars,rates)
    datasets=[(bars,"global_market_bars",["symbol","interval","bar_time"]),(snaps,"global_market_snapshots",["symbol","observed_at"]),(rates,"macro_series",["series_code","country","observation_date"]),(wb,"macro_series",["series_code","country","observation_date"]),(trade,"trade_flows",["period","reporter_code","flow_code","hs_code"]),(opportunities,"trade_opportunities",["as_of","reporter_name","hs_code","flow_code"]),(flows,"fund_flow_snapshots",["observed_at","symbol"]),(flow_scores,"fund_flow_scores",["as_of","symbol"]),(cycles,"cycle_assessments",["as_of","model_name"])]
    for frame,table,keys in datasets:
        if not frame.empty:copy_frame(frame,table,list(frame.columns),keys)
    return {"assets":len(assets),"bars":len(bars),"snapshots":len(snaps),"macro_rows":len(rates)+len(wb),"trade_rows":len(trade),"opportunities":len(opportunities),"fund_flows":len(flows),"cycles":len(cycles)}
