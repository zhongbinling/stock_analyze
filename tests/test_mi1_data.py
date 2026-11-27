# -*- coding: utf-8 -*-
"""MI-1 情绪图多指数 · 数据底座(docs/开发计划-情绪图多指数.md, 11-27 用户确认)

① 板块级日表 core/boards.py: 沪市 / 深市 / 创业板 / 科创板 的广度、强弱势结构、成交,
   由本地个股日线库聚合 — 指数情绪分的板块输入。
② 三个指数(深证成指 / 创业板指 / 科创50)的日线: 以 tushare 为主(量额必须是指数自身口径),
   东财 / 腾讯只作开高低收的回退。
③ 数据管理接入: 资产清单 / 补全数据集 / 体检修复映射 / 更新链, 四处都要登记。
所有用例都跑在临时库上(个股日线库与行情库都指向 tmp_path)。"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import boards as BD            # noqa: E402
import db as DB                # noqa: E402
import fetch_daily as FD       # noqa: E402
import raw_daily as RD         # noqa: E402
import tushare_api as TA       # noqa: E402

EXTRA = [
    {"code": "0.399001", "name": "深证成指", "ts_code": "399001.SZ", "board": "SZ", "start": "20040101"},
    {"code": "0.399006", "name": "创业板指", "ts_code": "399006.SZ", "board": "CYB", "start": "20090101"},
    {"code": "1.000688", "name": "科创50", "ts_code": "000688.SH", "board": "KCB", "start": "20191201"},
]


@pytest.fixture()
def dbs(tmp_path, monkeypatch):
    spath, rpath = tmp_path / "sentiment.db", tmp_path / "raw.db"
    monkeypatch.setattr(DB, "DB_PATH", spath)
    monkeypatch.setattr(RD, "RAW_DB", rpath)
    return {"sent": spath, "raw": rpath}


def _q(ts, pre, close, high=None, amount=1000.0):
    """一行个股日线: (ts_code, pre_close, close, high, amount 千元)"""
    return (ts, pre, close, high if high is not None else max(pre, close), amount)


# ════════════ ① 板块级日表 ════════════

def test_board_of_by_code_prefix():
    assert BD.board_of("600001.SH") == ("SH",)
    assert BD.board_of("688001.SH") == ("SH", "KCB")          # 科创板股也在沪市
    assert BD.board_of("300001.SZ") == ("SZ", "CYB")          # 创业板股也在深市
    assert BD.board_of("000001.SZ") == ("SZ",)
    assert BD.board_of("002001.SZ") == ("SZ",)                # 原中小板并入深市主板
    assert BD.board_of("830001.BJ") == ()                     # 北交所不计
    assert BD.board_of(None) == ()
    assert set(BD.BOARDS) == {"SH", "SZ", "CYB", "KCB"}


def test_day_stats_counts_strength_and_limit_flags():
    """一个交易日的聚合: 涨跌家数 / 强弱势(固定阈值 ±9.8%) / 冲高回落 / 封板口径 / 金额单位"""
    rows = [
        _q("600001.SH", 10.00, 11.00),          # 封板涨停(也是强势)
        _q("600002.SH", 10.00, 9.00),           # 封板跌停(也是弱势)
        _q("600003.SH", 10.00, 10.30, high=11.20),   # 盘中 +12% 收 +3% → 冲高回落
        _q("600004.SH", 10.00, 10.00),          # 平盘
        _q("600005.SH", 10.00, 10.20),          # 上涨
        _q("600006.SH", 10.00, 9.80),           # 下跌
        _q("688001.SH", 20.00, 22.40),          # 科创板 +12%: 没到 20cm 涨停价, 但 ≥9.8% → 强势
        _q("688002.SH", 20.00, 21.00),
        _q("000001.SZ", 10.00, 10.10),
        _q("000002.SZ", 10.00, 9.90),
        _q("300001.SZ", 10.00, 11.50),          # 创业板 +15%: 强势, 非封板
        _q("300002.SZ", 10.00, 12.00),          # 创业板 20cm 涨停价
        _q("300003.SZ", 10.00, 8.00),           # 20cm 跌停价
        _q("830001.BJ", 10.00, 13.00),          # 北交所: 不计
    ]
    out = BD.day_stats(rows, "20260105", mf={"600001.SH": 12345.0},
                       streak={"600001.SH": 1}, prev_strong={"SH": 7}, prev_zt={"SH": 5})
    sh, sz, cyb, kcb = (out["stats"][b] for b in ("SH", "SZ", "CYB", "KCB"))
    # 沪市 = 主板 6 只 + 科创板 2 只
    assert (sh["n"], sh["up"], sh["down"], sh["flat"]) == (8, 5, 2, 1)
    assert (sh["strong"], sh["weak"], sh["fade"]) == (2, 1, 1)
    assert sh["amt"] == pytest.approx(8 * 1000 * 1000)      # 千元 → 元
    assert sh["mf_net"] == 12345.0 and kcb["mf_net"] == 0.0
    # 封板口径(另存, 只作信息): 沪市 1 家涨停 1 家跌停 1 家炸板(600003 盘中触及涨停价);
    # 创业板 1 涨停 1 跌停, 那只 +15% 的不算
    assert (sh["zt"], sh["dt"], sh["zb"]) == (1, 1, 1)
    assert (cyb["zt"], cyb["dt"], cyb["strong"], cyb["weak"]) == (1, 1, 2, 1)
    # 连续强势: 600001 前一日已强势 → 今天第 2 天; 688001 第 1 天
    assert (sh["streak_max"], sh["streak2"]) == (2, 1)
    assert (sh["prev_strong"], sh["prev_zt"]) == (7, 5)    # 前一日的家数原样带出
    # 深市 = 深市主板 2 只 + 创业板 3 只; 创业板只数不重复计进两处
    assert (sz["n"], cyb["n"], kcb["n"]) == (5, 3, 2)
    assert sz["up"] == 3 and sz["strong"] == 2 and cyb["strong"] == 2   # 300001(+15%) 与 300002(20cm 涨停)
    # 当日各板块的强势 / 封板家数(供次日算接力: 连续强势家数 ÷ 前一日强势家数)
    assert (out["strong"], out["zt"]) == ({"SH": 2, "SZ": 2, "CYB": 2, "KCB": 1},
                                          {"SH": 1, "SZ": 1, "CYB": 1, "KCB": 0})


def test_day_stats_excludes_new_listings_from_strength():
    """上市头 5 个交易日的新股不计强弱势(注册制下前 5 日无涨跌幅限制), 涨跌家数照常计"""
    first = {"601999.SH": 10}                                # 库里首日 = 第 10 号交易日
    rows = [_q("601999.SH", 10.00, 14.40)]                   # 首日 +44%
    out = BD.day_stats(rows, "20260105", first_idx=first, day_no=10)["stats"]["SH"]
    assert (out["n"], out["up"], out["strong"], out["fade"]) == (1, 1, 0, 0)
    for day_no in (10, 11, 12, 13, 14):                      # 头 5 个交易日都不计
        out = BD.day_stats([_q("601999.SH", 10.00, 11.20, high=11.50)], "20260105",
                           first_idx=first, day_no=day_no)["stats"]["SH"]
        assert (out["strong"], out["fade"]) == (0, 0), day_no
        assert out["up"] == 1                                # 涨跌家数照常计
    out = BD.day_stats([_q("601999.SH", 10.00, 11.20, high=11.50)], "20260105",
                       first_idx=first, day_no=15)["stats"]["SH"]
    assert (out["strong"], out["fade"]) == (1, 0)            # 第 6 个交易日起照常
    out = BD.day_stats([_q("601999.SH", 10.00, 10.50, high=11.50)], "20260105",
                       first_idx=first, day_no=15)["stats"]["SH"]
    assert (out["strong"], out["fade"]) == (0, 1)


def test_streak_counts_only_consecutive_trading_days():
    """"连续强势 / 连板"只认相邻的交易日: 停牌几天后复牌再封板, 不算接上"""
    streak, zt_streak, seen = {}, {}, {}
    row = _q("600001.SH", 10.00, 11.00)                        # 封板涨停(也是强势)

    def day(day_no):
        return BD.day_stats([row], "20260105", streak=streak, zt_streak=zt_streak,
                           seen=seen, day_no=day_no)["stats"]["SH"]

    s1 = day(1)
    assert (s1["streak_max"], s1["lb_max"], s1["streak2"], s1["lb2"]) == (1, 1, 0, 0)
    s2 = day(2)                                                # 相邻的交易日: 接上
    assert (s2["streak_max"], s2["lb_max"], s2["streak2"], s2["lb2"]) == (2, 2, 1, 1)
    s4 = day(4)                                                # 中间停牌了一个交易日
    assert (s4["streak_max"], s4["lb_max"], s4["streak2"], s4["lb2"]) == (1, 1, 0, 0)


def test_check_rows_rejects_inconsistent_batch():
    good = ("SH", "2026-01-05", 1, 1, 0, 0, 1, 0, 0, 3, 0, 0, 1, 0, 0, 3, 0, 0, 1e6, 0.0)
    assert BD.check_rows([good]) is None                     # 连板高度是天数, 可以大于当日家数
    bad = list(good)
    bad[2] = 2                                               # 涨+跌+平 ≠ 家数
    assert "涨+跌+平" in BD.check_rows([tuple(bad)])
    bad = list(good)
    bad[6] = 2                                               # 强势家数 > 上涨家数
    assert "矛盾" in BD.check_rows([tuple(bad)])
    bad = list(good)
    bad[18] = -1.0                                           # 成交额为负
    assert "数值越界" in BD.check_rows([tuple(bad)])
    outflow = list(good)
    outflow[19] = -3.83e9                                    # 主力净流出: 有进有出, 负数是常态
    assert BD.check_rows([tuple(outflow)]) is None


DAYS = [f"202601{d:02d}" for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]   # 10 个交易日


def _mk_raw(dbs):
    """10 个交易日的小型个股日线库: 沪市主板两只(一只连强三天、一只隔日有行情)、
    深市主板一只、创业板一只、第 4 天才出现的科创板新股、北交所一只"""
    con = RD.raw_connect()
    rows = []

    def row(ts, d, close, amount, high=None):
        # 列序同 tushare daily: open, high, low, close, pre_close, change, pct_chg, vol, amount(千元)
        pct = round((close / 10.0 - 1) * 100, 2)
        return (ts, d, 10.0, high or max(close, 10.0), 10.0, close, 10.0, round(close - 10.0, 2),
                pct, 1000.0, amount)

    for i, d in enumerate(DAYS, 1):
        rows.append(row("600001.SH", d, 11.00 if i <= 3 else 10.10, 1000.0))   # 前 3 天天天封板(+10%)
        if i % 2 == 0:
            rows.append(row("600002.SH", d, 9.50, 500.0))                     # 隔日才有行情(停牌)
        rows.append(row("000001.SZ", d, 10.20 if i % 2 else 9.80, 300.0))
        rows.append(row("300001.SZ", d, 10.10, 200.0))
        if i >= 4:                                           # 科创板新股: 第 4 天上市
            close = 14.40 if i == 4 else (11.20 if i in (8, 9) else 10.50)   # 首日 +44%; 第 5、6 个交易日 +12%
            rows.append(row("688001.SH", d, close, 100.0))
        rows.append(row("830001.BJ", d, 13.00, 50.0))
    con.executemany("INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("""CREATE TABLE IF NOT EXISTS moneyflow(
      ts_code TEXT, trade_date TEXT, buy_elg_amount REAL, sell_elg_amount REAL,
      buy_lg_amount REAL, sell_lg_amount REAL, PRIMARY KEY(ts_code, trade_date))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_mf_tradedate ON moneyflow(trade_date)")
    con.executemany("INSERT OR REPLACE INTO moneyflow VALUES(?,?,?,?,?,?)", [
        # 第 5 天起有资金流; 净额逐日正负交替((买超大+买大)−(卖超大+卖大) = ±3 万元)
        ("600001.SH", d, 1.0, 0.5, 2.0, 0.5) if i % 2 == 0 else ("600001.SH", d, 0.5, 1.0, 0.5, 2.0)
        for i, d in enumerate(DAYS[4:])])
    con.commit()
    con.close()


