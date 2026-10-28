# -*- coding: utf-8 -*-
"""D1-17 R3+R4: 实时采集 — 时段判定/采样落库/跨日/快照日乖离/API/页面/备份注册"""
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


# ── 时段判定(纯函数) ──

def test_is_market_time():
    from server import rt
    assert rt.is_market_time(datetime(2026, 10, 28, 10, 0))       # 周三上午
    assert rt.is_market_time(datetime(2026, 10, 28, 14, 30))      # 下午
    assert rt.is_market_time(datetime(2026, 10, 28, 9, 16))       # 开盘后
    assert not rt.is_market_time(datetime(2026, 10, 31, 10, 0))   # 周六
    assert not rt.is_market_time(datetime(2026, 10, 28, 12, 0))   # 午休
    assert not rt.is_market_time(datetime(2026, 10, 28, 9, 5))    # 早于09:15
    assert not rt.is_market_time(datetime(2026, 10, 28, 15, 30))  # 收盘后


# ── tick 采样与落库(mock网络与评分) ──

@pytest.fixture()
def rt_env(tmp_path, monkeypatch):
    import server.rt as rt
    db = tmp_path / "realtime.db"
    monkeypatch.setattr(rt, "RT_DB", db)
    monkeypatch.setattr(rt, "_state", {"date": None, "samples": [], "updated_at": None})
    return {"rt": rt, "db": db, "monkeypatch": monkeypatch}


def _mock_sources(env, snap_date, score=-20.5, etf=123.4):
    rt = env["rt"]
    import types
    fake_rs = types.SimpleNamespace(
        fetch_snapshot=lambda conn: {"date": snap_date, "close": 3681.5,
                                     "volume1": 4.3e8, "amount1": 7.5e11,
                                     "zt_count": 72, "jl_rate": 19.5},
        compute_rt=lambda conn, snap: {"score": score, "v1": score - 5,
                                       "stage": "偏冷/退潮"})
    fake_db = types.SimpleNamespace(connect=lambda: sqlite3.connect(":memory:"))
    env["monkeypatch"].setitem(sys.modules, "rt_score", fake_rs)
    env["monkeypatch"].setitem(sys.modules, "db", fake_db)
    env["monkeypatch"].setattr(rt, "_fetch_etf_amount_yi", lambda: etf)


def test_tick_samples_and_persists(rt_env):
    _mock_sources(rt_env, "2026-10-28")
    now = datetime(2026, 10, 28, 10, 0, 3)
    assert rt_env["rt"].tick(now) == "ok"
    assert rt_env["rt"].tick(datetime(2026, 10, 28, 10, 0, 20)) == "ok"
    con = sqlite3.connect(str(rt_env["db"]))
    row = con.execute("SELECT date, n, payload FROM rt_day").fetchone()
    con.close()
    assert row[0] == "2026-10-28" and row[1] == 2
    samples = json.loads(row[2])["samples"]
    assert samples[0]["t"] == "10:00:03" and samples[0]["score"] == -20.5
    assert samples[0]["etf"] == 123.4 and samples[1]["cumamt"] == 7500.0


def test_tick_day_rolls(rt_env):
    """跨交易日: 缓冲重置, rt_day 两行各自独立"""
    _mock_sources(rt_env, "2026-10-28")
    rt_env["rt"].tick(datetime(2026, 10, 28, 10, 0))
    _mock_sources(rt_env, "2026-10-29", score=-10.0)
    rt_env["rt"].tick(datetime(2026, 10, 29, 9, 30))
    con = sqlite3.connect(str(rt_env["db"]))
    rows = dict(con.execute("SELECT date, n FROM rt_day").fetchall())
    con.close()
    assert rows == {"2026-10-28": 1, "2026-10-29": 1}
    st = rt_env["rt"].latest()
    assert st["date"] == "2026-10-29" and len(st["samples"]) == 1


