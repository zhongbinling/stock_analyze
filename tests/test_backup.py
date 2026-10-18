# -*- coding: utf-8 -*-
"""备份系统测试 (core/backup.py v2): 类型滚动/快照闭环/损坏检出/恢复往返/钩子"""
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
    monkeypatch.setattr(B, "LOG_FILE", bdir / "backup.log")
    return {"bdir": bdir, "sent": sent, "app": app, "secret": data / "secret.key"}


def _entries():
    return json.loads(B.MANIFEST.read_text(encoding="utf-8"))["entries"]


def _append(entry):
    m = json.loads(B.MANIFEST.read_text(encoding="utf-8")) if B.MANIFEST.exists() else {"entries": []}
    m["entries"].append(entry)
    B.MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    B.MANIFEST.write_text(json.dumps(m), encoding="utf-8")


# ── 快照闭环 ──

def test_run_all_roundtrip_and_manifest(env):
    r = B.run_all("daily")
    assert r["status"] == "ok" and r["files_n"] == len(B.ASSETS) and r["error"] is None
    ents = _entries()
    assert len(ents) == len(B.ASSETS)
    assert all(e["type"] == "daily" for e in ents)
    sent_e = next(e for e in ents if e["asset"] == "sentiment")
    assert sent_e["integrity"] == "ok"
    assert sent_e["stats"]["sector_scores"] == "2026-09-30"
    assert (env["bdir"] / sent_e["file"]).exists()
    assert B.verify("--all") is True


def test_skip_unchanged(env):
    B.snapshot("sentiment", "daily")
    n1 = len(_entries())
    assert B.snapshot("sentiment", "daily") is None          # sha相同 → 跳过
    assert len(_entries()) == n1
    assert "未变化" in B.LOG_FILE.read_text(encoding="utf-8")


def test_snapshot_after_change(env):
    B.snapshot("sentiment", "daily")
    conn = sqlite3.connect(str(env["sent"]))
    conn.execute("INSERT INTO sentiment VALUES('2026-10-09', 10.0, '中性', NULL, NULL)")
    conn.commit()
    conn.close()
    e = B.snapshot("sentiment", "pre-calc")
    assert e is not None and e["type"] == "pre-calc"
    assert e["stats"]["sentiment"] == "2026-10-09"


def test_log_file_written(env):
    B.run_all("daily")
    log = B.LOG_FILE.read_text(encoding="utf-8")
    assert "prune:" in log and "+ sentiment_" in log


def test_missing_asset_skips(env, monkeypatch):
    monkeypatch.setitem(B.ASSETS, "app", ("sqlite", env["bdir"] / "nope.db"))
    assert B.snapshot("app") is None


# ── 类型滚动清理 ──

def _fabricate(env, btype, days, end=dt.date(2026, 9, 30)):
    env["bdir"].mkdir(parents=True, exist_ok=True)
    for i in range(days):
        d = end - dt.timedelta(days=days - 1 - i)
        f = env["bdir"] / f"sentiment_{d.strftime('%Y%m%d')}__180000_{btype}.db"
        f.write_bytes(f"v{i}".encode())
        _append({"file": f.name, "asset": "sentiment", "kind": "sqlite",
                 "sha256": B._sha256(f), "size": f.stat().st_size,
                 "time": f"{d.isoformat()} 18:00:00", "date": d.strftime("%Y%m%d"),
                 "type": btype, "integrity": "ok", "stats": {}})


def test_prune_type_rolling(env):
    _fabricate(env, "daily", 20)
    _fabricate(env, "weekly", 10, end=dt.date(2026, 10, 2))
    _fabricate(env, "manual", 7)
    B.prune({"daily": 14, "weekly": 8, "manual": 5, "pre": 10})
    ents = _entries()
    by_type = {}
    for e in ents:
        by_type.setdefault(e["type"], []).append(e)
    assert len(by_type.get("daily", [])) == 14              # 各类型按N滚动
    assert len(by_type.get("weekly", [])) == 8
    assert len(by_type.get("manual", [])) == 5
    # 保留的是最近的N份
    days = sorted(e["date"] for e in by_type["daily"])
    assert days == sorted((dt.date(2026, 9, 30) - dt.timedelta(days=i)).strftime("%Y%m%d")
                          for i in range(14))
    # 磁盘与manifest同步
    on_disk = {p.name for p in env["bdir"].glob("sentiment_*.db")}
    assert on_disk == {e["file"] for e in ents}


def test_prune_keeps_latest_even_beyond_retention(env):
    _fabricate(env, "manual", 1)                            # 唯一一份manual
    B.prune({"daily": 14, "weekly": 8, "manual": 0, "pre": 10})
    assert len(_entries()) == 1                             # 最新一份保底不删


def test_prune_legacy_entries_normalized(env):
    """旧版manifest(无type, tag为空/pre-calc) → daily/pre 归一后参与滚动"""
    env["bdir"].mkdir(parents=True, exist_ok=True)
    for i, (tag, want) in enumerate([("", "daily"), ("pre-calc", "pre")]):
        f = env["bdir"] / f"sentiment_2026090{i}_1_180000.db"
        f.write_bytes(f"v{i}".encode())
        _append({"file": f.name, "asset": "sentiment", "kind": "sqlite",
                 "sha256": B._sha256(f), "size": 1, "time": f"2026-09-0{i+1} 18:00:00",
                 "date": f"2026090{i+1}", "tag": tag, "integrity": "ok", "stats": {}})
    B.prune({"daily": 14, "weekly": 8, "manual": 5, "pre": 10})
    types = {e["file"]: e["type"] for e in _entries()}
    assert "daily" in types.values() and "pre" in types.values()


# ── 校验与恢复 ──

def test_verify_detects_corruption(env):
    e = B.snapshot("sentiment", "daily")
    (env["bdir"] / e["file"]).write_bytes(b"corrupted!!")
    assert B.verify(e["file"]) is False
    assert B.verify("--all") is False


def test_restore_roundtrip(env):
    e = B.snapshot("sentiment", "daily")
    conn = sqlite3.connect(str(env["sent"]))
    conn.execute("DELETE FROM sector_scores")
    conn.commit()
    conn.close()
    assert B.restore(e["file"], yes=True) is True
    conn = sqlite3.connect(str(env["sent"]))
    n = conn.execute("SELECT COUNT(*) FROM sector_scores").fetchone()[0]
    conn.close()
    assert n == 1
    assert "pre-restore" in {x["type"] for x in _entries()}


# ── pre-calc 钩子 ──

def test_pre_calc_hook_fires(env, monkeypatch):
    calls = []
    monkeypatch.setattr(B, "snapshot",
                        lambda asset, btype="manual": calls.append((asset, btype)) or {"file": "x"})
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
    for d in ["2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"]:
        conn.execute("INSERT INTO sector_daily VALUES('BK0001',?,100,101,102,99,1e6,80,20)", (d,))
    conn.commit()
    S.calc_all(conn)                                 # 5行数据: 预热期NaN → 0行入库, 不崩
    assert ("sentiment", "pre-calc") in calls
    assert conn.execute("SELECT COUNT(*) FROM sector_scores WHERE score IS NULL").fetchone()[0] == 0
    conn.close()
