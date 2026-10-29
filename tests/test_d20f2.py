# -*- coding: utf-8 -*-
"""D1-20 F1+F2: 月度解禁 — 事件级聚合/单位/翻页归一/修订防双计/月聚合/
overview拼装/注册接线/页面门禁"""
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(REPO))

import fetch_float as FF  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """sentiment夹具库 + 可注入的raw_daily夹具库(daily带pre_close/close)"""
    sent = tmp_path / "s.db"
    raw = tmp_path / "raw.db"
    c = sqlite3.connect(str(raw))
    c.execute("""CREATE TABLE daily(ts_code TEXT, trade_date TEXT, close REAL,
      pre_close REAL, PRIMARY KEY(ts_code, trade_date))""")
    # 2024-09-09(解禁日)前收=9.9; 最新日2026-10-28 close=20.0
    c.executemany("INSERT INTO daily VALUES(?,?,?,?)", [
        ("600000.SH", "20240909", 10.0, 9.9),
        ("600001.SH", "20240909", 5.0, 4.9),
        ("600000.SH", "20261028", 20.0, 19.9),
        ("600001.SH", "20261028", 8.0, 7.9)])
    c.commit()
    c.close()
    monkeypatch.setattr(FF, "RAW_DB", raw)
    import server.macro as MC
    monkeypatch.setattr(MC, "CORE", tmp_path / "c")   # macro自身路径隔离
    (tmp_path / "c").mkdir()
    # macro 读 sentiment 用 path 参数注入; fetch_float.connect 指夹具
    conn = sqlite3.connect(str(sent))
    FF.ensure_tables(conn)
    FF._attach(conn)
    conn.close()
    monkeypatch.setattr(FF, "connect", lambda: _reconnect(sent, raw))
    return {"sent": sent, "raw": raw, "FF": FF, "MC": MC,
            "monkeypatch": monkeypatch, "tmp": tmp_path}


def _reconnect(sent, raw):
    conn = sqlite3.connect(str(sent))
    FF.ensure_tables(conn)
    conn.execute(f"ATTACH DATABASE '{raw.as_posix()}' AS rawdb")
    return conn


def _float_df(rows):
    return pd.DataFrame(rows, columns=["ts_code", "ann_date", "float_date",
                                       "float_share", "float_ratio", "share_type"])


def test_pull_event_level_and_units(env, monkeypatch):
    """按持有人拆行→事件级Σ入库(同额持有人不误并); YYYYMMDD归一ISO;
    翻页取全(offset递进); 单位=股"""
    calls = []
    all_rows = ([("600000.SH", "20240905", "20240909", float(i), 0.0, "分页填充")
                 for i in range(FF.PAGE)]              # 满页: 持有人拆行(跨页Σ)
                + [("600000.SH", "20240905", "20240909", 1e9, 0.1, "首发原股东限售"),
                   ("600000.SH", "20240906", "20240909", 1e9, 0.1, "首发原股东限售"),
                   ("600001.SH", "20240905", "20240909", 2e9, 0.2, "定增限售")])

    def fake_call(api, fields="", **kw):
        """窗口感知: 按start/end过滤公告日(半月窗) + offset翻页"""
        calls.append(kw)
        lo, hi, off = kw["start_date"], kw["end_date"], kw.get("offset", 0)
        rows = [r for r in all_rows if lo <= r[1] <= hi]
        return _float_df(rows[off:off + FF.PAGE])
    monkeypatch.setattr(FF.T, "call", fake_call)
    conn = FF.connect()
    n = FF._pull_month(conn, "2024-09")
    rows = conn.execute("SELECT ts_code, float_date, share_type, float_share "
                        "FROM float_raw ORDER BY ts_code, share_type").fetchall()
    conn.close()
    assert n == 3                                   # 事件级: 同code同日同类型Σ
    assert rows[1][:3] == ("600000.SH", "2024-09-09", "首发原股东限售")
    assert rows[1][3] == 2e9                        # 两持有人Σ, 未误并
    assert calls[0]["start_date"] == "20240901" and calls[0]["limit"] == FF.PAGE
    assert len(calls) == 2                          # 整月窗: 满→翻(递归拆窗仅在撞限时)


def test_aggregate_preclose_and_latest(env):
    """已发生=前收价(解禁日前收), 未来=最新价; 无价跳过"""
    conn = FF.connect()
    # 列序: ann_date, ts_code, float_date, share, ratio, share_type
    conn.executemany("INSERT INTO float_raw VALUES(?,?,?,?,?,?)", [
        ("2024-09-05", "600000.SH", "2024-09-09", 1e9, 0.1, "首发"),
        ("2024-09-05", "600001.SH", "2024-09-09", 2e9, 0.2, "定增"),
        ("2026-10-01", "600000.SH", "2026-12-31", 1e9, 0.1, "定增"),   # 未来
        ("2024-09-05", "600999.SH", "2024-09-09", 5e8, 0.05, "其他"),  # 无价
    ])
    conn.commit()
    r24 = FF.aggregate_month(conn, "2024-09")
    r26 = FF.aggregate_month(conn, "2026-12")
    conn.close()
    # 1e9×9.9 + 2e9×4.9 = 19.7亿(前收); 无价5e8跳过
    assert r24["amount_yi"] == round(1e9 * 9.9 / 1e8 + 2e9 * 4.9 / 1e8, 1)
    assert r24["n_events"] == 2 and r24["no_price"] == 1
    assert r26["amount_yi"] == round(1e9 * 20.0 / 1e8, 1)   # 最新价20


