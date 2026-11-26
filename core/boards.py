# -*- coding: utf-8 -*-
"""板块级日表(MI-1): 沪市 / 深市 / 创业板 / 科创板 每日的广度、强弱势结构、成交
— 指数情绪分里"结构""量价之外"那部分的板块输入。

原料全部在本地 raw_daily.db(全A个股日线 2005 起、主力资金流 2007 起), 不调外部接口。

板块 = 按代码前缀划定的全体个股, 不是指数成分股(成分股只有 50 / 100 / 500 只、月度才更新;
全板块样本大、历史长、不依赖成分数据):
  SH  沪市   60x + 68x        SZ  深市   00x + 30x
  CYB 创业板 30x               KCB 科创板 68x
北交所等不计。

强弱势口径 = 固定阈值 ±9.8%: 强势 = 涨幅 ≥ +9.8%, 弱势 = 跌幅 ≤ −9.8%,
冲高回落 = 盘中最高涨幅 ≥ +9.8% 而收盘未达到。
- 主板上与封板涨停 / 跌停几乎等价(近 500 个交易日相关 0.95 / 0.93);
- 20cm 板块上封板太稀疏(科创板封板涨停中位 1 只 / 日、28% 的交易日为 0), min-max 分位会把
  大量平常日子打成 −100; 阈值口径下科创板中位 7 只、创业板 17 只;
- 不受创业板 2020-08 涨跌幅改制影响, 也不依赖 ST / 新股首日等特殊规则。
封板口径(涨停价按限幅与 ST 规则精确计算, 复用 sw_pool._day_flags)的家数另存 zt / dt / zb 与
连板两列, 只作附图与悬停信息, 不进评分。
上市头 5 个交易日的新股不计强弱势(注册制下前 5 日无涨跌幅限制, 一根 +100% 的首日 K 线
不是"强势股"); 涨跌家数照常计。

表 board_daily: 每板块每日一行, 金额单位元(成交额、主力净流入 = 大单+超大单买入−卖出,
与 sw_fflow 同口径)。

用法: python boards.py build [--start 20050101]     # 回补; 日增由 fetch_daily.fetch_sw_daily 挂载
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import sw_pool as SP                     # noqa: E402
from db import connect                   # noqa: E402
from raw_daily import raw_connect        # noqa: E402

BOARDS = {                                # 代码 → (名称, 代码前缀)
    "SH": ("沪市", ("60", "68")),
    "SZ": ("深市", ("00", "30")),
    "CYB": ("创业板", ("30",)),
    "KCB": ("科创板", ("68",)),
}
STRONG = 0.098                           # 强势 / 弱势阈值(涨跌幅)
NEW_LISTING_DAYS = 5                     # 上市头几个交易日(无涨跌幅限制)不计强弱势

_PREFIX = {"60": ("SH",), "68": ("SH", "KCB"), "30": ("SZ", "CYB"), "00": ("SZ",)}

COLS = ("n", "up", "down", "flat", "strong", "weak", "fade", "streak_max", "streak2",
        "prev_strong", "zt", "dt", "zb", "lb_max", "lb2", "prev_zt", "amt", "mf_net")

SCHEMA = """CREATE TABLE IF NOT EXISTS board_daily(
  board TEXT NOT NULL, date TEXT NOT NULL,
  n INTEGER, up INTEGER, down INTEGER, flat INTEGER,
  strong INTEGER, weak INTEGER, fade INTEGER,
  streak_max INTEGER, streak2 INTEGER, prev_strong INTEGER,
  zt INTEGER, dt INTEGER, zb INTEGER, lb_max INTEGER, lb2 INTEGER, prev_zt INTEGER,
  amt REAL, mf_net REAL,
  PRIMARY KEY(board, date))"""

UPSERT = ("INSERT OR REPLACE INTO board_daily(board,date," + ",".join(COLS) + ") "
          "VALUES(?,?," + ",".join("?" * len(COLS)) + ")")


def board_of(ts_code) -> tuple:
    """股票所属板块(可能同时属于两个: 创业板股也在深市、科创板股也在沪市); 不计入的返回空元组"""
    return _PREFIX.get(str(ts_code or "")[:2], ())


def _new(ts, first_idx, day_no) -> bool:
    """上市头 NEW_LISTING_DAYS 个交易日(库里首日即上市首日; 2005 年前已上市的没有记录)"""
    i = first_idx.get(ts)
    return i is not None and 0 <= day_no - i < NEW_LISTING_DAYS


def day_stats(rows, d_ymd, st=None, first_idx=None, day_no=None, mf=None,
              streak=None, zt_streak=None, prev_strong=None, prev_zt=None, seen=None) -> dict:
    """一个交易日的全市场个股行 → {板块: dict(COLS 各列)}。

    rows: [(ts_code, pre_close, close, high, amount)](amount 单位千元)。涨跌方向一律按
    收盘 / 前收算, 不用源端的 pct_chg — 两者打架时(脏数据)家数会与强弱家数对不上;
    streak / zt_streak: 各股截至前一交易日的连续强势 / 连续封板天数(就地更新, 供次日沿用);
    prev_strong / prev_zt: {板块: 前一交易日的强势 / 封板涨停家数};
    seen: {股票: 上一次有行情的交易日序号}(就地更新) — "连续"只认相邻的交易日,
    停牌几天后复牌再强势不算接上(传入 day_no 时生效)。
    """
    streak = {} if streak is None else streak
    zt_streak = {} if zt_streak is None else zt_streak
    prev_strong = prev_strong or {}
    prev_zt = prev_zt or {}
    acc = {b: dict.fromkeys(COLS, 0) for b in BOARDS}
    lim = SP._day_flags([(r[0], r[1], r[2], r[3]) for r in rows], d_ymd, st=st)
    today_strong = dict.fromkeys(BOARDS, 0)
    today_zt = dict.fromkeys(BOARDS, 0)
    for ts, pre, close, high, amt in rows:
        bs = board_of(ts)
        if not bs or not pre or pre <= 0 or close is None or close <= 0:
            continue
        g = close / pre - 1.0
        hg = (high / pre - 1.0) if high else None
        new = first_idx is not None and day_no is not None and _new(ts, first_idx, day_no)
        strong = (not new) and g >= STRONG
        weak = (not new) and g <= -STRONG
        fade = (not new) and (not strong) and hg is not None and hg >= STRONG
        cont = True
        if seen is not None and day_no is not None:
            last = seen.get(ts)
            cont = last is None or day_no - last <= 1
            seen[ts] = day_no
        s = streak.get(ts, 0) + 1 if strong and cont else (1 if strong else 0)
        streak[ts] = s
        zt, dt, zb = lim.get(ts, (False, False, False))
        zs = zt_streak.get(ts, 0) + 1 if zt and cont else (1 if zt else 0)
        zt_streak[ts] = zs
        net = (mf or {}).get(ts)
        for b in bs:
            a = acc[b]
            a["n"] += 1
            if g > 0:
                a["up"] += 1
            elif g < 0:
                a["down"] += 1
            else:
                a["flat"] += 1
            if strong:
                a["strong"] += 1
                a["streak_max"] = max(a["streak_max"], s)
                a["streak2"] += s >= 2
                today_strong[b] += 1
            a["weak"] += weak
            a["fade"] += fade
            if zt:
                a["zt"] += 1
                a["lb_max"] = max(a["lb_max"], zs)
                a["lb2"] += zs >= 2
                today_zt[b] += 1
            a["dt"] += dt
            a["zb"] += zb
            if amt:
                a["amt"] += float(amt) * 1000.0          # 千元 → 元
            if net:
                a["mf_net"] += float(net)
    for b, a in acc.items():
        a["prev_strong"] = prev_strong.get(b, 0)
        a["prev_zt"] = prev_zt.get(b, 0)
    return {"stats": acc, "strong": today_strong, "zt": today_zt}


def check_rows(rows) -> str:
    """写库前的算术自检: 返回拒写原因(None = 通过)"""
    for r in rows:
        b, d, n, up, down, flat, strong, weak, fade, smax, s2, ps, zt, dt, zb, lbm, lb2, pzt, amt, mf = r
        if up + down + flat != n:
            return f"{b} {d}: 涨+跌+平 {up + down + flat} ≠ 家数 {n}"
        if not (strong <= up and weak <= down and s2 <= strong and lb2 <= zt <= n):
            return f"{b} {d}: 强弱/连板家数与涨跌家数矛盾 strong={strong} weak={weak} up={up} down={down} zt={zt}"
        if min(smax, lbm, fade, amt) < 0:               # 主力净流入有进有出, 可以为负
            return f"{b} {d}: 数值越界 streak_max={smax} fade={fade} amt={amt}"
    return None


def _first_days_idx(raw, day_idx, floor_ymd):
    """可能算"新股"的股票 → {ts: 库里首日的序号}。

    只看库里首日不早于 floor_ymd 的股票(更早上市的本来就不是新股)。个股日线库第一天就有
    行情的股票不算新股: 那是全库的起点而不是它们的上市日 — 否则全史回补时, 2005 年初
    那几天所有老股都会被当成上市头 5 日而不计强弱势。"""
    try:
        first_all = raw.execute("SELECT MIN(trade_date) FROM daily").fetchone()[0]
        rows = raw.execute(
            "SELECT ts_code, MIN(trade_date) FROM daily WHERE ts_code IN "
            "(SELECT DISTINCT ts_code FROM daily WHERE trade_date>=?) "
            "GROUP BY ts_code HAVING MIN(trade_date)>=?", (floor_ymd, floor_ymd)).fetchall()
    except sqlite3.Error:
        return {}
    return {ts: day_idx[fd] for ts, fd in rows
            if fd in day_idx and first_all and fd > first_all}


def build(start="20050101", sent=None, raw=None) -> bool:
    """逐日聚合 → board_daily。start 起的每个交易日整体重算(幂等);
    连板 / 接力要用到前几日的状态, 所以先从 start 之前两周的日子"预热"但不写库。"""
    import datetime as dt
    sent = connect() if sent is None else sent
    raw = raw_connect() if raw is None else raw
    try:
        sent.execute(SCHEMA)
        start = str(start).replace("-", "")
        warm = (dt.datetime.strptime(start, "%Y%m%d") - dt.timedelta(days=14)).strftime("%Y%m%d")
        days = [r[0] for r in raw.execute(
            "SELECT DISTINCT trade_date FROM daily WHERE trade_date>=? ORDER BY trade_date",
            (warm,))]
        if not days:
            return True
        day_idx = {d: i for i, d in enumerate(days)}
        # 新股判定按"库里首日"数交易日, 只对首日落在窗口内的股票判(见 _first_days_idx)
        first_idx = _first_days_idx(raw, day_idx, days[0])
        st_sets = SP._st_sets_by_day(days, sent)
        has_mf = True
        try:
            raw.execute("SELECT 1 FROM moneyflow LIMIT 1")
        except sqlite3.Error:
            has_mf = False
        streak, zt_streak, prev_strong, prev_zt, seen = {}, {}, {}, {}, {}
        t0 = time.time()
        n_written = 0
        for i, d in enumerate(days, 1):
            rows = raw.execute(
                "SELECT ts_code, pre_close, close, high, amount FROM daily "
                "WHERE trade_date=?", (d,)).fetchall()
            mf = None
            if has_mf:
                mf = {r[0]: r[1] for r in raw.execute(
                    "SELECT ts_code, (COALESCE(buy_elg_amount,0)+COALESCE(buy_lg_amount,0)"
                    "-COALESCE(sell_elg_amount,0)-COALESCE(sell_lg_amount,0))*10000 "
                    "FROM moneyflow WHERE trade_date=?", (d,))}
            out = day_stats(rows, d, st=st_sets.get(d), first_idx=first_idx,
                            day_no=day_idx[d], mf=mf, streak=streak, zt_streak=zt_streak,
                            prev_strong=prev_strong, prev_zt=prev_zt, seen=seen)
            prev_strong, prev_zt = out["strong"], out["zt"]
            if d < start:
                continue                                   # 预热: 只推进状态
            d_iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            batch = [(b, d_iso) + tuple(int(a[c]) if c not in ("amt", "mf_net") else round(a[c], 0)
                                        for c in COLS)
                     for b, a in out["stats"].items() if a["n"]]
            why = check_rows(batch)
            if why:
                raise RuntimeError(f"[boards] 拒写 {d_iso}: {why}")
            if batch:
                cur = sent.cursor()
                cur.execute("BEGIN")
                cur.executemany(UPSERT, batch)
                sent.commit()
                n_written += len(batch)
            if i % 500 == 0:
                print(f"  [{i}/{len(days)}] {time.time() - t0:.0f}s")
        print(f"[boards] 聚合完成 {time.time() - t0:.0f}s, 写入 {n_written} 行"
              f"({days[0]}~{days[-1]}, 自 {start} 起)")
        return True
    finally:
        sent.close()
        raw.close()


def main():
    ap = argparse.ArgumentParser(description="板块广度与结构日表")
    ap.add_argument("cmd", nargs="?", default="build", choices=["build"])
    ap.add_argument("--start", default="20050101")
    a = ap.parse_args()
    sys.exit(0 if build(a.start) else 1)


if __name__ == "__main__":
    main()
