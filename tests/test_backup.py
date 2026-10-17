# -*- coding: utf-8 -*-
"""备份系统测试 (core/backup.py): 快照闭环/去重/GFS清理/损坏检出/恢复往返"""
import datetime as dt
import json
import sqlite3

import pytest

import backup as B


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时环境: 迷你 sentiment/app 库 + 密钥/配置文件, 备份目录指到 tmp"""
    core = tmp_path / "core"
    data = tmp_path / "data"
    (core / "data").mkdir(parents=True)
    data.mkdir()

    sent = core / "data" / "sentiment.db"
    conn = sqlite3.connect(str(sent))
    conn.executescript("""
      CREATE TABLE sentiment(date TEXT PRIMARY KEY, score REAL, stage TEXT, components TEXT, weights TEXT);
      CREATE TABLE sector_scores(date TEXT, code TEXT, score REAL, rank_pct REAL, stage TEXT, components TEXT,
        PRIMARY KEY(date, code));
    """)
    conn.execute("INSERT INTO sentiment VALUES('2026-09-30', -30.0, '偏冷', NULL, NULL)")
    conn.execute("INSERT INTO sector_scores VALUES('2026-09-30', 'BK0001', 50.0, 90.0, '偏热', NULL)")
    conn.commit()
    conn.close()

    app = data / "app.db"
    conn = sqlite3.connect(str(app))
    conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO users VALUES(1, 'admin')")
    conn.commit()
    conn.close()

    (data / "secret.key").write_text("k" * 64)
    (core / "data" / "tushare_token.txt").write_text("tok123")
    (core / "config.yaml").write_text("window: 250\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "server.yaml").write_text("server: {port: 8501}\n")

    bdir = tmp_path / "backups"
    monkeypatch.setattr(B, "ASSETS", {
        "sentiment": ("sqlite", sent),
        "app": ("sqlite", app),
        "secret": ("file", data / "secret.key"),
        "token": ("file", core / "data" / "tushare_token.txt"),
        "cfg_core": ("file", core / "config.yaml"),
        "cfg_server": ("file", tmp_path / "config" / "server.yaml"),
    })
    monkeypatch.setattr(B, "BACKUP_DIR", bdir)
    monkeypatch.setattr(B, "MANIFEST", bdir / "manifest.json")
    return {"bdir": bdir, "sent": sent, "app": app, "secret": data / "secret.key"}


def _entries():
    return json.loads(B.MANIFEST.read_text(encoding="utf-8"))["entries"]


def test_snapshot_roundtrip_and_manifest(env):
    for a in B.ASSETS:
        assert B.snapshot(a) is not None
    ents = _entries()
    assert len(ents) == len(B.ASSETS)
    sent_e = next(e for e in ents if e["asset"] == "sentiment")
    assert sent_e["integrity"] == "ok"
    assert sent_e["stats"]["sector_scores"] == "2026-09-30"
    assert (env["bdir"] / sent_e["file"]).exists()
    assert B.verify("--all") is True


def test_skip_unchanged(env):
    B.snapshot("sentiment")
    n1 = len(_entries())
    assert B.snapshot("sentiment") is None          # sha相同 → 跳过
    assert len(_entries()) == n1


def test_snapshot_after_change(env):
    B.snapshot("sentiment")
    conn = sqlite3.connect(str(env["sent"]))
    conn.execute("INSERT INTO sentiment VALUES('2026-10-09', 10.0, '中性', NULL, NULL)")
    conn.commit()
    conn.close()
    e = B.snapshot("sentiment", tag="pre-calc")
    assert e is not None and e["tag"] == "pre-calc"
    assert e["stats"]["sentiment"] == "2026-10-09"  # 新鲜度随数据更新


def test_missing_asset_skips(env, monkeypatch):
    monkeypatch.setitem(B.ASSETS, "app", ("sqlite", env["bdir"] / "nope.db"))
    assert B.snapshot("app") is None                # 缺失告警不炸


def _fabricate(env, days=90):
    """伪造 days 天的 sentiment 快照(每天一份, 内容递增) + manifest"""
    env["bdir"].mkdir(parents=True, exist_ok=True)
    end = dt.date(2026, 9, 30)
    for i in range(days):
        d = end - dt.timedelta(days=days - 1 - i)
        f = env["bdir"] / f"sentiment_{d.strftime('%Y%m%d')}__180000.db"
        f.write_bytes(f"v{i}".encode())
        _append({"file": f.name, "asset": "sentiment", "kind": "sqlite",
                 "sha256": B._sha256(f), "size": f.stat().st_size,
                 "time": f"{d.isoformat()} 18:00:00", "date": d.strftime("%Y%m%d"),
                 "tag": "", "integrity": "ok", "stats": {}})


def _append(entry):
    m = json.loads(B.MANIFEST.read_text(encoding="utf-8")) if B.MANIFEST.exists() else {"entries": []}
    m["entries"].append(entry)
    B.MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    B.MANIFEST.write_text(json.dumps(m), encoding="utf-8")


def test_prune_gfs_properties(env):
    _fabricate(env, days=90)
    B.prune()
    ents = _entries()
    files = {e["file"] for e in ents}
    dates = sorted(dt.datetime.strptime(e["date"], "%Y%m%d").date() for e in ents)
    end = dt.date(2026, 9, 30)
    recent = [end - dt.timedelta(days=i) for i in range(30)]
    # 性质1: 最近30天每日全保留
    for d in recent:
        assert d.strftime("%Y%m%d") in {e["date"] for e in ents}, f"missing {d}"
    # 性质2: 总量有界 (30日 + 12周 + 12月)
    assert len(ents) <= B.DAILY_N + B.WEEKLY_N + B.MONTHLY_N
    # 性质3: 逾30天的快照只可能是其ISO周或月份的最新一份
    for e in ents:
        d = dt.datetime.strptime(e["date"], "%Y%m%d").date()
        if d in recent:
            continue
        same_week = [x for x in ents
                     if dt.datetime.strptime(x["date"], "%Y%m%d").date().isocalendar()[:2]
                     == d.isocalendar()[:2]]
        same_month = [x for x in ents
                      if dt.datetime.strptime(x["date"], "%Y%m%d").date().strftime("%Y%m")
                      == d.strftime("%Y%m")]
        assert (max(same_week, key=lambda x: x["date"])["file"] == e["file"]
                or max(same_month, key=lambda x: x["date"])["file"] == e["file"]), \
            f"非锚点快照未被清理: {e['file']}"
    # 性质4: 被清理的文件从磁盘移除, manifest同步
    on_disk = {p.name for p in env["bdir"].glob("sentiment_*.db")}
    assert on_disk == files


def test_verify_detects_corruption(env):
    e = B.snapshot("sentiment")
    f = env["bdir"] / e["file"]
    f.write_bytes(b"corrupted!!")                   # 篡改副本
    assert B.verify(e["file"]) is False
    assert B.verify("--all") is False


def test_restore_roundtrip(env):
    e = B.snapshot("sentiment")
    # 模拟数据被写坏
    conn = sqlite3.connect(str(env["sent"]))
    conn.execute("DELETE FROM sector_scores")
    conn.commit()
    conn.close()
    assert B.restore(e["file"], yes=True) is True
    conn = sqlite3.connect(str(env["sent"]))
    n = conn.execute("SELECT COUNT(*) FROM sector_scores").fetchone()[0]
    conn.close()
    assert n == 1                                    # 恢复后数据回来
    tags = [x["tag"] for x in _entries()]
    assert "pre-restore" in tags                     # 恢复前自动兜底快照


def test_pre_calc_hook_fires(env, monkeypatch):
    """sector.calc_all 全量重算前自动打 pre-calc 快照(防自己的代码写坏数据)"""
    calls = []
    monkeypatch.setattr(B, "snapshot",
                        lambda asset, tag="": calls.append((asset, tag)) or {"file": "x"})
    import sector as S
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE sectors(code TEXT PRIMARY KEY, name TEXT, kind TEXT, enabled INTEGER,
        first_date TEXT, last_date TEXT, updated_at TEXT);
      CREATE TABLE sector_daily(code TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL,
        amount REAL, up_count INTEGER, down_count INTEGER);
      CREATE TABLE sector_pool_stat(date TEXT, code TEXT, zt_count INTEGER, dt_count INTEGER,
        lb_max INTEGER, lb2_count INTEGER);
      CREATE TABLE sector_fundflow(date TEXT, code TEXT, main_net REAL);
      CREATE TABLE mood_raw(date TEXT, zt_count INTEGER);
      CREATE TABLE sector_scores(date TEXT, code TEXT, score REAL, rank_pct REAL,
        stage TEXT, components TEXT, PRIMARY KEY(date, code));
      CREATE TABLE sw1_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT, PRIMARY KEY(bk_code));
      CREATE TABLE sw2_map(bk_code TEXT, bk_name TEXT, sw1_name TEXT, sw2_name TEXT, PRIMARY KEY(bk_code));
    """)
    conn.execute("INSERT INTO sectors VALUES('BK0001','半导体','industry',1,NULL,NULL,NULL)")
    for i, d in enumerate(["2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"]):
        conn.execute("INSERT INTO sector_daily VALUES('BK0001',?,100,101,102,99,1e6,80,20)", (d,))
    conn.commit()
    S.calc_all(conn)                                 # 5行数据: 预热期NaN → 0行入库, 不崩
    assert ("sentiment", "pre-calc") in calls
    assert conn.execute("SELECT COUNT(*) FROM sector_scores WHERE score IS NULL").fetchone()[0] == 0
    conn.close()
