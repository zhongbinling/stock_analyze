# -*- coding: utf-8 -*-
"""MI-4 情绪图多指数 · 板块两融余额与成分股广度(docs/开发计划-情绪图多指数.md 第 5 节 MI-4; 用户 11-27 确认只做这两项)

① core/board_margin.py: tushare 个股两融明细按代码前缀聚合成 创业板 / 科创板 的两融余额(标的只数另存);
② core/index_breadth.py: 月度成分股快照落库(index_weight) + 由本地个股日线算"上涨占比 / 站上 20 日线占比"(index_breadth);
③ 数据管理接入: 资产清单 / 补全数据集 / 体检修复映射 / 源端滞后 / 更新链, 每一处都要登记。
所有用例都跑在临时库上(行情库与个股日线库都指向 tmp_path), 不调真实接口。"""
import math
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import board_margin as BM       # noqa: E402
import db as DB                 # noqa: E402
import fetch_daily as FD        # noqa: E402
import index_breadth as IB      # noqa: E402
import raw_daily as RD          # noqa: E402
import tushare_api as TA        # noqa: E402

EXTRA = [
    {"code": "0.399001", "name": "深证成指", "ts_code": "399001.SZ", "board": "SZ"},
    {"code": "0.399006", "name": "创业板指", "ts_code": "399006.SZ", "board": "CYB"},
    {"code": "1.000688", "name": "科创50", "ts_code": "000688.SH", "board": "KCB"},
]
CFG = {"index": {"extra": EXTRA}}


@pytest.fixture()
def dbs(tmp_path, monkeypatch):
    spath, rpath = tmp_path / "sentiment.db", tmp_path / "raw.db"
    monkeypatch.setattr(DB, "DB_PATH", spath)
    monkeypatch.setattr(RD, "RAW_DB", rpath)
    return {"sent": spath, "raw": rpath}


def _sql(path, sql, args=()):
    con = sqlite3.connect(str(path))
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _tdays(days, code="1.000001"):
    """在主力指数的日线表里登记交易日(板块两融按它判断哪些天该有数据)"""
    con = DB.connect()
    for d in days:
        con.execute("INSERT INTO index_daily VALUES(?,?,1,1,1,1,1,1,0)", (code, d))
    con.commit()
    con.close()


def _detail(d_ymd, codes, base=1.0):
    """一天的个股两融明细: 每只 rzye = base × 1e8, rqye = base × 1e6, rzrqye = base × 1.01e8"""
    n = len(codes)
    return pd.DataFrame({"trade_date": [d_ymd] * n, "ts_code": list(codes),
                         "rzye": [base * 1e8] * n, "rqye": [base * 1e6] * n,
                         "rzrqye": [base * 1.01e8] * n, "rzmre": [base * 1e7] * n})


# ════════════ ① 板块两融余额 ════════════

def test_aggregate_sums_board_stocks_only():
    df = pd.DataFrame({
        "trade_date": ["20261126"] * 8,
        "ts_code": ["300001.SZ", "300002.SZ", "300009.SZ", "688001.SH", "510300.SH", "159915.SZ", "600001.SH", "000001.SZ"],
        "rzye": [100.0, 200.0, float("nan"), 300.0, 1e6, 2e6, 5.0, 1.0],
        "rqye": [1.0, 2.0, float("nan"), 3.0, 1.0, 1.0, 0.1, 0.0],
        "rzrqye": [101.0, float("nan"), float("nan"), 303.0, 1e6 + 1, 2e6 + 1, 5.1, 1.0],
        "rzmre": [10.0, 20.0, 5.0, 30.0, 9.0, 9.0, 1.0, 0.5]})
    out = BM.aggregate(df)
    # 创业板 = 30 开头的个股(300009 三项余额都空, 不算标的、也不加钱); ETF(510300 / 159915)、主板个股都不算
    assert out["CYB"] == pytest.approx((300.0, 3.0, 303.0, 30.0, 2))
    assert out["KCB"] == pytest.approx((300.0, 3.0, 303.0, 30.0, 1))
    # 源端 rzrqye 为空 → 用 融资 + 融券 补(2024-01-02 实测有 1 行如此)
    df.loc[1, "rzrqye"] = float("nan")
    assert BM.aggregate(df)["CYB"] == pytest.approx((300.0, 3.0, 303.0, 30.0, 2))
    # 当天某个板块一只个股都没有 → 这个板块不出现(那天的行不写); 只有 ETF / 空表 → 空
    only_etf = df.iloc[4:]
    assert BM.aggregate(only_etf) == {}
    assert BM.aggregate(df.iloc[:0]) == {} and BM.aggregate(None) == {}
    # 全表三项余额都空的个股: 不算标的
    dead = _detail("20261126", ["300001.SZ"], base=1.0)
    dead[["rzye", "rqye", "rzrqye"]] = float("nan")
    assert BM.aggregate(dead) == {}
    assert set(BM.BOARDS) == {"CYB", "KCB"} and BM.FIRST == {"CYB": "20130131", "KCB": "20190722"}


def test_check_rows_rejects_bad_batch(dbs):
    good = ("CYB", "2026-11-26", 3.0e10, 5.0e8, 3.05e10, 1.0e9, 900)
    assert BM.check_rows([good]) is None
    for i, bad_v, frag in ((0, "SH", "未知板块"), (1, "20261126", "日期格式"), (2, -1.0, "余额为负"),
                           (4, 2.0e10, "两融余额"), (5, -1.0, "余额为负"), (6, 0, "标的只数")):
        bad = list(good)
        bad[i] = bad_v
        assert frag in BM.check_rows([tuple(bad)]), (i, bad_v)
    # 与交易所汇总比(margin_daily): 板块合计不得超过所在交易所当天的两融余额; 没有这一天的汇总就不比
    con = DB.connect()
    con.execute("INSERT INTO margin_daily(date,szse_rzrqye,sse_rzrqye) VALUES('2026-11-26', 3.2e10, 9.0e10)")
    con.commit()
    con.close()
    con = sqlite3.connect(str(dbs["sent"]))
    assert BM.check_rows([good], con) is None                       # 3.05e10 < 3.2e10
    assert "超过所在交易所" in BM.check_rows([("KCB", "2026-11-26", 9.9e10, 0.0, 9.9e10, 0.0, 5)], con)
    assert BM.check_rows([("KCB", "2026-11-25", 1.0, 0.0, 1.0, 0.0, 1)], con) is None
    con.close()


