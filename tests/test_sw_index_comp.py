# -*- coding: utf-8 -*-
"""sw_index_comp: SW成分等权自算指数 — 等权收益/累乘基点1000/增量承接/
开高低价格带包含open/一年窗口跨年规则"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import sw_index_comp as SIC  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    sent = tmp_path / "sent.db"
    raw = tmp_path / "raw.db"
    sc = sqlite3.connect(str(sent))
    sc.execute("CREATE TABLE sw_components(index_code TEXT, level TEXT, "
               "industry_name TEXT, con_code TEXT, in_date TEXT, out_date TEXT)")
    # 两只成分股(801120.SI 食品), 第二只在D2后进入(in_date过滤)
    sc.executemany("INSERT INTO sw_components VALUES(?,?,?,?,?,?)", [
        ("801120.SI", "L1", "食品", "600519.SH", "20050101", None),
        ("801120.SI", "L1", "食品", "000858.SZ", "20050104", None),
    ])
    sc.commit()
    rc = sqlite3.connect(str(raw))
    rc.execute("CREATE TABLE daily(ts_code TEXT, trade_date TEXT, pre_close REAL, "
               "open REAL, close REAL, high REAL, low REAL, amount REAL)")
    # 3日: D1仅519; D2起双股。pre_close/open/close/high/low/amount
    rows = [
        ("600519.SH", "20050104", 1000.0, 1010.0, 1020.0, 1030.0, 1005.0, 1.0e9),
        ("000858.SZ", "20050105", 100.0, 101.0, 102.0, 103.0, 100.5, 2.0e9),
        ("600519.SH", "20050105", 1020.0, 1030.0, 1040.4, 1050.0, 1025.0, 1.1e9),
    ]
    rc.executemany("INSERT INTO daily VALUES(?,?,?,?,?,?,?,?)", rows)
    rc.commit()
    monkeypatch.setattr(SIC, "connect", lambda: sqlite3.connect(str(sent)))
    monkeypatch.setattr(SIC, "raw_connect", lambda: sqlite3.connect(str(raw)))
    return {"sent": sent, "raw": raw}


def _rows(sent):
    con = sqlite3.connect(str(sent))
    out = con.execute("SELECT date, open, high, low, close, pct_chg, n, amount "
                      "FROM sw_index_comp ORDER BY date").fetchall()
    con.close()
    return out


def test_build_equal_weight_math(env):
    """D1: 单股ret=+2% → close=1020, pct_chg=2; D2: 等权ret=(1+2)均值;
    高低价带包含open(跳空越带钳回); amount求和; in_date过滤生效"""
    assert SIC.build("20050101") is True
    r = _rows(env["sent"])
    assert len(r) == 2
    d1, d2 = r          # (date, open, high, low, close, pct_chg, n, amount)
    # D1: 519 close 1000→1020 = +2%
    assert d1[0] == "2005-01-04"
    assert d1[4] == pytest.approx(1000 * 1.02, rel=1e-9)   # close累乘基点1000
    assert d1[5] == pytest.approx(2.0, abs=1e-9)           # pct_chg=ret
    assert d1[6] == 1 and d1[7] == pytest.approx(1.0e9)    # n/amount
    # D2: 等权 ret = ((1040.4/1020-1)+(102/100-1))/2 = (2%+2%)/2 = 2%
    assert d2[5] == pytest.approx(2.0, abs=1e-9)
    assert d2[6] == 2                                       # 两只成分
    assert d2[7] == pytest.approx(3.1e9)                    # amount求和
    # D2 close = 1020×1.02; 价格带包含open(首日open=前收, 开盘跳空钳回带内)
    close2 = d2[4]
    assert close2 == pytest.approx(1040.4, rel=1e-9)
    assert d2[2] >= d2[3] - 1e-9 and d2[1] <= close2 + 1e-9  # high>=low, open<=high


def test_build_carry_from_previous_block(env):
    """增量承接: 库内已有前收盘 → 新块基点=承接而非重置1000"""
    con = sqlite3.connect(str(env["sent"]))
    con.execute("CREATE TABLE IF NOT EXISTS sw_index_comp("
                "index_code TEXT, date TEXT, open REAL, high REAL, low REAL, "
                "close REAL, volume REAL, amount REAL, pct_chg REAL, n INTEGER, "
                "PRIMARY KEY(index_code, date))")
    con.execute("INSERT INTO sw_index_comp(index_code,date,close) "
                "VALUES('801120.SI','2005-01-03', 2000.0)")
    con.commit(); con.close()
    assert SIC.build("20050101") is True
    r = _rows(env["sent"])
    d1 = next(x for x in r if x[0] == "2005-01-04")       # 跳过预插的01-03承接锚
    # D1基点=2000: close = 2000×1.02 — 承接系数=2000/1000=2(不重置1000)
    assert d1[4] == pytest.approx(2000 * 1.02, rel=1e-9)


def test_build_empty_components(env):
    """sw_components空 → False不炸"""
    con = sqlite3.connect(str(env["sent"]))
    con.execute("DELETE FROM sw_components")
    con.commit(); con.close()
    assert SIC.build("20050101") is False


def test_build_year_window_cross_year(env, monkeypatch):
    """1-2月起始窗口含上一年(晚到数据自愈), 3月+不含"""
    con = sqlite3.connect(str(env["raw"]))
    con.execute("INSERT INTO daily VALUES('X','20041231',1,1,1,1,1,1)")
    con.commit(); con.close()
    years = []
    real_read = SIC.pd.read_sql

    def spy(sql, conn, params=(), *a, **kw):
        if "FROM daily" in sql:
            years.append(params)
        return real_read(sql, conn, params=params, *a, **kw)
    monkeypatch.setattr(SIC.pd, "read_sql", spy)
    SIC.build("20050102")          # 1-2月 → 应含2004
    assert any(p[0].startswith("2004") for p in years)
    years.clear()
    SIC.build("20050301")          # 3月+ → 不含2004
    assert not any(p[0].startswith("2004") for p in years)
