# -*- coding: utf-8 -*-
"""D1-6 P0 测试: sw_components 区间还原逻辑 / build 入库 / 校验"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import sw_components as SC  # noqa: E402
import tushare_api as T  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    monkeypatch.setattr(SC, "connect", lambda: sqlite3.connect(str(p)))
    return p


def _seed(p):
    conn = sqlite3.connect(str(p))
    conn.executescript("""
      CREATE TABLE sw_components(index_code TEXT, level TEXT, industry_name TEXT,
        con_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT,
        PRIMARY KEY(index_code, con_code, in_date));
    """)
    rows = [
        # 农林牧渔L1: 000001 一直在册; 000034 两段历史(2007-09, 2015-17)
        ("801010.SI", "L1", "农林牧渔", "000001.SZ", "20070703", None, "Y"),
        ("801010.SI", "L1", "农林牧渔", "000034.SZ", "20070703", "20090601", "N"),
        ("801010.SI", "L1", "农林牧渔", "000034.SZ", "20150701", "20170630", "N"),
        # 食品饮料L1: 000600 在册
        ("801120.SI", "L1", "食品饮料", "000600.SH", "20070703", None, "Y"),
    ]
    conn.executemany("INSERT INTO sw_components VALUES(?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_members_on_half_open_interval(db):
    _seed(db)
    # 2008年: 000001 + 000034(第一段)
    m = SC.members_on("20080115", level="L1")
    assert m["801010.SI"] == {"000001.SZ", "000034.SZ"}
    # out_date 当日 = 已移出(半开)
    m = SC.members_on("2009-06-01", level="L1")
    assert m["801010.SI"] == {"000001.SZ"}
    # 2016年: 第二段
    m = SC.members_on("20160101", level="L1")
    assert m["801010.SI"] == {"000001.SZ", "000034.SZ"}
    # 2018年: 两段都结束
    m = SC.members_on("20180101", level="L1")
    assert m["801010.SI"] == {"000001.SZ"}
    # 全层级无过滤
    m = SC.members_on("20260101")
    assert set(m) == {"801010.SI", "801120.SI"}
    # 指定指数
    m = SC.members_on("20260101", index_code="801120.SI")
    assert m == {"801120.SI": {"000600.SH"}}


def test_build_writes_intervals(db, monkeypatch):
    import pandas as pd
    calls = []

    def fake_call(api, fields="", **kw):
        calls.append((api, kw))
        if api == "index_classify":
            return pd.DataFrame({"index_code": ["801010.SI"],
                                 "industry_name": ["农林牧渔"]})
        return pd.DataFrame([
            {"index_code": "801010.SI", "con_code": "000001.SZ",
             "in_date": "20070703", "out_date": None, "is_new": "Y"},
            {"index_code": "801010.SI", "con_code": "000034.SZ",
             "in_date": "20150701", "out_date": "20170630", "is_new": "N"},
        ])
    monkeypatch.setattr(T, "call", fake_call)
    SC.build(levels=("L1",))
    assert ("index_classify", {"level": "L1", "src": "SW2021"}) in calls
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT con_code, in_date, out_date, industry_name, level "
                        "FROM sw_components ORDER BY con_code").fetchall()
    conn.close()
    assert rows == [("000001.SZ", "20070703", None, "农林牧渔", "L1"),
                    ("000034.SZ", "20150701", "20170630", "农林牧渔", "L1")]


def test_build_idempotent(db, monkeypatch):
    import pandas as pd
    monkeypatch.setattr(T, "call", lambda api, fields="", **kw: pd.DataFrame(
        {"index_code": ["801010.SI"] * 2, "industry_name": ["农林牧渔"] * 2}
        if api == "index_classify" else
        [{"index_code": "801010.SI", "con_code": "000001.SZ",
          "in_date": "20070703", "out_date": None, "is_new": "Y"},
         {"index_code": "801010.SI", "con_code": "000002.SZ",
          "in_date": "20070703", "out_date": None, "is_new": "Y"}]))
    SC.build(levels=("L1",))
    SC.build(levels=("L1",))
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM sw_components").fetchone()[0]
    conn.close()
    assert n == 2                                  # PK 去重


def test_validate_consistency(db):
    _seed(db)
    # 000001/000600 在册 == 今日还原(区间开放); 000034 历史段不计
    assert SC.validate() is True
