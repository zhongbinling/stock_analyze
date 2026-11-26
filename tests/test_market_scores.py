# -*- coding: utf-8 -*-
"""D2-2 评分数据状态测试: score_datasets 四类条目/健康三态/分量覆盖 + API scores 字段"""
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from server import market  # noqa: E402

# MI-2: 指数情绪分(深证成指 / 创业板指 / 科创50), 插在大盘情绪分之后、板块温度之前
IDX = ["score:idx_399001", "score:idx_399006", "score:idx_000688"]
CODES = ["score:sentiment"] + IDX + ["score:bk", "score:sw1", "score:sw2", "score:sw3"]


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
    assert codes == CODES
    by = {d["code"]: d for d in ds}
    # 夹具: sentiment 40行~09-30; BK 3板×30日(1 NULL仍计行); SW1/SW2 各1×30
    assert by["score:sentiment"]["rows"] == 40 and by["score:sentiment"]["last"] == "2026-09-30"
    assert by["score:sentiment"]["codes"] is None          # 大盘无"板块数"语义
    assert by["score:bk"]["codes"] == 3 and by["score:bk"]["rows"] == 90
    assert by["score:sw1"]["codes"] == 1 and by["score:sw2"]["codes"] == 1
    assert by["score:sw3"]["codes"] == 0 and by["score:sw3"]["health"] == "bad"  # 夹具无SW3行
    assert by["score:bk"]["first"] == "2026-09-01" and by["score:bk"]["last"] == "2026-09-30"
    assert by["score:bk"]["days"] == 30
    # MI-2: 指数情绪分三行 — 夹具库没有这三个指数的日线与评分表, 也要逐行给出、说明指向日线补全
    assert [by[c]["name"] for c in IDX] == ["指数情绪分·深证成指", "指数情绪分·创业板指", "指数情绪分·科创50"]
    for c in IDX:
        assert by[c]["source"] == "score_index.py" and by[c]["rows"] == 0 and by[c]["health"] == "bad"
        assert "无该指数日线" in by[c]["note"]


def test_health_ok_when_caught_up(mkt):
    """夹具中评分与 index_daily 同步到 2026-09-30 → 全部 ok, 说明可读(非'='标记)"""
    by = {d["code"]: d for d in mkt.score_datasets()}
    for c, d in by.items():
        if c == "score:sw3" or c in IDX:              # 夹具无SW3行 / 无三个新指数 → bad属预期
            continue
        assert d["health"] == "ok", f"{c} 应为 ok"
        assert "与行情同步" in d["note"], f"{c} ok态说明应含'与行情同步': {d['note']!r}"
    assert "=" not in by["score:sw1"]["note"].replace("==", "")  # 无孤立'='标记


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


def test_health_fallback_when_no_index_base(mkt):
    """行情基准缺失(index_daily空) → 回落自然日陈旧度: 夹具评分停在2026-09-30, 距今>4天 → warn"""
    import sqlite3
    conn = sqlite3.connect(str(mkt.SENT_DB))
    conn.execute("DELETE FROM index_daily")
    conn.commit()
    conn.close()
    assert mkt.latest_index_date() is None
    s = next(d for d in mkt.score_datasets() if d["code"] == "score:sentiment")
    assert s["health"] == "warn" and "落后行情" in s["note"]


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
    assert [s["code"] for s in j["scores"]] == CODES
    assert j["datasets"]                                  # 基础数据仍在