def test_fetch_day_pages_until_a_short_page(monkeypatch):
    total, calls = [4500], []

    def fake_call(api, fields, **kw):
        off = kw.get("offset", 0)
        calls.append((api, kw["trade_date"], kw.get("limit"), off))
        n = max(0, min(BM.PAGE, total[0] - off))
        return pd.DataFrame({"trade_date": ["20261126"] * n,
                             "ts_code": ["%06d.SZ" % (300000 + off + i) for i in range(n)],
                             "rzye": [1.0] * n, "rqye": [0.0] * n, "rzrqye": [1.0] * n, "rzmre": [0.0] * n})
    monkeypatch.setattr(BM.T, "call", fake_call)
    df = BM.fetch_day("20261126")
    assert len(df) == 4500 and df["ts_code"].is_unique            # 两页接上, 没有重复
    assert calls == [("margin_detail", "20261126", 4000, 0), ("margin_detail", "20261126", 4000, 4000)]
    assert BM.PAGE == 4000          # 低于已经见过的单页行数(4492): "不足一页即最后一页"才不会被服务端上限骗
    calls.clear()
    total[0] = 3999
    assert len(BM.fetch_day("20261126")) == 3999 and len(calls) == 1      # 不足一页: 只调一次
    total[0] = 0
    assert BM.fetch_day("20261126").empty and len(calls) == 2

    def bad(days, codes):
        n = len(days)
        return lambda api, fields, **kw: pd.DataFrame({
            "trade_date": days, "ts_code": codes, "rzye": [1.0] * n, "rqye": [0.0] * n,
            "rzrqye": [1.0] * n, "rzmre": [0.0] * n})
    monkeypatch.setattr(BM.T, "call", bad(["20261126", "20261127"], ["300001.SZ", "300002.SZ"]))
    with pytest.raises(RuntimeError, match="别的交易日"):
        BM.fetch_day("20261126")                                   # 接口把别的日子混了进来
    monkeypatch.setattr(BM.T, "call", bad(["20261126", "20261126"], ["300001.SZ", "300001.SZ"]))
    with pytest.raises(RuntimeError, match="个股重复"):
        BM.fetch_day("20261126")                                   # 翻页错位: 同一只出现两次(会被加两遍)


MDAYS = ["2013-01-30", "2013-01-31", "2013-02-01", "2013-02-04", "2013-02-05", "2019-07-19", "2019-07-22"]


def _detail_for(d8):
    """接口对某一天的返回: 2013-01-31 起有创业板个股(6 只), 2019-07-22 起再加科创板(25 只); 更早只有主板"""
    codes = ["600001.SH", "000001.SZ"]
    if d8 >= "20130131":
        codes += ["3000%02d.SZ" % i for i in range(6)]
    if d8 >= "20190722":
        codes += ["6880%02d.SH" % i for i in range(25)]
    return _detail(d8, codes, base=2.0)


def _bm_rows(dbs, board=None):
    con = sqlite3.connect(str(dbs["sent"]))
    try:
        cols = [c[1] for c in con.execute("PRAGMA table_info(board_margin)")]
        sql = "SELECT * FROM board_margin" + (" WHERE board=?" if board else "") + " ORDER BY board, date"
        return [dict(zip(cols, r)) for r in con.execute(sql, (board,) if board else ())]
    finally:
        con.close()


def test_run_fills_missing_days_from_each_board_s_first_day(dbs, monkeypatch):
    _tdays(MDAYS)
    calls = []

    def fake_call(api, fields, **kw):
        calls.append(kw["trade_date"])
        return _detail_for(kw["trade_date"])
    monkeypatch.setattr(BM.T, "call", fake_call)
    assert BM.run("20130101") is True
    rows = _bm_rows(dbs)
    # 创业板自 2013-01-31(首次有标的)起每个交易日一行; 科创板自 2019-07-22 起;
    # 2013-01-30 两个板块都还没有标的: 不取、不写
    assert [(r["board"], r["date"]) for r in rows] == [
        ("CYB", "2013-01-31"), ("CYB", "2013-02-01"), ("CYB", "2013-02-04"), ("CYB", "2013-02-05"),
        ("CYB", "2019-07-19"), ("CYB", "2019-07-22"), ("KCB", "2019-07-22")]
    assert calls == ["20130131", "20130201", "20130204", "20130205", "20190719", "20190722"]
    first = rows[0]
    assert (first["rzye"], first["rqye"], first["rzmre"], first["n"]) == (1.2e9, 1.2e7, 1.2e8, 6)   # 单位元
    assert first["rzrqye"] == pytest.approx(1.212e9)
    assert _bm_rows(dbs, "KCB")[0]["n"] == 25
    calls.clear()
    assert BM.run("20130101") is True and calls == []              # 幂等: 没有缺的就不再调接口
    # start 早于首次有标的日子: 那些日子照样不取
    assert BM.run("20100101") is True and calls == []


def test_run_keeps_going_past_a_failed_day_and_resumes(dbs, monkeypatch):
    _tdays(MDAYS)
    fail, calls = {"20130201"}, []

    def fake_call(api, fields, **kw):
        calls.append(kw["trade_date"])
        if kw["trade_date"] in fail:
            raise RuntimeError("限频")
        return _detail_for(kw["trade_date"])
    monkeypatch.setattr(BM.T, "call", fake_call)
    assert BM.run("20130101") is False                             # 有一天没取到: 如实报失败
    assert [r["date"] for r in _bm_rows(dbs, "CYB")] == ["2013-01-31", "2013-02-04", "2013-02-05", "2019-07-19", "2019-07-22"]
    assert len(_bm_rows(dbs, "KCB")) == 1                          # 失败那天空着, 后面的照常
    fail.clear()
    calls.clear()
    assert BM.run("20130101") is True and calls == ["20130201"]    # 重跑只补缺的那一天


def test_run_stops_after_five_consecutive_failures(dbs, monkeypatch):
    _tdays(MDAYS)
    calls = []

    def boom(api, fields, **kw):
        calls.append(kw["trade_date"])
        raise RuntimeError("网络")
    monkeypatch.setattr(BM.T, "call", boom)
    assert BM.run("20130101") is False
    assert len(calls) == 5 and _bm_rows(dbs) == []                 # 6 天要取, 连续 5 天失败就停


def test_run_does_not_write_a_day_the_guard_rejects(dbs, monkeypatch):
    _tdays(MDAYS)
    monkeypatch.setattr(BM.T, "call", lambda api, fields, **kw: _detail_for(kw["trade_date"]))
    real = BM.check_rows

    def picky(rows, conn=None):
        return "模拟: 超过所在交易所" if rows and rows[0][1] == "2013-02-04" else real(rows, conn)
    monkeypatch.setattr(BM, "check_rows", picky)
    assert BM.run("20130101") is False
    have = [r["date"] for r in _bm_rows(dbs, "CYB")]
    assert "2013-02-04" not in have and len(have) == 5             # 被拒的那天不写, 其余照常


def test_run_does_not_write_half_a_day_when_only_one_exchange_comes_back(dbs, monkeypatch, capsys):
    """某一天源端只回了深市的行(上交所发布晚 / 接口缺行): 这一天整批不写。只写创业板那一行的话, 科创板那一行
    会永久留洞, 而"这一天有没有行"的体检看不出来。这一天留到下次再取, 取全了两个板块一起写"""
    _tdays(MDAYS)
    half, calls = {"20190722"}, []

    def fake_call(api, fields, **kw):
        calls.append(kw["trade_date"])
        df = _detail_for(kw["trade_date"])
        if kw["trade_date"] in half:                                # 只有深市的行(主板 + 创业板), 没有科创板
            return df[df["ts_code"].str.endswith(".SZ")]
        return df
    monkeypatch.setattr(BM.T, "call", fake_call)
    assert BM.run("20130101") is False
    assert "2019-07-22" not in [r["date"] for r in _bm_rows(dbs)]    # 半截的一天: 两个板块都不写
    assert len(_bm_rows(dbs, "CYB")) == 5 and _bm_rows(dbs, "KCB") == []
    assert "不写半截数据" in capsys.readouterr().out
    half.clear()
    calls.clear()
    assert BM.run("20130101") is True and calls == ["20190722"]      # 源端回全了: 这一天两个板块一起补上
    assert [r["board"] for r in _bm_rows(dbs) if r["date"] == "2019-07-22"] == ["CYB", "KCB"]


