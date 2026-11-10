# -*- coding: utf-8 -*-
"""D1-28 N1: 宏观新闻抓取链 — news_raw 轻量标题层(macro.db)

实测语义(2026-11-01): major_news 日期过滤✓/800行每页+offset分页✓/
2018为海量灌入日(单日≥2400行)/近期官方口径2-5行每日/2023起稀疏缺日。
news接口无权限(40203), cls类接口名不存在 → 双源: tushare major_news
(深史回补) + akshare东财快讯stock_info_global_em(增量, 含摘要)。

D1-28新闻回补实测(2026-11-05): 限频为多层(20次/分钟|30次/小时|40次/天
三层均实测触发过, 报错文本随触发层变化), 与夜间链共享token预算,
滚动24h窗口(实测21:45随昨夜repair链调用老化而恢复) →
run_rounds()自适应退避探测恢复, 不假设重置时刻。

N1只存标题层(title/pub_time/src/url, em源含摘要) — 海量正文由N2的
AI阶段按需重查(content字段按日+标题精确), 避免全史HTML灌库。

用法: python fetch_news.py [--start 20180601] [--refresh] [--deadline-min 720]
"""
import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tushare_api as T                                    # noqa: E402

MACRO_DB = BASE / "data" / "macro.db"
PAGE = 800                     # 实测单页上限
RATE_LIMIT_PER_MIN = 18        # 实测20次/分钟(留余量)
INTERVAL = 60.0 / RATE_LIMIT_PER_MIN   # 调用间隔≈3.3s(天间+翻页共用)
MAX_EMPTY_ROUNDS = 5           # run_rounds: 连续零进展轮上限(配额窗口未恢复→退出)
SRC_MN = "major_news"          # tushare官方宏观(深史)
SRC_EM = "em_flash"            # akshare东财快讯(增量)


