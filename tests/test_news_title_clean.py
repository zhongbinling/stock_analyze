# -*- coding: utf-8 -*-
"""新闻标题清洗: 【主题】提取/句边界截断/入库单点接入/存量两侧同步"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import fetch_news as FN  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "macro.db"
    monkeypatch.setattr(FN, "MACRO_DB", db)
    return db


# ── _clean_title 单元 ──

def test_clean_title_bracket_theme():
    """【主题】+全文 → 取主题(用户实测样本形态)"""
    raw = ("【民主党夺下参议院 特朗普重塑美联储之路再添屏障】随着民主党赢得参议院控制权，"
           "唐纳德·特朗普试图重塑美联储、让利率政策顺从其意愿的行动遭遇了一道强有力的屏障。"
           "参议院控制权易手是一次重大的政治转变。")
    t = FN._clean_title(raw)
    assert t == "民主党夺下参议院 特朗普重塑美联储之路再添屏障"
    assert len(t) <= 60


def test_clean_title_sentence_boundary():
    """无【】: 超长→句边界截断(优先句号); ≤60原样; 短【】(如'【快讯】')剥前缀用正文"""
    long_sina = "央行今日开展3000亿元中期借贷便利操作，中标利率维持不变。" \
                "业内认为这体现了货币政策稳健中性的取向，市场流动性保持合理充裕，后续仍需关注跨年资金面变化情况。"
    t = FN._clean_title(long_sina)
    assert len(t) <= 35 and t.endswith("维持不变")   # 句号边界
    assert FN._clean_title("央行降准0.5个百分点") == "央行降准0.5个百分点"   # 短标题原样
    assert FN._clean_title("【快讯】今日市场表现平稳各大指数涨跌互现") \
        == "今日市场表现平稳各大指数涨跌互现"        # 短前缀剥离


def test_clean_title_whitespace_and_empty():
    assert FN._clean_title("  多余   空白  ") == "多余 空白"
    assert FN._clean_title("") == ""
    assert FN._clean_title(None) == ""


# ── 入库单点接入 ──

def test_dedup_insert_cleans_title(env):
    """_dedup_insert统一清洗: em快讯全文标题→主题入库"""
    con = sqlite3.connect(str(env))
    FN.ensure_db(env)
    now = "2026-11-06 10:00:00"
    FN._dedup_insert(con, FN.SRC_EM, "2026-11-06 09:00:00",
                     "【主题甲乙丙丁戊己庚辛壬癸】正文内容很长很长很长很长", None, "正文全文", now)
    con.commit()
    t = con.execute("SELECT title FROM news_raw").fetchone()[0]
    con.close()
    assert t == "主题甲乙丙丁戊己庚辛壬癸"


def test_refresh_em_cleans(env, monkeypatch):
    """em快照链路: akshare返回的全文标题被清洗, content保留全文"""
    import pandas as pd
    full = ("【某部委发布重要政策文件支持行业发展】今日某部委正式印发文件，"
            "提出多项措施，全文如下……" * 3)
    class FakeAK:
        @staticmethod
        def stock_info_global_em():
            return pd.DataFrame([
                {"标题": full, "发布时间": "2026-11-06 10:00", "链接": "", "摘要": full}])
    monkeypatch.setitem(sys.modules, "akshare", FakeAK)
    FN.refresh_em(env)
    con = sqlite3.connect(str(env))
    t, c = con.execute("SELECT title, content FROM news_raw").fetchone()
    con.close()
    assert t == "某部委发布重要政策文件支持行业发展"
    assert len(c) > len(t)                          # 全文在content, 标题解耦


# ── 存量清洗 ──

def test_clean_existing_both_sides(env):
    """存量: news_raw与macro_events两侧同步清洗(attach标题匹配不断链)"""
    con = sqlite3.connect(str(env))
    FN.ensure_db(env)
    long_t = ("【存量测试主题标题超长样本】这里是全文正文内容应该被清洗掉的部分"
              "还有很多很多很多很多的字用于超过六十字阈值" * 2)
    con.execute("INSERT INTO news_raw(src,pub_time,title,content) VALUES(?,?,?,?)",
                (FN.SRC_EM, "2026-11-05 09:00", long_t, "全文"))
    con.execute("""CREATE TABLE macro_events(id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT, level INTEGER, content TEXT, source TEXT, date TEXT, type TEXT,
      created_at TEXT, updated_at TEXT)""")
    con.execute("INSERT INTO macro_events(title,level,date) VALUES(?,?,?)",
                (long_t, 2, "2026-11-05"))
    con.execute("INSERT INTO news_raw(src,pub_time,title) VALUES(?,?,?)",
                (FN.SRC_EM, "2026-11-05 10:00", "正常短标题保持不变"))
    con.commit()
    con.close()
    r = FN.clean_existing(env)
    assert r["news_raw"] == 1 and r["macro_events"] == 1
    con = sqlite3.connect(str(env))
    t1 = con.execute("SELECT title FROM news_raw WHERE pub_time LIKE '2026-11-05 09%'").fetchone()[0]
    t2 = con.execute("SELECT title FROM macro_events").fetchone()[0]
    keep = con.execute("SELECT title FROM news_raw WHERE pub_time LIKE '2026-11-05 10%'").fetchone()[0]
    con.close()
    assert t1 == t2 == "存量测试主题标题超长样本"    # 两侧同步(attach不断链)
    assert keep == "正常短标题保持不变"


def test_clean_existing_idempotent(env):
    con = sqlite3.connect(str(env))
    FN.ensure_db(env)
    con.execute("INSERT INTO news_raw(src,pub_time,title) VALUES(?,?,?)",
                (FN.SRC_EM, "2026-11-05 09:00", "【幂等测试主题样本】正文" * 20))
    con.commit()
    con.close()
    FN.clean_existing(env)
    r2 = FN.clean_existing(env)                     # 二跑无>60标题 → 0
    assert r2["news_raw"] == 0