def test_run_reports_days_still_missing_after_an_empty_response(dbs, monkeypatch, capsys):
    """接口对某一天返回空表不算失败(两融 T+1 才发布, 日增时"昨天还没出"是常态), 但日志里要写明"取过之后还缺
    哪几天" — 手动补全时看得出哪些天其实没补上, 而不是只有一个 ok"""
    _tdays(MDAYS)
    empty = {"20130201"}
    monkeypatch.setattr(BM.T, "call", lambda api, fields, **kw: (
        _detail(kw["trade_date"], []) if kw["trade_date"] in empty else _detail_for(kw["trade_date"])))
    assert BM.run("20130101") is True
    out = capsys.readouterr().out
    assert "取过之后仍缺 1 个交易日(源端还没有这一天的数据" in out and "20130201" in out
    assert len(_bm_rows(dbs, "CYB")) == 5
    empty.clear()
    assert BM.run("20130101") is True and "取过之后仍缺" not in capsys.readouterr().out


def test_refresh_looks_back_and_skips_today(dbs, monkeypatch):
    days = [f"2026-11-{d}" for d in (13, 16, 17, 18, 19, 20, 23, 24, 25, 26, 27)]
    _tdays(days)

    class _D(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 27)
    monkeypatch.setattr(BM, "date", _D)
    monkeypatch.setattr(BM.T, "has_token", lambda: True)
    calls = []

    def fake_call(api, fields, **kw):
        calls.append(kw["trade_date"])
        return _detail_for(kw["trade_date"])
    monkeypatch.setattr(BM.T, "call", fake_call)
    assert BM.refresh(lookback_days=10) is True
    # 回看 10 个自然日 = 11-17 ~ 11-26(昨天): 13 / 16 不在窗口里; 今天(27)两融还没发布, 不取
    assert calls == ["20261117", "20261118", "20261119", "20261120", "20261123", "20261124", "20261125", "20261126"]
    assert BM.refresh(lookback_days=10) is True and len(calls) == 8     # 再跑: 没有缺的, 不调接口


def test_refresh_and_backfill_skip_without_token(monkeypatch, capsys):
    monkeypatch.setattr(BM.T, "has_token", lambda: False)
    monkeypatch.setattr(BM.T, "call", lambda *a, **k: pytest.fail("不应调接口"))
    assert BM.refresh() is True and BM.backfill("20130131") is True
    assert capsys.readouterr().out.count("无 tushare token") == 2


def test_run_backfill_measures_board_margin_rows(dbs, monkeypatch):
    import backup as BK
    snaps = []
    monkeypatch.setattr(BK, "snapshot", lambda asset, btype="manual": snaps.append((asset, btype)))
    _tdays(MDAYS)
    monkeypatch.setattr(BM.T, "has_token", lambda: True)
    monkeypatch.setattr(BM.T, "call", lambda api, fields, **kw: _detail_for(kw["trade_date"]))
    res = FD.run_backfill("board_margin", "20130101")
    assert res["status"] == "ok" and res["label"].startswith("板块两融余额")
    assert res["rows_written"] == 7 and res["date_from"] == "2013-01-31" and res["date_to"] == "2019-07-22"
    assert snaps == [("sentiment", "pre-backfill")]                # 回补前留了快照
    again = FD.run_backfill("board_margin")                        # 不给 start: 用默认起始日 20130131
    assert again["status"] == "ok" and again["rows_written"] == 0 and again["date_from"] == "2013-01-31"


# ════════════ ② 指数成分股与成分股广度 ════════════

TD = ["202601%02d" % i for i in range(1, 31)]          # 30 个交易日(只要求递增)
A, B, C, D = "300001.SZ", "300002.SZ", "300003.SZ", "688001.SH"


def test_stock_flags_up_and_ma20_on_adjusted_series():
    d = ["202601%02d" % i for i in range(1, 31)]
    # 前 25 天 10 元平盘; 第 26 天 10 送 10: 昨收(除权价)5 元、收 5.1; 之后 5.1 / 5.3 / 5.2
    bars = pd.DataFrame({"trade_date": d,
                         "pre_close": [10.0] * 25 + [5.0, 5.1, 5.1, 5.3, 5.2],
                         "close": [10.0] * 25 + [5.1, 5.1, 5.3, 5.3, 5.2]})
    f = IB.stock_flags(bars)
    assert list(f.columns) == ["up", "above"] and len(f) == 30
    assert f["up"].tolist() == [0.0] * 25 + [1.0, 0.0, 1.0, 0.0, 0.0]   # 方向 = 收盘 对 昨收(除权价)
    above = f["above"].tolist()
    assert all(math.isnan(x) for x in above[:19])                        # 不满 20 个交易日没有 20 日线
    assert above[19:25] == [0.0] * 6                                     # 平盘: 收盘 = 均线, 不算站上
    # 除权之后: 复权序列(1.02 …)一直高于自己的 20 日均值 → 站上。
    # 拿未复权收盘去比(5.1 对 9.77)会判成跌破 — 这是用"收盘 ÷ 昨收"连乘还原复权序列的原因
    assert above[25:] == [1.0] * 5
    # 昨收无效(空 / 0)的行不参与
    bars.loc[3, "pre_close"] = None
    bars.loc[7, "pre_close"] = 0.0
    f2 = IB.stock_flags(bars)
    assert len(f2) == 28 and "20260104" not in f2.index and "20260108" not in f2.index
    assert IB.MA_N == 20
    assert IB.stock_flags(bars.iloc[:0]).empty


def test_stock_flags_split_on_the_oldest_slot_of_the_window():
    """除权日恰好落在 20 日窗口最老一格的那一天: 窗口里的 20 个收盘分属除权前后, 仍要按复权口径比(走复权序列);
    再下一天起窗口里不再有除权, 回到按"分"的整数比较。把"窗口里有没有除权"少算一格(rolling(MA_N - 2))的话,
    这一天会被当成没有除权、拿未复权的 10.00 去比 4.90 — 判反(独立评审指出的没测到的边界)"""
    closes = [10.0] * 40 + [4.90] * 18 + [5.05] * 6          # 40 根 10.00 平盘 → 10 送 10 → 18 根 4.90 → 5.05
    pres = [9.9] + closes[:-1]
    pres[40] = 5.00                                           # 第 41 根(下标 40): 昨收是除权价
    d = list(pd.bdate_range("2026-01-05", periods=len(closes)).strftime("%Y%m%d"))
    above = IB.stock_flags(pd.DataFrame({"trade_date": d, "pre_close": pres, "close": closes}))["above"].tolist()
    assert above[38:44] == [0.0] * 6 and above[57] == 0.0     # 除权前后都是平盘(复权序列不动): 收盘 = 均线
    # 下标 58 的窗口 = 下标 39 ~ 58, 除权日(40)在最老一格: 复权口径 5.05 > (5.00 + 18 × 4.90 + 5.05) ÷ 20 = 4.9125
    assert above[58] == 1.0
    assert above[59:64] == [1.0] * 5                          # 再下一天起窗口里没有除权: 整数比较, 结果相同


