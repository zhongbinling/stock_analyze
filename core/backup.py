# -*- coding: utf-8 -*-
"""核心资产备份 (数据库/密钥/配置): SQLite在线快照 + 类型滚动保留 + 校验 + 恢复

类型 (B1): daily | weekly | manual | pre-calc | pre-backfill | pre-restore
保留: 每资产每类型留最近 N 份(可配), 每资产最新一份永不删; 内容未变化自动跳过。
日志: backups/backup.log 文件 + 调用方(服务)另行落 backup_runs 表。

用法:
  python backup.py now  [-t type]        # 立即快照全部资产(默认manual)
  python backup.py daily                # now -t daily + prune (CLI应急入口)
  python backup.py weekly               # now -t weekly + prune
  python backup.py prune                # 按类型滚动清理
  python backup.py list                 # 备份清单
  python backup.py verify [--all|文件名] # integrity + 新鲜度校验
  python backup.py restore <文件名> --yes  # 校验→pre-restore快照→原子替换
"""
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent

# 资产清单: key -> (kind, path); 缺失告警跳过(各环境资产集不同)
ASSETS = {
    "sentiment": ("sqlite", BASE / "data" / "sentiment.db"),
    # P4: raw个股库(~2.5GB) — 资产级策略: 仅周备/手动 + zlib压缩 + keep=4(见RAW_POLICY)
    "raw_daily": ("sqlite", BASE / "data" / "raw_daily.db"),
    # D1-17 R3: 实时行情独立库(缺失优雅跳过 — 首次采集前不存在)
    "realtime": ("sqlite", BASE / "data" / "realtime.db"),
    # D1-18 M1: 宏观舆情独立库(缺失优雅跳过 — 首次访问自动建库播种)
    "macro": ("sqlite", BASE / "data" / "macro.db"),
    "app": ("sqlite", ROOT / "data" / "app.db"),
    "secret": ("file", ROOT / "data" / "secret.key"),
    "token": ("file", BASE / "data" / "tushare_token.txt"),
    "cfg_core": ("file", BASE / "config.yaml"),
    "cfg_server": ("file", ROOT / "config" / "server.yaml"),
}
BACKUP_DIR = ROOT / "backups"
MANIFEST = BACKUP_DIR / "manifest.json"
LOG_FILE = BACKUP_DIR / "backup.log"

# 类型滚动保留(每资产每类型份数); 服务端从设置读取后传入
RETENTION = {"daily": 14, "weekly": 8, "manual": 5, "pre": 10}
# 资产级策略(P4): raw_daily 仅周备/手动快照(日备自动跳过, 避免14×2.5GB),
# zlib压缩存.db.z(标准库稳快 ~2.5GB→~1.2GB), 每类型keep=4独立滚动
RAW_POLICY = {"raw_daily": {"types": ("weekly", "manual", "pre-restore"), "keep": 4,
               "compress": True}}
TYPES = ("daily", "weekly", "manual", "pre-calc", "pre-backfill", "pre-restore")

# D1-10 B1: 备份内使用原名文件(子目录 {type}_{ts}/ 下)
ASSET_FILES = {"sentiment": "sentiment.db", "app": "app.db", "secret": "secret.key",
               "token": "tushare_token.txt", "cfg_core": "config.yaml",
               "cfg_server": "server.yaml", "raw_daily": "raw_daily.db",
               # D1-21: 批次6/7注册的资产漏登本表 → 日备KeyError('realtime')实测
               "realtime": "realtime.db", "macro": "macro.db"}


def _norm_type(btype: str) -> str:
    btype = (btype or "manual").strip()
    return btype if btype in TYPES else "manual"


def _retention_of(btype: str, retention: dict) -> int:
    return int(retention.get("pre") if btype.startswith("pre") else retention.get(btype, 5))