def _rows(dbs, board=None):
    con = sqlite3.connect(str(dbs["sent"]))
    try:
        sql = "SELECT * FROM board_daily" + (" WHERE board=?" if board else "") + " ORDER BY board, date"
        cols = [c[1] for c in con.execute("PRAGMA table_info(board_daily)")]
        return [dict(zip(cols, r)) for r in con.execute(sql, (board,) if board else ())]
    finally:
        con.close()


def test_build_aggregates_each_day_and_is_idempotent(dbs):
    _mk_raw(dbs)
    assert BD.build(DAYS[0], sqlite3.connect(str(dbs["sent"])),
                    sqlite3.connect(str(dbs["raw"]))) is True
    rows = _rows(dbs)
    # 沪市 / 深市 / 创业板 10 天, 科创板自第 4 天(上市)起; 北交所不在表里
    assert sorted({r["board"] for r in rows}) == ["CYB", "KCB", "SH", "SZ"]
    assert len([r for r in rows if r["board"] != "KCB"]) == 30 and len([r for r in rows if r["board"] == "KCB"]) == 7
    for r in rows:                                           # 算术自检
        assert r["up"] + r["down"] + r["flat"] == r["n"]
        assert r["strong"] <= r["up"] and r["weak"] <= r["down"]
        assert r["streak2"] <= r["strong"] and r["lb2"] <= r["zt"]
    sh = {r["date"]: r for r in rows if r["board"] == "SH"}
    d1, d2, d3, d4 = (f"2026-01-{x}" for x in ("05", "06", "07", "08"))
    # 连续强势 / 连板: 600001 前 3 天天天封板 → 第 2 天起计入"接力"
    assert (sh[d1]["streak_max"], sh[d1]["streak2"], sh[d1]["prev_strong"]) == (1, 0, 0)
    assert (sh[d2]["streak_max"], sh[d2]["streak2"], sh[d2]["prev_strong"]) == (2, 1, 1)
    assert (sh[d3]["lb_max"], sh[d3]["lb2"], sh[d3]["prev_zt"]) == (3, 1, 1)
    assert (sh[d4]["strong"], sh[d4]["prev_strong"]) == (0, 1)
    # 家数: 第 1 天沪市只有 600001; 第 2、4 天多一只隔日有行情的 600002; 第 4 天起多科创板新股
    assert (sh[d1]["n"], sh[d2]["n"], sh[d2]["down"], sh[d4]["n"]) == (1, 2, 1, 3)
    assert sh[d1]["amt"] == pytest.approx(1000 * 1000)      # 千元 → 元
    # 主力净流入: 资金流表 2007 年才有的那种情形 — 之前的日子记 0; 万元 → 元; 净流出为负
    assert sh[d4]["mf_net"] == 0
    assert sh["2026-01-09"]["mf_net"] == pytest.approx(20000.0)    # 资金流表从这一天起有数
    assert sh["2026-01-12"]["mf_net"] == pytest.approx(-20000.0)
    kcb = {r["date"]: r for r in rows if r["board"] == "KCB"}
    assert len(kcb) == 7 and kcb["2026-01-08"]["up"] == 1
    assert kcb["2026-01-08"]["strong"] == 0                  # 上市首日 +44%: 不计强弱势
    assert kcb["2026-01-14"]["strong"] == 0                  # 第 5 个交易日 +12%: 仍在前 5 日无涨跌幅限制期
    assert kcb["2026-01-15"]["strong"] == 1                  # 第 6 个交易日 +12%: 计入
    cyb = [r for r in rows if r["board"] == "CYB"]
    assert all(r["n"] == 1 and r["up"] == 1 for r in cyb)
    n_before = len(rows)
    BD.build(DAYS[0], sqlite3.connect(str(dbs["sent"])), sqlite3.connect(str(dbs["raw"])))
    assert _rows(dbs) == rows and len(_rows(dbs)) == n_before   # 重跑幂等


