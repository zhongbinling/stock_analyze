# -*- coding: utf-8 -*-
"""Tushare 数据源适配层 (pro.tushare.pro)

token 读取优先级: core/data/tushare_token.txt → 环境变量 TUSHARE_TOKEN
不同积分可用接口不同: probe 命令探测本 token 实际可用性。

用法:
  python tushare_api.py probe                 # 探测接口权限
  python tushare_api.py cal 20260101 20261231 # 交易日历
"""
import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
TOKEN_FILE = BASE / "data" / "tushare_token.txt"
API_URL = "https://api.tushare.pro"
SLEEP = 0.6          # 免费档限频(~200次/分), 保守间隔


def load_token() -> str:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    import os
    return os.environ.get("TUSHARE_TOKEN", "")


def has_token() -> bool:
    return bool(load_token())


def call(api: str, fields: str = "", **params) -> pd.DataFrame:
    """调用 tushare pro 接口; 失败抛 RuntimeError(含官方错误信息)"""
    token = load_token()
    if not token:
        raise RuntimeError("无 tushare token (core/data/tushare_token.txt)")
    body = {"api_name": api, "token": token,
            "params": params, "fields": fields}
    r = requests.post(API_URL, json=body, timeout=30,
                      headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(f"tushare {api}: [{j.get('code')}] {j.get('msg')}")
    fields_l = (fields or "").split(",") if fields else (j.get("data") or {}).get("fields") or []
    rows = (j.get("data") or {}).get("items") or []
    time.sleep(SLEEP)
    return pd.DataFrame(rows, columns=fields_l)


# ---------------------------------------------------------------- 分页取全
PAGE_ROWS = 6000        # 单次请求的行数(服务端实际上限可能更低, 见 pull_frames)
MAX_ROWS = 60000        # 单个查询对象的全史行数上限: 接口异常时不至于无限翻页


def pull_frames(api: str, fields: str, **params) -> list:
    """接口分页取全: 返回各页的 DataFrame(接口原始顺序), 不排序不去重。

    一页 = 首页实际返回的行数。服务端的单次上限低于请求值 limit=PAGE_ROWS 时不报错:
    日线/复权因子 6000 行, 三大报表与财务指标只有 100 行。按"返回行数不足请求值即
    最后一页"来判断的话, 首页就被当成最后一页, 截断悄悄回来。
    偏移量超过末尾时返回 0 行; 总行数恰为页长整数倍时会多一次空页请求。"""
    frames, offset, page = [], 0, None
    while True:
        df = call(api, fields, limit=PAGE_ROWS, offset=offset, **params)
        frames.append(df)
        n = 0 if df is None else len(df)
        if page is None:
            page = n or PAGE_ROWS
        if n < page:
            break
        offset += n
        if offset >= MAX_ROWS:
            break
    return frames


# ---------------------------------------------------------------- 常用接口

def trade_cal(start: str, end: str, exchange: str = "SSE") -> pd.DataFrame:
    """交易日历: exchange, cal_date, is_open, pretrade_date"""
    return call("trade_cal", "exchange,cal_date,is_open,pretrade_date",
                exchange=exchange, start_date=start, end_date=end,
                is_open="")


def index_daily(ts_code: str, start: str, end: str = "20500101") -> pd.DataFrame:
    """指数日线: ts_code,trade_date,open,high,low,close,vol,amount"""
    return call("index_daily",
                "ts_code,trade_date,open,high,low,close,vol,amount",
                ts_code=ts_code, start_date=start, end_date=end)


def limit_list_d(trade_date: str) -> pd.DataFrame:
    """涨跌停名单(单日, 2020年起): limit列 U涨停/D跌停/Z炸板, industry行业,
    limit_times连板数, open_times打开次数, up_stat几天几板"""
    return call("limit_list_d",
                "trade_date,ts_code,industry,name,limit,pct_chg,fd_amount,"
                "first_time,last_time,open_times,up_stat,limit_times",
                trade_date=trade_date)


def margin(start: str, end: str = "") -> pd.DataFrame:
    """融资融券交易汇总(按交易所, 区间): rzye融资余额/rzmre买入/rzche偿还/
    rqye融券余额/rzrqye两融余额; exchange_id: SSE/SZSE/BSE"""
    p = {"start_date": start}
    if end:
        p["end_date"] = end
    return call("margin",
                "trade_date,exchange_id,rzye,rzmre,rzche,rqye,rqmcl,rzrqye,rqyl",
                **p)


def probe():
    """逐个探测常用接口在本 token 下的可用性"""
    token = load_token()
    print(f"token: {token[:8]}...{token[-4:]}" if token else "✗ 未配置 token")
    if not token:
        return
    tests = [
        ("trade_cal 交易日历", lambda: trade_cal("20260101", "20260131")),
        ("index_daily 指数日线", lambda: index_daily("000001.SH", "20260101", "20260110")),
        ("limit_list_d 涨跌停名单", lambda: limit_list_d("20261015")),
        ("margin 融资融券汇总", lambda: margin("20261010", "20261015")),
        ("daily_basic 每日指标", lambda: call("daily_basic", "ts_code,close,turnover_rate",
                                          trade_date="20261015")),
    ]
    for name, fn in tests:
        try:
            df = fn()
            print(f"  ✓ {name}: {len(df)} 行 | 字段: {list(df.columns)[:6]}")
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {name}: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["probe", "cal"])
    ap.add_argument("a", nargs="?", default="20260101", help="cal: 起始YYYYMMDD")
    ap.add_argument("b", nargs="?", default="20261231", help="cal: 结束YYYYMMDD")
    args = ap.parse_args()
    if args.cmd == "probe":
        probe()
    else:
        df = trade_cal(args.a, args.b)
        print(df.to_string(max_rows=10))


if __name__ == "__main__":
    main()
