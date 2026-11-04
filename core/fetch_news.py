# -*- coding: utf-8 -*-
"""D1-28 N1: 宏观新闻抓取链 — news_raw 轻量标题层(macro.db)

实测语义(2026-11-01): major_news 日期过滤✓/800行每页+offset分页✓/
2018为海量灌入日(单日≥2400行)/近期官方口径2-5行每日/2023起稀疏缺日。
news接口无权限(40203), cls类接口名不存在 → 双源: tushare major_news
(深史回补) + akshare东财快讯stock_info_global_em(增量, 含摘要)。

N1只存标题层(title/pub_time/src/url, em源含摘要) — 海量正文由N2的
AI阶段按需重查(content字段按日+标题精确), 避免全史HTML灌库。

用法: python fetch_news.py [--start 20180601] [--refresh] [--sample]
"""
import argparse
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
    con.commit()
    con.close()


def _conn(path=None):
    ensure_db(path)
    return sqlite3.connect(str(path or MACRO_DB))


def fetch_day(day_ymd: str, path=None) -> int:
    """单日 major_news(分页拉全, 轻量字段), 幂等upsert; 返回入库行数/-1失败"""
    frames, offset = [], 0
    try:
        while True:
            df = T.call("major_news", "title,pub_time,src,url",
                        start_date=day_ymd, end_date=day_ymd,
                        limit=PAGE, offset=offset)
            frames.append(df)
            if len(df) < PAGE:
                break
            offset += PAGE
            if offset >= 4800:            # 单日>6页的保护(6×800)
                break
    except Exception as e:                                 # noqa: BLE001
        if "频率超限" in str(e):
            print(f"[news] {day_ymd} 频率超限(30次/小时), 跳过本轮")
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
            con.execute(
                "INSERT OR IGNORE INTO news_raw(src,pub_time,title,url,content,fetched_at)"
                " VALUES(?,?,?,?,?,?)",
                (SRC_MN, pub[:19], title[:500],
                 str(r.get("url")) if r.get("url") and str(r.get("url")) != "None" else None,
                 None, now))
            n += 1
    con.commit()
    con.close()
    return n


def _dedup_insert(con, src, pub, title, url, content, now):
    """跨源去重: 同日同标题已从其他源入库则跳过(东财/同花顺/新浪
    覆盖同一市场事件, 重复入库浪费AI分析算力)"""
    existing = con.execute(
        "SELECT 1 FROM news_raw WHERE title=? AND substr(pub_time,1,10)=substr(?,1,10)",
        (title, pub)).fetchone()
    if existing:
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


def run(start: str = "20180601", end: str = None, path=None) -> bool:
    """全史回补: 逐自然日幂等(已覆盖日跳过), 支持断点续传"""
    if not T.has_token():
        print("[news] 无 token, 跳过")
        return True
    import datetime as dt
    end = end or dt.date.today().strftime("%Y%m%d")
    done = days_done(SRC_MN, path)
    d = dt.date(int(start[:4]), int(start[4:6]), int(start[6:8]))
    d2 = dt.date(int(end[:4]), int(end[4:6]), int(end[6:8]))
    todo, cur = 0, d
    while cur <= d2:
        if cur.isoformat() not in done:
            todo += 1
        cur += dt.timedelta(days=1)
    print(f"[news] major_news 待回补 {todo} 日 ({start}~{end})")
    t0, i, cur = time.time(), 0, d
    interval = 60.0 / RATE_LIMIT_PER_MIN          # 均匀限速(18次/分≈3.3s间隔)
    rate_capped = False
    while cur <= d2:
        if rate_capped:                           # 小时限频(30/h)耗尽 → 本轮到此
            break
        iso = cur.isoformat()
        if iso not in done:
            time.sleep(interval)
            n = fetch_day(cur.strftime("%Y%m%d"), path)
            if n == -2:                           # 小时限频 → 不再空转(曾空转500日×3.3s)
                print(f"  ⏳ 小时限频(30/h)耗尽于{iso}, 本轮结束(渐进回补次轮续)")
                rate_capped = True
            i += 1
            if i % 30 == 0:
                print(f"  [{i}/{todo}] {time.time()-t0:.0f}s → {iso}", flush=True)
        cur += dt.timedelta(days=1)
    refresh_em(path)
    _summary(path)
    print(f"[news] 回补轮结束 {time.time()-t0:.0f}s"
          + (f" (限频中断于{i}/{todo})" if rate_capped else ""))
    return True


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
    """日增: major_news 近3日 + em/sina/ths三路快讯; 顺带渐进回补(限额内推10日/天)"""
    import datetime as dt
    done = days_done(SRC_MN, path)
    for back in range(3):
        d = (dt.date.today() - dt.timedelta(days=back)).strftime("%Y%m%d")
        fetch_day(d, path)
    # 渐进回补: 从最近未覆盖日往前推10日(30次/小时限内安全)
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
    a = ap.parse_args()
    refresh() if a.refresh else run(a.start, a.end)
    return 0


if __name__ == "__main__":
    sys.exit(main())