def test_build_incremental_only_rewrites_from_start(dbs):
    """日增(近 10 日): 只重写 start 起的日子; 连续状态由 start 之前的"预热"日推进而来"""
    _mk_raw(dbs)
    BD.build(DAYS[0], sqlite3.connect(str(dbs["sent"])), sqlite3.connect(str(dbs["raw"])))
    con = sqlite3.connect(str(dbs["sent"]))
    con.execute("UPDATE board_daily SET up=999 WHERE board='SH' AND date='2026-01-05'")
    con.commit()
    con.close()
    BD.build(DAYS[1], sqlite3.connect(str(dbs["sent"])), sqlite3.connect(str(dbs["raw"])))
    rows = _rows(dbs)
    sh = {r["date"]: r for r in rows if r["board"] == "SH"}
    assert sh["2026-01-05"]["up"] == 999                     # start 之前的日子不动
    assert sh["2026-01-06"]["up"] == 1 and sh["2026-01-06"]["up"] != 999
    assert (sh["2026-01-06"]["streak_max"], sh["2026-01-06"]["prev_strong"]) == (2, 1)   # 状态接得上
    assert all(r["up"] != 999 for r in rows if r["date"] > "2026-01-05")


def test_build_without_moneyflow_table(dbs):
    """个股资金流表还没建(新环境): 成交与广度照常聚合, 主力净流入记 0"""
    _mk_raw(dbs)
    con = sqlite3.connect(str(dbs["raw"]))
    con.execute("DROP TABLE moneyflow")
    con.commit()
    con.close()
    BD.build(DAYS[0], sqlite3.connect(str(dbs["sent"])), sqlite3.connect(str(dbs["raw"])))
    rows = _rows(dbs)
    assert len(rows) == 37 and all(r["mf_net"] == 0 for r in rows)


