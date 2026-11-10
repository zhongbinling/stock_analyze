# -*- coding: utf-8 -*-
"""避雷针A2: 十大流通股东/两融/季度净利(单季差分)/估值分位/首访自动拉取/API"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import fetch_stock as FS  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "stock.db"
    monkeypatch.setattr(FS, "STOCK_DB", db)
    return db


# ── 单季差分(模块④核心口径) ──

def test_single_quarters_diff():
    """累计→单季: 0331原值, 其余年内差分; 跨年重置"""
    cum = [("2025-03-31", 1.0), ("2025-06-30", 3.0), ("2025-09-30", 6.0),
           ("2025-12-31", 8.0), ("2026-03-31", 2.5)]
    s = FS._single_quarters(cum)
    assert [v for _, v in s] == [1.0, 2.0, 3.0, 2.0, 2.5]     # Q1=1, Q2=3-1, Q3=6-3, Q4=8-6, 次Q1=2.5


def test_profit_payload_yoy(env):
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('000001.SZ','测试','','t','t')")
    data = [("2025-03-31", 1.0e8), ("2025-06-30", 3.0e8), ("2025-09-30", 6.0e8),
            ("2025-12-31", 8.0e8), ("2026-03-31", 1.0e8), ("2026-06-30", 2.0e8)]
    con.executemany("INSERT INTO stock_finance(ts_code,end_date,profit_dedt) "
                    "VALUES('000001.SZ',?,?)", data)
    con.commit()
    con.close()
    p = FS.profit_payload("000001.SZ", env)
    assert p["cum"] == [1.0, 3.0, 6.0, 8.0, 1.0, 2.0]           # 元→亿
    assert p["single"] == [1.0, 2.0, 3.0, 2.0, 1.0, 1.0]
    # 同比: 2026Q1单季1.0 vs 2025Q1单季1.0 → 0%; 2026Q2单季1.0 vs 2.0 → -50%
    assert p["single_yoy"][-2] == 0.0
    assert p["single_yoy"][-1] == -50.0


# ── 十大流通(模块②) ──

def test_holders_payload_tags_and_chg(env):
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('000001.SZ','测试','','t','t')")
    for period, holders in {
            "2026-06-30": [("香港中央结算有限公司", 1e8, 3.0), ("某公募基金A", 5e7, 1.5)],
            "2026-09-30": [("香港中央结算有限公司", 1.2e8, 3.5), ("某公募基金A", 4e7, 1.0)]}.items():
        con.executemany("INSERT INTO stock_holders VALUES('000001.SZ',?,?,?,?)",
                        [(period, n, a, r) for n, a, r in holders])
    con.execute("INSERT INTO stock_holdernum VALUES('000001.SZ','2026-06-30', 5e5)")
    con.execute("INSERT INTO stock_holdernum VALUES('000001.SZ','2026-09-30', 6e5)")
    con.commit()
    con.close()
    d = FS.holders_payload("000001.SZ", env)
    assert d["periods"] == ["2026-09-30", "2026-06-30"]         # 倒序
    q3 = d.data if hasattr(d, "data") else d["data"]
    cur = q3[0]
    hk = next(h for h in cur["holders"] if "香港" in h["name"])
    assert hk["tag"] == "北向" and hk["chg"] == 0.5             # 3.5-3.0环比
    pub = next(h for h in cur["holders"] if "基金" in h["name"])
    assert pub["tag"] == "公募/资管" and pub["chg"] == -0.5
    assert cur["holder_num"] == 6e5


def test_holder_tag_keywords():
    assert FS._holder_tag("中央汇金资产管理") == "国家队"
    assert FS._holder_tag("全国社保基金一零四组合") == "国家队"
    assert FS._holder_tag("张三") == "其他"


# ── 两融(模块③): 占流通市值比 ──

def test_margin_ratio_with_val_join(env):
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('000001.SZ','测试','','t','t')")
    # rzye元(margin_detail口径): 11-05=50亿(5e9), 11-06=52亿; 融券各0.2亿(2e7)
    con.executemany("INSERT INTO stock_margin VALUES('000001.SZ',?,?,?,NULL,NULL)", [
        ("2026-11-05", 5.0e9, 2.0e7), ("2026-11-06", 5.2e9, 2.0e7)])
    # 流通市值100亿(1e6万元)仅11-05有 → 11-06前向沿用
    con.execute("INSERT INTO stock_val(ts_code,trade_date,circ_mv) "
                "VALUES('000001.SZ','2026-11-05',1.0e6)")
    con.commit()
    con.close()
    m = FS.margin_payload("000001.SZ", env)
    assert m["rzye"] == [50.0, 52.0]                            # 万元→亿
    assert m["rqye"] == [0.2, 0.2]                              # 2e3万=0.2亿
    assert m["ratio"] == [50.0, 52.0]                           # 50亿/100亿=50%, 前向沿用


# ── 估值(模块⑤): 季度末取值与分位 ──

def test_val_payload_quarter_end_and_pct(env):
    con = FS._conn(env)
    con.execute("INSERT INTO stock_pool VALUES('000001.SZ','测试','','t','t')")
    # 12个季度末PE(1..12), 每季末后一日(次季初)有值也应被正确跳过
    import datetime as dt
    rows = []
    for i in range(1, 13):
        y, m = 2024 + (i - 1) // 4, ((i - 1) % 4) * 3 + 3
        rows.append((dt.date(y, m, 28).isoformat(), float(i), round(0.5 + i * 0.1, 2)))
        if i < 12:                                            # 末季不加(避免自成一组)
            ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)      # 次季初噪声(下月2日)
            rows.append((dt.date(ny, nm, 2).isoformat(), 99.0, 9.9))
    con.executemany("INSERT INTO stock_val(ts_code,trade_date,pe_ttm,pb) "
                    "VALUES('000001.SZ',?,?,?)", rows)
    con.commit()
    con.close()
    v = FS.val_payload("000001.SZ", env)
    assert len(v["dates"]) == 12                                 # 恰12个季度末(噪声剔除)
    assert v["pe"][-1] == 12.0                                   # 末日取季度末值非噪声
    # 最新PE=12为窗口最大 → 分位100%
    assert v["pe_p5"][-1] == 100


# ── 首访自动拉取 + API ──

def test_module_autofetch_and_api(server_app, monkeypatch, tmp_path):
    import fetch_stock as FS
    refetched = []

    def mk_payload(empty):
        def p(code, path=None):
            return {"ok": True, "code": code, "data": [] if empty else [1],
                    "dates": [], "n": 0 if empty else 1}
        return p
    monkeypatch.setattr(FS, "holders_payload", mk_payload(True))
    monkeypatch.setattr(FS, "margin_payload", mk_payload(True))
    monkeypatch.setattr(FS, "val_payload", mk_payload(True))
    monkeypatch.setattr(FS, "profit_payload", lambda code, path=None:
                        {"ok": True, "code": code, "dates": []})
    for fn in ("refresh_holders", "refresh_margin", "refresh_val", "refresh_finance"):
        monkeypatch.setattr(FS, fn, lambda code, path=None: refetched.append(1) or 1)
    c = server_app["client"]
    # 空数据触发refresh
    r = c.get("/admin/api/stock/holders?code=000001.SZ")
    assert r.status_code == 200 and refetched == [1]
    # profit空dates触发finance拉取
    c.get("/admin/api/stock/profit?code=000001.SZ")
    assert refetched == [1, 1]
    # 非法mod
    assert c.get("/admin/api/stock/hack").status_code == 400


def test_a2_ui_markers():
    import re
    import shutil
    import subprocess
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    for mk in ("hdPeriod", "chartMargin", "chartProfit", "chartVal", "chartHnum",
               "loadA2", "renderHolders", "renderMargin", "renderProfit", "renderVal",
               "pfMode", "北向", "国家队", "同比%", "3年分位"):
        assert mk in html, f"缺: {mk}"
    if shutil.which("node"):
        blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
        main = max(blocks, key=len)
        r = subprocess.run([shutil.which("node"), "-e",
                            "new Function(process.argv[1])", main],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f"JS语法错误: {r.stderr[:300]}"


def test_single_quarters_missing_base_none():
    """年内缺基期(有Q2无Q1) → 单季不可知=None(不拿累计冒充单季)"""
    s = FS._single_quarters([("2026-06-30", 3.0), ("2026-09-30", 6.0)])
    assert s == [("2026-06-30", None), ("2026-09-30", 3.0)]     # Q2无基期None, Q3有基期差分


def test_pfmsg_not_pfstate():
    """UI: 消息写pfMsg(独立span), 不再覆盖pfMode单选(review修复)"""
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    assert 'id="pfMsg"' in html
    assert "$('pfMsg').textContent" in html
    assert "$('pfState').textContent" not in html


def test_a2_load_function_exists():
    """review修复回归: loadA2定义必须存在 — B2批JS块替换时误删定义,
    调用点仍在 → holders/margin/profit/val四模块全空(用户实测)"""
    html = (REPO / "server" / "templates" / "admin" / "stock.html").read_text(encoding="utf-8")
    assert "async function loadA2" in html, "loadA2定义被删!"
    assert html.count("loadA2(") >= 2               # 定义+调用
    assert "户数环比" in html and "筹码" in html      # 户数变化指示(用户找回)
