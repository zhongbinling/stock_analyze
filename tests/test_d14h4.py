# -*- coding: utf-8 -*-
"""D1-14 H4: 情绪图副图 — aux数据对齐/容错/模板接线/JS语法"""
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))


@pytest.fixture()
def aux_db(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    c = sqlite3.connect(str(p))
    c.executescript("""
      CREATE TABLE index_val_daily(code TEXT, date TEXT, pe REAL, pb REAL, name TEXT,
        PRIMARY KEY(code, date));
      CREATE TABLE margin_daily(date TEXT PRIMARY KEY, rzrqye REAL);
      CREATE TABLE etf_daily(code TEXT, date TEXT, close REAL, amount REAL, name TEXT,
        vol REAL, PRIMARY KEY(code, date));
    """)
    c.execute("INSERT INTO index_val_daily VALUES('000300.SH','2026-10-22',13.8,1.35,'沪深300')")
    c.execute("INSERT INTO index_val_daily VALUES('000300.SH','2026-10-23',13.9,1.36,'沪深300')")
    c.execute("INSERT INTO margin_daily VALUES('2026-10-23', 2.4e12)")
    c.execute("INSERT INTO etf_daily VALUES('510300.SH','2026-10-23',4.1,15.4e5,'300ETF华泰柏瑞',3.7e7)")
    c.commit()
    c.close()
    import viewer as VW
    monkeypatch.setattr(VW, "DB_PATH", p)
    return {"VW": VW, "path": p}


def test_read_aux_aligned_and_units(aux_db):
    """对齐全量日期轴; 单位换算(两融元→亿, ETF千元→亿); 缺日为None"""
    VW = aux_db["VW"]
    dates = ["2026-10-21", "2026-10-22", "2026-10-23"]
    r = VW._read_aux(dates)
    assert r["val"]["000300.SH"]["n"] == "沪深300"
    assert r["val"]["000300.SH"]["pe"] == [None, 13.8, 13.9]
    assert r["margin"] == [None, None, 24000.0]          # 2.4万亿→24000亿
    assert r["etf"]["510300.SH"]["amt"] == [None, None, 15.4]   # 15.4e5千元→15.4亿
    assert len(r["val"]) == 7 and len(r["etf"]) == 8


def test_read_aux_tolerates_missing_tables(tmp_path, monkeypatch):
    """空库(表缺失) → 空副图不抛错(视图降级)"""
    import viewer as VW
    monkeypatch.setattr(VW, "DB_PATH", tmp_path / "none.db")
    r = VW._read_aux(["2026-10-23"])
    assert r == {"val": {}, "margin": [], "etf": {}}


def test_html_aux_panels_wired():
    """模板接线: 副图控制行/PANELS/开关记忆/数据消费"""
    import viewer as VW
    h = VW.HTML
    assert 'id="ckPE"' in h and 'id="ckPB"' in h
    assert 'id="ckMargin"' in h and 'id="ckEtf"' in h
    assert "PANELS" in h and "auxPanels_v1" in h and "localStorage" in h
    assert "valSeries" in h and "marginSeries" in h and "etfSeries" in h
    assert "D.etfSum" in h and "300PE" in h


def test_html_js_syntax():
    """主脚本块 node 语法门禁"""
    import shutil
    if not shutil.which("node"):
        pytest.skip("node 不可用")
    import viewer as VW
    h = VW.HTML
    import re
    blocks = re.findall(r"<script>([\s\S]*?)</script>", h)
    main = max(blocks, key=len)                     # 主脚本(非echarts占位)
    assert "render" in main
    r = subprocess.run([shutil.which("node"), "-e", "new Function(process.argv[1])", main],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"JS 语法错误: {r.stderr[:600]}"


def test_read_aux_partial_db(tmp_path, monkeypatch):
    """D1-14r: 三源独立容错 — 只有margin表的旧库, 两融副图照常出, 估值/ETF空"""
    p = tmp_path / "s.db"
    c = sqlite3.connect(str(p))
    c.execute("CREATE TABLE margin_daily(date TEXT PRIMARY KEY, rzrqye REAL)")
    c.execute("INSERT INTO margin_daily VALUES('2026-10-23', 1.8e12)")
    c.commit()
    c.close()
    import viewer as VW
    monkeypatch.setattr(VW, "DB_PATH", p)
    r = VW._read_aux(["2026-10-23"])
    assert r["margin"] == [18000.0]           # 两融在
    assert r["val"] == {} and r["etf"] == {}  # 缺表的源各自降级不拖累


def test_zb_jl_not_duplicated_in_base_series():
    """D1-14r: 炸板/接力只由SUB_OPTS动态提供一次(不进base series重复叠加)"""
    import viewer as VW
    h = VW.HTML
    assert "data: D.zb, showSymbol" not in h      # base series 无静态炸板/接力线
    assert "data: D.jl, showSymbol" not in h
    assert "data = D.zb" in h and "data = D.jl" in h   # SUB_OPTS 动态提供恰一处