def test_build_direction_follows_prices_not_source_pct(dbs):
    """源端 pct_chg 与价格打架(脏数据)时, 涨跌方向按 收盘/前收 算 —
    照抄 pct_chg 会出现"强势家数 > 上涨家数", 整天的聚合都会被自检拒写"""
    con = RD.raw_connect()
    con.execute("INSERT INTO daily VALUES('600001.SH','20260105',10,11,10,11,10,1,-5.0,1000,1000)")   # +10% 却记成 -5%
    con.execute("INSERT INTO daily VALUES('600002.SH','20260105',10,9.5,9.5,9.5,10,-0.5,5.0,1000,1000)")   # -5% 却记成 +5%
    con.commit()
    con.close()
    BD.build("20260105", sqlite3.connect(str(dbs["sent"])), sqlite3.connect(str(dbs["raw"])))
    sh = _rows(dbs, "SH")[0]
    assert (sh["n"], sh["up"], sh["down"], sh["strong"], sh["weak"]) == (2, 1, 1, 1, 0)


# ════════════ ② 三个指数的日线 ════════════

def _bars(code, dates):
    return pd.DataFrame({"trade_date": dates, "open": [100.0] * len(dates), "high": [101.0] * len(dates),
                         "low": [99.0] * len(dates), "close": [100.5] * len(dates), "vol": [1000.0] * len(dates),
                         "amount": [123456.0] * len(dates), "pct_chg": [0.5] * len(dates),
                         "ts_code": [code] * len(dates)})


