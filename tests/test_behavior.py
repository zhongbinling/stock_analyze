# -*- coding: utf-8 -*-
"""历史回测: expanding分位无前视/因子组合缺失归一/状态划分/阈值回测无前视/
API与策略CRUD/页面门禁"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import behavior as BH  # noqa: E402


# ── 引擎单元 ──

def test_expanding_pct_no_lookahead():
    """递增序列: 分位=i/(i+1)·200-100单调升且始终>0 — 未来值不参与当期分位"""
    pairs = [(f"2026-01-{i+1:02d}", float(i)) for i in range(5)]
    out = BH._expanding_pct(pairs)
    vals = [v for _, v in out]
    assert vals[0] == 100.0                     # 单值序列: 自身即100%位
    assert vals == sorted(vals)                 # 递增序列分位单调升
    # 前视检验: 把未来值改大, 前期分位不变
    pairs2 = pairs + [("2026-01-06", 999.0)]
    vals2 = [v for _, v in BH._expanding_pct(pairs2)]
    assert vals2[:5] == vals                    # 新增未来值不影响历史分位


def test_composite_weight_renorm_missing():
    """因子缺失日按当日可得因子权重归一(与评分引擎同构)"""
    fr = {"a": [("2026-01-01", 1.0), ("2026-01-02", 2.0)],
          "b": [("2026-01-02", 1.0)]}           # b首日缺失
    dates, vals = BH.composite(fr, {"a": 30, "b": 10})
    assert dates == ["2026-01-01", "2026-01-02"]
    assert vals[0] is not None                  # 仅a → a自身分位
    assert vals[1] is not None


def test_regime_probs_sum_and_direction():
    """概率和=100且非负; 强上涨→牛概率高, 深跌→熊概率高"""
    up = [100 + i for i in range(200)]          # 稳定上涨120日动量>0
    dn = [300 - i for i in range(200)]
    ru = BH.regime([f"d{i}" for i in range(200)], up)
    rd = BH.regime([f"d{i}" for i in range(200)], dn)
    for b, g, e in zip(ru["bull"][120:], ru["range"][120:], ru["bear"][120:]):
        assert abs(b + g + e - 100) < 0.3       # 各日三态和=100(舍入容差)
    assert ru["bull"][-1] > 60 and rd["bear"][-1] > 60
    assert ru["bear"][-1] < 40


def test_backtest_no_lookahead_and_rules():
    """信号在当日收盘后才算得出 → 下一交易日收盘成交: 持仓从信号日的次日起计, 成交之后
    各日的收益归策略。(D1-38: 原"信号日收盘"即成交 — 吃到了拿不到的"信号日收盘→次日
    收盘"这一段, 单因子回测因此被大幅高估)"""
    comp_d = [f"2026-01-{i:02d}" for i in range(1, 9)]
    comp_v = [10, 50, 10, 10, 10, 10, 10, 10]   # 第2日≥entry, 其余回落
    bench = [(f"2026-01-{i:02d}", 100.0 + i) for i in range(1, 9)]  # 每日+1
    r = BH.backtest(comp_d, comp_v, bench, entry=30, exit_=-30)
    # d2收盘后出买入信号 → d3收盘成交 → d4起持仓; d3收盘信号10(滞回持有)…全程持有到末尾
    assert r["pos"] == [0, 0, 0, 1, 1, 1, 1, 1]
    # 策略收益 = d4..d8的5日复利(103→108)
    assert r["metrics"]["total_ret"] == round((108 / 103 - 1) * 100, 1)
    # 深跌卖出规则: d3收盘后-50 → d4收盘卖出; d4当天的收益仍归策略
    comp_v2 = [10, 50, -50, -50, 10, 10, 10, 10]
    r2 = BH.backtest(comp_d, comp_v2, bench, entry=30, exit_=-30)
    assert r2["pos"] == [0, 0, 0, 1, 0, 0, 0, 0]


# ── API + 策略CRUD + 页面 ──

def test_bt_page_and_factors_api(server_app):
    c = server_app["client"]
    assert c.get("/admin/backtest").status_code == 200
    d = c.get("/admin/api/backtest/factors").get_json()
    keys = {f["key"] for f in d["factors"]}
    assert {"sentiment", "pe_pct", "buffett", "margin", "fflow", "breadth",
            "bb_ratio", "nt_flow"} <= keys   # D1-31b: 牛熊比+国家队净申购
    assert d["defaults"]["entry"] == 30


def test_bt_compute_api(server_app, monkeypatch):
    """compute API: 因子权重/阈值透传; 引擎打桩避免连真库"""
    calls = {}

    def fake_payload(weights=None, entry=30, exit_=-30, path=None):
        calls.update(w=weights, e=entry, x=exit_)
        return {"dates": ["2026-01-01"], "composite": [5.0],
                "regime": {"dates": ["2026-01-01"], "closes": [3000.0],
                           "bull": [40.0], "range": [30.0], "bear": [30.0]},
                "overlay_index": {"dates": ["2026-01-01"], "norm": [100.0]},
                "backtest": {"dates": ["2026-01-01"], "equity": [1.0],
                             "bench_equity": [1.0], "pos": [0],
                             "metrics": {"total_ret": 0.0, "bench_ret": 0.0,
                                         "cagr": None, "bench_cagr": None,
                                         "max_dd": 0.0, "win_rate": None,
                                         "n_trades": 0, "sharpe": None, "n_days": 1},
                             "bench_name": "stub"},
                "factors_now": {}}
    import behavior as BH2
    monkeypatch.setattr(BH2, "payload", fake_payload)
    c = server_app["client"]
    r = c.post("/admin/api/backtest/compute", json={
        "factors": {"sentiment": -40}, "entry": 40, "exit": -20})
    assert r.status_code == 200 and r.get_json()["ok"] is True
    assert calls["w"] == {"sentiment": -40} and calls["e"] == 40 and calls["x"] == -20
    # 非法阈值拒
    assert c.post("/admin/api/backtest/compute",
                  json={"entry": "abc"}).status_code == 400


def test_bt_strategy_crud(server_app):
    c = server_app["client"]
    r = c.post("/admin/api/backtest/strategies", json={
        "name": "低估反转", "factors": {"pe_pct": -40, "sentiment": -30},
        "entry": 25, "exit": -25}).get_json()
    assert r["ok"] and r["id"]
    sid = r["id"]
    # 同名: 未明确要求覆盖 → 409; 带 overwrite 才覆盖(D1-38: 原直接覆盖)
    r_dup = c.post("/admin/api/backtest/strategies", json={
        "name": "低估反转", "factors": {"pe_pct": -50}, "entry": 20, "exit": -20})
    assert r_dup.status_code == 409 and r_dup.get_json()["exists"] is True
    assert c.get("/admin/api/backtest/strategies").get_json()["strategies"][0]["config"]["entry"] == 25
    r2 = c.post("/admin/api/backtest/strategies", json={
        "name": "低估反转", "factors": {"pe_pct": -50}, "entry": 20,
        "exit": -20, "overwrite": True}).get_json()
    assert r2["ok"] and r2["id"] == sid
    lst = c.get("/admin/api/backtest/strategies").get_json()["strategies"]
    s = next(x for x in lst if x["id"] == sid)
    assert s["name"] == "低估反转" and s["config"]["entry"] == 20
    # 空名拒
    assert c.post("/admin/api/backtest/strategies", json={"name": ""}).status_code == 400
    r3 = c.post(f"/admin/api/backtest/strategies/{sid}/delete").get_json()
    assert r3["ok"]
    assert c.post("/admin/api/backtest/strategies/99999/delete").status_code == 404


def test_bt_page_gates():
    html = (REPO / "server" / "templates" / "admin" / "backtest.html").read_text(encoding="utf-8")
    for mk in ("chartRegime", "chartComp", "chartBt", "chartNt", "facTable",
               "btnCompute", "btnSave", "stList", "data-load",
               "api/backtest/compute", "api/backtest/strategies", "叠加上证",
               "牛熊比", "国家队ETF动向", "累计净申购", "r.regime.bb", "r.nt",
               "data: r.overlay_index.norm,",
               "r.nt_overlay.norm[i]",
               "dataThrough", "btnRefresh", "visibilitychange", "data-fdir", "facShare",
               "pctWord", "顺势 +", "反向 −", "splitW", "bindFacInputs"):
        assert mk in html, f"缺: {mk}"
    import shutil, subprocess, tempfile, os, re
    blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
    main = max(blocks, key=len)
    fd, p = tempfile.mkstemp(suffix=".js"); os.close(fd)
    open(p, "w", encoding="utf-8").write(main)
    r = subprocess.run([shutil.which("node"), "-e",
                        'new Function(require("fs").readFileSync(process.argv[1],"utf8"))', p],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    os.unlink(p)
    assert r.returncode == 0, f"JS语法错误: {(r.stderr or '')[:300]}"


def test_bt_compute_and_save_validation(server_app):
    """review修复回归: 全不勾因子→400(曾静默回退默认权重);
    策略保存阈值非法/无因子→400"""
    c = server_app["client"]
    r = c.post("/admin/api/backtest/compute", json={"factors": {}})
    assert r.status_code == 400 and "至少勾选一个因子" in r.get_json()["msg"]
    assert c.post("/admin/api/backtest/strategies", json={
        "name": "坏阈值", "factors": {"pe_pct": -40},
        "entry": "abc", "exit": -20}).status_code == 400
    assert c.post("/admin/api/backtest/strategies", json={
        "name": "空因子", "factors": {}, "entry": 30,
        "exit": -30}).status_code == 400


def test_bb_series_direction():
    """牛熊比=牛-熊概率差(tanh): 强涨→正, 深跌→负, 前120日None"""
    up = [(f"d{i}", 100.0 + i) for i in range(150)]
    dn = [(f"d{i}", 300.0 - i) for i in range(150)]
    assert BH._bb_series(up)[-1][1] > 50
    assert BH._bb_series(dn)[-1][1] < -50
    assert BH._bb_series(up)[0][1] is None


def test_create_app_auto_creates_new_model_table(tmp_path, monkeypatch):
    """D1-31r回归: create_app启动幂等create_all — 生产app.db历史初始化且
    全仓无create_all调用点, behavior_strategies曾永不创建致策略列表500
    (测试fixture手动create_all掩盖了该路径)"""
    import os
    data = tmp_path / "data"; data.mkdir()
    cfg = tmp_path / "config"; cfg.mkdir()
    (cfg / "server.yaml").write_text(
        f"server: {{host: 127.0.0.1, port: 8599}}\n"
        f"app: {{theme_default: light}}\n"
        f"data: {{app_db: '{(data / 'app.db').as_posix()}', "
        f"secret_key_file: '{(data / 'secret.key').as_posix()}'}}\n"
        f"scheduler: {{enabled: false}}\n", encoding="utf-8")
    import server as SV
    monkeypatch.setattr(SV, "SERVER_CFG", cfg / "server.yaml")
    monkeypatch.setenv("SA_NO_JOBS", "1")
    app = SV.create_app()                     # 不手动create_all(生产等价)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.app_context():                   # 建管理员(users表也来自启动建表)
        from server.models import User
        u = User(username="bt_admin_t", level=9)
        u.set_password("x")
        SV.db.session.add(u)
        SV.db.session.commit()
        uid = u.id
    c = app.test_client()
    with c.session_transaction() as s:
        s["_user_id"] = str(uid)
    r = c.get("/admin/api/backtest/strategies")   # 曾500(no such table)
    assert r.status_code == 200 and r.get_json()["strategies"] == []


def test_nt_axis_extension_t1(tmp_path, monkeypatch):
    """D1-32: 份额源T+1 — nt轴延伸至最新交易日, 无数据日柱None/累计走平"""
    import behavior as BH
    monkeypatch.setattr(BH, "fetch_factors", lambda path=None: {
        "sentiment": [("2026-11-19", 10.0), ("2026-11-20", 11.0)]})
    monkeypatch.setattr(BH, "_index_close", lambda path=None: (
        [("2026-11-19", 3000.0), ("2026-11-20", 3050.0)], []))
    # nt_flow止于11-19 → 轴延伸到11-20
    monkeypatch.setattr(BH, "_fetch_factors_impl", lambda path=None: {})
    BH._fact_cache.update(at=0.0, data=None)     # 清缓存走stub
    orig_fetch = BH.fetch_factors
    def fetch_stub(path=None):
        d = orig_fetch(path)
        d["nt_flow"] = [("2026-11-19", 50.0)]
        d["bb_raw"] = []
        return d
    monkeypatch.setattr(BH, "fetch_factors", fetch_stub)
    r = BH.payload({"sentiment": 10}, path="whatever")
    assert r["nt"]["dates"] == ["2026-11-19", "2026-11-20"]   # 轴到最新交易日
    assert r["nt"]["ma5"] == [50.0, None]                      # T+1日无柱
    assert r["nt"]["cum"] == [50.0, 50.0]                      # 累计走平=持仓不变
    assert r["nt"]["data_date"] == "2026-11-19"                # 有真实数据的最后一天


def test_split_w_absent_factor_unchecked():
    """D1-32r回归: 加载只含部分因子的策略 — 未保存因子必须未勾选。
    曾 weights[key] ?? default_w 兜底, 未保存因子被渲染成勾选+默认权重
    (node实测复现: 策略仅含a, b却勾选权重25)"""
    import os
    import re
    import shutil
    import subprocess
    import tempfile
    html = (REPO / "server" / "templates" / "admin" / "backtest.html").read_text(encoding="utf-8")
    m = re.search(r"function splitW\(f, weights\) \{[\s\S]*?\n\}", html)
    assert m, "splitW函数缺失"
    if not shutil.which("node"):
        pytest.skip("node不可用")
    js = m.group(0) + (
        "\nconst out = ["
        "splitW({key:'a', default_w:-30}, {a:-40}),"
        "splitW({key:'b', default_w:25}, {a:-40}),"
        "splitW({key:'c', default_w:25}, {c:0}),"
        "splitW({key:'d', default_w:-10}, {d:15})];"
        "console.log(JSON.stringify(out.map(x => [x.checked, x.dir, x.mag])));")
    fd, p = tempfile.mkstemp(suffix=".js")
    os.close(fd)
    open(p, "w", encoding="utf-8").write(js)
    r = subprocess.run([shutil.which("node"), p], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    os.unlink(p)
    assert r.returncode == 0, r.stderr[:200]
    # a: 反向40勾选 / b: 缺省未勾选(显示默认) / c: 显式0未勾选 / d: 正值覆盖默认
    assert json.loads(r.stdout) == [[True, -1, 40], [False, 1, 25],
                                    [False, 1, 25], [True, 1, 15]]


# ── D1-34: 份额按需拉取(回测页末端空柱: 份额源D+1上午发布, 库只在16:35刷新) ──

def _pull_db(path):
    import sqlite3
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE etf_share(code TEXT, trade_date TEXT, fd_share REAL)")
    con.execute("CREATE TABLE index_daily(code TEXT, date TEXT, close REAL)")
    con.execute("INSERT INTO index_daily VALUES('1.000001','2026-11-23',3000)")
    con.execute("INSERT INTO etf_share VALUES('510050.SH','20261120',1)")
    con.commit()
    con.close()


def test_pull_latest_only_when_stale(tmp_path, monkeypatch):
    """份额落后于最新交易日 → 向源端拉一次并让因子缓存失效; 进程内节流
    (源端未发布时不会每开一次页面就打一轮接口); 追平后不再拉"""
    import sqlite3
    import fetch_etf_flow as EF
    db = tmp_path / "s.db"
    _pull_db(db)
    calls = []
    monkeypatch.setattr(EF, "refresh",
                        lambda path=None, lookback=30: calls.append(path) or 0)
    monkeypatch.setattr(BH, "_pull_state", {"at": 0.0})
    monkeypatch.setattr(BH, "_fact_cache", {"at": 123.0, "data": {"x": 1}})
    r = BH.pull_latest(db)
    assert calls == [db]                              # 源端尚未发布: 拉了但仍落后
    assert r == {"pulled": True, "stale": True, "rows": 0,
                 "advanced": False, "latest": "20261120"}
    assert BH._fact_cache["data"] is None             # 因子缓存失效
    assert BH.pull_latest(db)["pulled"] is False and len(calls) == 1     # 节流
    con = sqlite3.connect(str(db))                    # 源端发布后入库 → 追平
    con.execute("INSERT INTO etf_share VALUES('510050.SH','20261123',1)")
    con.commit()
    con.close()
    assert BH.pull_latest(db) == {"pulled": False, "stale": False, "rows": 0,
                                  "advanced": False, "latest": "20261123"}
    assert len(calls) == 1


def test_pull_latest_tolerates_failures(tmp_path, monkeypatch):
    """接口抛错/无token(-1)/库表缺失 → 沿用库内数据, 不向上抛"""
    import fetch_etf_flow as EF
    db = tmp_path / "s.db"
    _pull_db(db)
    monkeypatch.setattr(BH, "_pull_state", {"at": 0.0})
    monkeypatch.setattr(EF, "refresh", lambda path=None, lookback=30: -1)
    assert BH.pull_latest(db)["rows"] == 0
    monkeypatch.setattr(BH, "_pull_state", {"at": 0.0})

    def boom(path=None, lookback=30):
        raise RuntimeError("网络不通")
    monkeypatch.setattr(EF, "refresh", boom)
    assert BH.pull_latest(db) == {"pulled": True, "stale": True, "rows": 0,
                                  "advanced": False, "latest": "20261120"}
    assert BH.pull_latest(tmp_path / "none.db") == {          # 无库/无表
        "pulled": False, "stale": False, "rows": 0,
        "advanced": False, "latest": None}


def test_pull_latest_reports_advance(tmp_path, monkeypatch):
    """源端已发布 → 拉取后份额推进到新的一天(advanced)且不再滞后"""
    import sqlite3
    import fetch_etf_flow as EF
    db = tmp_path / "s.db"
    _pull_db(db)

    def refresh(path=None, lookback=30):
        con = sqlite3.connect(str(path))
        con.execute("INSERT INTO etf_share VALUES('510050.SH','20261123',2)")
        con.commit()
        con.close()
        return 160                                     # 整窗upsert的行数
    monkeypatch.setattr(EF, "refresh", refresh)
    monkeypatch.setattr(BH, "_pull_state", {"at": 0.0})
    assert BH.pull_latest(db) == {"pulled": True, "stale": False, "rows": 160,
                                  "advanced": True, "latest": "20261123"}


def test_bt_compute_pull_flag(server_app, monkeypatch):
    """pull=true: 先按需拉份额(结果随响应返回); 不带pull只读库(调权重/算回测)"""
    calls = []
    monkeypatch.setattr(BH, "pull_latest", lambda: calls.append(1) or {
        "pulled": True, "stale": True, "rows": 0, "latest": "20261120"})
    monkeypatch.setattr(BH, "payload", lambda w=None, e=30, x=-30, path=None: {
        "dates": ["2026-11-20"], "composite": [1.0],
        "nt": {"data_date": "2026-11-20"}})
    c = server_app["client"]
    r = c.post("/admin/api/backtest/compute",
               json={"factors": {"sentiment": -30}, "pull": True}).get_json()
    assert calls == [1] and r["nt_pull"]["latest"] == "20261120"
    assert r["nt"]["data_date"] == "2026-11-20"
    r2 = c.post("/admin/api/backtest/compute",
                json={"factors": {"sentiment": -30}}).get_json()
    assert calls == [1] and "nt_pull" not in r2


def test_bt_page_unpublished_is_labeled():
    """页面: 末端空柱显示"源端未发布"而非"-"; 标题注明数值属于哪天、哪些天
    在等; 首载/自动刷新/刷新数据带pull; [计算回测]不得直接绑compute
    (点击事件对象会被当成pull=true → 每次算回测都打一轮接口)"""
    html = (REPO / "server" / "templates" / "admin" / "backtest.html").read_text(encoding="utf-8")
    for mk in ("源端未发布", "待源端发布", "截至${ntDD}", "nt.data_date",
               "pull: !!pull", "formatter: NT_TIP", "await compute(true);",
               "r.nt_pull.advanced"):
        assert mk in html, f"缺: {mk}"
    assert "$('btnCompute').onclick = () => compute(false);" in html
    assert "$('btnCompute').onclick = compute;" not in html
