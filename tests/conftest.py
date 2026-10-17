# -*- coding: utf-8 -*-
"""测试夹具: 用临时 SQLite 构造确定性数据 → build_payload/generate_html 全栈可测"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

SCHEMA = """
CREATE TABLE sectors(code TEXT PRIMARY KEY, name TEXT, kind TEXT,
  enabled INTEGER DEFAULT 1, first_date TEXT, last_date TEXT, updated_at TEXT);
CREATE TABLE sector_scores(date TEXT, code TEXT, score REAL, rank_pct REAL,
  stage TEXT, components TEXT, PRIMARY KEY(date, code));
CREATE TABLE sentiment(date TEXT PRIMARY KEY, score REAL, stage TEXT,
  components TEXT, weights TEXT);
CREATE TABLE sw1_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT, PRIMARY KEY(bk_code));
CREATE TABLE sw2_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT, sw2_name TEXT, PRIMARY KEY(bk_code));
"""


def _dates(n, end=(2026, 9, 30)):
    """n 个连续日历日(倒推), ISO 格式"""
    import datetime
    d = datetime.date(*end)
    out = []
    for _ in range(n):
        out.append(d.isoformat())
        d -= datetime.timedelta(days=1)
    return list(reversed(out))


def build_fixture_db(path: Path):
    """3 BK + 1 SW1 + 1 SW2 板块 × 30 日; sentiment 40 日; 近5日分量; 含 NULL score 行"""
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    dates30 = _dates(30)
    dates40 = _dates(40)

    boards = {  # code: (name, 起始分, 每日步长) — BK0001 升温, BK0002 修复, BK0003 含 NULL
        "BK0001": ("半导体", 20, 1.4),
        "BK0002": ("白酒", -40, 0.7),
        "BK0003": ("证券", 10, 0),
    }
    for i, d in enumerate(dates30):
        for c, (_n, s0, step) in boards.items():
            score = None if (c == "BK0003" and i == 10) else round(s0 + step * i, 1)
            conn.execute("INSERT INTO sector_scores VALUES(?,?,?,?,?,?)",
                         (d, c, score, 50.0, "中性", None))
        conn.execute("INSERT INTO sector_scores VALUES(?,?,?,?,?,?)", (d, "SW1_信息技术", 10.0, None, "中性", None))
        conn.execute("INSERT INTO sector_scores VALUES(?,?,?,?,?,?)", (d, "SW2_半导体", 30.0, None, "偏热", None))
    for c, (n, _s0, _step) in boards.items():
        conn.execute("INSERT INTO sectors VALUES(?,?,?,?,?,?,?)",
                     (c, n, "industry", 1, dates30[0], dates30[-1], None))

    # 近5日分量: BK0001 触发缩量上涨背离(chg≥50 且 vol_dir≤-50), BK0002 无 chg(守卫)
    comp1 = json.dumps({"chg": 55, "vol_dir": -60, "rsi": 40, "pos": 80}, ensure_ascii=False)
    comp2 = json.dumps({"vol_dir": -10, "rsi": 30}, ensure_ascii=False)
    for d in dates30[-5:]:
        conn.execute("UPDATE sector_scores SET components=? WHERE date=? AND code='BK0001'", (comp1, d))
        conn.execute("UPDATE sector_scores SET components=? WHERE date=? AND code='BK0002'", (comp2, d))

    # 大盘情绪 40 日, 尾部 -30(冷)
    for i, d in enumerate(dates40):
        conn.execute("INSERT INTO sentiment VALUES(?,?,?,?,?)",
                     (d, round(-30 + i * 0.5, 1), "偏冷", None, None))

    conn.execute("INSERT INTO sw1_map VALUES(?,?,?)", ("BK0001", "半导体", "信息技术"))
    conn.execute("INSERT INTO sw1_map VALUES(?,?,?)", ("BK0002", "白酒", "消费"))
    conn.execute("INSERT INTO sw2_map VALUES(?,?,?,?)", ("BK0001", "半导体", "信息技术", "半导体"))
    conn.execute("INSERT INTO sw2_map VALUES(?,?,?,?)", ("BK0002", "白酒", "消费", "白酒"))
    conn.commit()
    conn.close()


@pytest.fixture(scope="session")
def fixture_db(tmp_path_factory):
    p = tmp_path_factory.mktemp("db") / "fixture.db"
    build_fixture_db(p)
    return p


@pytest.fixture(scope="session")
def fixture_html(fixture_db, tmp_path_factory):
    """用夹具库生成自包含 HTML(供 Node 前端套件消费)"""
    import sector_viewer as SV
    old_db = SV.DB
    SV.DB = fixture_db
    try:
        out = tmp_path_factory.mktemp("html") / "fixture.html"
        SV.generate_html(years=3, out=out)
    finally:
        SV.DB = old_db
    return out