def _stored(dbs, code):
    con = sqlite3.connect(str(dbs["sent"]))
    try:
        return con.execute("SELECT date, open, close, volume, amount, pct_chg FROM index_daily "
                           "WHERE code=? ORDER BY date", (code,)).fetchall()
    finally:
        con.close()


def test_fetch_index_extra_tushare_primary(dbs, monkeypatch):
    """tushare 为主: 日期归一、成交额千元→元; 各指数不收自己起点之前的日子"""
    monkeypatch.setattr(TA, "has_token", lambda: True)
    seen = []

    def fake(api, fields, **kw):
        seen.append((api, kw["ts_code"], kw["start_date"]))
        frames = [_bars(kw["ts_code"], ["20031231", "20040102", "20040105"])]
        if kw["ts_code"] == "399006.SZ":
            base = _bars("399006.SZ", ["20100531", "20100601"])   # 基日 05-31: 只有基点收盘值
            base.loc[0, ["open", "high", "low", "close", "vol", "amount", "pct_chg"]] = [
                None, None, None, 1000.0, None, None, None]
            frames.append(base.iloc[:1])
            frames.append(base.iloc[1:2].assign(open=None, high=None, low=None, close=None,
                                                 vol=None, amount=None, pct_chg=None))   # 整行空的
            frames.append(_bars("399006.SZ", ["20100602"]))                         # 正常的交易日
        return frames
    monkeypatch.setattr(TA, "pull_frames", fake)
    con = DB.connect()
    con.execute("INSERT INTO index_daily VALUES('1.000001','2005-01-04',1,1,1,1,10,20,0)")
    con.commit()
    con.close()
    out = FD.fetch_index_extra({"index": {"extra": EXTRA}})
    assert set(out) == {"0.399001", "0.399006", "1.000688"}
    assert all(v["src"] == "tushare" for v in out.values())
    assert sorted(seen) == [("index_daily", "000688.SH", "20191201"), ("index_daily", "399001.SZ", "20040101"),
                            ("index_daily", "399006.SZ", "20090101")]
    assert out["0.399001"]["n"] == 2                          # 2003-12-31 在起点之前, 不收
    assert _stored(dbs, "0.399001") == [("2004-01-02", 100.0, 100.5, 1000.0, 123456000.0, 0.5),
                                        ("2004-01-05", 100.0, 100.5, 1000.0, 123456000.0, 0.5)]
    # 指数基日: 只有基点收盘值(创业板指 2010-05-31 = 1000), 开高低与量额留空; 全空的行不收
    assert _stored(dbs, "0.399006") == [("2010-05-31", None, 1000.0, None, None, None),
                                        ("2010-06-02", 100.0, 100.5, 1000.0, 123456000.0, 0.5)]
    assert len(_stored(dbs, "1.000688")) == 0                 # 科创50 2019-12 才开始
    assert _stored(dbs, "1.000001") == [("2005-01-04", 1, 1, 10, 20, 0)]   # 主力指数不动