def _tie_series(head, x, pre_x, c):
    """个股日线: 先是 head 这段小幅上下的收盘, 之后 18 天收 10.00, 一天收 x(昨收 pre_x), 最后一天收 c。
    最后一天的 20 日窗口 = 18 个 10.00 + x + c。head 的两组取值是用脚本搜出来的: 拿它们做"直接比较复权序列"的
    老写法, 至少有一个起点会把下面这个"相等"判成"略大"(差 2.2e-16)"""
    closes = list(head) + [10.0] * 18 + [x, c]
    pres = [9.9] + closes[:-1]
    pres[-2] = pre_x
    d = list(pd.bdate_range("2026-01-05", periods=len(closes)).strftime("%Y%m%d"))
    return pd.DataFrame({"trade_date": d, "pre_close": pres, "close": closes}), d[-1]


HEAD_A = [10.0, 9.95, 9.94, 9.96, 10.0, 9.95, 10.01, 10.03, 10.09, 10.08, 10.14, 10.09, 10.06, 10.1, 10.14, 10.11,
          10.1, 10.09, 10.08, 10.1, 10.09, 10.13, 10.06, 10.07, 10.04, 10.01, 9.98, 10.02, 9.99, 9.94, 9.98, 10.02]
HEAD_B = [10.0, 9.95, 9.9, 9.85, 9.84, 9.83, 9.76, 9.69, 9.71, 9.77, 9.78, 9.8, 9.81, 9.8, 9.79, 9.72, 9.73, 9.66,
          9.59, 9.63, 9.67, 9.6, 9.53, 9.55, 9.5, 9.51, 9.53, 9.59]


def test_stock_flags_close_exactly_on_the_ma_is_not_above_whatever_the_series_starts_at():
    """收盘恰好等于自己的 20 日均价(股价只有两位小数, 这不罕见 — 11-27 生产数据上 34 天里深证成指的成分股出现过
    2 次): 等于不算站上, 而且不能因为"从哪一天开始累乘"而变。复权序列是连乘出来的, 带 1e-16 量级的浮点误差,
    直接比较会把"相等"随机判成"略大"(全量重算与日增量算因此对不上)。
    窗口里没有除权: 按"分"的整数精确比较; 有除权: 比复权序列并留相对容差。差一分钱的两种情形照常分得出"""
    # 没有除权: 18 × 10.00 + 11.90 + 10.10 = 202.00 = 20 × 10.10
    # 有除权(倒数第二天除息, 昨收 9.90): 除权前 18 天的 10.00 折成 9.90, 18 × 9.90 + 13.70 + 10.10 = 202.00 = 20 × 10.10
    for head, x, pre_x, what in ((HEAD_A, 11.90, 10.00, "窗口里没有除权"), (HEAD_B, 13.70, 9.90, "窗口里有除权")):
        for c, want in ((10.09, 0.0), (10.10, 0.0), (10.11, 1.0)):
            bars, last = _tie_series(head, x, pre_x, c)
            got = [IB.stock_flags(bars.iloc[k:]).at[last, "above"] for k in (0, 5, 20, 25)]      # 从不同的一天开始
            assert got == [want] * 4, (what, c, got)
    bars, last = _tie_series(HEAD_A, 11.90, 10.00, 10.10)
    full = IB.stock_flags(bars)
    assert len(full) == 52 and math.isnan(full["above"].iloc[18]) and not math.isnan(full["above"].iloc[19])   # 第 20 天起才有 20 日线
    assert full["up"].tolist()[-2:] == [1.0, 0.0] and full["above"].iloc[-1] == 0.0


def _mk_bars(suspend=()):
    """个股日线库: A、B 逐日上涨(每天 +0.1), C 逐日下跌, D 平盘; B 在 suspend 的那些天下标停牌(没有行)"""
    con = RD.raw_connect()
    rows = []
    for i, d in enumerate(TD):
        rows.append((A, d, 10.0 + 0.1 * i, 9.9 if i == 0 else 10.0 + 0.1 * (i - 1)))
        if i not in suspend:
            rows.append((B, d, 20.0 + 0.1 * i, 19.9 if i == 0 else 20.0 + 0.1 * (i - 1)))
        rows.append((C, d, 30.0 - 0.1 * i, 30.1 if i == 0 else 30.0 - 0.1 * (i - 1)))
        rows.append((D, d, 5.0, 5.0))
        rows.append(("600001.SH", d, 8.0, 8.0))                  # 不是任何指数的成分股
    con.executemany("INSERT OR REPLACE INTO daily(ts_code,trade_date,close,pre_close) VALUES(?,?,?,?)", rows)
    con.commit()
    con.close()


def _mk_snaps(ts, snaps):
    con = DB.connect()
    con.execute(IB.W_SCHEMA)
    for d, codes in snaps:
        con.executemany("INSERT OR REPLACE INTO index_weight VALUES(?,?,?,?)",
                        [(ts, d, c, 1.0) for c in codes])
    con.commit()
    con.close()


def _breadth(dbs, code):
    con = sqlite3.connect(str(dbs["sent"]))
    try:
        cols = [c[1] for c in con.execute("PRAGMA table_info(index_breadth)")]
        return [dict(zip(cols, r)) for r in con.execute(
            "SELECT * FROM index_breadth WHERE code=? ORDER BY date", (code,))]
    finally:
        con.close()


def _tuple(r):
    return (r["n"], r["up"], r["n20"], r["above20"], r["nc"])


def test_build_breadth_from_snapshots_and_daily_bars(dbs):
    _mk_bars(suspend=(8,))
    _mk_snaps("399006.SZ", [("2026-01-06", [A, B]), ("2026-01-21", [A, B, C])])   # 第 6 天 2 只, 第 21 天起 3 只
    _mk_snaps("000688.SH", [("2026-01-11", [D])])
    con = sqlite3.connect(str(dbs["raw"]))
    con.execute("DELETE FROM daily WHERE ts_code=? AND trade_date=?", (D, TD[15]))   # 第 16 天 D 没有行情
    con.commit()
    con.close()
    out = IB.build("20260101", cfg=CFG)
    assert out == {"0.399001": 0, "0.399006": 25, "1.000688": 19}  # 深证成指没有快照: 跳过
    cyb = {r["date"][8:]: r for r in _breadth(dbs, "0.399006")}
    assert len(cyb) == 25 and min(cyb) == "06"                     # 第 6 天(第一份快照)起才有; 之前的日子不出行
    assert _tuple(cyb["06"]) == (2, 2, 0, 0, 2)                   # 都涨; 不满 20 天没有 20 日线
    assert _tuple(cyb["09"]) == (1, 1, 0, 0, 2)                   # B 停牌: 不计
    assert _tuple(cyb["19"]) == (2, 2, 0, 0, 2)                   # A 第 19 天、B(停过牌)更少: 都还没有 20 日线
    assert _tuple(cyb["20"]) == (2, 2, 1, 1, 2)                   # A 的第 20 个交易日
    assert _tuple(cyb["21"]) == (3, 2, 3, 2, 3)                   # 换了快照: 多了 C(下跌, 已有 20 日线); B 的第 20 个交易日
    assert _tuple(cyb["30"]) == (3, 2, 3, 2, 3)                   # C 也有了 20 日线(在均线之下)
    kcb = {r["date"][8:]: r for r in _breadth(dbs, "1.000688")}
    assert len(kcb) == 19 and "16" not in kcb                     # 当天唯一的成分股没有行情: 这天不出行
    assert _tuple(kcb["11"]) == (1, 0, 0, 0, 1)                   # 平盘: 收盘 = 昨收, 不算上涨
    assert _tuple(kcb["30"]) == (1, 0, 1, 0, 1)                   # 有了 20 日线: 平盘在均线上, 不算站上
    assert _breadth(dbs, "0.399001") == []
    for r in _breadth(dbs, "0.399006") + _breadth(dbs, "1.000688"):
        assert IB.check_rows([tuple(r.values())], r["code"]) is None