def test_refresh_replace_prevents_double(env, monkeypatch):
    """修订滚动: 同月重拉先清后插, float_share变化不双计"""
    def df_of(v):
        return _float_df([("600000.SH", "20240905", "20240909", v, 0.1, "首发")])
    box = {"v": 1e9}

    def fake(api, fields="", **kw):                     # 窗口感知(半月窗)
        lo, hi = kw["start_date"], kw["end_date"]
        rows = df_of(box["v"])
        return rows[(rows["ann_date"] >= lo) & (rows["ann_date"] <= hi)]
    monkeypatch.setattr(FF.T, "call", fake)
    conn = FF.connect()
    FF._pull_month(conn, "2024-09")
    box["v"] = 1.5e9                                  # 计划修订
    FF._pull_month(conn, "2024-09", replace=True)
    n = conn.execute("SELECT COUNT(*), SUM(float_share) FROM float_raw").fetchone()
    conn.close()
    assert n[0] == 1 and n[1] == 1.5e9


def test_float_overview_assembly(env, monkeypatch):
    """overview: 历史月(float_month) + 当月两段(前收/最新价) + 未来月 + 最新日"""
    import shutil as _sh
    import time as _t
    conn = FF.connect()
    cur = _t.strftime("%Y-%m")
    y, m = int(cur[:4]), int(cur[5:7]) - 1
    if m == 0:
        y, m = y - 1, 12
    last = f"{y:04d}-{m:02d}"
    conn.execute("INSERT INTO float_month VALUES(?,?,?,?)", (last, 1234.5, 10, "x"))
    # 当月: 两笔已发生(≤2026-10-28 → 前收口径), 一笔未来(>最新日 → 最新价)
    conn.executemany("INSERT INTO float_raw VALUES(?,?,?,?,?,?)", [
        (f"{cur}-01", "600000.SH", f"{cur}-05", 1e9, 0.1, "定增"),
        (f"{cur}-01", "600001.SH", f"{cur}-10", 2e9, 0.1, "定增"),
        (f"{cur}-01", "600000.SH", f"{cur}-30", 1e9, 0.1, "定增")])
    conn.commit()
    conn.close()
    # 补当月解禁日的前收行(前收口径数据源)
    rc = sqlite3.connect(str(env["raw"]))
    rc.executemany("INSERT OR REPLACE INTO daily VALUES(?,?,?,?)", [
        ("600000.SH", f"{cur}-05".replace("-", ""), 9.9, 9.9),
        ("600001.SH", f"{cur}-10".replace("-", ""), 7.9, 7.9)])
    rc.commit()
    rc.close()
    MC = env["MC"]
    monkeypatch.setattr(MC, "CORE", env["tmp"])
    (env["tmp"] / "data").mkdir(exist_ok=True)
    _sh.copy(env["sent"], env["tmp"] / "data" / "sentiment.db")
    _sh.copy(env["raw"], env["tmp"] / "data" / "raw_daily.db")
    r = MC.float_overview()
    assert r["latest_day"] == "2026-10-28"
    assert r["hist"][-1]["month"] == last and r["hist"][-1]["amount_yi"] == 1234.5
    # 当月done: 1e9×9.9/1e8 + 2e9×7.9/1e8 ≈ 99+158=257亿(前收)
    assert r["cur"]["done_yi"] == round(1e9 * 9.9 / 1e8 + 2e9 * 7.9 / 1e8, 1)
    # 当月plan: 10亿股×20元=200亿(最新价)
    assert r["cur"]["plan_yi"] == 200.0
    assert len(r["future"]) == 3 and r["future"][0]["plan_yi"] == 0.0


def test_registrations():
    import fetch_daily as FD
    from server import jobs
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "float_hist" in FD.BACKFILLS and "float_month" in str(FD.BACKFILLS["float_hist"][1])
    assert "float_hist" in jobs.BACKFILL_DATASETS
    assert jobs.BACKFILL_DEFAULT_START["float_hist"] == "20060101"
    assert "月度解禁·share_float(2006起)" in html
    src = (CORE / "fetch_daily.py").read_text(encoding="utf-8")
    assert "fetch_float.refresh()" in src            # 夜间滚动修订挂载


def test_api_and_page(server_app):
    r = server_app["client"].get("/admin/api/macro/float")
    assert r.status_code == 200
    j = r.get_json()
    assert "hist" in j and "cur" in j and "future" in j
    assert server_app["client"].get("/admin/api/macro/float?hist=x").status_code == 400
    html = (REPO / "server" / "templates" / "admin" / "macro.html").read_text(encoding="utf-8")
    for marker in ("chartFloat", "api/macro/float", "月度解禁总市值",
                   "已发生(前收价)", "预计(最新价)", "flState"):
        assert marker in html, f"缺接线: {marker}"
