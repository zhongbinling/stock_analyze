# -*- coding: utf-8 -*-
"""D1-26: 实时页三优化 — ETF昨日曲线/情绪放大镜/三grid量额分离"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    import server.rt as rt
    monkeypatch.setattr(rt, "RT_DB", tmp_path / "rt.db")
    monkeypatch.setattr(rt, "_mkline_cache", {})
    core = tmp_path / "c"
    (core / "data").mkdir(parents=True)
    import shutil
    shutil.copy(CORE / "config.yaml", core / "config.yaml")
    monkeypatch.setattr(rt, "CORE", core)
    return {"rt": rt, "mp": monkeypatch, "core": core}


def _mk_bars(code, day1, day2):
    """两日分钟bar(每3分钟一根, 量递增)"""
    out = []
    for day in (day1, day2):
        for i in range(5):
            out.append([f"{day}{930 + i:04d}", "1", "1", "1", "1",
                        str(100 * (i + 1))])
    return out


def test_yday_etf_curve_anchored(env):
    """昨日ETF分钟曲线: 8只量比形态 × etf_daily昨日精确总额锚定"""
    rt, mp = env["rt"], env["mp"]
    bars = _mk_bars("x", "20261029", "20261030")
    mp.setattr(rt, "_mkline", lambda code="sh000001", n=640: bars)
    db = env["core"] / "data" / "sentiment.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE etf_daily(date TEXT, amount REAL)")
    c.execute("INSERT INTO etf_daily VALUES('2026-10-29', 4.65e7)")   # 465亿=4.65e7千元
    c.commit(); c.close()
    r = rt._yday_etf_curve("2026-10-30")
    assert r["date"] == "2026-10-29" and r["total_yi"] == 465.0
    pts = list(r["points"].items())
    assert pts[0][1] < pts[-1][1] and pts[-1][1] == 465.0
    vals = [v for _, v in pts]
    assert vals == sorted(vals)                        # 累计单调
    assert rt._yday_etf_curve(None) is None


def test_yday_etf_no_data(env):
    """mkline缺/昨日额缺 → None(前端graceful)"""
    rt, mp = env["rt"], env["mp"]
    mp.setattr(rt, "_mkline", lambda code="sh000001", n=640: [])
    assert rt._yday_etf_curve("2026-10-30") is None


def test_page_gates():
    html = (REPO / "server" / "templates" / "admin" / "realtime.html").read_text(encoding="utf-8")
    for mk in ("chartScoreZoom",          # 情绪放大镜
               "scOpen - 20", "scOpen + 20",   # ±20定域
               "chartMain", "height:660px",    # 主图扩容
               "yday_etf", "昨日累计",          # ETF昨对照
               "gridIndex: 2"):               # 三grid
        assert mk in html, f"页面缺: {mk}"


def test_latest_carries_yday_etf(server_app, monkeypatch, tmp_path):
    from server import rt
    monkeypatch.setattr(rt, "RT_DB", tmp_path / "rt.db")
    monkeypatch.setattr(rt, "_mkline_cache", {})
    monkeypatch.setattr(rt, "_yday_etf_curve", lambda d: {"date": "2026-10-29",
                                                         "total_yi": 465.0, "points": {}})
    st = rt.latest()
    assert st["meta"]["yday_etf"]["total_yi"] == 465.0


def test_yday_etf_prev_mismatch_skips_code(env):
    """某code昨日基准日与他code不一(覆盖差) → 跳过该code不混日"""
    rt, mp = env["rt"], env["mp"]
    full = _mk_bars("x", "20261029", "20261030")          # 昨日=10-29
    short = _mk_bars("x", "20261028", "20261029")[:3]     # 该code只到10-29前段? 制造prev=10-28
    # 实际构造: 无10-30的bars → prev(相对10-30)=10-28 ≠ 10-29
    older = [b for b in short if b[0].startswith("20261028")]
    seq = iter([full, older] + [full] * 6)                 # 第2个code异常
    mp.setattr(rt, "_mkline", lambda code="sh000001", n=640: next(seq, full))
    db = env["core"] / "data" / "sentiment.db"
    import sqlite3 as sq
    c = sq.connect(str(db))
    c.execute("CREATE TABLE IF NOT EXISTS etf_daily(date TEXT, amount REAL)")
    c.execute("INSERT INTO etf_daily VALUES('2026-10-29', 4.65e7)")
    c.commit(); c.close()
    r = rt._yday_etf_curve("2026-10-30")
    assert r is not None and r["date"] == "2026-10-29"     # 仍以多数code基准日
