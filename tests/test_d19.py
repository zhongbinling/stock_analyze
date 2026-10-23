# -*- coding: utf-8 -*-
"""D1-9 测试: sw_index_comp 等权自算指数(累乘/聚合/PK) + 分量对齐"""
import sqlite3
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import sw_index_comp as SIC  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    raw_p, sent_p = tmp_path / "raw.db", tmp_path / "s.db"
    sent = sqlite3.connect(str(sent_p))
    sent.executescript("""
      CREATE TABLE sw_components(index_code TEXT, level TEXT, industry_name TEXT,
        con_code TEXT, in_date TEXT, out_date TEXT, is_new TEXT);
      CREATE TABLE sw_index_comp(index_code TEXT, date TEXT, open REAL, high REAL,
        low REAL, close REAL, volume REAL, amount REAL, pct_chg REAL, n INTEGER,
        PRIMARY KEY(index_code, date));
    """)
    # 两指数: A(000001,000002) B(000003); 000004 无归属
    for c in ("000001.SZ", "000002.SZ"):
        sent.execute("INSERT INTO sw_components VALUES('801010.SI','L1','农林牧渔',?,'20000101',NULL,'Y')", (c,))
    sent.execute("INSERT INTO sw_components VALUES('801120.SI','L1','食品饮料','000003.SZ','20000101',NULL,'Y')")
    sent.commit()
    sent.close()
    raw = sqlite3.connect(str(raw_p))
    raw.executescript("""
      CREATE TABLE daily(ts_code TEXT, trade_date TEXT, pre_close REAL, open REAL,
        close REAL, high REAL, low REAL, amount REAL);
      CREATE INDEX idx_daily_tradedate ON daily(trade_date);
    """)
    days = {
        "20050104": [("000001.SZ", 10, 10.2, 11, 11.5, 9.5, 1e6),   # +10% gap+2%
                     ("000002.SZ", 10, 10.0, 10, 10, 10, 1e6),        # 0% gap0
                     ("000003.SZ", 10, 10.4, 12, 12, 12, 2e6),        # +20% gap+4%
                     ("000004.SZ", 10, 10.5, 10.5, 10.5, 10.5, 5e5)], # 无归属不计
        "20050105": [("000001.SZ", 11, 11.0, 12.1, 12.1, 11, 1e6),   # +10% gap0
                     ("000002.SZ", 10, 9.8, 9.0, 10, 9, 1e6),        # -10% gap-2%
                     ("000003.SZ", 12, 12.2, 12, 12, 12, 2e6)],       # 0% gap+1.67%
    }
    for d, rows in days.items():
        for ts, pre, o, c, h, l, amt in rows:
            raw.execute("INSERT INTO daily VALUES(?,?,?,?,?,?,?,?)", (ts, d, pre, o, c, h, l, amt))
    raw.commit()
    raw.close()

    monkeypatch.setattr(SIC, "connect", lambda: sqlite3.connect(str(sent_p)))
    import raw_daily as RD
    monkeypatch.setattr(RD, "RAW_DB", raw_p)
    return sent_p


def test_composite_equal_weight_cumprod(env):
    assert SIC.build("20050101") is True
    conn = sqlite3.connect(str(env))
    # 801010: 0104 等权收益=(10%+0%)/2=5% → close=1000; 0105=(10%-10%)/2=0% → close=1000
    r = conn.execute("SELECT date, close, pct_chg, amount, n FROM sw_index_comp "
                     "WHERE index_code='801010.SI' ORDER BY date").fetchall()
    assert abs(r[0][2] - 5.0) < 1e-6 and abs(r[0][1] - 1050) < 1e-6   # 1000*1.05 不吸收
    assert abs(r[1][2] - 0.0) < 1e-6 and abs(r[1][1] - 1050) < 1e-6
    assert r[0][3] == 2e6 and r[0][4] == 2
    # 801120: 归一基点1000(首日收益入基, 百分位不受影响); 次日0% → 1000
    r = conn.execute("SELECT close, pct_chg FROM sw_index_comp WHERE index_code='801120.SI' "
                     "ORDER BY date").fetchall()
    assert abs(r[0][0] - 1200) < 1e-6 and abs(r[0][1] - 20.0) < 1e-6  # 不吸收首日
    assert abs(r[1][0] - 1200) < 1e-6 and abs(r[1][1] - 0.0) < 1e-6
    # 无归属股不产生指数
    n = conn.execute("SELECT COUNT(*) FROM sw_index_comp WHERE index_code='999999'").fetchone()[0]
    assert n == 0
    conn.close()