def test_build_is_idempotent_and_only_rewrites_from_start(dbs):
    _mk_bars()
    _mk_snaps("399006.SZ", [("2026-01-06", [A, B])])
    IB.build("20260101", cfg=CFG)
    before = _breadth(dbs, "0.399006")
    assert len(before) == 25
    IB.build("20260101", cfg=CFG)
    assert _breadth(dbs, "0.399006") == before                    # 重跑: 逐行相同
    con = sqlite3.connect(str(dbs["sent"]))
    con.execute("UPDATE index_breadth SET up=999 WHERE code='0.399006' AND date='2026-01-07'")
    con.execute("UPDATE index_breadth SET up=999 WHERE code='0.399006' AND date='2026-01-20'")
    con.commit()
    con.close()
    out = IB.build("20260113", cfg=CFG)                           # 日增: 只重算 start 起的日子
    assert out["0.399006"] == 18
    after = {r["date"]: r["up"] for r in _breadth(dbs, "0.399006")}
    assert after["2026-01-07"] == 999                             # start 之前的行不动
    assert after["2026-01-20"] == 2
    # start 之前的行情照样读进来算 20 日线: 第 13 天起 n20 就是对的
    assert _tuple(next(r for r in _breadth(dbs, "0.399006") if r["date"] == "2026-01-20"))[2] == 2


def test_incremental_build_equals_full_build_for_a_stock_back_from_long_suspension(dbs, monkeypatch):
    """日增只读 start 之前一小段行情: 长期停牌的股票在这一段里凑不满 20 个交易日, 它的 20 日线不能因此变成
    "没有" — 同一天, 全量重算与日增量算必须得到同一个数(11-27 拿生产数据空跑时发现两者不一致)"""
    days = list(pd.bdate_range("2026-02-02", periods=40).strftime("%Y%m%d"))
    con = RD.raw_connect()
    rows = []
    for i, d in enumerate(days):
        rows.append((A, d, 10.0 + 0.1 * i, 9.9 if i == 0 else 10.0 + 0.1 * (i - 1)))       # 天天有行情
        if i <= 24 or i >= 38:                                                            # C: 下标 25 ~ 37 停牌(13 个交易日)
            rows.append((C, d, 30.0 - 0.1 * i, 30.1 if i == 0 else 30.0 - 0.1 * (i - 1)))
    con.executemany("INSERT OR REPLACE INTO daily(ts_code,trade_date,close,pre_close) VALUES(?,?,?,?)", rows)
    con.commit()
    con.close()
    _mk_snaps("399006.SZ", [("-".join([days[0][:4], days[0][4:6], days[0][6:]]), [A, C])])
    monkeypatch.setattr(IB, "WARM_DAYS", 3)                  # 日增只回看 3 天: 窗口里 C 一行行情都没有(停牌中)
    full = IB.build(days[0], cfg=CFG)["0.399006"]
    everything = _breadth(dbs, "0.399006")
    inc = IB.build(days[36], cfg=CFG)["0.399006"]
    after = _breadth(dbs, "0.399006")
    assert (full, inc) == (40, 4)                            # 全量: 自第一份快照起 40 天; 日增: 重算第 37 ~ 40 天
    assert after == everything                               # 日增重算之后逐行与全量重算相同
    late = {r["date"][8:]: r for r in after}
    assert _tuple(late[days[38][6:]]) == (2, 1, 2, 1, 2)     # C 复牌: 它自己最近 20 个交易日的均线是有的(在均线之下)


def test_build_with_empty_inputs(dbs, capsys):
    assert IB.build("20260101", cfg={"index": {}}) == {}           # 没有指数配置
    out = IB.build("20260101", cfg=CFG)                           # 既没有快照也没有个股日线
    assert out == {"0.399001": 0, "0.399006": 0, "1.000688": 0}
    _mk_snaps("399006.SZ", [("2026-01-06", [A, B])])
    assert IB.build("20260101", cfg=CFG)["0.399006"] == 0          # 有快照、个股日线库是空的
    _mk_bars()
    _mk_snaps("000688.SH", [("2030-01-11", [D])])
    assert IB.build("20260101", cfg=CFG)["1.000688"] == 0          # 快照日还在个股日线之后(未来)
    assert "没有成分股快照" in capsys.readouterr().out


def test_build_rejects_inconsistent_batch(dbs, monkeypatch):
    _mk_bars()
    _mk_snaps("399006.SZ", [("2026-01-06", [A, B])])
    monkeypatch.setattr(IB, "breadth_rows",
                        lambda *a, **k: [("20260106", 2, 3, 0, 0, 2)])   # 上涨 3 只 > 有行情 2 只
    with pytest.raises(RuntimeError, match="拒写.*家数不自洽"):
        IB.build("20260101", cfg=CFG)
    assert _sql(dbs["sent"], "SELECT COUNT(*) FROM index_breadth")[0][0] == 0   # 整批不写


def test_breadth_check_rows():
    good = ("0.399006", "2026-01-06", 2, 1, 2, 1, 2)
    assert IB.check_rows([good], "0.399006") is None
    for i, bad_v, frag in ((0, "0.399001", "别的指数"), (1, "20260106", "日期格式"), (2, 0, "家数不自洽"),
                           (3, 3, "家数不自洽"), (4, 3, "家数不自洽"), (5, 3, "家数不自洽"), (6, 1, "家数不自洽")):
        bad = list(good)
        bad[i] = bad_v
        assert frag in IB.check_rows([tuple(bad)], "0.399006"), (i, bad_v)


def test_year_chunks():
    assert IB.year_chunks("20100601", "20120115") == [
        ("20100601", "20101231"), ("20110101", "20111231"), ("20120101", "20120115")]
    assert IB.year_chunks("20261013", "20261127") == [("20261013", "20261127")]
    assert IB.year_chunks("20270101", "20261127") == []


