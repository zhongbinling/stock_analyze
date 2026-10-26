# -*- coding: utf-8 -*-
"""D1-14 H2: 两融链 — 重叠自愈窗口/历史回补边界/守卫批次"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import fetch_daily as FD  # noqa: E402


@pytest.fixture()
def m_env(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    c = sqlite3.connect(str(p))
    c.execute("""CREATE TABLE margin_daily(date TEXT PRIMARY KEY, rzye REAL, rzmre REAL,
      rqye REAL, rzrqye REAL, sse_rzrqye REAL, szse_rzrqye REAL)""")
    c.commit()
    c.close()
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(p)))
    return {"path": p}


def _marg_df(rows):
    return pd.DataFrame(rows, columns=["exchange_id", "trade_date", "rzye",
                                       "rzmre", "rqye", "rzrqye"])


def test_fetch_margin_overlap_selfheal(m_env, monkeypatch):
    """H2核心: 增量起点回看3天 — 延迟发布的深市行被次日自动补齐(幂等覆盖)"""
    calls = []

    def fake(start, end=""):
        calls.append((start, end))
        # 返回三所: 深市10-23延迟到今天才出现
        return _marg_df([
            ("SSE", "20261022", 1e12, 1e10, 1e10, 1.2e12),
            ("SZSE", "20261022", 1e12, 1e10, 1e10, 1.1e12),
            ("SSE", "20261023", 1e12, 1e10, 1e10, 1.25e12),
            ("SZSE", "20261023", 1e12, 1e10, 1e10, 1.15e12),   # 此前缺失的深市行
            ("SSE", "20261026", 1e12, 1e10, 1e10, 1.26e12),
        ])
    c = sqlite3.connect(str(m_env["path"]))
    c.execute("INSERT INTO margin_daily VALUES('2026-10-23',1e12,1e10,1e10,1.25e12,1.25e12,NULL)")
    c.commit()
    c.close()
    import tushare_api as TA
    monkeypatch.setattr(TA, "has_token", lambda: True)
    monkeypatch.setattr(FD.tushare_api, "margin", fake) if hasattr(FD, "tushare_api") else None
    # fetch_margin函数内import tushare_api → patch模块属性
    import tushare_api
    monkeypatch.setattr(tushare_api, "margin", fake)
    monkeypatch.setattr(tushare_api, "has_token", lambda: True)
    FD.fetch_margin()
    assert calls[0][0] == "20261020"                     # 回看3天(last=10-23)
    c = sqlite3.connect(str(m_env["path"]))
    row = c.execute("SELECT rzrqye, szse_rzrqye FROM margin_daily "
                    "WHERE date='2026-10-23'").fetchone()
    assert row[0] == pytest.approx(1.25e12 + 1.15e12)    # 三所合计已覆盖旧值
    assert row[1] == pytest.approx(1.15e12)              # 深市延迟行已补
    c.close()


def test_backfill_margin_earlier_segment_only(m_env, monkeypatch):
    """回补只拉[库内最早-1]之前, 不动现有段; 无更早段时no-op"""
    c = sqlite3.connect(str(m_env["path"]))
    c.execute("INSERT INTO margin_daily VALUES('2020-05-18',1,1,1,1,1,1)")
    c.commit()
    c.close()
    got = []

    def fake(start, end=""):
        got.append((start, end))
        if start > "20100331":                            # 第二页起无更多数据 → 结束分页
            return _marg_df([])
        return _marg_df([("SSE", "20100331", 1, 1, 1, 100)])
    import tushare_api
    monkeypatch.setattr(tushare_api, "has_token", lambda: True)
    monkeypatch.setattr(tushare_api, "margin", fake)
    assert FD.backfill_margin("20100331") is True
    assert got[0] == ("20100331", "20200517")            # 止于库内最早日前一天
    c = sqlite3.connect(str(m_env["path"]))
    assert c.execute("SELECT COUNT(*) FROM margin_daily WHERE date='2010-03-31'").fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM margin_daily WHERE date='2020-05-18'").fetchone()[0] == 1
    c.close()
    # 再跑一次: 库内最早=2010-03-31, 无更早段 → no-op
    got.clear()
    assert FD.backfill_margin("20100331") is True
    assert not got


def test_margin_hist_registered():
    import fetch_daily as FD2
    assert "margin_hist" in FD2.BACKFILLS
    from server import jobs
    assert "margin_hist" in jobs.BACKFILL_DATASETS
    assert jobs.BACKFILL_DEFAULT_START["margin_hist"] == "20100331"
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "margin_hist" in html and "自2010-03-31起" in html


def test_pull_margin_no_degradation(m_env, monkeypatch):
    """D1-14r: 源端退化保护 — 某日源端暂缺深市行时, 不用单所合计覆盖完整三所值"""
    c = sqlite3.connect(str(m_env["path"]))
    c.execute("INSERT INTO margin_daily VALUES('2026-10-22',1e12,1e10,1e10,2.4e12,1.26e12,1.18e12)")
    c.commit()
    c.close()

    def fake(start, end=""):
        if start > "20261022":
            return _marg_df([])
        return _marg_df([                   # 10-22 源端只剩沪市行(退化)
            ("SSE", "20261022", 1e12, 1e10, 1e10, 1.26e12),
            ("SSE", "20261023", 1e12, 1e10, 1e10, 1.25e12),
            ("SZSE", "20261023", 1e12, 1e10, 1e10, 1.15e12),
        ])
    import fetch_daily as FD2
    from db import connect as _c
    monkeypatch.setattr(FD2, "connect", lambda: sqlite3.connect(str(m_env["path"])))
    import tushare_api
    monkeypatch.setattr(tushare_api, "margin", fake)
    n = FD2._pull_margin(sqlite3.connect(str(m_env["path"])), "20261020", "20261023")
    c = sqlite3.connect(str(m_env["path"]))
    row = c.execute("SELECT rzrqye, szse_rzrqye FROM margin_daily "
                    "WHERE date='2026-10-22'").fetchone()
    assert row == (2.4e12, 1.18e12)          # 完整三所值未被降级
    row23 = c.execute("SELECT rzrqye FROM margin_daily WHERE date='2026-10-23'").fetchone()
    assert row23[0] == pytest.approx(2.4e12)  # 新日正常写入合计
    c.close()
