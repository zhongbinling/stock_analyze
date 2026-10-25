# -*- coding: utf-8 -*-
"""D1-11h: 热力图日期轴修复 — 视图交易日过滤/周末快照守卫/曲线类目轴"""
import datetime as dt
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))


def test_is_trading_day():
    import sectors as S
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER, pretrade_date TEXT)")
    c.executemany("INSERT INTO calendar VALUES(?,?,NULL)",
                  [("20261016", 1), ("20261017", 0), ("20261018", 0), ("20261019", 1)])
    c.commit()
    assert S.is_trading_day(c, "2026-10-16") is True       # 日历开市
    assert S.is_trading_day(c, "2026-10-17") is False      # 周六关市
    assert S.is_trading_day(c, "2026-10-18") is False      # 周日关市
    assert S.is_trading_day(c, "2030-01-07") is True       # 未覆盖 → 周一粗口径
    assert S.is_trading_day(c, "2030-01-05") is False      # 未覆盖 → 周六粗口径
    c.close()


def test_snapshot_guard_wired():
    """D1-11h: 当日快照写入前有交易日守卫(周末不产生伪交易日行)"""
    import inspect
    import sectors as S
    src = inspect.getsource(S.update_daily)
    assert "is_trading_day(conn, today)" in src
    assert "非交易日" in src


def test_build_payload_filters_non_trading(fixture_db, tmp_path, monkeypatch):
    """D1-11h: 日历覆盖段内的非交易日评分日被滤出视图轴(伪交易日行防护)"""
    import sector_viewer as SV
    p = tmp_path / "copy.db"
    shutil.copy2(fixture_db, p)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE calendar(cal_date TEXT PRIMARY KEY, is_open INTEGER, pretrade_date TEXT)")
    rows = [r[0] for r in c.execute("SELECT DISTINCT date FROM sector_scores ORDER BY date")]
    for d in rows:                      # 覆盖全区间: 周六关市, 其余开市
        is_open = 0 if dt.date.fromisoformat(d).weekday() == 5 else 1
        c.execute("INSERT OR REPLACE INTO calendar VALUES(?,?,NULL)",
                  (d.replace("-", ""), is_open))
    c.commit()
    c.close()
    monkeypatch.setattr(SV, "DB", p)
    payload = SV.build_payload(years=3)
    assert "2026-09-26" not in payload["dates"]        # 周六(评分表有行)被滤除
    assert "2026-09-25" in payload["dates"]            # 交易日保留


def test_generated_html_trading_day_axes(fixture_html):
    """D1-11h: 曲线改交易日类目轴 — 废除连续time轴(周末刻度/空隙的来源)"""
    html = Path(fixture_html).read_text(encoding="utf-8")
    assert "type: 'time', axisLabel" not in html
    assert "xAxis: catAxis" in html
    assert "type: 'category', data: D.dates.map" in html
