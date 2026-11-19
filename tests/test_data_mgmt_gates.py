# -*- coding: utf-8 -*-
"""数据管理逻辑一致性门禁: 修复页BF_ROWS ↔ BACKFILLS注册表 ↔ _REPAIR_DS映射
三份清单不允许漂移(etf_flow曾漂移过一次: 注册表有而页面不可见);
任务队列同键去重; 备份块UI与raw_daily策略提示"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

HTML = (REPO / "server" / "templates" / "admin" / "data.html").read_text(encoding="utf-8")

# 各自专属管理卡维护、不进数据修复表的数据集
MANAGED_ELSEWHERE = {"stock_daily", "stock_hist", "news_fetch"}


def test_bf_rows_cover_backfills_registry():
    """注册表(BACKFILLS)新增数据集必须出现在修复页BF_ROWS — 否则永远不可见"""
    import fetch_daily as FD
    from server.market import _REPAIR_DS
    ds_in_page = set(re.findall(r"ds: '(\w+)'", HTML))
    missing = [d for d in FD.BACKFILLS
               if d not in ds_in_page and d not in MANAGED_ELSEWHERE]
    assert not missing, f"注册表有而修复页缺(需补BF_ROWS行): {missing}"
    unknown = [d for d in ds_in_page if d not in FD.BACKFILLS]
    assert not unknown, f"修复页有而注册表缺(死按钮): {unknown}"
    # 页面asset字段必须经_REPAIR_DS有映射(完成度列取数依赖)
    assets_in_page = set(re.findall(r"asset: '(\w+)'", HTML))
    unmapped = [a for a in assets_in_page if a not in _REPAIR_DS]
    assert not unmapped, f"页面asset无修复通道映射: {unmapped}"
    # 反向: _REPAIR_DS的资产若被页面引用, 其数据集必须真实存在
    for asset, ds in _REPAIR_DS.items():
        if asset in assets_in_page:
            assert ds in FD.BACKFILLS, f"{asset}→{ds} 映射指向未注册数据集"


def test_repair_ds_injected_single_source():
    """SCAN_DS服务端单点注入(P4①): 页面消费repair_ds_json而非自带镜像"""
    assert "repair_ds_json" in (REPO / "server" / "routes_admin.py").read_text(encoding="utf-8")
    assert "SCAN_DS = {{ repair_ds_json | safe }}" in HTML


def test_enqueue_dedup_same_kind_target(server_app):
    """同(kind,target)在queued/running态重复入队被拒; 完成态放行重跑"""
    from server import jobs as J
    J._queue_log.clear()
    while not J._q.empty():
        J._q.get_nowait()
    ok, _ = J.enqueue("backfill", lambda append: 0, target="etf_flow",
                      trigger="manual")
    assert ok
    ok2, msg2 = J.enqueue("backfill", lambda append: 0, target="etf_flow",
                          trigger="scheduled")          # 定时叠加
    assert not ok2 and "排队" in msg2
    # running态同样拒绝
    for e in J._queue_log:
        if e["target"] == "etf_flow":
            e["state"] = "running"
    ok3, msg3 = J.enqueue("backfill", lambda append: 0, target="etf_flow")
    assert not ok3 and "运行" in msg3
    # done态放行(失败重跑/定时重入)
    for e in J._queue_log:
        if e["target"] == "etf_flow":
            e["state"] = "done"
    ok4, _ = J.enqueue("backfill", lambda append: 0, target="etf_flow")
    assert ok4
    # 不同target不受影响
    ok5, _ = J.enqueue("backfill", lambda append: 0, target="zt_ts")
    assert ok5
    while not J._q.empty():
        J._q.get_nowait()


def test_backup_block_ui_hints_raw_policy():
    """基础块勾选框提示raw_daily资产级策略(日备自动跳过) — UI承诺与策略对齐"""
    assert "仅周备/手动" in HTML
    from backup import RAW_POLICY
    assert "raw_daily" in RAW_POLICY
    assert "daily" not in RAW_POLICY["raw_daily"]["types"]  # 策略确实排除日备


def test_backfill_datasets_single_source():
    """jobs侧dataset清单/默认起始日派生自BACKFILLS(第四/五份清单根治):
    键集一致 + etf_flow在列(曾因手维护漂移致修复入口400) + 懒加载可访问"""
    import fetch_daily as FD
    from server import jobs as J
    assert set(J.BACKFILL_DATASETS) == set(FD.BACKFILLS)
    assert set(J.BACKFILL_DEFAULT_START) == set(FD.BACKFILLS)
    assert "etf_flow" in J.BACKFILL_DATASETS
    assert J.BACKFILL_DEFAULT_START["etf_flow"] == FD.BACKFILLS["etf_flow"][3]
    # 派生值真源一致(标签/起始日)
    assert J.BACKFILL_DATASETS["zt_est"] == FD.BACKFILLS["zt_est"][0]


def test_repair_api_accepts_registered_dataset(server_app):
    """端到端: 修复入口POST注册表内数据集不再400(etf_flow实测回归)"""
    c = server_app["client"]
    r = c.post("/admin/api/backfill/run", json={"dataset": "etf_flow"})
    assert r.status_code == 200
    from server import jobs as J
    item = J._q.get_nowait()
    assert item["kind"] == "backfill" and "净流入" in item["target"]  # target=中文标签
    J._queue_log.clear()


def test_execute_entry_finalized_on_crash(server_app, monkeypatch):
    """D1-30r回归: DB故障贯穿任务执行(Job建行后commit全炸) —
    登记簿条目仍必终结且异常不外泄杀worker; 去重不死锁"""
    from server import db as sdb
    from server import jobs as J
    J._queue_log.clear()
    while not J._q.empty():
        J._q.get_nowait()
    ok, _ = J.enqueue("boom", lambda a: 0, target="t")
    item = J._q.get_nowait()
    # 首次commit(Job建行)成功, 之后全部炸(append/收尾全被击穿)
    n = {"c": 0}
    real = sdb.session.commit

    def flaky():
        n["c"] += 1
        if n["c"] >= 2:
            raise RuntimeError("db down")
        real()
    monkeypatch.setattr(sdb.session, "commit", flaky)
    with server_app["app"].app_context():
        J._execute(item)          # 不抛(收尾失败只打日志)
    ent = next(e for e in J._queue_log if e["kind"] == "boom")
    assert ent["state"] == "done"               # finally终结, 去重不死锁
    assert n["c"] >= 3                          # 故障确实贯穿了执行
    ok2, _ = J.enqueue("boom", lambda a: 0, target="t")   # 同键可再入队
    assert ok2
    J._queue_log.clear()
    while not J._q.empty():
        J._q.get_nowait()
