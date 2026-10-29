# -*- coding: utf-8 -*-
"""D1-21 体检修复三轮: 备份KeyError根修 / margin近端段 / sw_daily退出豁免 /
无资产行区间显示 / 修复提示"""
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))


# ── 备份: 新资产登记完整性(昨晚日备 KeyError 'realtime' 根因) ──

def test_asset_files_covers_all_assets():
    """ASSETS 的每个资产都必须在 ASSET_FILES(备份文件名映射) — 注册新资产
    漏登本表 = 日备在 sentiment 之后 KeyError(实测 2026-10-29 22:35)"""
    import backup as BK
    missing = set(BK.ASSETS) - set(BK.ASSET_FILES)
    assert not missing, f"ASSET_FILES 缺: {missing}"
    assert BK.ASSET_FILES["realtime"] == "realtime.db"
    assert BK.ASSET_FILES["macro"] == "macro.db"


def test_backup_with_realtime_file(tmp_path, monkeypatch):
    """realtime.db 实际存在时日备全链不再 KeyError"""
    import backup as BK
    sent = tmp_path / "sentiment.db"
    c = sqlite3.connect(str(sent))
    c.execute("CREATE TABLE sentiment(date TEXT PRIMARY KEY, score REAL)")
    c.execute("INSERT INTO sentiment VALUES('2026-09-30', -30.0)")
    c.commit(); c.close()
    rt = tmp_path / "realtime.db"
    c = sqlite3.connect(str(rt))
    c.execute("CREATE TABLE rt_day(date TEXT PRIMARY KEY)")
    c.execute("INSERT INTO rt_day VALUES('2026-10-29')")
    c.commit(); c.close()
    monkeypatch.setattr(BK, "ASSETS", {
        "sentiment": ("sqlite", sent), "realtime": ("sqlite", rt)})
    monkeypatch.setattr(BK, "BACKUP_DIR", tmp_path / "b")
    monkeypatch.setattr(BK, "MANIFEST", tmp_path / "b" / "manifest.json")
    monkeypatch.setattr(BK, "LOG_FILE", tmp_path / "b" / "backup.log")
    r = BK.run_all("daily")
    assert r["status"] in ("done", "ok") and r["files_n"] == 2, r.get("error")


# ── margin 回补两段化(近端缺口死路根修) ──

def test_backfill_margin_recent_segment(tmp_path, monkeypatch):
    """近端缺口段: 库内最晚+1 → 今天 也拉取(原仅"更早段"对近端是死路)"""
    import fetch_daily as FD
    p = tmp_path / "s.db"
    c = sqlite3.connect(str(p))
    c.execute("CREATE TABLE margin_daily(date TEXT PRIMARY KEY, rzrqye REAL)")
    c.execute("INSERT INTO margin_daily VALUES('2026-10-28', 1.8e12)")
    c.commit(); c.close()
    pulled = []
    monkeypatch.setattr(FD, "connect", lambda: sqlite3.connect(str(p)))
    monkeypatch.setattr(FD, "_pull_margin",
                        lambda conn, lo, hi: (pulled.append((lo, hi)), 0)[1])
    monkeypatch.setattr("tushare_api.has_token", lambda: True, raising=False)
    import types
    fake_t = types.SimpleNamespace(has_token=lambda: True)
    monkeypatch.setitem(sys.modules, "tushare_api", fake_t)
    assert FD.backfill_margin("20100331") is True
    los = [lo for lo, _ in pulled]
    assert any(lo.endswith("1029") or lo >= "20261029" for lo in los), pulled  # 近端段
    assert any(lo == "20100331" for lo in los), pulled                          # 更早段


# ── sw_daily 退出体检豁免(源已恢复) ──

def test_sw_daily_back_in_scan():
    from server import market
    assert "sw_daily" not in market._SCAN_SKIP
    import fetch_daily as FD
    assert "sw_daily" in FD.BACKFILLS          # 行内补全仍可用


# ── 页面: 无资产行区间 + margin提示 ──

def test_page_span_and_hint_gates():
    html = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")
    assert "span: ['2012-01', '2019-12']" in html          # zt_est 静态区间
    assert "${r.span ? r.span[0]" in html                  # 最早列区间渲染
    assert "${r.span ? r.span[1]" in html
    assert "targets.includes('margin_hist')" in html       # 两融T+1提示
    assert "官方源2026-10恢复" in html                      # sw_daily 文案更新
