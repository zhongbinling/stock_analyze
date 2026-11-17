# -*- coding: utf-8 -*-
"""fetch_float 补齐: _months兼容口径/月位移/日期归一/翻页撞限拆窗/refresh月跨度"""
import sqlite3
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import fetch_float as FF  # noqa: E402

ROW = {"ts_code": "600519.SH", "ann_date": "20240105", "float_date": "20240120",
       "float_share": 100.0, "float_ratio": 1.0, "share_type": "定增"}


# ── 纯helper ──

def test_months_iso_and_compact():
    """P4r: ISO与紧凑两种口径; 跨年; 单月"""
    assert FF._months("2024-01", "2024-03") == ["2024-01", "2024-02", "2024-03"]
    assert FF._months("202401", "202401") == ["2024-01"]
    assert FF._months("2023-11", "2024-02") == ["2023-11", "2023-12",
                                                "2024-01", "2024-02"]


def test_shift_month():
    """ISO月位移返回ISO(曾返回紧凑月致垃圾行); 负向/跨年/紧凑入参"""
    assert FF._shift_month("2026-01", -1) == "2025-12"
    assert FF._shift_month("202611", 4) == "2027-03"
    assert FF._shift_month("2026-12", 1) == "2027-01"
    assert FF._shift_month("2026-03", -6) == "2025-09"


def test_iso_norm():
    assert FF._iso("20240120") == "2024-01-20"
    assert FF._iso("2024-01-20") == "2024-01-20"
    assert FF._iso(None) == "" and FF._iso("bad") == "bad"


# ── 翻页撞offset上限 → 递归拆窗(首窗帧弃, Σ不重复) ──

def test_pull_month_window_split(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    con = sqlite3.connect(str(db))
    FF.ensure_tables(con)
    calls = []
    import datetime as dtm

    def _days(s, e):
        return (dtm.date(int(e[:4]), int(e[4:6]), int(e[6:8]))
                - dtm.date(int(s[:4]), int(s[4:6]), int(s[6:8]))).days

    def fake(api, fields="", **kw):
        s, e, off = kw["start_date"], kw["end_date"], kw["offset"]
        calls.append((s, e, off))
        if _days(s, e) >= 10:                    # 宽窗: 恒满页 → 撞offset上限
            return pd.DataFrame([ROW] * FF.PAGE)
        if s <= "20240105" <= e:                 # 仅覆盖公告日的窄窗有数据
            return pd.DataFrame([ROW])           # (真实窗口互不相交, 同假行会双计)
        return pd.DataFrame()
    monkeypatch.setattr(FF.T, "has_token", lambda: True)
    monkeypatch.setattr(FF.T, "call", fake)
    n = FF._pull_month(con, "2024-01")
    assert n == 1                                # 同键事件聚合为1
    rows = con.execute("SELECT ts_code, float_date, float_share FROM float_raw"
                       ).fetchall()
    con.close()
    assert rows == [("600519.SH", "2024-01-20", 100.0)]
    # 撞限路径被触发(offset达96000的调用)且发生了拆窗(窄窗调用存在)
    assert any(c[2] >= 96000 for c in calls)
    assert any(_days(c[0], c[1]) < 10 for c in calls)


def test_pull_month_single_day_overflow_path(tmp_path, monkeypatch, capsys):
    """单日拆无可拆 → float_date分片路径; 分片首页空 → 安全返回不炸"""
    db = tmp_path / "s.db"
    con = sqlite3.connect(str(db))
    FF.ensure_tables(con)
    monkeypatch.setattr(FF.T, "has_token", lambda: True)

    def fake(api, fields="", **kw):
        if kw.get("limit") == 100:                 # 分片首页(分布探测)
            return pd.DataFrame()                  # 空 → 早退
        return pd.DataFrame([ROW] * FF.PAGE)       # 翻页恒满页(单日也超限)
    monkeypatch.setattr(FF.T, "call", fake)
    n = FF._pull_month(con, "2024-01")             # 拆到单日后走分片
    con.close()
    assert n >= 0                                   # 不炸即过(尾部容错跳过)
    assert "float_date" in capsys.readouterr().out or n == 0


# ── refresh 月跨度(近6+未来4滚动修订) ──

def test_refresh_month_span(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    con = sqlite3.connect(str(db))
    FF.ensure_tables(con)
    rawdb = tmp_path / "raw.db"
    rc = sqlite3.connect(str(rawdb))
    rc.execute("CREATE TABLE daily(ts_code TEXT, trade_date TEXT, close REAL, "
               "pre_close REAL)")
    rc.commit(); rc.close()
    monkeypatch.setattr(FF, "RAW_DB", rawdb)
    monkeypatch.setattr(FF, "connect", lambda: con)
    pulls, aggs = [], []
    monkeypatch.setattr(FF, "_pull_month",
                        lambda c, m, replace=False: pulls.append((m, replace)) or 1)
    monkeypatch.setattr(FF, "aggregate_month",
                        lambda c, m: aggs.append(m) or {"amount_yi": 0})
    assert FF.refresh() is True
    con.close()
    now_ym = time.strftime("%Y-%m")
    lo = FF._shift_month(now_ym, -6)
    hi = FF._shift_month(now_ym, FF.FUTURE_M)
    assert pulls and all(r is True for _, r in pulls)     # 全部replace(防双计)
    assert [m for m, _ in pulls] == FF._months(lo, hi)    # 近6+未来4共11月
    assert len(aggs) >= len(pulls)                        # 每月聚合+当月起5月再聚