def ensure_db(path=None):
    p = path or MACRO_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.execute("""CREATE TABLE IF NOT EXISTS news_raw(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      src TEXT NOT NULL, pub_time TEXT NOT NULL, title TEXT NOT NULL,
      url TEXT, content TEXT, fetched_at TEXT,
      UNIQUE(src, pub_time, title))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_news_pub ON news_raw(pub_time)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_news_src ON news_raw(src)")
    # P4r: 跨源去重按title查 — 无索引时O(n×m)扫全表, 有索引O(m×log n)
    con.execute("CREATE INDEX IF NOT EXISTS idx_news_title ON news_raw(title)")
    con.commit()
    con.close()


def _conn(path=None):
    ensure_db(path)
    # timeout=30s: 与夜间链(repair/auto_analyze)并发写macro.db时等待
    # 而非立即database is locked崩掉(2026-11-09实测: 21:54驱动撞22:03 repair)
    return sqlite3.connect(str(path or MACRO_DB), timeout=30)


def fetch_day(day_ymd: str, path=None, with_content: bool = False) -> int:
    """单日 major_news(分页拉全, 轻量字段), 幂等upsert; 返回入库行数/-1失败

    with_content: 近期日增量拉content字段(富化事件详情 — 轻量层设计下
    content为空导致macro_events内容=标题; 稀疏日载荷可忽略)。已存在的
    同日同标题薄行顺带回填content(不重复插入)。"""
    fields = "title,content,pub_time,src,url" if with_content else "title,pub_time,src,url"
    frames, offset = [], 0
    try:
        while True:
            df = T.call("major_news", fields,
                        start_date=day_ymd, end_date=day_ymd,
                        limit=PAGE, offset=offset)
            frames.append(df)
            if len(df) < PAGE:
                break
            # 翻页间也限速: 海量日3页连发2s内3次调用, 混合速率超20次/分层
            time.sleep(INTERVAL)
            offset += PAGE
            if offset >= 4800:            # 单日>6页的保护(6×800)
                break
    except Exception as e:                                 # noqa: BLE001
        if "频率超限" in str(e):
            # 打印API原始限额文本(40次/天|20次/分钟), 便于判断窗口语义
            lim = str(e).split("频率超限")[-1].split("，")[0][:20]
            print(f"[news] {day_ymd} 频率超限{lim}, 跳过本轮")
            return -2                       # 调用方可判断是否继续
        print(f"[news] {day_ymd} 拉取失败: {str(e)[:80]}")
        return -1
    con = _conn(path)
    n = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for df in frames:
        for _, r in df.iterrows():
            title = str(r.get("title") or "").strip()
            pub = str(r.get("pub_time") or "")
            if not title or not pub:
                continue
            url = str(r.get("url")) if r.get("url") and str(r.get("url")) != "None" else None
            ctext = str(r.get("content") or "").strip() or None if with_content else None
            title = _clean_title(title)
            if ctext:                                # 富化: 已有薄行回填content
                rid = con.execute(
                    "SELECT id FROM news_raw WHERE title=? AND substr(pub_time,1,10)=substr(?,1,10)",
                    (title, pub)).fetchone()
                if rid:                              # 跨源已有(em摘要版更优, 不覆盖)
                    cur = con.execute(
                        "UPDATE news_raw SET content=? WHERE id=? AND COALESCE(content,'')=''",
                        (ctext[:2000], rid[0]))
                    if cur.rowcount:
                        n += 1
                    continue
            n += _dedup_insert(con, SRC_MN, pub, title, url, ctext, now)
    con.commit()
    con.close()
    return n


def _clean_title(raw: str, limit: int = 60) -> str:
    """快讯标题清洗: em源"标题"字段常为【主题】+全文, sina源拿内容当标题 —
    【主题】6~40字→取主题, 短主题(如"央行")→拼接保留不丢主语;
    超70字→句边界截断(≤70字视为合格直接放行 — 幂等, 二次清洗不再缩短);
    标题=内容由清洗解耦, 全文在content"""
    t = re.sub(r"\s+", " ", str(raw or "").strip())
    m = re.match(r"^【(.+?)】(.*)$", t, re.S)
    if m:
        theme, rest = m.group(1).strip(), m.group(2).strip()
        if 6 <= len(theme) <= 40:
            return theme[:limit]
        if rest:                          # 短前缀: 噪音(快讯)或主语(央行)都不丢
            t = f"{theme}：{rest}" if len(theme) >= 2 else rest
    if len(t) <= limit + 10:
        return t
    cut = t[:limit + 10]
    for p in ("。", "；", ";", "！", "？", "?", "，", "、", ",", " "):
        i = cut.rfind(p)
        if i >= 20:
            return t[:i].rstrip("。，,、；;！!？? ")
    return t[:limit]


def _dedup_insert(con, src, pub, title, url, content, now):
    """跨源去重: 同日同标题已从其他源入库则跳过(东财/同花顺/新浪
    覆盖同一市场事件, 重复入库浪费AI分析算力); 标题先清洗(快讯体全文→主题)。
    内容升级: 已有行content为空(major_news轻量层/短快讯)且新源带富文本
    (em摘要) → 回填 — 实测major_news接口无content值, 富化靠跨源回流。"""
    title = _clean_title(title)[:500]      # 与INSERT截断一致(原文>500时查询会漏)
    import re as _re
    if content and "<" in content:          # 剥离网页HTML(广告/正文标记 — 全文
        content = _re.sub(r"<[^>]+>", "", content)  # HTML入库会污染下游HTML生成)
        content = _re.sub(r"\s+", " ", content).strip()[:2000] or None
    existing = con.execute(
        "SELECT id FROM news_raw WHERE title=? AND substr(pub_time,1,10)=substr(?,1,10)",
        (title, pub)).fetchone()
    if existing:
        if content and len(content) > len(title):      # 富文本才值得升级
            con.execute(
                "UPDATE news_raw SET content=? WHERE id=? AND COALESCE(content,'')=''",
                (content[:2000], existing[0]))
        return 0                       # 跨源重复, 跳过
    con.execute(
        "INSERT OR IGNORE INTO news_raw(src,pub_time,title,url,content,fetched_at)"
        " VALUES(?,?,?,?,?,?)",
        (src, pub[:19], title[:500], url, content, now))
    return 1


def refresh_em(path=None) -> int:
    """akshare东财快讯增量(latest~200条含摘要), 幂等upsert"""
    try:
        import akshare as ak
        df = ak.stock_info_global_em()
    except Exception as e:                                 # noqa: BLE001
        print(f"[news] em增量失败: {str(e)[:80]}")
        return -1
    con = _conn(path)
    n, now = 0, time.strftime("%Y-%m-%d %H:%M:%S")
    for _, r in df.iterrows():
        title = str(r.get("标题") or "").strip()
        pub = str(r.get("发布时间") or "")
        if not title or not pub:
            continue
        n += _dedup_insert(con, SRC_EM, pub, title,
                           str(r.get("链接") or "")[:500] or None,
                           str(r.get("摘要") or "")[:2000], now)
    con.commit()
    con.close()
    return n


def days_done(src: str, path=None) -> set:
    """该源已覆盖的日期集(YYYY-MM-DD)"""
    con = _conn(path)
    out = {r[0] for r in con.execute(
        "SELECT DISTINCT substr(pub_time,1,10) FROM news_raw WHERE src=?", (src,))}
    con.close()
    return out


def _missing_dates(start_d, end_d, done: set) -> list:
    """区间内未覆盖日列表 — 近端优先(用户最关心近期事件, 情绪图标注
    主要落在近端; 深史2018海量日放后)。返回按'离今天近→远'排序。"""
    import datetime as dt
    out, cur = [], start_d
    while cur <= end_d:
        if cur.isoformat() not in done:
            out.append(cur)
        cur += dt.timedelta(days=1)
    return sorted(out, reverse=True)          # 近端在前


def run(start: str = "20180601", end: str = None, path=None, tail: bool = True):
    """全史回补: 逐自然日幂等(已覆盖日跳过), 支持断点续传。
    遍历序=近端优先(_missing_dates) — 修双头推进中间空洞(2026-08~10
    62日缺口实测: 旧正向遍历从2018起, 近端缺口永远轮不到)。

    返回: True=区间全覆盖完成 / "capped"=限频中断(真值, fetch_daily
    backfill协议把False当自报失败, 故限频不用False)
    tail: 轮尾是否跑refresh_em+_summary(驱动多轮场景跳过, 免重复akshare拉取)"""
    if not T.has_token():
        print("[news] 无 token, 跳过")
        return True
    import datetime as dt
    end = end or dt.date.today().strftime("%Y%m%d")
    done = days_done(SRC_MN, path)
    d = dt.date(int(start[:4]), int(start[4:6]), int(start[6:8]))
    d2 = dt.date(int(end[:4]), int(end[4:6]), int(end[6:8]))
    missing = _missing_dates(d, d2, done)
    todo = len(missing)
    front = missing[0].isoformat() if missing else "-"
    print(f"[news] major_news 待回补 {todo} 日 ({start}~{end}), 本轮自 {front} (近端优先)")
    t0, i = time.time(), 0
    rate_capped = False
    for cur in missing:
        if rate_capped:                           # 限频(40次/天)耗尽 → 本轮到此
            break
        iso = cur.isoformat()
        time.sleep(INTERVAL)                  # 均匀限速(18次/分≈3.3s间隔)
        n = fetch_day(cur.strftime("%Y%m%d"), path)
        if n == -2:                           # 限频 → 不再空转(曾空转500日×3.3s)
            print(f"  ⏳ 限频耗尽于{iso}, 本轮结束(渐进回补次轮续)")
            rate_capped = True
        i += 1
        if i % 30 == 0:
            print(f"  [{i}/{todo}] {time.time()-t0:.0f}s → {iso}", flush=True)
    if tail:
        refresh_em(path)
        _summary(path)
    print(f"[news] 回补轮结束 {time.time()-t0:.0f}s"
          + (f" (限频中断于{i}/{todo})" if rate_capped else ""))
    return "capped" if rate_capped else True


def clean_existing(path=None) -> dict:
    """存量清洗: news_raw与macro_events两侧同步清洗超长标题(>60字快讯体)。
    同步用同一清洗函数 → _attach_ai_analysis按标题的匹配不断链;
    news_raw撞UNIQUE(src,pub_time,title)的行跳过(OR IGNORE)。"""
    con = _conn(path)
    out = {"news_raw": 0, "macro_events": 0, "skipped": 0}
    for nid, t in con.execute(
            "SELECT id, title FROM news_raw WHERE length(title) > 60").fetchall():
        c = _clean_title(t)
        if c == t:
            continue
        cur = con.execute("UPDATE OR IGNORE news_raw SET title=? WHERE id=?",
                          (c, nid))
        if cur.rowcount:
            out["news_raw"] += 1
        else:
            out["skipped"] += 1
    has_ev = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='macro_events'").fetchone()
    if has_ev:
        for eid, t in con.execute(
                "SELECT id, title FROM macro_events WHERE length(title) > 60").fetchall():
            c = _clean_title(t)
            if c == t:
                continue
            con.execute("UPDATE macro_events SET title=? WHERE id=?", (c, eid))
            out["macro_events"] += 1
    con.commit()
    con.close()
    print(f"[news] 存量清洗: news_raw {out['news_raw']}行 / macro_events "
          f"{out['macro_events']}行" + (f" / UNIQUE跳过{out['skipped']}" if out["skipped"] else ""))
    return out


def refresh_sina(path=None) -> int:
    """akshare新浪全球快讯(20条快照)"""
    try:
        import akshare as ak
        df = ak.stock_info_global_sina()
    except Exception as e:
        print(f"[news] sina失败: {str(e)[:60]}")
        return -1
    con = _conn(path)
    n, now = 0, time.strftime("%Y-%m-%d %H:%M:%S")
    for _, r in df.iterrows():
        title = str(r.get("内容") or "").strip()
        pub = str(r.get("时间") or "")
        if not title or not pub:
            continue
        n += _dedup_insert(con, "sina_flash", pub, title, None, title[:2000], now)
    con.commit(); con.close()
    return n


def refresh_ths(path=None) -> int:
    """akshare同花顺快讯(20条快照)"""
    try:
        import akshare as ak
        df = ak.stock_info_global_ths()
    except Exception as e:
        print(f"[news] ths失败: {str(e)[:60]}")
        return -1
    con = _conn(path)
    n, now = 0, time.strftime("%Y-%m-%d %H:%M:%S")
    for _, r in df.iterrows():
        title = str(r.get("标题") or "").strip()
        pub = str(r.get("发布时间") or "")
        if not title or not pub:
            continue
        n += _dedup_insert(con, "ths_flash", pub, title,
                           str(r.get("链接") or "")[:500] or None,
                           str(r.get("内容") or "")[:2000], now)
    con.commit(); con.close()
    return n


def refresh(path=None) -> bool:
    """日增: major_news 近3日(含content富化) + em/sina/ths三路快讯; 顺带渐进回补"""
    import datetime as dt
    done = days_done(SRC_MN, path)
    for back in range(3):
        d = (dt.date.today() - dt.timedelta(days=back)).strftime("%Y%m%d")
        fetch_day(d, path, with_content=True)      # 近期稀疏日顺带拉content
    # 渐进回补: 从最近未覆盖日往前推10日(多层限频下安全余量, -2即止)
    probe = dt.date.today() - dt.timedelta(days=4)
    filled = 0
    while filled < 10 and probe >= dt.date(2018, 6, 1):
        iso = probe.isoformat()
        if iso not in done:
            n = fetch_day(probe.strftime("%Y%m%d"), path)
            if n == -2:                       # 限频 → 本轮回补到此为止
                break
            if isinstance(n, int) and n >= 0:
                filled += 1
        probe -= dt.timedelta(days=1)
    refresh_em(path)
    refresh_sina(path)                  # D1-28 N1+: 新浪快讯20条
    refresh_ths(path)                   # D1-28 N1+: 同花顺快讯20条
    _summary(path)
    return True


def _mn_days(path=None) -> int:
    """major_news已覆盖日数(驱动轮间进展度量)"""
    return len(days_done(SRC_MN, path))


def _driver_heartbeat(on=True):
    """任务运行中心心跳登记(外部进程视角可见; on=False清除)"""
    try:
        import json as _j
        import time as _t
        f = BASE.parent / "data" / "task_heartbeat.json"
        beats = {}
        if f.exists():
            try:
                beats = _j.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                pass
        beats["news_driver"] = _t.time() if on else 0
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(_j.dumps(beats, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def run_rounds(deadline_min: float = 720, start: str = "20180601",
               end: str = None, path=None) -> bool:
    """D1-28新闻回补: 限频自适应驱动 — run()至限频→指数退避→续传, 直至完成/截止/连续空轮

    限频窗口语义不明(官方口径40次/天, 实测与夜间链共享预算burst即触发,
    非整点滚动) → 不假设重置时刻: 退避5→10→20→40→60min封顶探测恢复;
    有进展即重置退避; 空轮容忍随截止时长放大(长驱动可等到日内/午夜
    窗口恢复, 60min退避下探测成本仅1次/小时), 短驱动快速退出防空转。
    """
    t0 = time.time()
    empty, backoff = 0, 5 * 60
    bail = max(MAX_EMPTY_ROUNDS, int(deadline_min // 60) + 1)
    try:
        _driver_heartbeat(True)                    # 心跳续期(任务运行中心可见)
        while time.time() - t0 < deadline_min * 60:
            _driver_heartbeat(True)
            before = _mn_days(path)
            r = run(start, end, path, tail=False)   # 轮尾akshare/汇总只做一次(退出前)
            if r is True:
                print(f"[news] ✓ 全史回补完成 ({time.time()-t0:.0f}s)")
                return True
            if _mn_days(path) > before:            # 本轮有新覆盖日 → 重置退避
                empty, backoff = 0, 5 * 60
                print(f"[news] 本轮有进展, 立即续传下一轮")
                continue
            empty += 1
            if empty >= bail:
                print(f"[news] 连续{empty}轮零进展(配额窗口未恢复), 驱动退出"
                      f" — 断点已保存, 稍后重跑或等夜间渐进10日/天")
                return False
            print(f"[news] 限频未恢复, 退避{backoff//60}min后重试"
                  f" (空轮{empty}/{bail})", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60 * 60)
        _driver_heartbeat(False)
        print(f"[news] 截止{deadline_min:.0f}min到, 驱动退出(断点已保存, 重跑续传)")
        return False
    finally:
        refresh_em(path)                           # 驱动收尾: 增量+汇总各一次
        _summary(path)


def _summary(path=None):
    con = _conn(path)
    rows = con.execute(
        "SELECT src, COUNT(*), MIN(substr(pub_time,1,10)), MAX(substr(pub_time,1,10)), "
        "COUNT(DISTINCT substr(pub_time,1,10)) FROM news_raw GROUP BY src").fetchall()
    con.close()
    for src, n, lo, hi, nd in rows:
        print(f"[news] {src}: {n}行 / {nd}日 ({lo}~{hi})")


def sync_map(path=None) -> dict:
    """同步区间图: 每源 覆盖日数/起止/近90日缺口列表"""
    con = _conn(path)
    out = {}
    for src, n, lo, hi, nd in con.execute(
            "SELECT src, COUNT(*), MIN(substr(pub_time,1,10)), "
            "MAX(substr(pub_time,1,10)), COUNT(DISTINCT substr(pub_time,1,10)) "
            "FROM news_raw GROUP BY src").fetchall():
        out[src] = {"rows": n, "days": nd, "first": lo, "last": hi}
    con.close()
    return out


def main():
    ap = argparse.ArgumentParser(description="宏观新闻抓取(D1-28 N1)")
    ap.add_argument("--start", default="20180601")
    ap.add_argument("--end", default=None)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--clean", action="store_true",
                    help="存量清洗: news_raw/macro_events超长标题(快讯体)两侧同步")
    ap.add_argument("--deadline-min", type=float, default=None,
                    help="驱动模式: 限频自适应多轮回补直至完成/截止(分钟)")
    a = ap.parse_args()
    if a.refresh:
        refresh()
    elif a.clean:
        clean_existing()
    elif a.deadline_min:
        run_rounds(a.deadline_min, a.start, a.end)
    else:
        run(a.start, a.end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
