# -*- coding: utf-8 -*-
"""D1-17 R1: 实时数据源探测 — 快照/分钟线/涨跌家数/ETF 的可用性·延迟·限流

候选源(盘中轮询 5~60s 档):
  快照: 东财push2 quote / 腾讯qt.gtimg / 新浪hq.sinajs
  分钟: 东财trends2 / 腾讯ifzq minute
  涨跌家数: push2 f104-f106(沪000001+深399106合计) / 涨停池: push2ex(既有)

用法: python probe_rt.py [--n-qt 30] [--n-em 5] [--n-sn 10]
输出: 各源 ok率/延迟p50·p95/字段样例 + 选型建议原始依据。
"""
import argparse
import json
import re
import subprocess
import sys
import time

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


# ---------------------------------------------------------------- 解析(纯函数, 可单测)

def parse_qt(text: str) -> dict:
    """腾讯 qt.gtimg: v_sh000001="1~上证指数~000001~现价~昨收~今开~量(手)~...~最高~最低~..."
    字段索引: 1名称 2代码 3现价 4昨收 5今开 6成交量(手) 30时间 33最高 34最低 37成交额(万)"""
    m = re.search(r'v_(\w+)="([^"]*)"', text)
    if not m:
        return {}
    f = m.group(2).split("~")
    if len(f) < 38:
        return {}
    g = lambda i: f[i] if i < len(f) else ""      # noqa: E731
    return {"code": m.group(1), "name": g(1), "price": g(3), "prev_close": g(4),
            "open": g(5), "vol_hand": g(6), "ts": g(30), "high": g(33),
            "low": g(34), "amount_wan": g(37)}


def parse_sina(text: str) -> dict:
    """新浪 hq: var hq_str_sh000001="名称,今开,昨收,现价,最高,最低,...,日期,时间,00";
    注: 尾部日期/时间字段为**个股**口径, 指数报文错位(实测date取到时刻) —
    新浪未选型, 本函数仅作可用性/延迟探测的价格字段校验。"""
    m = re.search(r'hq_str_(\w+)="([^"]*)"', text)
    if not m:
        return {}
    f = m.group(2).split(",")
    if len(f) < 32:
        return {}
    return {"code": m.group(1), "name": f[0], "open": f[1], "prev_close": f[2],
            "price": f[3], "high": f[4], "low": f[5],
            "date": f[-3] if len(f) >= 3 else "", "time": f[-2] if len(f) >= 2 else ""}


def parse_em_quote(j: dict) -> dict:
    """东财 push2 stock/get(fltt=2): f43现价 f60昨收 f46开 f47量 f48额
    f104/f105/f106=上涨/下跌/平盘家数 f86时间戳"""
    d = j.get("data") or {}
    if not d.get("f43"):
        return {}
    return {"price": d.get("f43"), "prev_close": d.get("f60"), "open": d.get("f46"),
            "vol": d.get("f47"), "amount": d.get("f48"),
            "up": d.get("f104"), "down": d.get("f105"), "flat": d.get("f106"),
            "ts": d.get("f86")}


def parse_tx_minute(j: dict) -> dict:
    """腾讯 ifzq minute/query: data.{code}.data.data = ["0930 价格 累计量", ...]"""
    try:
        arr = j["data"][list(j["data"])[0]]["data"]["data"]
        rows = [x.split() for x in arr]
        return {"n": len(rows), "first": arr[0][:12], "last": arr[-1][:16]}
    except (KeyError, IndexError, TypeError, AttributeError):
        return {}


def parse_em_trends(j: dict) -> dict:
    """东财 push2his trends2: data.trends = ["YYYY-MM-DD HH:MM,价,量,额", ...]"""
    try:
        arr = j["data"]["trends"]
        return {"n": len(arr), "first": arr[0][:16], "last": arr[-1][:16],
                "pre_close": j["data"].get("preClose")}
    except (KeyError, TypeError):
        return {}


def parse_em_breadth(j: dict) -> dict:
    """D1-17 R1 实测选型: 涨跌家数走 ulist.np(沪深两指数单call合计, f104涨/f105跌/f106平);
    stock/get 的 f104-106 对指数返回'-'/'0.0'不可用"""
    try:
        diff = j["data"]["diff"]
    except (KeyError, TypeError):
        return {}
    up = sum(int(d.get("f104") or 0) for d in diff)
    down = sum(int(d.get("f105") or 0) for d in diff)
    flat = sum(int(d.get("f106") or 0) for d in diff)
    if up <= 0 or down <= 0:      # 两侧缺任一=无效(stock/get伪报文 f104=0/f106=100)
        return {}
    return {"up": up, "down": down, "flat": flat, "markets": [d.get("f12") for d in diff]}