def test_composite_incremental_carry(env):
    """D1-9r-review: 真carry路径 — 跨年块重建承接前收盘且不吸收首日收益
    (旧测试因年粒度过滤走fresh分支而空转)"""
    import raw_daily as RD
    # 跨年数据: 2005两日 + 2006首日
    raw = sqlite3.connect(str(RD.RAW_DB))
    raw.execute("INSERT INTO daily VALUES('000003.SZ','20060102',13.2,13.4,14.52,14.52,13.2,2e6)")  # +10% gap+1.5%
    raw.commit(); raw.close()
    SIC.build("20050101")                       # 全量(仅2005有数据): 801120 0104+20%,0105 0%
    conn = sqlite3.connect(str(env))
    c05 = conn.execute("SELECT close FROM sw_index_comp WHERE index_code='801120.SI' "
                       "AND date='2005-01-05'").fetchone()[0]
    conn.close()
    # 全量基点1000且不吸收首日: 0104=1000*1.2=1200, 0105=1200
    assert abs(c05 - 1200.0) < 1e-6, c05
    # 2006增量块(年过滤只含2006) → carry=1200, 首日收益保留: 1200*1.10=1320
    SIC.build("20060102")
    conn = sqlite3.connect(str(env))
    r = conn.execute("SELECT date, close, pct_chg FROM sw_index_comp "
                     "WHERE index_code='801120.SI' ORDER BY date").fetchall()
    # D1-9r2: open=前收盘×(1+成分跳空均值) — gap分量真实(原合成open≡前收盘)
    r0 = conn.execute("SELECT open, close FROM sw_index_comp WHERE index_code='801120.SI' "
                      "AND date='2005-01-05'").fetchone()
    conn.close()
    assert abs(r[-1][1] - 1320.0) < 1e-6, r[-1]       # 承接+首日收益(非1200也非重基1000系)
    assert abs(r[-1][2] - 10.0) < 1e-6                # pct_chg列与close隐含一致
    assert abs(r0[0] - 1200 * 1.0166667) < 0.01, r0   # 1200×(1+1.67%)
    # D1-9r3-review: 价格带包含open(跳空>振幅半宽时open不越high/low带)
    conn2 = sqlite3.connect(str(env))
    n_bad = conn2.execute("SELECT COUNT(*) FROM sw_index_comp WHERE open > high OR open < low").fetchone()[0]
    conn2.close()
    assert n_bad == 0, f"open越界{n_bad}行"


def test_composite_idempotent(env):
    SIC.build("20050101")
    SIC.build("20050101")
    conn = sqlite3.connect(str(env))
    n = conn.execute("SELECT COUNT(*) FROM sw_index_comp").fetchone()[0]
    conn.close()
    assert n == 4                                # 2指数 × 2日, PK不重复


def test_fetch_sw_daily_wired_and_tolerant(tmp_path, monkeypatch):
    """D1-9#5: SW底座每日增量入链 + 单步失败不阻塞"""
    import fetch_daily as FD
    calls = []

    def mk(n, ok=True):
        def f(start=""):
            calls.append(n)
            if not ok:
                raise RuntimeError(f"{n}挂了")
            return ok
        return f
    import types
    mods = {"raw_daily": types.SimpleNamespace(backfill=mk("raw"), agg=mk("agg")),
            "sw_pool": types.SimpleNamespace(build=mk("pool-est"), overlay=mk("pool-official")),
            "sw_fflow": types.SimpleNamespace(backfill=mk("fflow-b"), agg=mk("fflow-a")),
            "sw_index_comp": types.SimpleNamespace(build=mk("sic"))}
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name in mods:
            return mods[name]
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert FD.fetch_sw_daily(lookback=10) is True
    assert set(calls) == {"raw", "agg", "pool-est", "pool-official", "fflow-b", "fflow-a", "sic"}
    # 单步失败容错: pool挂 → 其余仍执行
    calls.clear()
    mods["sw_pool"] = types.SimpleNamespace(build=mk("pool-est", ok=False),
                                            overlay=mk("pool-official"))
    assert FD.fetch_sw_daily() is True
    assert "sic" in calls and "pool-est" in calls   # 失败步记录但链继续
    # 主链接入断言
    src = Path(FD.__file__).read_text(encoding="utf-8")
    assert "fetch_sw_daily()" in src.split("def main")[1]


def test_sector_sw_weights_parity():
    """D1-9: SW行分量对齐 — weights剔除ud_ratio/fflow"""
    import sector as S
    import inspect
    src = inspect.getsource(S._calc_sw_temperature)
    assert 'k not in ("ud_ratio", "fflow")' in src
