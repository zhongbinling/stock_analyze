# -*- coding: utf-8 -*-
"""行情接口客户端: 指数K线(东财→腾讯回退) + 东财涨停专题池

- 东财 push2his 提供成交额, 但对高频请求会临时封 IP(WAF), 只轻量重试;
  失败自动回退腾讯 ifzq(仅成交量/手, 无成交额)。
- 评分的量能分量统一用"两市成交量(手)", 保证跨数据源口径一致
  (也与通达信V1公式用VOL一致)。
- 口径注意(东财官方): 涨停板专题统计**不含 ST 股与科创板股**,
  涨停/跌停/炸板家数为 沪深主板+创业板(非ST) 口径, 序列内部一致。
"""
import random
import socket
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json",
}
_UA_TX = {"User-Agent": _UA["User-Agent"], "Accept": "application/json"}

EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TX_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
POOL_URLS = {
    "zt": ("https://push2ex.eastmoney.com/getTopicZTPool", "fbt:asc"),        # 涨停股池
    "prev": ("https://push2ex.eastmoney.com/getYesterdayZTPool", "zs:desc"),   # 昨日涨停股池
    "zb": ("https://push2ex.eastmoney.com/getTopicZBPool", "fbt:asc"),        # 炸板股池
    "dt": ("https://push2ex.eastmoney.com/getTopicDTPool", "fund:asc"),       # 跌停股池
}
_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _get(url, params, retries=3, headers=None, backoff=1.5):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=12, headers=headers or _UA)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(backoff + i + random.random())
    raise last


# ---------------------------------------------------------------- 指数K线

def index_kline(secid: str, beg="19900101", end="20500000"):
    """指数日线, 返回 (DataFrame, 数据源)。
    secid: '1.000001'(上证) / '0.399106'(深证综指)
    列: date,open,close,high,low,volume[,amount],amp,pct_chg,chg,turnover
    腾讯源 amount/turnover 为 NaN(接口不提供)。
    """
    try:
        df = _em_kline(secid, beg, end)
        if not df.empty:
            return df, "eastmoney"
    except Exception:  # noqa: BLE001
        pass
    return _tx_kline(_tx_code(secid), beg), "tencent"


