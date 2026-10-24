# -*- coding: utf-8 -*-
"""备份系统测试 (core/backup.py v2): 类型滚动/快照闭环/损坏检出/恢复往返/钩子"""
import datetime as dt
import json
import shutil
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
    # D1-10 B1/B3: 子目录zip布局 {type}_{ts}.zip/{原名}
    assert "/" in sent_e["file"] and sent_e["file"].endswith("sentiment.db")
    assert (env["bdir"] / sent_e["file"].split("/")[0]).exists()
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
    assert B.verify(e["file"]) is True


def test_log_file_written(env):
    B.run_all("daily")
    log = B.LOG_FILE.read_text(encoding="utf-8")
    assert "prune:" in log and "sentiment.db" in log


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
    e = B.snapshot("sentiment", "daily", compress=False)      # 明文子目录便于篡改
    (env["bdir"] / e["file"]).write_bytes(b"corrupted!!")
    assert B.verify(e["file"]) is False
    assert B.verify("--all") is False


def test_verify_never_deletes_folder_backups(env):
    """D1-10r(严重回归): verify对子目录明文备份只读 — 文件必须原地保留"""
    e = B.snapshot("sentiment", "daily", compress=False)
    f = env["bdir"] / e["file"]
    assert f.exists()
    assert B.verify("--all") is True
    assert f.exists(), "verify删除了子目录内的备份文件!"


def test_snapshot_zip_layout_and_no_compress_dir(env):
    """B1/B3: 压缩=zip单文件 / 不压缩=子目录原名义件"""
    ez = B.snapshot("sentiment", "daily", compress=True)
    import zipfile as zf
    zp = env["bdir"] / ez["file"].split("/")[0]
    assert zp.suffix == ".zip" and zp.exists()
    with zf.ZipFile(zp) as z:
        assert "sentiment.db" in z.namelist()
    ed = B.snapshot("app", "daily", compress=False)
    d = env["bdir"] / ed["file"].split("/")[0]
    assert d.is_dir() and (d / "app.db").exists()
    assert B.verify("--all") is True


def test_manifest_sizes_are_member_sizes(env):
    """D1-10r: zip布局entry.size必须=成员自身大小(曾误记为整zip大小,
    多资产合并后所有条目都显示成zip总大小)"""
    import zipfile as zf
    r = B.run_all("daily")
    assert r["status"] == "ok" and r["files_n"] == len(B.ASSETS)
    for e in _entries():
        root, name = e["file"].split("/", 1)
        with zf.ZipFile(env["bdir"] / root) as z:
            assert e["size"] == z.getinfo(name).file_size, \
                f"{e['file']} 记录了 {e['size']} != 成员 {z.getinfo(name).file_size}"


def test_noop_skip_uses_source_sha_fast_path(env, monkeypatch):
    """D1-10r: 未变化跳过用源sha快速判定, 不再先整库拷贝再比对"""
    B.snapshot("sentiment", "daily")
    n_before = len(_entries())

    def _boom(*a, **k):
        raise AssertionError("未变化跳过不应再拷贝/打开数据库")
    monkeypatch.setattr(B.sqlite3, "connect", _boom)
    assert B.snapshot("sentiment", "daily") is None
    assert len(_entries()) == n_before
    assert not list(env["bdir"].glob("manual_*")), "跳过后不应残留快照目录"


def test_verify_writes_back_state(env):
    """D1-11 C2: 校验结果写回条目(✓与✗都持久化, 重载清单可见)"""
    B.snapshot("sentiment", "daily")
    assert B.verify("--all") is True
    e = _entries()[-1]
    assert e["verify"]["ok"] is True and e["verify"]["at"]
    # 篡改zip成员 → 校验失败, 状态翻红并带原因
    root, name = e["file"].split("/", 1)
    zp = env["bdir"] / root
    import zipfile as zf
    with zf.ZipFile(zp) as z:
        z.read(name)
    with zf.ZipFile(zp, "w") as z:
        z.writestr(name, b"broken-not-a-db")
    assert B.verify(e["file"]) is False
    e2 = _entries()[-1]
    assert e2["verify"]["ok"] is False
    assert any("sha256" in i for i in e2["verify"]["issues"])


def test_verify_folder_target_and_progress(env):
    """D1-11 C1/C2: 按运行目录整组校验 + progress回调(i,n)供任务进度"""
    r = B.run_all("daily")
    folder = r["files"][0]["file"].split("/")[0]
    got = []
    assert B.verify(folder, progress=lambda i, n, f: got.append((i, n))) is True
    n_assets = len(B.ASSETS)
    assert got == [(i, n_assets) for i in range(1, n_assets + 1)]
    # 组内每条都已写回校验状态
    assert all(x.get("verify", {}).get("ok") for x in _entries())


