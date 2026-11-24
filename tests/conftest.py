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

    # D2-2: index_daily(主力指数到最新日, 作评分健康基准)
    conn.execute("""CREATE TABLE index_daily(code TEXT, date TEXT, open REAL, high REAL,
      low REAL, close REAL, amount REAL, PRIMARY KEY(code, date))""")
    for d in dates40:
        conn.execute("INSERT INTO index_daily VALUES('1.000001',?,3000,3010,2990,3005,3e11)", (d,))
    # D1-10 S1: SW底座表(资产注册覆盖)
    for t in ("sw_daily", "sw_index_comp", "sw_breadth", "sw_pool", "sw_fflow"):
        conn.execute(f"CREATE TABLE {t}(index_code TEXT, date TEXT, x REAL)")
    for d in dates30:
        conn.execute("INSERT INTO sw_breadth VALUES('801010.SI',?,10)", (d,))
        conn.execute("INSERT INTO sw_pool VALUES('801010.SI',?,1)", (d,))

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


@pytest.fixture()
def server_app(tmp_path, monkeypatch):
    """共享 Flask 测试应用: 临时配置/app.db + 登录管理员(level 9) 的 test client

    - SA_NO_JOBS=1: 不启动工作线程/调度器, 便于断言队列
    - WTF_CSRF 关闭; session 预置 _user_id
    """
    import server as SV
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "server.yaml").write_text(
        f"server: {{host: 127.0.0.1, port: 8599}}\n"
        f"app: {{theme_default: light}}\n"
        f"data: {{app_db: '{(data / 'app.db').as_posix()}', "
        f"secret_key_file: '{(data / 'secret.key').as_posix()}'}}\n"
        f"scheduler: {{enabled: false}}\n", encoding="utf-8")
    monkeypatch.setattr(SV, "SERVER_CFG", cfg / "server.yaml")
    monkeypatch.setenv("SA_NO_JOBS", "1")
    app = SV.create_app()
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["TESTING"] = True
    # 全局任务队列排空(前序测试自动入队的 recalc 等残留会污染 queue_size 断言)
    # D1-30: 登记簿一并清 — enqueue同键去重后, 跨测试残留的queued条目
    # 会把后续测试的合法入队误判为重复(409/queue_size=0实测)
    from server import jobs as _jobs
    while not _jobs._q.empty():
        _jobs._q.get_nowait()
    _jobs._queue_log.clear()
    with app.app_context():
        SV.db.create_all()
        from server.models import User
        u = User(username="admin_t", level=9)
        u.set_password("x")
        SV.db.session.add(u)
        SV.db.session.commit()
        uid = u.id
    client = app.test_client()
    with client.session_transaction() as s:
        s["_user_id"] = str(uid)
    return {"app": app, "client": client, "tmp": tmp_path}


# ── D1-33r: 测试与真实备份彻底隔离 ──
# 事故复盘(11-06 / 11-18 / 11-24 三次"备份清单只剩几份、磁盘上一堆文件却不可见"):
#   ① tests/test_d27 把夹具条目写进真实 backups/manifest.json — 自10-30起
#      每跑一次测试套件就清空一次真实清单;
#   ② 有测试在进程内调 fetch_daily.run_backfill, 顺带对真实 sentiment.db
#      (1.4GB) 做了一次真实的 pre-backfill 快照(03:22:39 实测)。
# 此前两次都误判为"并发覆盖/旧进程", 真正的写入方是测试套件自己。
_REAL_BACKUPS = REPO / "backups"


def _manifest_files() -> set:
    """真实清单里在册的备份文件键集合(读不到/非法JSON → 空集)"""
    try:
        m = json.loads((_REAL_BACKUPS / "manifest.json").read_text(encoding="utf-8"))
        return {e["file"] for e in m.get("entries", [])
                if isinstance(e, dict) and e.get("file")}
    except (OSError, ValueError, AttributeError):
        return set()


def _backup_file_exists(f: str) -> bool:
    if "/" not in f:
        return (_REAL_BACKUPS / f).exists()
    root, name = f.split("/", 1)
    return (_REAL_BACKUPS / root).exists() or (_REAL_BACKUPS / root / name).exists()


@pytest.fixture(autouse=True)
def _isolate_backups(tmp_path, monkeypatch):
    """每个测试默认把 core/backup.py 指向临时目录, 资产源指向不存在的路径
    (未隔离的 snapshot() 直接"不存在跳过", 零I/O)。
    需要真实备份行为的测试用自己的夹具覆盖 — autouse先执行, 后设置的生效。"""
    import backup as BK
    root = tmp_path / "backups"
    empty = tmp_path / "no_such_assets"
    monkeypatch.setattr(BK, "BACKUP_DIR", root)
    monkeypatch.setattr(BK, "MANIFEST", root / "manifest.json")
    monkeypatch.setattr(BK, "LOG_FILE", root / "backup.log")
    monkeypatch.setattr(BK, "ASSETS", {
        k: (kind, empty / f"{k}.dat") for k, (kind, _p) in BK.ASSETS.items()})