def _log(msg: str):
    """双写: 控制台 + backups/backup.log"""
    line = f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(f"[backup] {msg}")
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest() -> dict:
    if MANIFEST.exists():
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        for e in m.get("entries", []):        # 旧版兼容: tag→type 归一
            if "type" not in e:
                t = e.get("tag") or ""
                e["type"] = "pre" if t.startswith("pre") else (t if t in TYPES else "daily")
        return m
    return {"entries": []}


def _save_manifest(m: dict):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


@contextlib.contextmanager
def _manifest_lock(timeout: float = 60.0):
    """跨进程互斥(O_EXCL锁文件): 保护manifest读-改-写窗口。

    D1-10r: 修复链的pre-calc/pre-backfill钩子与服务/CLI备份并发时,
    后写者用陈旧视图整档覆盖, 吃掉先写者的条目(zip在盘上但清单不可见)。
    超时降级继续(可用性优先, 记录警告); 持有者崩溃残留>10分钟可被抢占。
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    lock = BACKUP_DIR / "manifest.lock"
    t0 = time.time()
    held = False
    while time.time() - t0 < timeout:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            held = True
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 600:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    if not held:
        _log("⚠ manifest锁获取超时, 降级无锁写入(并发条目可能丢失)")
    try:
        yield
    finally:
        if held:
            lock.unlink(missing_ok=True)


def _db_stats(p: Path, asset: str) -> dict:
    """数据新鲜度抽查: 资产关键表存在且可查询(损坏/空库在此暴露)"""
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        out = {}
        for tbl, col in (("sentiment", "date"), ("sector_scores", "date"), ("mood_raw", "date")):
            try:
                out[tbl] = conn.execute(f"SELECT MAX({col}) FROM {tbl}").fetchone()[0]
            except sqlite3.DatabaseError:
                out[tbl] = None
        if asset == "app":
            try:
                out["users"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                out["view_defs"] = conn.execute("SELECT COUNT(*) FROM view_defs").fetchone()[0]
            except sqlite3.DatabaseError:
                pass
        return out
    finally:
        conn.close()


def _integrity_ok(p: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        try:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError:      # 非 SQLite 文件/严重损坏
        return False


def snapshot(asset: str, btype: str = "manual", compress: bool = True,
             folder: str | None = None) -> dict | None:
    """单资产快照: 在线备份→校验→sha去重→子目录(可选zip)→manifest; 缺失/未变化None

    D1-10 B1/B3: 目录结构 backups/{type}_{YYYYMMDD}_{HHMMSS}/{原名};
    compress=True 时整目录压成 {folder}.zip 后删目录(manifest.file=folder.zip/name)。
    raw_daily 例外: 自身已zlib(.db.z), zip内用STORED避免双重压缩。
    D1-10r: 未变化跳过走源sha快速路径(免整库拷贝); entry.size=成员自身大小
    (非zip整体大小); run_all前置清理崩溃残留的纯.part目录。
    """
    kind, src = ASSETS[asset]
    btype = _norm_type(btype)
    pol = RAW_POLICY.get(asset)
    if pol and btype not in pol["types"]:
        _log(f"= {asset} 跳过{btype}快照(策略仅 {pol['types']})")
        return None
    if not src.exists():
        _log(f"= 跳过 {asset}: {src} 不存在")
        return None
    m = _load_manifest()
    last = next((e for e in reversed(m["entries"]) if e["asset"] == asset), None)
    # 快速去重: 源sha与上次快照时的源sha一致 → 免拷贝直接跳过。注: sqlite backup
    # 副本间字节稳定但与源文件不完全相同, 故条目单独记src_sha256(源文件口径),
    # 不能拿sha256(副本口径)比对; 各库均delete日志模式, 源sha可代表已提交内容
    src_sha = _sha256(src)
    if last and last.get("src_sha256") == src_sha:
        _log(f"= {asset} [{btype}] 未变化, 跳过 ({src_sha[:12]})")
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now()
    if folder is None:                     # 独立调用: mkdir原子占位, 同秒并发不共目录
        base = f"{btype}_{ts.strftime('%Y%m%d_%H%M%S')}"
        n = 0
        folder = base
        while True:
            try:
                (BACKUP_DIR / folder).mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                n += 1
                folder = f"{base}_{n}"
    else:                                  # run_all统一目录(进程内串行)
        (BACKUP_DIR / folder).mkdir(parents=True, exist_ok=True)
    outdir = BACKUP_DIR / folder
    fname = ASSET_FILES[asset]
    tmp = outdir / (fname + ".part")

    if kind == "sqlite":
        s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        d = sqlite3.connect(str(tmp))
        try:
            s.backup(d)
        finally:
            s.close()
            d.close()
        if not _integrity_ok(tmp):
            shutil.rmtree(outdir, ignore_errors=True)
            _log(f"✗ {asset} [{btype}] 快照完整性校验失败, 已丢弃")
            return None
        stats = _db_stats(tmp, asset)
    else:
        shutil.copy2(src, tmp)
        stats = {}

    # raw: zlib流式压缩存 .db.z(原子写防半压缩孤儿)
    if pol and pol.get("compress"):
        zpath = outdir / (fname + ".z")
        zpart = zpath.with_name(zpath.name + ".part")
        with open(tmp, "rb") as fi, open(zpart, "wb") as fo:
            zo = zlib.compressobj(6)
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                fo.write(zo.compress(chunk))
            fo.write(zo.flush())
        tmp.unlink(missing_ok=True)
        zpart.replace(zpath)
        fname = fname + ".z"
    else:
        tmp.replace(outdir / fname)               # .part → 原名

    sha = _sha256(outdir / fname)
    if last and last["sha256"] == sha:      # raw(.z)压缩后到此才可比对
        shutil.rmtree(outdir, ignore_errors=True)
        if last.get("src_sha256") != src_sha:   # 旧条目回填源sha, 下次走快速路径
            with _manifest_lock():
                m2 = _load_manifest()
                for e2 in m2["entries"]:
                    if e2["file"] == last["file"]:
                        e2["src_sha256"] = src_sha
                        break
                _save_manifest(m2)
        _log(f"= {asset} [{btype}] 未变化, 跳过 ({sha[:12]})")
        return None

    final = outdir / fname
    size = final.stat().st_size
    entry_file = f"{folder}/{fname}"
    if compress:                                   # D1-10 B3: 整目录zip(原子写)
        zpath = BACKUP_DIR / f"{folder}.zip"
        # 同秒多资产共用同一zip: 已存在的成员先解出再合并重建(原子替换)
        merge_dir = None
        if zpath.exists():
            merge_dir = Path(tempfile.mkdtemp(prefix="bkmerge_"))
            with zipfile.ZipFile(zpath) as zold:
                for info in zold.infolist():
                    with zold.open(info) as fi, open(merge_dir / info.filename, "wb") as fo:
                        shutil.copyfileobj(fi, fo)
        zpart = zpath.with_name(zpath.name + ".part")
        with zipfile.ZipFile(zpart, "w") as zf:
            src_files = list((merge_dir or outdir).iterdir()) + [final]
            seen = set()
            for f in sorted(src_files, key=lambda x: x.name):
                if f.name in seen or not f.is_file():
                    continue
                seen.add(f.name)
                ct = zipfile.ZIP_STORED if f.suffix in (".z", ".bin") else zipfile.ZIP_DEFLATED
                zf.write(f, arcname=f.name, compress_type=ct, compresslevel=6)
        shutil.rmtree(outdir, ignore_errors=True)
        if merge_dir:
            shutil.rmtree(merge_dir, ignore_errors=True)
        zpart.replace(zpath)
        entry_file = f"{folder}.zip/{fname}"
        # entry.size保持成员自身大小(final.stat().st_size, 压缩前原始字节);
        # D1-10r修复: 曾误记zpath整体大小, 多资产合并后所有条目都变成zip总大小
    entry = {"file": entry_file, "asset": asset, "kind": kind, "sha256": sha,
             "src_sha256": src_sha,
             "size": size, "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
             "date": ts.strftime("%Y%m%d"), "type": btype,
             "integrity": "ok" if kind == "sqlite" else "-", "stats": stats}
    with _manifest_lock():                 # 重读最新清单再落, 防并发覆盖他人条目
        m2 = _load_manifest()
        if not any(e2["file"] == entry_file for e2 in m2["entries"]):
            m2["entries"].append(entry)
        _save_manifest(m2)
    fresh = stats.get("sector_scores") or stats.get("sentiment") or ""
    _log(f"+ {entry_file} ({size / 1048576:.1f} MB{', ' + fresh if fresh else ''})")
    return entry


def _materialize(e: dict):
    """备份条目 → (存储态路径, 是否临时需调用方删除)。

    zip成员/旧平铺 → 解出/拷出到系统临时目录(is_temp=True);
    子目录明文 → 原地返回(is_temp=False, 绝不删除——verify曾按
    parent!=BACKUP_DIR误删子目录内备份文件, 已改为显式is_temp契约)。
    sha对存储字节(不解压.z)。
    """
    f = e["file"]
    root, name = (f.split("/", 1) if "/" in f else ("", f))
    src = (BACKUP_DIR / root / name) if root else (BACKUP_DIR / f)
    if root.endswith(".zip"):
        _fd, tname = tempfile.mkstemp(suffix=Path(name).name)
        os.close(_fd)                              # Win32: 句柄不关无法删除
        tmp = Path(tname)
        with zipfile.ZipFile(BACKUP_DIR / root) as zf:
            with zf.open(name) as fi, open(tmp, "wb") as fo:
                shutil.copyfileobj(fi, fo)
        return tmp, True
    if src.parent == BACKUP_DIR:                   # 旧平铺: 拷出(含.z)
        _fd, tname = tempfile.mkstemp(suffix=src.name)
        os.close(_fd)
        tmp = Path(tname)
        shutil.copy2(src, tmp)
        return tmp, True
    return src, False                              # 子目录明文: 原地, 不删


def _decompress_if_copy(p: Path) -> Path:
    """zlib解压到系统临时目录(不产生backups内.v.tmp孤儿)"""
    fd, tname = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    tmp = Path(tname)
    with open(p, "rb") as fi, open(tmp, "wb") as fo:
        d = zlib.decompressobj()
        for chunk in iter(lambda: fi.read(1 << 20), b""):
            fo.write(d.decompress(chunk))
        fo.write(d.flush())
    return tmp


def _sweep_stale_parts(max_age_s: float = 3600.0):
    """清理崩溃残留: 超过1小时且只含.part(或空)的快照目录整目录移除

    正常流程不会留下此类目录(成功→zip/明文成员, 未变化跳过→rmtree);
    只有进程被杀(Ctrl+C/断电)会残留。时限避开并发在途的快照。
    """
    if not BACKUP_DIR.exists():
        return
    now = time.time()
    for d in BACKUP_DIR.iterdir():
        if not d.is_dir() or "_" not in d.name:
            continue
        try:
            if now - d.stat().st_mtime <= max_age_s:
                continue
            if all(p.name.endswith(".part") for p in d.iterdir()):
                shutil.rmtree(d, ignore_errors=True)
                _log(f"~ 清理崩溃残留目录 {d.name}/")
        except OSError:
            pass


def run_all(btype: str = "manual", retention: dict | None = None,
            compress: bool = True, assets: list | None = None) -> dict:
    """编程接口(B1): 全资产(或assets子集)快照+清理, 返回结构化结果供服务落 backup_runs

    D1-11 C3: assets 子集保持 ASSETS 顺序; 未知名 → 结果带error不落任何备份。
    """
    t0 = time.time()
    files, err = [], None
    btype = _norm_type(btype)
    sel = list(ASSETS)
    if assets:
        unknown = [a for a in assets if a not in ASSETS]
        if unknown:
            _log(f"✗ [{btype}] 未知资产: {','.join(unknown)}")
            return {"btype": btype, "status": "fail", "files": [], "files_n": 0,
                    "total_mb": 0.0, "duration_s": round(time.time() - t0, 1),
                    "error": f"未知资产: {','.join(unknown)}"}
        sel = [a for a in ASSETS if a in set(assets)]
    base = f"{btype}_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        _sweep_stale_parts()
        n = 0
        run_folder = base                 # mkdir原子占位, 同秒并发run_all不共目录
        while True:
            try:
                (BACKUP_DIR / run_folder).mkdir(parents=True, exist_ok=False)
                break
            except FileExistsError:
                n += 1
                run_folder = f"{base}_{n}"
        for a in sel:
            e = snapshot(a, btype, compress=compress, folder=run_folder)
            if e:
                files.append({"file": e["file"], "asset": a, "size": e["size"]})
        d = BACKUP_DIR / run_folder       # 全部跳过时清掉空占位目录
        if d.is_dir() and not any(d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)
        prune(retention)
    except Exception as ex:  # noqa: BLE001
        err = f"{type(ex).__name__}: {ex}"
        _log(f"✗ [{btype}] 异常: {err}")
    return {"btype": btype, "status": "fail" if err else "ok",
            "files": files, "files_n": len(files),
            "total_mb": round(sum(f["size"] for f in files) / 1048576, 1),
            "duration_s": round(time.time() - t0, 1), "error": err}


def prune(retention: dict | None = None):
    """类型滚动清理: 每资产每类型留最近N份; 每资产最新一份永不删;
    RAW_POLICY资产用自己的keep覆盖每类型份数"""
    retention = retention or RETENTION
    with _manifest_lock():
        m = _load_manifest()
        removed = 0
        for asset in ASSETS:
            ents = sorted((e for e in m["entries"] if e["asset"] == asset),
                          key=lambda e: (e["date"], e["time"]), reverse=True)
            if not ents:
                continue
            keep = {ents[0]["file"]}                     # 最新一份保底
            by_type = {}
            for e in ents:                               # 降序, 每类型取前N
                by_type.setdefault(e["type"], []).append(e)
            pol = RAW_POLICY.get(asset, {})
            for t, lst in by_type.items():
                n = pol.get("keep", _retention_of(t, retention))
                keep |= {e["file"] for e in lst[:n]}
            for e in ents:
                if e["file"] not in keep:
                    _delete_entry_file(e["file"])
                    m["entries"].remove(e)
                    removed += 1
        _save_manifest(m)
    _log(f"prune: 保留 {len(m['entries'])} 份, 清理 {removed} 份")


def _delete_entry_file(f: str):
    """删除一个备份条目的文件: zip→删zip / 子目录→删目录 / 平铺→删文件"""
    if "/" not in f:
        p = BACKUP_DIR / f
        if p.exists():
            p.unlink()
        return
    root, _name = f.split("/", 1)
    rootp = BACKUP_DIR / root
    if root.endswith(".zip"):
        rootp.unlink(missing_ok=True)
    elif rootp.is_dir():
        shutil.rmtree(rootp, ignore_errors=True)


def _entry_exists(f: str) -> bool:
    if "/" not in f:
        return (BACKUP_DIR / f).exists()
    root, name = f.split("/", 1)
    rootp = BACKUP_DIR / root
    if root.endswith(".zip"):
        if not rootp.exists():
            return False
        try:
            with zipfile.ZipFile(rootp) as zf:
                return name in zf.namelist()
        except (zipfile.BadZipFile, OSError):
            return False
    return (rootp / name).exists()


def _writeback_verify(fname: str, state: dict):
    """D1-11 C2: 校验结果写回manifest条目(走清单锁, 重读防并发覆盖)"""
    with _manifest_lock():
        m2 = _load_manifest()
        for e2 in m2["entries"]:
            if e2["file"] == fname:
                e2["verify"] = state
                break
        else:
            return                            # 条目已被并发prune → 放弃写回
        _save_manifest(m2)


def verify(target: str = "--all", progress=None) -> bool:
    """校验: 文件存在 + sha256比对 + integrity + 新鲜度; 返回整体是否通过。

    D1-11 C2: 每条结果即时写回条目 verify:{ok,at,issues}(逐条落盘, 中断不丢已校验的);
    progress(i, n, file) 回调供任务层上报 [i/N] 进度。
    """
    m = _load_manifest()
    if target == "--all":
        ents = m["entries"]
    else:                                    # 精确条目 或 运行目录(组)前缀
        ents = [e for e in m["entries"]
                if e["file"] == target or e["file"].startswith(target + "/")]
    if not ents:
        _log(f"✗ 无匹配备份: {target}")
        return False
    all_ok = True
    for idx, e in enumerate(ents, 1):
        issues = []
        if not _entry_exists(e["file"]):
            issues.append("文件缺失")
        else:
            temps = []
            try:
                tmp, t_is_temp = _materialize(e)   # 存储态字节(sha口径)
                if t_is_temp:
                    temps.append(tmp)
                if _sha256(tmp) != e["sha256"]:
                    issues.append("sha256不一致(内容被改动)")
                plain = _decompress_if_copy(tmp) if tmp.name.endswith(".z") else tmp
                if plain is not tmp:
                    temps.append(plain)
                if e["kind"] == "sqlite" and not _integrity_ok(plain):
                    issues.append("integrity_check失败")
                try:
                    _db_stats(plain, e["asset"])
                except sqlite3.DatabaseError as ex:
                    issues.append(f"新鲜度查询失败: {ex}")
            finally:
                for t in temps:                    # 仅删显式临时文件(原地备份绝不删)
                    t.unlink(missing_ok=True)
        ok = not issues
        all_ok &= ok
        _writeback_verify(e["file"], {
            "ok": ok, "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "issues": issues[:3]})
        if progress:
            progress(idx, len(ents), e["file"])
        _log(f"{'✓' if ok else '✗'} {e['file']} ({e['time']}[{e.get('type', '')}])"
             f"{' ' + '; '.join(issues) if issues else ''}")
    return all_ok


def _decompress_if(p: Path) -> Path:
    """zlib压缩备份(.z) → 解压到临时明文文件; 非压缩原样返回"""
    if not str(p).endswith(".z"):
        return p
    plain = p.with_suffix("")                       # xxx.db.z → xxx.db(临时)
    plain = plain.with_name(plain.name + ".v.tmp")
    with open(p, "rb") as fi, open(plain, "wb") as fo:
        d = zlib.decompressobj()
        for chunk in iter(lambda: fi.read(1 << 20), b""):
            fo.write(d.decompress(chunk))
        fo.write(d.flush())
    return plain


def restore(fname: str, yes: bool = False) -> bool:
    """恢复: 校验→当前态打pre-restore快照→原子替换目标库"""
    m = _load_manifest()
    e = next((x for x in m["entries"] if x["file"] == fname), None)
    if not e:
        _log(f"✗ manifest中找不到 {fname}")
        return False
    if not verify(fname):
        _log("✗ 校验未通过, 拒绝恢复")
        return False
    src_stored, st_is_temp = _materialize(e)        # 存储态(可能.z)
    src_plain = (_decompress_if_copy(src_stored) if src_stored.name.endswith(".z")
                 else src_stored)
    _temps = ([src_stored] if st_is_temp else []) +              ([src_plain] if src_plain is not src_stored else [])
    if not yes and input(f"确认用 {fname} 覆盖 {ASSETS[e['asset']][1]} ? [y/N] ").lower() != "y":
        _log("= 已取消恢复")
        for t in _temps:
            t.unlink(missing_ok=True)
        return False
    snapshot(e["asset"], "pre-restore")              # 恢复前兜底
    dst = Path(ASSETS[e["asset"]][1])
    tmp = dst.with_suffix(dst.suffix + ".part")
    shutil.copy2(src_plain, tmp)
    tmp.replace(dst)
    for t in _temps:
        t.unlink(missing_ok=True)
    _log(f"↺ 已恢复 {fname} → {dst}")
    return True


def migrate_legacy():
    """D1-10 B2: 存量平铺备份 → 子目录结构; manifest同步改写。

    平铺名 {asset}_{YYYYMMDD_HHMMSS}_{type}.{ext} → {type}_{YYYYMMDD_HHMMSS}/{原名};
    .db.z(raw) → raw_daily.db.z。幂等: 已迁移(文件已在子目录/无平铺文件)时无操作。
    """
    import re as _re
    pat = _re.compile(r"^(\w+?)_(\d{8}_\d{6})_?([\w-]*?)\.(db\.z|db|bin|z)$")
    with _manifest_lock():
        m = _load_manifest()
        moved = 0
        for f in sorted(BACKUP_DIR.iterdir()):
            if not f.is_file() or f.name in ("manifest.json", "backup.log"):
                continue
            mm = pat.match(f.name)
            if not mm:
                continue                              # 非平铺备份(或.part残留)跳过
            asset, ts, btype, ext = mm.groups()
            if asset not in ASSET_FILES:
                continue
            if not btype or btype not in TYPES:        # V1无类型名/pre-calc等 → 归一
                btype = "pre-calc" if "pre" in btype else "manual"
            fname = ASSET_FILES[asset] + (".z" if ext in ("db.z", "z") and asset == "raw_daily" else "")
            outdir = BACKUP_DIR / f"{btype}_{ts}"
            outdir.mkdir(parents=True, exist_ok=True)
            target = outdir / fname
            if not target.exists():
                shutil.move(str(f), str(target))
            old_entry = f"{f.name}"
            new_entry = f"{btype}_{ts}/{fname}"
            for e in m["entries"]:
                if e["file"] == old_entry:
                    e["file"] = new_entry
            moved += 1
        if moved:
            _save_manifest(m)
    _log(f"migrate-legacy: 归档 {moved} 个平铺文件")
    return moved


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="核心资产备份")
    ap.add_argument("cmd", choices=["now", "daily", "weekly", "prune", "list",
                                    "verify", "restore", "migrate-legacy"])
    ap.add_argument("-t", "--tag", default="manual", help="备份类型 daily|weekly|manual")
    ap.add_argument("target", nargs="?", default=None,
                    help="verify/restore 的备份文件名; verify 省略时校验全部")
    ap.add_argument("--all", dest="verify_all", action="store_true", help="verify 校验全部")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    if args.cmd == "now":
        run_all(args.tag)
    elif args.cmd in ("daily", "weekly"):
        run_all(args.cmd)
    elif args.cmd == "prune":
        prune()
    elif args.cmd == "migrate-legacy":
        migrate_legacy()
    elif args.cmd == "list":
        m = _load_manifest()
        for e in sorted(m["entries"], key=lambda x: (x["date"], x["asset"]), reverse=True):
            print(f"  {e['time']}  {e['file']:<46} {e['size'] / 1048576:>8.1f}MB "
                  f"[{e.get('type', '')}] {e['integrity']}")
        print(f"  共 {len(m['entries'])} 份")
    elif args.cmd == "verify":
        t = args.target if (args.target and not args.verify_all) else "--all"
        sys.exit(0 if verify(t) else 1)
    elif args.cmd == "restore":
        sys.exit(0 if restore(args.target, args.yes) else 1)
    print(f"[backup] done ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