def test_fetch_index_extra_fallback_leaves_turnover_empty(dbs, monkeypatch):
    """tushare 失败 → 东财/腾讯回退: 只补库里还没有的日子, 只取开高低收与涨跌幅。
    这两个源给深证成指 / 创业板指的量额是全市场 / 全板块口径, 存进来会被当成指数量能 —
    拿去覆盖已有的行, 一次网络故障就能把整段历史的成交量 / 成交额换成别的口径"""
    monkeypatch.setattr(TA, "has_token", lambda: True)
    monkeypatch.setattr(TA, "pull_frames",
                        lambda api, fields, **kw: [_bars(kw["ts_code"], ["20100601", "20100602"])])
    FD.fetch_index_extra({"index": {"extra": EXTRA[:2]}})          # 先有两天的完整数据
    full = _stored(dbs, "0.399006")
    assert [r[3] for r in full] == [1000.0, 1000.0]
    monkeypatch.setattr(TA, "pull_frames", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("网络")))
    df = pd.DataFrame({"date": ["2010-06-01", "2010-06-02", "2010-06-03"],
                       "open": [1.0, 1.0, 1001.0], "high": [1.0, 1.0, 1006.0], "low": [1.0, 1.0, 999.0],
                       "close": [1.0, 1.0, 1003.0], "volume": [5.4e8, 5.5e8, 5.6e8],
                       "amount": [7.3e11, 7.4e11, 7.5e11], "amp": [0.5] * 3, "pct_chg": [9.9, 9.9, 0.1],
                       "chg": [1.0] * 3, "turnover": [1.0] * 3})   # 回退源给的"量额"与价格都不一样
    monkeypatch.setattr(FD.em_api, "index_kline", lambda secid, beg="": (df, "tencent"))
    out = FD.fetch_index_extra({"index": {"extra": EXTRA[:2]}})
    assert out["0.399001"]["src"] == "fallback:tencent" and out["0.399006"]["n"] == 1   # 只补了新的一天
    assert _stored(dbs, "0.399006") == full + [("2010-06-03", 1001.0, 1003.0, None, None, 0.1)]
    # tushare 恢复后整段重写, 那一天的量额补上
    monkeypatch.setattr(TA, "pull_frames",
                        lambda api, fields, **kw: [_bars(kw["ts_code"], ["20100601", "20100602", "20100603"])])
    FD.fetch_index_extra({"index": {"extra": EXTRA[:2]}})
    assert [r[3:] for r in _stored(dbs, "0.399006")] == [(1000.0, 123456000.0, 0.5)] * 3


def test_fetch_index_extra_keeps_turnover_when_source_row_has_none(dbs, monkeypatch):
    """tushare 某天只给收盘不给量额(或接口缺列): 不把库里已有的量额冲成空"""
    monkeypatch.setattr(TA, "has_token", lambda: True)
    monkeypatch.setattr(TA, "pull_frames",
                        lambda api, fields, **kw: [_bars(kw["ts_code"], ["20100601", "20100602"])])
    FD.fetch_index_extra({"index": {"extra": EXTRA[1:2]}})

    def hole(api, fields, **kw):
        df = _bars(kw["ts_code"], ["20100601", "20100602"])
        df.loc[0, ["vol", "amount"]] = None                  # 这一天接口没给量额
        return [df]
    monkeypatch.setattr(TA, "pull_frames", hole)
    FD.fetch_index_extra({"index": {"extra": EXTRA[1:2]}})
    assert [r[3] for r in _stored(dbs, "0.399006")] == [1000.0, 1000.0]


def test_fetch_index_extra_start_floor_and_no_config(dbs, monkeypatch):
    monkeypatch.setattr(TA, "has_token", lambda: True)
    monkeypatch.setattr(TA, "pull_frames",
                        lambda api, fields, **kw: [_bars(kw["ts_code"], ["20040102", "20100104", "20200102"])])
    FD.fetch_index_extra({"index": {"extra": EXTRA}}, start="20100101")
    assert _stored(dbs, "0.399001") and _stored(dbs, "0.399001")[0][0] == "2010-01-04"   # 下限生效
    assert FD.fetch_index_extra({"index": {}}) == {}                                       # 没配置就不抓


def test_fetch_index_extra_all_sources_down_skips_only_that_index(dbs, monkeypatch):
    """tushare 与东财 / 腾讯都不通: 只跳过这一个指数, 其余指数照常入库"""
    monkeypatch.setattr(TA, "has_token", lambda: True)

    def pull(api, fields, **kw):
        if kw["ts_code"] == "399006.SZ":
            raise RuntimeError("网络")
        return [_bars(kw["ts_code"], ["20200102"])]         # 三个指数的起点之后
    monkeypatch.setattr(TA, "pull_frames", pull)
    monkeypatch.setattr(FD.em_api, "index_kline",
                        lambda secid, beg="": (_ for _ in ()).throw(RuntimeError("全部host失败")))
    out = FD.fetch_index_extra({"index": {"extra": EXTRA}})
    assert out == {"0.399001": {"n": 1, "src": "tushare"},
                   "0.399006": {"n": 0, "src": "none"},
                   "1.000688": {"n": 1, "src": "tushare"}}
    assert len(_stored(dbs, "0.399006")) == 0 and len(_stored(dbs, "0.399001")) == 1


