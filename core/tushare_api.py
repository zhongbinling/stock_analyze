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
    """涨跌停名单(单日): ts_code,name,limit_type(U涨停/D跌停/Z炸板)...
    注意: 该接口对积分有要求"""
    return call("limit_list_d",
                "trade_date,ts_code,name,limit_type,fd_amount,first_time,"
                "last_time,open_times,up_stat,limit_times",
                trade_date=trade_date)


def probe():
    """逐个探测常用接口在本 token 下的可用性"""
    token = load_token()
    print(f"token: {token[:8]}...{token[-4:]}" if token else "✗ 未配置 token")
    if not token:
        return
    tests = [
        ("trade_cal 交易日历", lambda: trade_cal("20260101", "20260131")),
        ("index_daily 指数日线", lambda: index_daily("000001.SHY", "20260101", "20260110")
         if False else index_daily("000001.SH", "20260101", "20260110")),
        ("limit_list_d 涨跌停名单", lambda: limit_list_d("20261015")),
        ("daily_basic 每日指标", lambda: call("daily_basic", "ts_code,close,turnover_rate",
                                          trade_date="20261015")),
    ]
    for name, fn in tests:
        try:
            df = fn()
            print(f"  ✓ {name}: {len(df)} 行 | 字段: {list(df.columns)[:5]}")
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