def test_fetch_weights_chunks_by_year_and_ignores_rows_outside_window(dbs, monkeypatch):
    calls = []

    def fake_pull(api, fields, **kw):
        calls.append((kw["index_code"], kw["start_date"], kw["end_date"]))
        idx = kw["index_code"]
        if kw["start_date"][:4] >= "2015":
            y = kw["start_date"][:4]
            return [pd.DataFrame({"index_code": [idx] * 2, "con_code": ["300001.SZ", "300002.SZ"],
                                  "trade_date": [y + "0130", y + "0130"], "weight": [60.0, 40.0]})]
        # 2010 ~ 2014 年的窗口: 接口多给了一行 2016 年的(无视起止日期)
        return [pd.DataFrame({"index_code": [idx], "con_code": ["300009.SZ"],
                              "trade_date": ["20161230"], "weight": [1.0]})]
    monkeypatch.setattr(IB.T, "pull_frames", fake_pull)
    monkeypatch.setattr(IB.T, "has_token", lambda: True)
    out = IB.fetch_weights("20100101", end="20161231", cfg=CFG)
    assert out == {"399001.SZ": 4, "399006.SZ": 4, "000688.SH": 0}   # 2015、2016 两年 × 2 只
    # 按年分段; 各指数不早于自己最早的快照月(创业板指 2010-06 起; 科创50 2020-07 起 → 本例的窗口里没有它)
    one = [c for c in calls if c[0] == "399001.SZ"]
    assert one[0] == ("399001.SZ", "20100101", "20101231") and one[-1] == ("399001.SZ", "20160101", "20161231")
    assert len(one) == 7
    cyb = [c for c in calls if c[0] == "399006.SZ"]
    assert cyb[0] == ("399006.SZ", "20100601", "20101231") and len(cyb) == 7
    assert not [c for c in calls if c[0] == "000688.SH"]
    rows = _sql(dbs["sent"], "SELECT index_code, trade_date, con_code, weight FROM index_weight ORDER BY 1,2,3")
    assert rows == [("399001.SZ", "2015-01-30", "300001.SZ", 60.0), ("399001.SZ", "2015-01-30", "300002.SZ", 40.0),
                    ("399001.SZ", "2016-01-30", "300001.SZ", 60.0), ("399001.SZ", "2016-01-30", "300002.SZ", 40.0),
                    ("399006.SZ", "2015-01-30", "300001.SZ", 60.0), ("399006.SZ", "2015-01-30", "300002.SZ", 40.0),
                    ("399006.SZ", "2016-01-30", "300001.SZ", 60.0), ("399006.SZ", "2016-01-30", "300002.SZ", 40.0)]
    assert IB.fetch_weights("20100101", end="20161231", cfg=CFG) == out     # 幂等: 再拉一遍不翻倍
    assert len(_sql(dbs["sent"], "SELECT * FROM index_weight")) == 8
    con = DB.connect()
    try:
        assert IB.snapshots(con, "399001.SZ") == [("20150130", ["300001.SZ", "300002.SZ"]),
                                                  ("20160130", ["300001.SZ", "300002.SZ"])]
        assert IB.snapshots(con, "000688.SH") == []
    finally:
        con.close()

    class _D(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 27)
    monkeypatch.setattr(IB, "date", _D)
    calls.clear()
    IB.fetch_weights(end="20261127", cfg=CFG)                      # 不给 start(日增): 从库内最新快照(2016-01)所在的那个月起
                                                                  # (end 写死: 结果不随跑测试的真实日期变)
    by_idx = {i: [c for c in calls if c[0] == i] for i in ("399001.SZ", "399006.SZ", "000688.SH")}
    assert len(by_idx["399001.SZ"]) == 11 and by_idx["399001.SZ"][0] == ("399001.SZ", "20160101", "20161231")   # 2016 ~ 2026 逐年
    assert len(by_idx["399006.SZ"]) == 11 and by_idx["399006.SZ"][0] == ("399006.SZ", "20160101", "20161231")
    star = by_idx["000688.SH"]                                     # 科创50 不早于自己最早的快照月(2020-07)
    assert len(star) == 7 and star[0] == ("000688.SH", "20200701", "20201231") and star[-1] == ("000688.SH", "20260101", "20261127")
    assert by_idx["399001.SZ"][-1] == ("399001.SZ", "20260101", "20261127")
    assert IB.daily_start() == "20260101"                          # 这一遍拉取存进了到 2026-01 的快照: 起点跟着挪
    con = DB.connect()
    con.execute("DELETE FROM index_weight")
    con.commit()
    con.close()
    calls.clear()
    IB.fetch_weights(end="20261127", cfg=CFG)                      # 库里还没有快照: 近 45 天
    assert calls == [("399001.SZ", "20261013", "20261127"), ("399006.SZ", "20261013", "20261127"),
                     ("000688.SH", "20261013", "20261127")]
    assert IB.daily_start() == "20261013"
    monkeypatch.setattr(IB.T, "has_token", lambda: False)
    calls.clear()
    assert IB.fetch_weights("20100101", cfg=CFG) == {} and calls == []
    assert IB.fetch_weights("20100101", cfg={"index": {}}) == {}


def test_refresh_pulls_snapshots_then_rebuilds_recent_days(dbs, monkeypatch):
    """这个用例必须带 dbs(行情库指向临时库): refresh() 第一句就是 connect()"""
    order = []
    monkeypatch.setattr(IB, "fetch_weights", lambda start=None, **kw: order.append(("fetch", start)) or {})
    monkeypatch.setattr(IB, "build", lambda start=None, **kw: order.append(("build", start)) or {})
    assert IB.refresh() is True
    want = (date.today() - timedelta(days=IB.REFRESH_DAYS)).strftime("%Y%m%d")   # 库里没有快照: 近 45 天
    assert order == [("fetch", want), ("build", want)]             # 先接上新快照, 再从同一个起点重算
    assert IB.REFRESH_DAYS >= 31                                   # 回看要跨过一个整月, 月末快照才接得上

    def boom(start=None, **kw):
        raise RuntimeError("限频")
    order.clear()
    monkeypatch.setattr(IB, "fetch_weights", boom)
    assert IB.refresh() is False                                   # 快照拉不下来: 照常用库里已有的算
    assert order == [("build", want)]

    def broken(start=None, **kw):
        raise RuntimeError("库坏了")
    monkeypatch.setattr(IB, "fetch_weights", lambda start=None, **kw: {})
    monkeypatch.setattr(IB, "build", broken)
    assert IB.refresh() is False