@pytest.fixture(scope="session", autouse=True)
def _real_manifest_canary():
    """守卫: 测试期间真实备份清单不得丢失"在册且磁盘文件仍在"的条目
    (服务端正常操作删除条目时连同文件一起删; 只有被整档覆盖才会出现
    文件在而条目丢)。套件结束时任选失败 — 防止再有测试绕过 backup 模块
    直接写 backups/ 下的文件。"""
    before = _manifest_files()
    yield
    lost = sorted(f for f in before
                  if _backup_file_exists(f) and f not in _manifest_files())
    assert not lost, (
        f"真实备份清单在测试期间被改写: {len(lost)}/{len(before)} 条在册条目丢失"
        f"(如 {lost[:3]}) — 有测试未隔离, 直接写了 backups/ 下的文件")


# ── D1-35: 测试与生产个股库(core/data/stock.db)隔离 ──
# 事故复盘(11-24): 新写的避雷针页面渲染测试带了server_app却没带env(各stock测试
# 文件里把FS.STOCK_DB指到临时库的夹具) — server_app只隔离app.db, FS._conn()连的
# 是生产库, 一条INSERT OR REPLACE往生产自选池塞了14只假股票、覆盖了茅台那一行
# (已按事前快照逐行恢复)。此前个股库的隔离全靠每个测试自己记得带env。
_REAL_STOCK_DB = CORE / "data" / "stock.db"


def _stock_pool_rows():
    """生产自选池成员(代码/名称/行业/入池时间); 库不存在或读失败 → None"""
    if not _REAL_STOCK_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{_REAL_STOCK_DB.as_posix()}?mode=ro", uri=True,
                              timeout=30)
        rows = con.execute("SELECT ts_code, name, industry, added_at FROM stock_pool "
                           "ORDER BY ts_code").fetchall()
        con.close()
        return rows
    except sqlite3.Error:
        return None


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_stock_db: 该用例接触生产个股库(仅T3端到端: 读生产真数据+真模型)")


@pytest.fixture(autouse=True)
def _isolate_stock_db(request, tmp_path, monkeypatch):
    """每个测试默认把 fetch_stock.STOCK_DB 指到空的临时库 — 测试里任何不带path的
    FS._conn()/刷新函数都落不到生产库。需要真实数据的用例显式打 real_stock_db
    标记; 各文件自带的env夹具在其后执行(同为monkeypatch, 后设置的生效), 行为不变。"""
    if request.node.get_closest_marker("real_stock_db"):
        return
    import fetch_stock as FS
    monkeypatch.setattr(FS, "STOCK_DB", tmp_path / "_isolated_stock.db")


@pytest.fixture(scope="session", autouse=True)
def _real_stock_pool_canary():
    """守卫: 测试期间生产自选池成员不得变化 — 没有测试应该增删改池成员(页面上
    正常入池是用户操作, 与跑测试同时发生才会误报)。套件结束时任选失败。"""
    before = _stock_pool_rows()
    yield
    after = _stock_pool_rows()
    if before is None or after is None or after == before:
        return
    gone = [r for r in before if r not in after]
    new = [r for r in after if r not in before]
    raise AssertionError(
        f"生产自选池在测试期间被改写: 新增{len(new)}条(如 {new[:3]})、"
        f"丢失或变更{len(gone)}条(如 {gone[:3]}) — 有测试未隔离, 直接写了生产 stock.db")


# ── JS语法门禁(共享): 脚本写临时文件再交给node ──
# 原各测试内联 `node -e "new Function(process.argv[1])" <整段脚本>` — 脚本经
# 命令行参数传递, Windows命令行上限约32767字符; 避雷针页脚本涨到33076字符时
# 5个门禁同时报 WinError 206(文件名或扩展名太长), 被误读成页面脚本有语法错。
def _main_script(html: str) -> str:
    """模板里最长的一段内联<script>(无src的才是页面逻辑)"""
    import re
    blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
    assert blocks, "模板中没有内联<script>"
    return max(blocks, key=len)


@pytest.fixture()
def js_syntax(tmp_path):
    """返回校验函数: js_syntax(source) / js_syntax.check_html(html)"""
    import os
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node不可用")

    def check(source: str) -> None:
        if "<script>" in source:                 # 传入整份模板 → 取最长内联脚本
            source = _main_script(source)
        p = tmp_path / "_gate.js"
        p.write_text(source, encoding="utf-8")
        r = subprocess.run(
            [node, "-e",
             'new Function(require("fs").readFileSync(process.argv[1],"utf8"))', str(p)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        assert r.returncode == 0, f"JS语法错误: {(r.stderr or '')[:300]}"

    check.check_html = lambda html: check(_main_script(html))
    return check
