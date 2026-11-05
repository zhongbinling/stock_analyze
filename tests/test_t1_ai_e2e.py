# -*- coding: utf-8 -*-
"""测试T1: AI链真实E2E — 本地Ollama无mock调用 + 批次管道 + 云端批次(配置时)

E2E语义: 用生产配置(app.db的ai_local_*/ai_cloud_*)打真模型 —
Ollama未启用/不可达时整文件skip(不假绿); 云端未配置skip(0成本空转)。
锚定用例来自AI分析质量批的校准结论(特朗普/美联储→L2, 降准→L3)。
"""
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import ai_client  # noqa: E402
import analyze_news as AN  # noqa: E402


# ── 环境守卫: 读生产配置, 不可达即skip ──

def _ollama_ready() -> bool:
    """本地Ollama可达性(3s探针, 不耗AI算力)"""
    import requests
    cfg = AN.get_ai_config()
    if not (cfg["enabled"] and cfg["model"]):
        return False
    try:
        root = cfg["base_url"].rstrip("/").rsplit("/v1", 1)[0]
        r = requests.get(f"{root}/api/tags", timeout=3)
        return any(m.get("name") == cfg["model"]
                   for m in r.json().get("models", []))
    except Exception:                                         # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _ollama_ready(),
                                reason="本地Ollama未启用/不可达/模型缺失 — E2E跳过")


@pytest.fixture(scope="module")
def cfg():
    return AN.get_ai_config()


# ── ai_client 直连(无mock) ──

def test_analyze_news_structure(cfg):
    """真实调用: 返回四字段结构, level钳位1-5, direction合法"""
    r = ai_client.analyze_news(cfg["base_url"], cfg["model"], cfg["api_key"],
                               "某上市公司发布新一代办公软件产品", "产品发布会新闻稿")
    assert set(r) >= {"level", "type", "impact", "direction"}
    assert isinstance(r["level"], int) and 1 <= r["level"] <= 5
    assert r["direction"] in ("利多", "利空", "中性")
    assert "_parse_error" not in r          # 真模型必须产出可解析JSON


def test_anchor_fed_min_l2(cfg):
    """机构锚定(校准结论): 特朗普/美联储对A股影响 → 最低L2"""
    r = ai_client.analyze_news(cfg["base_url"], cfg["model"], cfg["api_key"],
                               "特朗普称美联储应立即降息", "美国总统施压美联储货币政策")
    assert r["level"] >= 2, f"美联储锚定失效: {r}"


def test_anchor_rrr_min_l3(cfg):
    """幅度区分(校准结论): 央行降准0.5个百分点 → L3(政策转向)"""
    r = ai_client.analyze_news(cfg["base_url"], cfg["model"], cfg["api_key"],
                               "央行宣布降准0.5个百分点，释放长期资金约1万亿元", "")
    assert r["level"] >= 3, f"降准幅度区分失效: {r}"


# ── 批次管道E2E: 临时库走完整链路(news_raw→news_local→macro_events) ──

def _seed_macro_db(db: Path):
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE news_raw(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      src TEXT NOT NULL, pub_time TEXT NOT NULL, title TEXT NOT NULL,
      url TEXT, content TEXT, fetched_at TEXT)""")
    AN.ensure_tables(con)
    con.execute("INSERT INTO news_raw(src,pub_time,title,content) VALUES"
                "('em_flash','2026-11-05 10:00:00','央行开展3000亿元MLF操作','中期借贷便利')")
    con.execute("INSERT INTO news_raw(src,pub_time,title,content) VALUES"
                "('em_flash','2026-11-05 11:00:00','某公司发布年度新品','产品新闻')")
    con.commit()
    con.close()


def test_analyze_batch_pipeline_e2e(cfg, tmp_path):
    """临时库全链路: 2条→AI分析→news_local done→macro_events upsert(source=ai_local:)"""
    if AN._is_paused():
        pytest.skip("AI已暂停(ai_pause=1) — 尊重运行态")
    db = tmp_path / "macro.db"
    _seed_macro_db(db)
    r = AN.analyze_batch(2, db_path=db, cfg=cfg)
    assert r["analyzed"] == 2 and r["failed"] == 0, r
    con = sqlite3.connect(str(db))
    rows = con.execute("""SELECT status, level, model FROM news_local
                          ORDER BY news_id""").fetchall()
    evs = con.execute("""SELECT title, level, source FROM macro_events
                         ORDER BY id""").fetchall()
    con.close()
    assert len(rows) == 2 and all(s == "done" for s, _, _ in rows)
    assert all(m == cfg["model"] for _, _, m in rows)
    assert len(evs) == 2
    assert all(s.startswith("ai_local:") for _, _, s in evs)
    # MLF操作应≥L2(机构锚定: 央行公开市场操作=L2)
    mlf = next(e for e in evs if "MLF" in e[0])
    assert mlf[1] >= 2, f"央行MLF锚定失效: {mlf}"


def test_cloud_batch_e2e(tmp_path):
    """云端批次E2E(真API): 临时库L3种子→云端二次分析→成品库ai_cloud:覆盖

    未配置/未启用云端 → skip(不产生API费用)"""
    ccfg = AN.get_cloud_config()
    if not (ccfg["enabled"] and ccfg["base_url"] and ccfg["api_key"]):
        pytest.skip("云端AI未配置/未启用 — E2E跳过")
    db = tmp_path / "macro.db"
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE news_raw(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      src TEXT NOT NULL, pub_time TEXT NOT NULL, title TEXT NOT NULL,
      url TEXT, content TEXT, fetched_at TEXT)""")
    AN.ensure_tables(con)
    con.execute("INSERT INTO news_raw(src,pub_time,title) VALUES"
                "('major_news','2026-11-05 09:30:00','央行降准0.5个百分点')")
    con.execute("""INSERT INTO news_local
      (news_id,status,level,type,impact,direction) VALUES(1,'done',3,'货币','降准','利多')""")
    con.commit()
    con.close()
    r = AN.analyze_cloud_batch(2, cfg=ccfg, db_path=db)
    assert r["analyzed"] >= 1, r
    con = sqlite3.connect(str(db))
    st, model = con.execute("SELECT status, model FROM news_cloud WHERE news_id=1").fetchone()
    src = con.execute("SELECT source FROM macro_events WHERE title LIKE '%降准%'").fetchone()[0]
    con.close()
    assert st == "done" and model == ccfg["model"]
    assert src.startswith("ai_cloud:")