def test_tick_stale_and_idle(rt_env):
    """非交易时段=idle不采样; 盘中但快照日≠今天(节假日)=stale"""
    _mock_sources(rt_env, "2026-10-27")
    assert rt_env["rt"].tick(datetime(2026, 10, 28, 12, 0)) == "idle"      # 午休
    assert rt_env["rt"].tick(datetime(2026, 10, 31, 10, 0)) == "idle"      # 周六
    assert rt_env["rt"].tick(datetime(2026, 10, 28, 10, 0)) == "stale"     # 节假日兜底
    assert rt_env["rt"]._state["samples"] == []                            # 未采样


def test_day_readback(rt_env):
    """R4 回看: day()/days() 读存档"""
    _mock_sources(rt_env, "2026-10-28")
    rt_env["rt"].tick(datetime(2026, 10, 28, 10, 0))
    d = rt_env["rt"].day("2026-10-28")
    assert d["samples"][0]["score"] == -20.5
    assert rt_env["rt"].day("2026-10-27") is None
    assert rt_env["rt"].days() == ["2026-10-28"]


# ── API 与页面 ──

def test_rt_api_endpoints(server_app, monkeypatch):
    from server import rt
    c = server_app["client"]
    # latest: 空态也 200
    r = c.get("/admin/api/rt/latest")
    assert r.status_code == 200 and r.get_json()["samples"] == []
    # day: 格式校验400 / 无存档404
    assert c.get("/admin/api/rt/day?date=bad").status_code == 400
    assert c.get("/admin/api/rt/day?date=2020-01-01").status_code == 404
    # settings: 开关落 AppSetting
    r = c.post("/admin/api/rt/settings", json={"enabled": False})
    assert r.status_code == 200 and "关" in r.get_json()["msg"]
    from server import jobs
    with server_app["app"].app_context():
        assert jobs.get_setting("rt_enabled") == "0"
        jobs.set_setting("rt_enabled", "1")
    assert c.post("/admin/api/rt/settings", json={}).status_code == 400
    # days
    r = c.get("/admin/api/rt/days")
    assert r.status_code == 200 and "days" in r.get_json()
    monkeypatch.setattr(rt, "RT_DB", rt.RT_DB)      # 真库路径不动测试断言


def test_realtime_page_renders(server_app):
    r = server_app["client"].get("/admin/realtime")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    for marker in ("实时行情", "chartMain", "chartScore", "chartEtf",
                   "echarts.min.js", "rtEnabled", "回看模式"):
        assert marker in body, f"页面缺标记: {marker}"


def test_realtime_html_js_syntax():
    import re
    import shutil as _sh
    if not _sh.which("node"):
        pytest.skip("node 不可用")
    html = (REPO / "server" / "templates" / "admin" / "realtime.html").read_text(encoding="utf-8")
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m, "缺 script 块"
    r = subprocess.run([_sh.which("node"), "-e", "new Function(process.argv[1])", m.group(1)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"JS 语法错误: {r.stderr[:400]}"


def test_realtime_registered_in_backup():
    """备份矩阵注册: ASSETS + base块 + 展示块名"""
    import backup as BK
    from server.routes_admin import BACKUP_BLOCKS, BLOCK_ASSETS
    assert "realtime" in BK.ASSETS
    assert "realtime" in BLOCK_ASSETS["base"]
    assert BACKUP_BLOCKS["realtime"] == "实时行情"


def test_backup_missing_realtime_skips_gracefully(tmp_path, monkeypatch):
    """首次采集前 realtime.db 不存在 → 备份优雅跳过不报错"""
    import backup as BK
    monkeypatch.setattr(BK, "ASSETS", {"realtime": ("sqlite", tmp_path / "none.db")})
    monkeypatch.setattr(BK, "BACKUP_DIR", tmp_path / "b")
    monkeypatch.setattr(BK, "MANIFEST", tmp_path / "b" / "manifest.json")
    monkeypatch.setattr(BK, "LOG_FILE", tmp_path / "b" / "backup.log")
    r = BK.run_all("daily")
    assert not r.get("error") and r["files"] == []          # 跳过=无文件非失败
