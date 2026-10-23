# -*- coding: utf-8 -*-
"""SW成分股等权自算指数 (D1-9): 替换官方sw_daily作温度量价输入 — 与东财板块口径可比

每指数每日: ret=成分股pct_chg均值(等权), amp=成分(high-low)/pre_close均值,
amount=成分成交额求和; close=ret累乘(首日=1000), high/low=close*(1±amp/2)。
幂等按(index_code)整指数重算(PK覆盖); 分年分块控内存。

用法: python sw_index_comp.py build|check
"""
import argparse
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402
from db import connect  # noqa: E402
from raw_daily import raw_connect  # noqa: E402


def build(start="20050101") -> bool:
    sent = connect()
    sent.execute("""CREATE TABLE IF NOT EXISTS sw_index_comp(
      index_code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
      volume REAL, amount REAL, pct_chg REAL, n INTEGER,
      PRIMARY KEY(index_code, date))""")
    sent.execute("CREATE INDEX IF NOT EXISTS idx_sic_date ON sw_index_comp(date)")
    iv = pd.read_sql(
        "SELECT index_code, level, industry_name, con_code, in_date, out_date "
        "FROM sw_components", sent)
    if iv.empty:
        print("[sic] sw_components 为空, 跳过")
        return False
    iv["out_date"] = iv["out_date"].fillna("99999999")
    raw = raw_connect()
    years = [r[0] for r in raw.execute(
        "SELECT DISTINCT substr(trade_date,1,4) FROM daily ORDER BY 1")]
    sy = int(str(start).replace("-", "")[:4])
    # D1-9r-review: 1-2月的增量窗口跨年 → 含上一年(晚到数据自愈)
    sm = int(str(start).replace("-", "-")[4:6].lstrip("0") or 1)
    years = [y for y in years if int(y) >= sy - (1 if sm <= 2 else 0)]
    t0 = time.time()
    frames = []
    for y in years:
        d = pd.read_sql(
            "SELECT ts_code, trade_date, pre_close, open, close, high, low, amount FROM daily "
            "WHERE trade_date>=? AND trade_date<=?", raw,
            params=(f"{y}0101", f"{y}1231"))
        if d.empty:
            continue
        m = d.merge(iv[["index_code", "con_code", "in_date", "out_date"]],
                    left_on="ts_code", right_on="con_code", how="inner")
        m = m[(m["trade_date"] >= m["in_date"]) & (m["trade_date"] <= m["out_date"])]
        if m.empty:
            continue
        m["ret"] = (m["close"] / m["pre_close"] - 1) * 100
        m["amp"] = (m["high"] - m["low"]) / m["pre_close"] * 100
        m["gp"] = (m["open"] / m["pre_close"] - 1) * 100     # 成分真实跳空
        g = m.groupby(["index_code", "trade_date"]).agg(
            ret=("ret", "mean"), amp=("amp", "mean"), gp=("gp", "mean"),
            amount=("amount", "sum"), n=("ts_code", "count")).reset_index()
        frames.append(g)
        print(f"  {y}: {len(d)//10000}万行 → {len(g)} 指数日 ({time.time()-t0:.0f}s)")
    raw.close()
    if not frames:
        print("[sic] 无数据")
        return False
    allg = pd.concat(frames, ignore_index=True).sort_values(["index_code", "trade_date"])
    # 累乘等权指数: 基点=承接前收盘(增量)或1000(全量) — 统一不吸收块首日收益,
    # close隐含收益与pct_chg列一致(D1-9r-review: 原首日吸收致每年一天chg读0%)
    allg["factor"] = (1 + allg["ret"] / 100)
    allg["cum"] = allg.groupby("index_code")["factor"].cumprod()
    first_day = allg["trade_date"].min()
    fd_iso = f"{first_day[:4]}-{first_day[4:6]}-{first_day[6:8]}"
    carry = {}
    for ic in allg["index_code"].unique():
        r = sent.execute(
            "SELECT close FROM sw_index_comp WHERE index_code=? AND date<? "
            "ORDER BY date DESC LIMIT 1", (ic, fd_iso)).fetchone()
        if r and r[0]:
            carry[ic] = r[0] / 1000.0      # 相对1000基点的续算系数
    if carry:
        print(f"  承接前收盘: {len(carry)} 指数 (块首 {fd_iso})")
    allg["base"] = allg["index_code"].map(carry).fillna(1.0) * 1000.0
    allg["close"] = allg["cum"] * allg["base"]
    allg["pct_chg"] = allg["ret"]
    allg["high"] = allg["close"] * (1 + allg["amp"] / 200)
    allg["low"] = allg["close"] * (1 - allg["amp"] / 200)
    # D1-9r2: open=前收盘×(1+成分跳空均值) — gap分量用真实跳空
    # (原合成open=close/(1+ret)≡前收盘, gap恒0退化为分位噪声)
    prev_close = allg.groupby("index_code")["close"].shift(1)
    allg["open"] = prev_close * (1 + allg["gp"] / 100)
    allg.loc[allg["open"].isna(), "open"] = allg["close"] / (1 + allg["ret"] / 100)
    # D1-9r3-review: 价格带包含open(真实指数high/low必含开盘价; 跳空>振幅
    # 半宽时open越带, kr=(close-open)/rng会超[-1,1] — 实测872/5293行越界)
    allg["high"] = allg[["high", "open"]].max(axis=1)
    allg["low"] = allg[["low", "open"]].min(axis=1)
    allg["volume"] = allg["amount"]
    allg["date"] = allg["trade_date"].str.slice(0, 4) + "-" + \
        allg["trade_date"].str.slice(4, 6) + "-" + allg["trade_date"].str.slice(6, 8)
    sent.executemany(
        "INSERT OR REPLACE INTO sw_index_comp(index_code,date,open,high,low,close,"
        "volume,amount,pct_chg,n) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(r["index_code"], r["date"], r["open"], r["high"], r["low"], r["close"],
          r["volume"], r["amount"], r["pct_chg"], int(r["n"]))
         for _, r in allg.iterrows()])
    sent.commit()
    ni = allg["index_code"].nunique()
    sent.close()
    print(f"[sic] 完成: {ni} 指数 × {allg['trade_date'].nunique()} 日 ({time.time()-t0:.0f}s)")
    return True


def check():
    sent = connect()
    try:
        r = sent.execute("SELECT COUNT(*), COUNT(DISTINCT index_code), MIN(date), MAX(date) "
                         "FROM sw_index_comp").fetchone()
        print(f"[sic] sw_index_comp: {r[0]} 行 / {r[1]} 指数 ({r[2]} ~ {r[3]})")
        # 对照: 等权指数 vs EM板块 250日位置分位
        rows = [x[0] for x in sent.execute(
            "SELECT close FROM sw_index_comp WHERE index_code='801120.SI' "
            "ORDER BY date DESC LIMIT 250")][::-1]
        if len(rows) > 50:
            lo, hi = min(rows), max(rows)
            print(f"  等权食品饮料 250日位置分位 {(rows[-1]-lo)/(hi-lo)*100:.0f}% (官方21% / EM 33%)")
    except Exception as e:
        print("[sic] 未建:", e)
    sent.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "check"])
    ap.add_argument("--start", default="20050101")
    a = ap.parse_args()
    if a.cmd == "build":
        sys.exit(0 if build(a.start) else 1)
    check()


if __name__ == "__main__":
    main()