def test_refresh_catches_up_when_snapshots_arrive_late_or_the_chain_was_down(dbs, monkeypatch):
    """更新链停摆(或月末快照晚到)超过 45 天: 固定只回看 45 天的话, 中间月份的快照不会入库, 之后的广度按过期的名单算,
    而"这一天有没有行"的体检看不出来(评审意见)。日增的起点不晚于库内最新快照所在的那个月 — 漏掉的月份接得上,
    新快照之后的日子按新名单重算"""
    days = list(pd.bdate_range("2026-06-01", "2026-11-27").strftime("%Y%m%d"))
    con = RD.raw_connect()
    con.executemany("INSERT OR REPLACE INTO daily(ts_code,trade_date,close,pre_close) VALUES(?,?,?,?)",
                    [(c, d, 10.0 + 0.1 * i, 9.9 if i == 0 else 10.0 + 0.1 * (i - 1))
                     for i, d in enumerate(days) for c in (A, B, C)])
    con.commit()
    con.close()
    _mk_snaps("399006.SZ", [("2026-06-30", [A, B])])               # 停摆前库里只有 6 月末的快照

    class _D(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 27)
    monkeypatch.setattr(IB, "date", _D)
    assert IB.daily_start() == "20260601"                          # 不是固定的 20261013
    IB.build("20260101", cfg={"index": {"extra": CFG["index"]["extra"][1:]}})
    before = {r["date"]: r for r in _breadth(dbs, "0.399006")}
    assert before["2026-11-27"]["nc"] == 2 and len(before) == len(days) - days.index("20260630")
    api = {"20260731": [A, B], "20260831": [A, B], "20260930": [A, B, C], "20261030": [A, B, C]}   # 漏掉的 4 个月
    windows = []

    def fake_pull(name, fields, **kw):
        windows.append((kw["index_code"], kw["start_date"]))
        return [pd.DataFrame({"index_code": [kw["index_code"]] * len(codes), "con_code": codes,
                              "trade_date": [d] * len(codes), "weight": [1.0] * len(codes)})
                for d, codes in api.items()
                if kw["start_date"] <= d <= kw["end_date"]]
    monkeypatch.setattr(IB.T, "has_token", lambda: True)
    monkeypatch.setattr(IB.T, "pull_frames", fake_pull)
    assert IB.refresh() is True                                   # refresh 读的是真实 config: 三个指数各拉一次
    assert sorted(windows) == [("000688.SH", "20260601"), ("399001.SZ", "20260601"), ("399006.SZ", "20260601")]   # 都从最新快照那个月起
    after = {r["date"]: r for r in _breadth(dbs, "0.399006")}
    assert after["2026-09-29"]["nc"] == 2 and after["2026-09-30"]["nc"] == 3     # 新快照起按新名单(新快照所在的那个月也在重算范围里)
    assert _tuple(after["2026-09-30"])[:2] == (3, 3)
    assert after["2026-06-30"] == before["2026-06-30"] and len(after) == len(before)   # 旧快照管的日子: 行不变


def test_backfill_entry_points(dbs, monkeypatch):
    seen = []
    monkeypatch.setattr(IB, "fetch_weights", lambda start=None, **kw: seen.append(("fetch", start)) or {"x": 3})
    monkeypatch.setattr(IB, "build", lambda start=None, **kw: seen.append(("build", start)) or {})
    assert IB.build_run("20150101") is True and seen == [("build", "20150101")]
    monkeypatch.setattr(IB.T, "has_token", lambda: False)
    assert IB.fetch_run("20100101") is True and seen == [("build", "20150101")]    # 无 token: 跳过
    monkeypatch.setattr(IB.T, "has_token", lambda: True)
    assert IB.fetch_run("20100101") is True
    assert seen[1:] == [("fetch", "20100101"), ("build", "20100101")]              # 补完快照顺带重算广度(两张表是连着的)
    seen.clear()
    assert IB.fetch_run() is True                                              # 不给 start(命令行): 与日增同一个起点, 不是 2010 年全量
    want = (date.today() - timedelta(days=IB.REFRESH_DAYS)).strftime("%Y%m%d")   # 库里没有快照: 近 45 天
    assert seen == [("fetch", want), ("build", want)]


def test_run_backfill_measures_index_weight_and_breadth(dbs, monkeypatch):
    import backup as BK
    monkeypatch.setattr(BK, "snapshot", lambda asset, btype="manual": None)
    _mk_bars()
    _mk_snaps("399006.SZ", [("2026-01-06", [A, B])])
    res = FD.run_backfill("index_breadth", "20260101")
    assert res["status"] == "ok" and res["label"].startswith("成分股广度")
    assert res["rows_written"] == 25 and res["date_from"] == "2026-01-06" and res["date_to"] == "2026-01-30"
    assert FD.run_backfill("index_breadth")["rows_written"] == 0    # 幂等

    def fake_pull(api, fields, **kw):
        idx = kw["index_code"]
        if not (kw["start_date"] <= "20260130" <= kw["end_date"]):
            return [pd.DataFrame(columns=IB.W_COLS.split(","))]
        return [pd.DataFrame({"index_code": [idx] * 2, "con_code": ["300001.SZ", "300002.SZ"],
                              "trade_date": ["20260130"] * 2, "weight": [60.0, 40.0]})]
    monkeypatch.setattr(IB.T, "has_token", lambda: True)
    monkeypatch.setattr(IB.T, "pull_frames", fake_pull)
    res = FD.run_backfill("index_weight", "20250101")
    assert res["status"] == "ok" and res["label"].startswith("指数成分股")
    assert res["rows_written"] == 6 and res["days"] == [{"date": "2026-01-30", "rows": 6}]   # 3 个指数 × 2 只
    assert FD.run_backfill("index_weight", "20250101")["rows_written"] == 0


# ════════════ ③ 数据管理接入 ════════════

def test_backfill_datasets_registered():
    from server import jobs, market
    for ds in ("board_margin", "index_weight", "index_breadth"):
        assert ds in FD.BACKFILLS and ds in jobs.BACKFILL_DATASETS
    assert FD.BACKFILLS["board_margin"][1] == [("board_margin", None, "date")]
    assert FD.BACKFILLS["board_margin"][3] == "20130131" == jobs.BACKFILL_DEFAULT_START["board_margin"]
    assert FD.BACKFILLS["index_weight"][1] == [("index_weight", None, "trade_date")]
    assert jobs.BACKFILL_DEFAULT_START["index_weight"] == "20100101"
    assert FD.BACKFILLS["index_breadth"][1] == [("index_breadth", None, "date")]
    assert jobs.BACKFILL_DEFAULT_START["index_breadth"] == "20100101"
    codes = {s[0]: s for s in market._ASSET_DATE_TABLES}
    # 每个板块 / 每个指数各一行资产(完成度与缺口按各自算), 映射到同一个补全数据集
    for c, table, where in (("board_margin_cyb", "board_margin", "board='CYB'"), ("board_margin_kcb", "board_margin", "board='KCB'"),
                            ("index_breadth_399001", "index_breadth", "code='0.399001'"),
                            ("index_breadth_399006", "index_breadth", "code='0.399006'"),
                            ("index_breadth_000688", "index_breadth", "code='1.000688'")):
        assert list(codes[c][2:4]) == [table, where], c
        assert market._REPAIR_DS[c] == ("board_margin" if c.startswith("board_margin") else "index_breadth")
    assert not {"board_margin", "index_breadth", "index_weight"} & set(codes)   # 月度快照不是日频数据: 只进补全清单
    assert market._SOURCE_LAG["board_margin_cyb"] == 1 and market._SOURCE_LAG["board_margin_kcb"] == 1   # 与两融汇总同源同节奏
    assert not {"index_breadth_399001", "index_breadth_399006", "index_breadth_000688"} & set(market._SOURCE_LAG)   # 跟个股日线走
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    for frag in ("ds: 'board_margin', label: '板块两融余额·创业板(2013起)', asset: 'board_margin_cyb'",
                 "asset: 'board_margin_kcb'", "label: '成分股广度·深证成指(2010起)', asset: 'index_breadth_399001'",
                 "asset: 'index_breadth_399006'", "asset: 'index_breadth_000688'",
                 "ds: 'index_weight', label: '指数成分股·月度快照", "每次重拉全部月份(约2分钟)后重算广度"):
        assert frag in html, frag


