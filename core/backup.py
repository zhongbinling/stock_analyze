# -*- coding: utf-8 -*-
"""核心资产备份 (数据库/密钥/配置): SQLite在线快照 + GFS保留 + 完整性校验 + 恢复

设计:
  - SQLite 用在线备份 API (Connection.backup), 不拷贝正在写入的库
  - 每份快照落盘即 integrity_check + 数据新鲜度抽查, sha256 记入 manifest
  - GFS保留: 最近30份 ∪ 最近12个ISO周各1份(周内最新) ∪ 最近12个月各1份
  - 高危操作前置快照: sector.py calc 前自动打 pre-calc 标签

用法:
  python backup.py now  [-t tag]        # 立即快照(sha与最近一次相同则跳过)
  python backup.py daily                # now + prune (任务计划程序每日入口)
  python backup.py prune                # GFS清理
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
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(__file__).resolve().parent
ROOT = BASE.parent

# 资产清单: key -> (kind, path); 缺失告警跳过(各环境资产集不同)
ASSETS = {
    "sentiment": ("sqlite", BASE / "data" / "sentiment.db"),
    "app": ("sqlite", ROOT / "data" / "app.db"),
    "secret": ("file", ROOT / "data" / "secret.key"),
    "token": ("file", BASE / "data" / "tushare_token.txt"),
    "cfg_core": ("file", BASE / "config.yaml"),
    "cfg_server": ("file", ROOT / "config" / "server.yaml"),
}
BACKUP_DIR = ROOT / "backups"
MANIFEST = BACKUP_DIR / "manifest.json"

DAILY_N, WEEKLY_N, MONTHLY_N = 30, 12, 12


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
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


def snapshot(asset: str, tag: str = "") -> dict | None:
    """单资产快照: 在线备份→校验→sha去重→落manifest; 缺失/未变化返回None"""
    kind, src = ASSETS[asset]
    if not src.exists():
        print(f"[backup] 跳过 {asset}: {src} 不存在")
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now()
    stem = f"{asset}_{ts.strftime('%Y%m%d_%H%M%S')}" + (f"_{tag}" if tag else "")
    tmp = BACKUP_DIR / (stem + ".part")

    if kind == "sqlite":
        s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
        d = sqlite3.connect(str(tmp))
        try:
            s.backup(d)
        finally:
            s.close()
            d.close()
        ok = _integrity_ok(tmp)
        if not ok:
            tmp.unlink(missing_ok=True)
            print(f"[backup] ✗ {asset} 快照完整性校验失败, 已丢弃")
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
        print(f"[backup] = {asset} 未变化, 跳过 ({sha[:12]})")
        return None

    final = BACKUP_DIR / f"{stem}.{ext}"
    tmp.replace(final)
    entry = {"file": final.name, "asset": asset, "kind": kind, "sha256": sha,
             "size": final.stat().st_size, "time": ts.strftime("%Y-%m-%d %H:%M:%S"),
             "date": ts.strftime("%Y%m%d"), "tag": tag,
             "integrity": "ok" if kind == "sqlite" else "-", "stats": stats}
    m["entries"].append(entry)
    _save_manifest(m)
    print(f"[backup] + {final.name} ({final.stat().st_size / 1048576:.1f} MB"
          + (f", {stats.get('sector_scores') or stats.get('sentiment') or ''}" if stats else "") + ")")
    return entry


def prune():
    """GFS清理: 每资产保留 最近DAILY_N份 ∪ 最近WEEKLY_N个ISO周各最新1份 ∪ 最近MONTHLY_N个月各最新1份"""
    m = _load_manifest()
    keep_files = set()
    for asset in ASSETS:
        ents = sorted((e for e in m["entries"] if e["asset"] == asset),
                      key=lambda e: (e["date"], e["time"]), reverse=True)
        if not ents:
            continue
        prot = set(e["file"] for e in ents[:DAILY_N])
        weeks, months = [], []
        for e in ents:                       # 降序: 每桶最先遇到的是该桶最新一份
            d = dt.datetime.strptime(e["date"], "%Y%m%d").date()
            wk, mo = d.isocalendar()[:2], d.strftime("%Y%m")
            if wk not in [w for w, _ in weeks]:
                weeks.append((wk, e["file"]))
            if mo not in [x for x, _ in months]:
                months.append((mo, e["file"]))
        prot |= set(f for _, f in weeks[:WEEKLY_N])
        prot |= set(f for _, f in months[:MONTHLY_N])
        keep_files |= prot
    removed = 0
    for e in list(m["entries"]):
        if e["file"] not in keep_files:
            p = BACKUP_DIR / e["file"]
            if p.exists():
                p.unlink()
            m["entries"].remove(e)
            removed += 1
    _save_manifest(m)
    print(f"[backup] prune: 保留 {len(m['entries'])} 份, 清理 {removed} 份")


def verify(target: str = "--all") -> bool:
    """校验: 文件存在 + sha256比对 + integrity + 新鲜度; 返回整体是否通过"""
    m = _load_manifest()
    ents = ([e for e in m["entries"] if e["file"] == target] if target != "--all"
            else m["entries"])
    if not ents:
        print(f"[backup] 无匹配备份: {target}")
        return False
    all_ok = True
    for e in ents:
        p = BACKUP_DIR / e["file"]
        issues = []
        if not p.exists():
            issues.append("文件缺失")
        else:
            if _sha256(p) != e["sha256"]:
                issues.append("sha256不一致(内容被改动)")
            if e["kind"] == "sqlite":
                if not _integrity_ok(p):
                    issues.append("integrity_check失败")
                try:
                    _db_stats(p, e["asset"])
                except sqlite3.DatabaseError as ex:
                    issues.append(f"新鲜度查询失败: {ex}")
        ok = not issues
        all_ok &= ok
        print(f"[backup] {'✓' if ok else '✗'} {e['file']} "
              f"({e['time']}{', ' + e['tag'] if e['tag'] else ''}) {'; '.join(issues)}")
    return all_ok


def restore(fname: str, yes: bool = False) -> bool:
    """恢复: 校验→当前态打pre-restore快照→原子替换目标库"""
    m = _load_manifest()
    e = next((x for x in m["entries"] if x["file"] == fname), None)
    if not e:
        print(f"[backup] manifest中找不到 {fname}")
        return False
    if not verify(fname):
        print(f"[backup] 校验未通过, 拒绝恢复")
        return False
    if not yes and input(f"确认用 {fname} 覆盖 {ASSETS[e['asset']][1]} ? [y/N] ").lower() != "y":
        print("[backup] 已取消")
        return False
    snapshot(e["asset"], tag="pre-restore")           # 恢复前兜底
    src = BACKUP_DIR / fname
    dst = Path(ASSETS[e["asset"]][1])
    tmp = dst.with_suffix(dst.suffix + ".part")
    shutil.copy2(src, tmp)
    tmp.replace(dst)
    print(f"[backup] 已恢复 {fname} → {dst}")
    return True


def cmd_now(tag=""):
    for a in ASSETS:
        snapshot(a, tag)


def main():
    ap = argparse.ArgumentParser(description="核心资产备份")
    ap.add_argument("cmd", choices=["now", "daily", "prune", "list", "verify", "restore"])
    ap.add_argument("-t", "--tag", default="")
    ap.add_argument("target", nargs="?", default=None,
                    help="verify/restore 的备份文件名; verify 省略时校验全部")
    ap.add_argument("--all", dest="verify_all", action="store_true", help="verify 校验全部")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    if args.cmd == "now":
        cmd_now(args.tag)
    elif args.cmd == "daily":
        cmd_now()
        prune()
    elif args.cmd == "prune":
        prune()
    elif args.cmd == "list":
        m = _load_manifest()
        for e in sorted(m["entries"], key=lambda x: (x["date"], x["asset"]), reverse=True):
            print(f"  {e['time']}  {e['file']:<44} {e['size'] / 1048576:>8.1f}MB "
                  f"{'[' + e['tag'] + ']' if e['tag'] else '':<12} {e['integrity']}")
        print(f"  共 {len(m['entries'])} 份")
    elif args.cmd == "verify":
        t = args.target if (args.target and not args.verify_all) else "--all"
        sys.exit(0 if verify(t) else 1)
    elif args.cmd == "restore":
        sys.exit(0 if restore(args.target, args.yes) else 1)
    print(f"[backup] done ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