def parse_qt_multi(text: str) -> dict:
    """腾讯 qt 多码应答(分号分隔): 统计成功条数+首条成交额(万)"""
    segs = [s for s in text.split(";") if "v_" in s]
    got = [parse_qt(s + ";") for s in segs]
    got = [g for g in got if g]
    if not got:
        return {}
    return {"n": len(got), "name样例": got[0]["name"],
            "amount_wan样例": got[0]["amount_wan"]}


# ---------------------------------------------------------------- HTTP(curl直连, 同em_api)

def _curl(url: str, timeout: int = 8, referer: str = "") -> tuple[int, str]:
    cmd = ["curl", "-sS", "-m", str(timeout), "-A", UA,
           "-H", "Accept: */*", "-w", "\n%{http_code}"]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return 0, ""
    body = r.stdout.rsplit("\n", 1)
    code = int(body[1]) if len(body) == 2 and body[1].isdigit() else 0
    return code, body[0] if body else ""


def _probe(name: str, url: str, fn, n: int, gap: float, referer: str = "") -> dict:
    """单源探测: n次调用, 统计ok/延迟; 返回结果摘要"""
    lat, ok, sample, fails = [], 0, None, []
    for i in range(n):
        t0 = time.time()
        code, body = _curl(url, referer=referer)
        ms = (time.time() - t0) * 1000
        d = fn(body) if code == 200 and body else {}
        if d:
            ok += 1
            lat.append(ms)
            if sample is None:
                sample = d
        else:
            fails.append(f"{code or 'timeout'}")
        if i < n - 1 and gap:
            time.sleep(gap)
    lat.sort()
    p = lambda q: round(lat[min(len(lat) - 1, int(len(lat) * q))], 0) if lat else -1  # noqa: E731
    print(f"[{name}] {ok}/{n} ok | p50 {p(0.5)}ms p95 {p(0.95)}ms | 样例 {sample}"
          + (f" | 失败 {fails[:3]}" if fails else ""))
    return {"name": name, "ok": ok, "n": n, "p50": p(0.5), "p95": p(0.95),
            "sample": sample, "fails": fails}


def run(n_qt=30, n_em=5, n_sn=10):
    print(f"== D1-17 R1 实时源探测 {time.strftime('%Y-%m-%d %H:%M')} ==")
    out = []
    # 快照(上证指数)
    out.append(_probe("腾讯qt·快照", "https://qt.gtimg.cn/q=sh000001", parse_qt,
                      n_qt, gap=0.05))                      # burst: 拟主源轮询档
    out.append(_probe("东财push2·快照",
                      "https://push2.eastmoney.com/api/qt/stock/get?secid=1.000001"
                      "&fltt=2&invt=2&fields=f43,f46,f47,f48,f60,f86,f104,f105,f106",
                      lambda b: parse_em_quote(json.loads(b) if b.startswith("{") else {}),
                      n_em, gap=1.0))                       # 只间隔探测, 不burst(护IP)
    out.append(_probe("新浪hq·快照", "https://hq.sinajs.cn/list=sh000001",
                      parse_sina, n_sn, gap=0.3, referer="https://finance.sina.com.cn"))
    # 分钟线(当日分时)
    out.append(_probe("腾讯ifzq·分时",
                      "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001",
                      lambda b: parse_tx_minute(json.loads(b) if b.startswith("{") else {}), 3, 1.0))
    out.append(_probe("东财trends2·分时",
                      "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
                      "?secid=1.000001&ndays=1&fields1=f1,f2,f3&fields2=f51,f52,f53",
                      lambda b: parse_em_trends(json.loads(b) if b.startswith("{") else {}), 3, 1.0))
    # ETF实时(8只国家队单-call)
    etfs = "sz159901,sz159915,sz159919,sz159949,sh510050,sh510300,sh510500,sh512100"
    out.append(_probe("腾讯qt·ETF×8单call", f"https://qt.gtimg.cn/q={etfs}",
                      parse_qt_multi, 3, 1.0))
    # 涨跌家数(R1实测: ulist.np 沪深合计; stock/get 的f104-106不可用)
    out.append(_probe("东财ulist·涨跌家数(沪深单call)",
                      "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2"
                      "&secids=1.000001,0.399001&fields=f12,f14,f104,f105,f106",
                      lambda b: parse_em_breadth(json.loads(b) if b.startswith("{") else {}),
                      2, 1.0))
    print("== 探测结束 ==")
    return out


def main():
    ap = argparse.ArgumentParser(description="实时数据源探测(D1-17 R1)")
    ap.add_argument("--n-qt", type=int, default=30)
    ap.add_argument("--n-em", type=int, default=5)
    ap.add_argument("--n-sn", type=int, default=10)
    a = ap.parse_args()
    run(a.n_qt, a.n_em, a.n_sn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