def test_update_chain_mounts_mi4_steps():
    src = (CORE / "fetch_daily.py").read_text(encoding="utf-8")
    for call in ("board_margin.refresh()", "index_breadth.refresh()"):
        assert src.count(call) == 1                                # 各自包在 try 里: 失败不挡主流程
    assert src.index("fetch_margin()\n") < src.index("board_margin.refresh()")
    # 成分股广度要用当天的个股日线: 排在个股日线入库(fetch_sw_daily)之后
    assert src.index("fetch_sw_daily()\n") < src.index("index_breadth.refresh()")
    assert "board_margin" not in src.split("def fetch_sw_daily")[1].split("def backfill_sw_daily")[0]


def test_market_datasets_gap_scan_and_pipeline_stats(dbs, monkeypatch):
    """数据管理页: 每个板块 / 每个指数各成一行(区间 / 完成度 / 健康); 缺的一天能被体检报出且有补全通道 —
    只缺一个板块、只缺一个指数也看得出(评审意见: 按"日期并集"算的话, 另一个有行就把这一天遮住了);
    板块两融的末端一日按源端延迟处理(与两融汇总一样), 不算真缺口"""
    from server import market
    monkeypatch.setattr(market, "SENT_DB", dbs["sent"])
    monkeypatch.setattr(market, "_stale_days", lambda last: 1)   # 陈旧度不随跑测试的真实日期变(这里只看完成度)
    days = ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"]
    con = DB.connect()
    con.execute(BM.SCHEMA)
    con.execute(IB.B_SCHEMA)
    for d in days:
        con.execute("INSERT INTO index_daily VALUES('1.000001',?,1,1,1,1,1,1,0)", (d,))
        if d <= "2026-11-25":                                      # 创业板两融 T+1: 到 25 日
            con.execute("INSERT INTO board_margin VALUES('CYB',?,1.0,0.0,1.0,0.0,900)", (d,))
        if d in ("2026-11-18", "2026-11-24"):                      # 科创板两融: 只有这两天有(半截的日子)
            con.execute("INSERT INTO board_margin VALUES('KCB',?,1.0,0.0,1.0,0.0,600)", (d,))
        if d != "2026-11-20":                                      # 创业板指的广度缺 20 日
            con.execute("INSERT INTO index_breadth VALUES('0.399006',?,100,60,100,60,100)", (d,))
        if d in ("2026-11-18", "2026-11-26"):                      # 科创50 的广度只有首尾两天
            con.execute("INSERT INTO index_breadth VALUES('1.000688',?,50,25,50,20,50)", (d,))
    con.commit()
    con.close()
    rows = {r["code"]: r for r in market.datasets()}
    cyb, kcb = rows["board_margin_cyb"], rows["board_margin_kcb"]
    assert (cyb["rows"], cyb["first"], cyb["last"]) == (6, "2026-11-18", "2026-11-25")   # 各自算, 不再是"日期并集"
    assert (cyb["completeness"], cyb["source"], cyb["db"], cyb["health"]) == (100, "tushare", "sentiment.db", "ok")
    assert (kcb["rows"], kcb["completeness"], kcb["health"]) == (2, 40, "bad")            # 18 ~ 24 的 5 天里只有 2 天
    assert kcb["missing_recent"] == ["2026-11-19", "2026-11-20", "2026-11-23"] and "缺 3 天" in kcb["note"]
    assert "缺" not in cyb["note"]
    b6, b8 = rows["index_breadth_399006"], rows["index_breadth_000688"]
    assert (b6["rows"], b6["missing_n"], b6["missing_recent"], b6["health"]) == (6, 1, ["2026-11-20"], "warn")
    assert b8["rows"] == 2 and b8["missing_recent"] == ["2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25"]
    stats = market.pipeline_stats()                               # 更新链日志的增量计数: 整张表
    assert stats["board_margin"] == {"rows": 8, "last": "2026-11-25"}
    assert stats["index_breadth"] == {"rows": 8, "last": "2026-11-26"}
    monkeypatch.setattr(market, "_trading_days_since", lambda n: list(days))
    monkeypatch.setattr(market, "_today", lambda: "2026-11-27")
    gaps = {a["code"]: a for a in market.scan_gaps(7)["assets"]}
    assert gaps["board_margin_cyb"]["missing"] == [] and gaps["board_margin_cyb"]["delay"] == ["2026-11-26"]
    assert gaps["board_margin_kcb"]["missing"] == ["2026-11-19", "2026-11-20", "2026-11-23"]
    assert gaps["board_margin_kcb"]["delay"] == ["2026-11-25", "2026-11-26"]      # 末端两天按源端延迟处理(与两融汇总一样)
    assert gaps["index_breadth_399006"]["missing"] == ["2026-11-20"] and gaps["index_breadth_399006"]["delay"] == []
    assert gaps["index_breadth_000688"]["missing"] == days[1:6]
    assert all(gaps[c]["has_path"] for c in gaps if c.startswith(("board_margin", "index_breadth")))


def test_backfill_of_mi4_datasets_does_not_enqueue_a_score_recalc(server_app):
    """板块两融 / 成分股快照 / 成分股广度不是评分的输入: 补全之后不必连带重算评分(每次白占 15 ~ 20 分钟队列);
    进评分的数据集照旧入队"""
    import json
    from server import jobs as J
    result = {"dataset": "x", "status": "ok", "date_from": "2026-01-01", "date_to": "2026-01-02", "rows_written": 9,
              "days": [{"date": "2026-01-01", "rows": 9}], "duration_s": 1.0, "error": None}
    real = J._run
    J._run = lambda cmd, append, cwd=None, env_extra=None: append("@@BACKFILL@@ " + json.dumps(result)) or 0
    try:
        with server_app["app"].app_context():
            J.set_setting("backfill_recalc", "1")
            for ds in ("board_margin", "index_weight", "index_breadth"):
                assert J.task_backfill(ds, "20260101")(lambda s: None) == 0
            assert J.queue_size() == 0                           # 不入队评分重算
            assert J.task_backfill("etf_daily", "20260101")(lambda s: None) == 0
            assert J.queue_size() == 1                           # 对照: 别的数据集照旧
            assert J._q.get_nowait()["kind"] == "recalc"
    finally:
        J._run = real
        J._queue_log.clear()
    assert {"board_margin", "index_weight", "index_breadth"} <= set(J.NO_RECALC_DATASETS)
    assert not {"zt_ts", "etf_daily", "index_extra"} & set(J.NO_RECALC_DATASETS)


def test_data_page_lists_mi4_datasets_and_repair_accepts_them(server_app):
    html = server_app["client"].get("/admin/data").get_data(as_text=True)
    for ds in ("board_margin", "index_weight", "index_breadth"):
        assert ds in html, ds
    c = server_app["client"]
    for ds in ("board_margin", "index_breadth", "index_weight"):
        assert c.post("/admin/api/backfill/run", json={"dataset": ds}).status_code == 200
    from server import jobs as J
    targets = []
    while not J._q.empty():
        targets.append(J._q.get_nowait()["target"])
    assert len(targets) == 3 and "板块两融余额" in targets[0]
    J._queue_log.clear()
