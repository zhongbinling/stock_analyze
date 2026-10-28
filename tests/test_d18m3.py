# -*- coding: utf-8 -*-
"""D1-18 M3: 事件标注 — 事件读取/模板接线/转义/生成e2e"""
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))


def test_read_events_from_fixture(tmp_path, monkeypatch):
    import viewer as VW
    db = tmp_path / "data" / "macro.db"
    db.parent.mkdir()
    c = sqlite3.connect(str(db))
    c.execute("""CREATE TABLE macro_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT, level INTEGER, content TEXT, source TEXT, date TEXT, type TEXT,
      created_at TEXT, updated_at TEXT)""")
    c.executemany("INSERT INTO macro_events(title,level,date,type,content,source) VALUES(?,?,?,?,?,?)",
                  [("b事件", 4, "2024-09-24", "政策", "组合拳", "s"),
                   ("a事件", 5, "2024-02-05", "市场", "危机", "s"),
                   ("c事件", 3, "2026-10-27", "产业", None, None)])
    c.commit()
    c.close()
    monkeypatch.setattr(VW, "BASE", tmp_path)
    evs = VW._read_events()
    assert [e["date"] for e in evs] == ["2024-02-05", "2024-09-24", "2026-10-27"]  # 升序
    assert evs[0]["level"] == 5 and evs[2]["content"] is None
    # 缺失库 → 空列表(标注面板自动隐藏)
    monkeypatch.setattr(VW, "BASE", tmp_path / "none")
    assert VW._read_events() == []


def test_html_event_annotation_wired():
    import viewer as VW
    h = VW.HTML
    # 控件: 五级勾选, 默认L3-L5
    assert h.count('class="ckEvt"') == 5
    assert 'class="ckEvt" value="5" checked' in h and 'class="ckEvt" value="2"' in h
    # 机制: 二分吸附/双grid/按id增量/勾选刷新/转义
    for marker in ("EV_COLORS", "evSeriesData", "setEvents", "evIdx", "evScore",
                   "D.evOn", "escE", "时间区事件", "slice(-12).reverse()"):
        assert marker in h, f"缺接线: {marker}"
    assert "symbol: 'diamond'" in h
    # 悬停框事件行
    assert "seriesId === 'evIdx'" in h
    # 载荷消费(HTML侧); 生成侧由 test_read_events + e2e 覆盖
    assert "D.events" in h and "D.evIdx" in h


def test_viewer_js_syntax_still_clean():
    """既有 node 门禁自动覆盖新增脚本(单独跑一遍确保)"""
    if not shutil.which("node"):
        pytest.skip("node 不可用")
    import viewer as VW
    blocks = re.findall(r"<script>([\s\S]*?)</script>", VW.HTML)
    main = max(blocks, key=len)
    r = subprocess.run([shutil.which("node"), "-e", "new Function(process.argv[1])", main],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"JS 语法错误: {r.stderr[:400]}"