def test_pull_frames_pages_by_first_page_size(monkeypatch):
    """分页取全(自 fetch_stock 提到 tushare_api, 日线与季报表共用): 以首页实际行数为一页"""
    calls = []

    def fake(api, fields, **kw):
        off = kw.get("offset") or 0
        calls.append(off)
        return pd.DataFrame({"x": [off] * min(50, max(0, 120 - off))})   # 服务端每次只给 50 行
    monkeypatch.setattr(TA, "call", fake)
    frames = TA.pull_frames("index_daily", "x", ts_code="399001.SZ")
    assert [len(f) for f in frames] == [50, 50, 20] and calls == [0, 50, 100]
    assert TA.PAGE_ROWS == 6000 and TA.MAX_ROWS == 60000


def test_config_and_fallback_map_cover_the_three_indices():
    cfg = FD.load_cfg()
    extra = cfg["index"]["extra"]
    assert [(x["code"], x["board"]) for x in extra] == [("0.399001", "SZ"), ("0.399006", "CYB"), ("1.000688", "KCB")]
    assert all(x["board"] in BD.BOARDS for x in extra)
    # 各指数起点比评分起点早一个滚动分位窗口(250 个交易日 ≈ 12 个月)
    assert extra[0]["start"] == "20040101" and extra[1]["start"] == "20090101" and extra[2]["start"] == "20191201"
    import em_api
    assert em_api._TS_MAP["0.399006"] == "399006.SZ" and em_api._TS_MAP["1.000688"] == "000688.SH"


# ════════════ ③ 数据管理接入 ════════════

def test_backfill_datasets_registered():
    from server import jobs, market
    for ds in ("index_extra", "board_daily"):
        assert ds in FD.BACKFILLS and ds in jobs.BACKFILL_DATASETS
    assert FD.BACKFILLS["index_extra"][1] == [("index_daily", None, "date")]
    assert FD.BACKFILLS["index_extra"][3] == "20040101"
    assert jobs.BACKFILL_DEFAULT_START["board_daily"] == "20050101"
    assert FD.BACKFILLS["board_daily"][1] == [("board_daily", None, "date")]
    # 资产清单: 三个指数各一行(完成度与缺口按指数各自算) + 板块日表
    codes = [s[0] for s in market._ASSET_DATE_TABLES]
    for c in ("index_399001", "index_399006", "index_000688", "board_daily"):
        assert c in codes and market._REPAIR_DS[c] == ("index_extra" if c.startswith("index_") else "board_daily")
    row = next(s for s in market._ASSET_DATE_TABLES if s[0] == "index")
    assert row[3] == "code IN ('1.000001','0.399106')"      # 主力行只认两个代码
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    for frag in ("ds: 'index_extra', label: '指数日线·深证成指", "asset: 'index_399006'",
                 "asset: 'index_000688'", "ds: 'board_daily', label: '板块广度与结构",
                 "asset: 'index_val'", "国家队口径8只+深成指ETF"):
        assert frag in html, frag


def test_update_chain_mounts_new_steps(monkeypatch):
    from server import jobs
    src = (CORE / "fetch_daily.py").read_text(encoding="utf-8")
    assert '("boards", lambda: __import__("boards").build(start))' in src
    assert "fetch_index_extra(cfg)" in src
    seen = []
    monkeypatch.setattr(jobs, "_run_steps", lambda append, steps: seen.append(steps) or 0)
    for task in (jobs.task_update, jobs.task_recalc):
        task(lambda line: None)
    # MI-4 加了两个数据集(板块两融余额、成分股广度), 也在"抓取"这一步里增量
    assert seen[0][0][2] == ("index", "index_extra", "board_daily", "board_margin", "index_breadth",
                             "mood", "sector_daily")
    assert seen[1][0][2] == ("sentiment",)                  # 评分重算不抓数据


