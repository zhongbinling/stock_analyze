# -*- coding: utf-8 -*-
"""B3: 个股数据基础数据化 — BACKFILLS注册/data.html管理/jobs调度/AI不自动"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "core"))
sys.path.insert(0, str(REPO / "server"))


def test_backfills_registered():
    """stock_daily/stock_hist注册到BACKFILLS + jobs + DEFAULT_START"""
    import core.fetch_daily as FD
    for k in ("stock_daily", "stock_hist"):
        assert k in FD.BACKFILLS, f"BACKFILLS缺{k}"
        label, targets, fn, start = FD.BACKFILLS[k]
        assert "个股数据" in label
        assert targets == []              # stock.db独立库不走缺口检测
        assert callable(fn)
        assert start >= "20050101"
    from server import jobs as J
    for k in ("stock_daily", "stock_hist"):
        assert k in J.BACKFILL_DATASETS
        assert k in J.BACKFILL_DEFAULT_START


def test_stock_backfill_no_ai():
    """B3核心: 日增/全量补全不调AI(AI仅页面点击触发)"""
    import inspect
    import fetch_stock as FS
    src_daily = inspect.getsource(FS.refresh_daily)
    src_nightly = inspect.getsource(FS.run_nightly)
    src_full = inspect.getsource(FS.full_refresh)
    src_backfill = inspect.getsource(FS.backfill_hist) if hasattr(FS, "backfill_hist") else ""
    for name, src in [("refresh_daily", src_daily), ("run_nightly", src_nightly),
                      ("full_refresh", src_full)]:
        assert "stock_risk" not in src and "ai_client" not in src and \
            "scan_stock" not in src, f"{name}不应调AI"
    # run_nightly和full_refresh仅调refresh_*系列(FS属性查找, 非AI模块)


def test_stock_backfill_api(server_app):
    """POST backfill(stock_daily)正常入队"""
    from server import jobs
    c = server_app["client"]
    r = c.post("/admin/api/backfill/run", json={"dataset": "stock_daily"})
    assert r.status_code == 200
    item = jobs._q.get_nowait()
    assert item["kind"] == "backfill"
    assert "个股" in (item["target"] or "")


def test_data_html_has_stock_dataset():
    """data.html数据集表显示个股数据行(前端由BACKFILLS驱动, 源级验证)"""
    import core.fetch_daily as FD
    # data.html的数据集表由API从BACKFILLS渲染, 后端注册即显示
    assert "stock_daily" in FD.BACKFILLS
    assert "stock_hist" in FD.BACKFILLS


def test_ai_only_manual():
    """AI解析仅通过页面按钮(POST /api/stock/scan)触发, 不在任何自动链"""
    import inspect
    from server import jobs as J
    src = inspect.getsource(J.task_stock_daily)
    src2 = inspect.getsource(J.task_stock_full)
    assert "scan_stock" not in src and "stock_risk" not in src, "日增不应调AI"
    assert "scan_stock" not in src2 and "stock_risk" not in src2, "全量不应调AI"