def test_skip_backfills_src_sha_for_legacy_entries(env):
    """D1-10r: 无src_sha256的旧条目在首次跳过时回填, 之后走快速路径"""
    B.snapshot("sentiment", "daily")
    m = json.loads(B.MANIFEST.read_text(encoding="utf-8"))
    del m["entries"][-1]["src_sha256"]            # 伪装旧版条目
    B._save_manifest(m)
    assert B.snapshot("sentiment", "daily") is None       # 慢路径跳过(拷贝后比对)
    e = _entries()[-1]
    assert e.get("src_sha256"), "旧条目未回填src_sha256, 永远无法走快速路径"
    assert B.snapshot("sentiment", "daily") is None       # 现在可快速跳过


def test_sweep_stale_part_dirs(env):
    """D1-10r: 崩溃残留(只含.part且超1小时)在run_all时被清理; 在途新目录不动"""
    import os
    import time
    stale = env["bdir"] / "manual_20200101_000000"
    stale.mkdir(parents=True)
    (stale / "sentiment.db.part").write_bytes(b"x")
    t_old = time.time() - 7200
    os.utime(stale, (t_old, t_old))
    fresh = env["bdir"] / "manual_20990101_000000"
    fresh.mkdir()
    (fresh / "x.db.part").write_bytes(b"x")
    B.run_all("daily")
    assert not stale.exists()
    assert fresh.exists()


def test_pre_backfill_type_not_normed_to_manual(env):
    """D1-10r: pre-backfill曾是合法类型缺失被误归为manual, 污染manual滚动桶
    (修复链/回填钩子每次产生400MB级manual噪音备份)"""
    e = B.snapshot("sentiment", "pre-backfill")
    assert e is not None and e["type"] == "pre-backfill"
    assert B._retention_of("pre-backfill", {"daily": 14, "weekly": 8, "manual": 5, "pre": 10}) == 10


def test_manifest_lock_mutual_exclusion(env):
    """D1-10r: manifest锁互斥 — 持锁期间第二个上下文必须等待"""
    import threading
    import time as _t
    order = []

    def worker():
        with B._manifest_lock():
            order.append("b-in")
            _t.sleep(0.3)
            order.append("b-out")

    with B._manifest_lock():
        order.append("a-in")
        th = threading.Thread(target=worker)
        th.start()
        _t.sleep(0.1)                     # b此时应阻塞在锁上
        assert order == ["a-in"]
        order.append("a-out")
    th.join(timeout=5)
    assert order == ["a-in", "a-out", "b-in", "b-out"]


def test_concurrent_snapshots_keep_all_entries(env):
    """D1-10r: 并发快照(钩子+CLI并存场景)不丢清单条目 — 落盘前重读+锁"""
    import threading
    errs = []

    def snap(asset):
        try:
            B.snapshot(asset, "daily")
        except Exception as ex:  # noqa: BLE001
            errs.append(ex)
    env["bdir"].mkdir(parents=True, exist_ok=True)
    ths = [threading.Thread(target=snap, args=(a,)) for a in ("sentiment", "app", "secret")]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=10)
    assert not errs
    files = {e["file"] for e in _entries()}
    assert any("sentiment.db" in f for f in files)
    assert any("app.db" in f for f in files)
    assert any("secret.key" in f for f in files)
    assert B.verify("--all") is True


def test_restore_roundtrip(env):
    e = B.snapshot("sentiment", "daily")              # 默认zip布局
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
    assert B.verify("--all") is True                 # 恢复后备份本体未损


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
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="为空"):
        S.calc_all(conn)                             # 5行预热期NaN→0行 → D1-7拒绝切换空表
    # 前置钩子已触发; 旧表(空)未被破坏
    assert ("sentiment", "pre-calc") in calls
    assert conn.execute("SELECT COUNT(*) FROM sector_scores WHERE score IS NULL").fetchone()[0] == 0
    conn.close()


def test_migrate_legacy_flat_files(env):
    """B2: 平铺文件归档进子目录 + manifest改写, 迁移后verify通过"""
    ts = "20261001_120000"
    env["bdir"].mkdir(parents=True, exist_ok=True)
    flat = env["bdir"] / f"sentiment_{ts}_daily.db"
    shutil.copy2(env["sent"], flat)
    m = {"entries": [{"file": flat.name, "asset": "sentiment", "kind": "sqlite",
                      "sha256": B._sha256(flat), "size": flat.stat().st_size,
                      "time": "2026-10-01 12:00:00", "date": "20261001", "type": "daily",
                      "integrity": "ok", "stats": {}}]}
    B._save_manifest(m)
    moved = B.migrate_legacy()
    assert moved >= 1
    assert not flat.exists()                          # 已移入子目录
    d = env["bdir"] / f"daily_{ts}"
    assert (d / "sentiment.db").exists()
    assert _entries()[0]["file"] == f"daily_{ts}/sentiment.db"
    assert B.verify("--all") is True
    # 幂等: 再跑无操作
    assert B.migrate_legacy() == 0
