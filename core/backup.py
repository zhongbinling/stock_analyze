# -*- coding: utf-8 -*-
"""核心资产备份 (数据库/密钥/配置): SQLite在线快照 + 类型滚动保留 + 校验 + 恢复

类型 (B1): daily | weekly | manual | pre-calc | pre-restore
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
import datetime as dt
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
import zlib
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent

# 资产清单: key -> (kind, path); 缺失告警跳过(各环境资产集不同)
ASSETS = {
    "sentiment": ("sqlite", BASE / "data" / "sentiment.db"),
    # P4: raw个股库(~2.5GB) — 资产级策略: 仅周备/手动 + zlib压缩 + keep=4(见RAW_POLICY)
    "raw_daily": ("sqlite", BASE / "data" / "raw_daily.db"),
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
TYPES = ("daily", "weekly", "manual", "pre-calc", "pre-restore")


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


def snapshot(asset: str, btype: str = "manual") -> dict | None:
    """单资产快照: 在线备份→校验→sha去重→落manifest; 缺失/未变化返回None"""
    kind, src = ASSETS[asset]
    btype = _norm_type(btype)
    pol = RAW_POLICY.get(asset)
    if pol and btype not in pol["types"]:
        _log(f"= {asset} 跳过{btype}快照(策略仅 {pol['types']})")
        return None
    if not src.exists():
        _log(f"= 跳过 {asset}: {src} 不存在")
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now()
    stem = f"{asset}_{ts.strftime('%Y%m%d_%H%M%S')}_{btype}"
    tmp = BACKUP_DIR / (stem + ".part")

    if kind == "sqlite":
        s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        d = sqlite3.connect(str(tmp))
        try:
            s.backup(d)
        finally:
            s.close()
            d.close()
        if not _integrity_ok(tmp):
            tmp.unlink(missing_ok=True)
            _log(f"✗ {asset} [{btype}] 快照完整性校验失败, 已丢弃")
            return None
        ext, stats = "db", _db_stats(tmp, asset)
    else:
        shutil.copy2(src, tmp)
        ext, stats = "bin", {}

    sha = _sha256(tmp)
    m = _load_manifest()
    last = next((e for e in reversed(m["entries"]) if e["asset"] == asset), None)
    if last and last["sha256"] == sha:
        tmp.unlink(missing_ok=True)
        _log(f"= {asset} [{btype}] 未变化, 跳过 ({sha[:12]})")
        return None

    final = BACKUP_DIR / f"{stem}.{ext}"
    if pol and pol.get("compress"):                 # P4: zlib流式压缩存 .db.z
        final = BACKUP_DIR / f"{stem}.{ext}.z"
        zpart = final.with_name(final.name + ".part")   # 原子写: 防半压缩孤儿
        with open(tmp, "rb") as fi, open(zpart, "wb") as fo:
            zo = zlib.compressobj(6)
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                fo.write(zo.compress(chunk))
            fo.write(zo.flush())
        tmp.unlink(missing_ok=True)
        zpart.replace(final)
    else:
        tmp.replace(final)
    entry = {"file": final.name, "asset": asset, "kind": kind, "sha256": sha,
             "size": final.stat().st_size, "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
             "date": ts.strftime("%Y%m%d"), "type": btype,
             "integrity": "ok" if kind == "sqlite" else "-", "stats": stats}
    m["entries"].append(entry)
    _save_manifest(m)
    fresh = stats.get("sector_scores") or stats.get("sentiment") or ""
    _log(f"+ {final.name} ({final.stat().st_size / 1048576:.1f} MB{', ' + fresh if fresh else ''})")
    return entry


def run_all(btype: str = "manual", retention: dict | None = None) -> dict:
    """编程接口(B1): 全资产快照+清理, 返回结构化结果供服务落 backup_runs"""
    t0 = time.time()
    files, err = [], None
    try:
        for a in ASSETS:
            e = snapshot(a, btype)
            if e:
                files.append({"file": e["file"], "asset": a, "size": e["size"]})
        prune(retention)
    except Exception as ex:  # noqa: BLE001
        err = f"{type(ex).__name__}: {ex}"
        _log(f"✗ [{btype}] 异常: {err}")
    return {"btype": _norm_type(btype), "status": "fail" if err else "ok",
            "files": files, "files_n": len(files),
            "total_mb": round(sum(f["size"] for f in files) / 1048576, 1),
            "duration_s": round(time.time() - t0, 1), "error": err}


def prune(retention: dict | None = None):
    """类型滚动清理: 每资产每类型留最近N份; 每资产最新一份永不删;
    RAW_POLICY资产用自己的keep覆盖每类型份数"""
    retention = retention or RETENTION
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
                p = BACKUP_DIR / e["file"]
                if p.exists():
                    p.unlink()
                m["entries"].remove(e)
                removed += 1
    _save_manifest(m)
    _log(f"prune: 保留 {len(m['entries'])} 份, 清理 {removed} 份")


def verify(target: str = "--all") -> bool:
    """校验: 文件存在 + sha256比对 + integrity + 新鲜度; 返回整体是否通过"""
    m = _load_manifest()
    ents = ([e for e in m["entries"] if e["file"] == target] if target != "--all"
            else m["entries"])
    if not ents:
        _log(f"✗ 无匹配备份: {target}")
        return False
    all_ok = True
    for e in ents:
        p = BACKUP_DIR / e["file"]
        issues = []
        if not p.exists():
            issues.append("文件缺失")
        else:
            plain = _decompress_if(p)               # .db.z → 临时明文
            try:
                if _sha256(plain) != e["sha256"]:
                    issues.append("sha256不一致(内容被改动)")
                if e["kind"] == "sqlite" and not _integrity_ok(plain):
                    issues.append("integrity_check失败")
            finally:
                if plain != p:
                    plain.unlink(missing_ok=True)
                try:
                    _db_stats(p, e["asset"])
                except sqlite3.DatabaseError as ex:
                    issues.append(f"新鲜度查询失败: {ex}")
        ok = not issues
        all_ok &= ok
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
    src_z = BACKUP_DIR / fname
    src_plain = _decompress_if(src_z)               # 压缩备份先解压
    if not yes and input(f"确认用 {fname} 覆盖 {ASSETS[e['asset']][1]} ? [y/N] ").lower() != "y":
        _log("= 已取消恢复")
        return False
    snapshot(e["asset"], "pre-restore")              # 恢复前兜底
    dst = Path(ASSETS[e["asset"]][1])
    tmp = dst.with_suffix(dst.suffix + ".part")
    shutil.copy2(src_plain, tmp)
    tmp.replace(dst)
    if src_plain != src_z:
        src_plain.unlink(missing_ok=True)
    _log(f"↺ 已恢复 {fname} → {dst}")
    return True


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="核心资产备份")
    ap.add_argument("cmd", choices=["now", "daily", "weekly", "prune", "list",
                                    "verify", "restore"])
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