def _em_kline(secid, beg, end):
    j = _get(EM_KLINE_URL, {
        "secid": secid, "ut": _UT,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": "0", "beg": beg, "end": end}, retries=2, backoff=2.0)
    d = j.get("data") or {}
    kl = d.get("klines") or []
    if not kl:
        return pd.DataFrame()
    df = pd.DataFrame([k.split(",") for k in kl],
                      columns=["date", "open", "close", "high", "low", "volume",
                               "amount", "amp", "pct_chg", "chg", "turnover"])
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(df[c])
    return df


def _tx_code(secid: str) -> str:
    mkt, code = secid.split(".")
    return ("sh" if mkt == "1" else "sz") + code


def _tx_kline(txcode: str, beg: str) -> pd.DataFrame:
    """腾讯日K分块抓取(从最新往回翻页): [date,open,close,high,low,volume(手)], 无成交额"""
    beg_d = f"{beg[:4]}-{beg[4:6]}-{beg[6:8]}"
    rows = {}
    end = ""  # 空 = 至最新
    while True:
        j = _get(TX_KLINE_URL, {"param": f"{txcode},day,,{end},700,qfq"},
                 headers=_UA_TX)
        d = (j.get("data") or {}).get(txcode) or {}
        kl = d.get("qfqday") or d.get("day") or []
        if not kl:
            break
        for row in kl:
            if row[0] not in rows:
                rows[row[0]] = row
        earliest = kl[0][0]
        if earliest <= beg_d:
            break
        end = (datetime.strptime(earliest, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        time.sleep(0.3)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(sorted(rows.values()),
                      columns=["date", "open", "close", "high", "low", "volume"])
    for c in df.columns[1:]:
        df[c] = pd.to_numeric(df[c])
    df["amount"] = float("nan")
    prev = df["close"].shift(1)
    df["amp"] = (df["high"] - df["low"]) / prev * 100
    df["pct_chg"] = (df["close"] / prev - 1) * 100
    df["chg"] = df["close"].diff()
    df["turnover"] = float("nan")
    return df


# ---------------------------------------------------------------- 涨停专题池
# 主源: 同花顺涨停复盘(历史约一年滚动, 沪深主板+创业板+科创板口径)
# 兜底: 东财涨停专题(仅最近约一个月, 不含ST与科创板口径)

THS_POOL_URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
_THS_H = {
    "User-Agent": _UA["User-Agent"],
    "Referer": "http://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html",
    "Accept": "application/json",
}


def _lb_from_str(s):
    """"首板"→1, "3连板"→3"""
    s = str(s or "")
    if not s or "首板" in s:
        return 1
    import re
    m = re.search(r"(\d+)", s)
    return int(m.group(1)) if m else 1


def ths_pool_stats(date: str) -> dict:
    """同花顺涨停复盘统计。date: YYYYMMDD。
    一次接口同时返回: 涨停/跌停家数、封板率(炸板率=1-封板率)、连板结构。
    非交易日/无数据返回 {}。"""
    page, info, up, down = 1, [], None, None
    while True:
        j = _get(THS_POOL_URL, {
            "filter": "HS,GEM2STAR", "limit": "200",
            "field": "199112,10,9001,330323,330324,330325,9002,330329,133971,"
                     "133970,1968584,3475914,9003,9004",
            "order_field": "330323", "order_type": "0",
            "date": date, "page": str(page)}, headers=_THS_H)
        if j.get("status_code") != 0:
            raise RuntimeError(f"THS接口: {j.get('status_msg')}")
        d = j.get("data") or {}
        up, down = d.get("limit_up_count"), d.get("limit_down_count")
        info.extend(d.get("info") or [])
        pages = int((d.get("page") or {}).get("count") or 1)
        if not d.get("info") or page >= pages:
            break
        page += 1
        time.sleep(0.2)
    if up is None:
        return {}
    zt = int((up.get("today") or {}).get("num") or 0)
    if zt == 0 and not info:
        return {}  # 非交易日/越界日期
    dt_ = int((down.get("today") or {}).get("num") or 0)
    t = up.get("today") or {}
    rate = t.get("rate")
    if rate is not None:
        zb_rate = round((1 - float(rate)) * 100, 1)
    else:
        hn, on = t.get("history_num"), t.get("open_num")
        zb_rate = round(on / hn * 100, 1) if hn else 0.0
    yzt = int((up.get("yesterday") or {}).get("num") or 0)
    lbs = [_lb_from_str(x.get("high_days")) for x in info]
    lb_max = max(lbs) if lbs else 0
    lb2 = sum(1 for v in lbs if v >= 2)
    # 接力率: 昨日涨停股中今日继续涨停(≥2板)的比例, 赚钱效应代理
    jl = round(lb2 / yzt * 100, 1) if yzt else None
    return {"zt_count": zt, "dt_count": dt_, "zb_rate": zb_rate,
            "lb_max": lb_max, "lb2_count": lb2, "jl_rate": jl,
            "yzt_count": yzt}


def topic_pool(kind: str, date: str) -> list:
    """东财涨停专题池原始记录列表(仅最近约一个月有效)。kind: zt/prev/zb/dt"""
    if kind not in POOL_URLS:
        raise ValueError(f"unknown pool kind: {kind}")
    url, sort = POOL_URLS[kind]
    j = _get(url, {
        "ut": _UT, "dpt": "wz.ztzt", "Pageindex": "0",
        "pagesize": "10000", "sort": sort, "date": date})
    d = j.get("data")
    return (d or {}).get("pool") or []


def em_pool_stats(date: str) -> dict:
    """东财四池聚合统计(仅最近约一个月; 含 prev_zt_chg 昨日涨停今日表现,
    留作 V2.1 逐日增量使用, 历史回补不走此函数)"""
    zt = topic_pool("zt", date)
    if not zt:
        return {}
    dt_ = topic_pool("dt", date)
    zb = topic_pool("zb", date)
    prev = topic_pool("prev", date)

    zt_n, dt_n, zb_n = len(zt), len(dt_), len(zb)
    lb = [int(x.get("lbc") or 0) for x in zt]
    lb_max = max(lb) if lb else 0
    lb2 = sum(1 for v in lb if v >= 2)
    prev_chg = None
    zdps = [float(x["zdp"]) for x in prev if x.get("zdp") is not None]
    if zdps:
        prev_chg = round(sum(zdps) / len(zdps), 2)
    zb_rate = round(zb_n / (zt_n + zb_n) * 100, 1) if (zt_n + zb_n) else 0.0
    return {"zt_count": zt_n, "dt_count": dt_n, "zb_count": zb_n,
            "zb_rate": zb_rate, "lb_max": lb_max, "lb2_count": lb2,
            "prev_zt_chg": prev_chg}
