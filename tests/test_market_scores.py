# -*- coding: utf-8 -*-
"""D2-2 评分数据状态测试: score_datasets 四类条目/健康三态/分量覆盖 + API scores 字段"""
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server import market  # noqa: E402


@pytest.fixture()
def mkt(fixture_db, tmp_path, monkeypatch):
    """每测一份夹具库副本(本文件用例会删数据验证健康态, 不污染共享夹具)"""
    p = tmp_path / "mkt.db"
    shutil.copy2(fixture_db, p)
    monkeypatch.setattr(market, "SENT_DB", p)
    monkeypatch.setattr(market, "_cfg_cache", None)
    return market


def test_score_datasets_four_types(mkt):
    ds = mkt.score_datasets()
    codes = [d["code"] for d in ds]
    assert codes == ["score:sentiment", "score:bk", "score:sw1", "score:sw2"]
    by = {d["code"]: d for d in ds}
    # 夹具: sentiment 40行~09-30; BK 3板×30日(1 NULL仍计行); SW1/SW2 各1×30
    assert by["score:sentiment"]["rows"] == 40 and by["score:sentiment"]["last"] == "2026-09-30"
    assert by["score:bk"]["codes"] == 3 and by["score:bk"]["rows"] == 90
    assert by["score:sw1"]["codes"] == 1 and by["score:sw2"]["codes"] == 1
    assert by["score:bk"]["first"] == "2026-09-01" and by["score:bk"]["last"] == "2026-09-30"
    assert by["score:bk"]["days"] == 30


def test_health_ok_when_caught_up(mkt):
    """夹具中评分与 index_daily 同步到 2026-09-30 → 全部 ok"""
    for d in mkt.score_datasets():
        assert d["health"] == "ok", f"{d['code']} 应为 ok"


def test_health_warn_when_behind(mkt):
    """删掉 sentiment 尾部 → 落后行情 → warn, 说明含落后天数"""
    import sqlite3
    conn = sqlite3.connect(str(mkt.SENT_DB))
    conn.execute("DELETE FROM sentiment WHERE date > '2026-09-25'")
    conn.commit()
    conn.close()
    s = next(d for d in mkt.score_datasets() if d["code"] == "score:sentiment")
    assert s["health"] == "warn"
    assert "落后行情" in s["note"]


def test_health_bad_when_empty(mkt):
    import sqlite3
    conn = sqlite3.connect(str(mkt.SENT_DB))
    conn.execute("DELETE FROM sector_scores WHERE code LIKE 'SW1_%'")
    conn.commit()
    conn.close()
    s = next(d for d in mkt.score_datasets() if d["code"] == "score:sw1")
    assert s["health"] == "bad" and s["rows"] == 0


def test_bk_components_coverage_note(mkt):
    s = next(d for d in mkt.score_datasets() if d["code"] == "score:bk")
    assert "分量明细覆盖至 2026-09-30" in s["note"]      # 夹具近5日有分量
    # SW 行无分量 → 说明不含分量覆盖
    w = next(d for d in mkt.score_datasets() if d["code"] == "score:sw1")
    assert "分量明细" not in w["note"]


def test_datasets_no_longer_lists_score_rows(mkt):
    """datasets() 已移除 sentiment/sector_scores 行(评分归评分卡)"""
    codes = [d["code"] for d in mkt.datasets()]
    assert "sentiment" not in codes
    # 夹具有 sector_scores 数据 → 不再输出聚合行
    assert "sector_scores" not in codes


def test_api_datasets_includes_scores(server_app, mkt):
    r = server_app["client"].get("/admin/api/datasets")
    assert r.status_code == 200
    j = r.get_json()
    assert [s["code"] for s in j["scores"]] == \
           ["score:sentiment", "score:bk", "score:sw1", "score:sw2"]
    assert j["datasets"]                                  # 基础数据仍在