def test_fetch_sw_daily_runs_boards_after_raw(monkeypatch, dbs):
    order = []
    fake_mods = {"raw_daily": type("M", (), {"backfill": staticmethod(lambda s: order.append("raw") or True)})(),
                 "boards": type("M", (), {"build": staticmethod(lambda s: order.append("boards:" + s) or True)})(),
                 "sw_pool": type("M", (), {"build": staticmethod(lambda s: True), "overlay": staticmethod(lambda s: True)})(),
                 "sw_fflow": type("M", (), {"backfill": staticmethod(lambda s: True), "agg": staticmethod(lambda s: True)})(),
                 "sw_index_comp": type("M", (), {"build": staticmethod(lambda s: True)})(),
                 "sw_bars": type("M", (), {"refresh": staticmethod(lambda: True)})()}
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name in fake_mods and not a:                    # 只拦 fetch_sw_daily 里的 __import__("x")
            return fake_mods[name]
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert FD.fetch_sw_daily(lookback=10) is True
    assert order[0] == "raw" and order[1].startswith("boards:")   # 先有个股日线, 再聚合板块
    import datetime as dt
    assert order[1] == "boards:" + (dt.date.today() - dt.timedelta(days=10)).strftime("%Y%m%d")


def test_market_datasets_and_gap_scan(dbs, monkeypatch):
    """数据管理页: 三个指数与板块表各成一行(区间 / 完成度 / 健康); 缺的一天能被体检报出且有补全通道"""
    from server import market
    monkeypatch.setattr(market, "SENT_DB", dbs["sent"])
    con = DB.connect()
    con.execute(BD.SCHEMA)                                  # 板块日表(回补后才有)
    days = ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20"]
    for d in days:
        con.execute("INSERT INTO index_daily VALUES('1.000001',?,1,1,1,1,1,1,0)", (d,))
        con.execute("INSERT INTO index_daily VALUES('0.399106',?,1,1,1,1,1,1,0)", (d,))
        con.execute("INSERT INTO index_daily VALUES('0.399001',?,1,1,1,1,1,100,0)", (d,))
        con.execute("INSERT INTO index_daily VALUES('1.000688',?,1,1,1,1,1,100,0)", (d,))
        for b in ("SH", "SZ", "CYB", "KCB"):
            con.execute("INSERT INTO board_daily(board,date,n,up,down,flat) VALUES(?,?,10,5,4,1)", (b, d))
    for d in days[1:]:                                       # 创业板指 17 日起才有, 且缺 18 日
        if d != "2026-11-18":
            con.execute("INSERT INTO index_daily VALUES('0.399006',?,1,1,1,1,1,100,0)", (d,))
    con.commit()
    con.close()
    rows = {r["code"]: r for r in market.datasets()}
    assert rows["index"]["rows"] == 10 and rows["index"]["first"] == "2026-11-16"   # 不混进三个新指数
    assert rows["index_399001"]["first"] == "2026-11-16" and rows["index_399001"]["completeness"] == 100
    assert rows["index_399006"]["first"] == "2026-11-17" and rows["index_399006"]["completeness"] == 75
    assert rows["index_000688"]["rows"] == 5 and rows["index_000688"]["source"] == "tushare"
    assert rows["board_daily"]["rows"] == 20 and rows["board_daily"]["completeness"] == 100
    monkeypatch.setattr(market, "_trading_days_since", lambda n: list(days))
    gaps = {a["code"]: a for a in market.scan_gaps(7)["assets"]}
    # 创业板指: 16 日没有行(本例里它 17 日才"上市"), 18 日是缺口 — 体检都报得出, 且有补全通道
    assert gaps["index_399006"]["missing"] == ["2026-11-16", "2026-11-18"] and gaps["index_399006"]["has_path"]
    assert gaps["index_399001"]["missing"] == [] and gaps["board_daily"]["missing"] == []
    stats = market.pipeline_stats()
    assert stats["index_extra"]["rows"] == 13 and stats["board_daily"]["last"] == "2026-11-20"   # 5+3+5


def test_run_backfill_measures_new_rows(dbs, monkeypatch):
    """统一补全入口: 板块日表按日计数, 新增行数与日期范围如实回报"""
    import backup as BK
    snaps = []
    monkeypatch.setattr(BK, "snapshot",                      # 不然会对生产行情库做真实快照
                        lambda asset, btype="manual": snaps.append((asset, btype)))
    _mk_raw(dbs)
    res = FD.run_backfill("board_daily", DAYS[0])
    assert res["status"] == "ok" and res["label"].startswith("板块广度与结构")
    assert res["rows_written"] == 37 and res["date_from"] == "2026-01-05" and res["date_to"] == "2026-01-16"
    assert snaps == [("sentiment", "pre-backfill")]         # 回补前留了快照
    again = FD.run_backfill("board_daily", DAYS[0])
    assert again["status"] == "ok" and again["rows_written"] == 0   # 幂等: 重跑不报"新增"


def test_data_page_lists_new_datasets(server_app):
    html = server_app["client"].get("/admin/data").get_data(as_text=True)
    assert "index_extra" in html and "board_daily" in html
