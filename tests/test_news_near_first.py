# -*- coding: utf-8 -*-
"""D1-28新闻回补推进: 近端优先遍历(修双头推进中间空洞) + 驱动重排"""
import datetime as dt
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


def test_missing_dates_near_first():
    """遍历序=近端优先: 未覆盖日按离今天近→远排序(修中间空洞)"""
    done = {"2018-06-01", "2026-11-09"}       # 两头各有一点
    d1, d2 = dt.date(2018, 6, 1), dt.date(2018, 6, 3)
    out = FN._missing_dates(d1, d2, {"2018-06-02"})
    assert out == [dt.date(2018, 6, 3), dt.date(2018, 6, 1)]   # 近在前
    # 空: 全覆盖
    assert FN._missing_dates(dt.date(2026, 1, 1), dt.date(2026, 1, 2),
                             {"2026-01-01", "2026-01-02"}) == []


def test_run_near_first_order(env, monkeypatch):
    """run()实际拉取顺序=近端优先(2026-08缺口先于2018深史)"""
    import sqlite3
    FN.ensure_db(env)
    # 已覆盖: 2018-06-01(深史前沿) + 近3日; 中间全空
    con = sqlite3.connect(str(env))
    con.execute("INSERT INTO news_raw(src,pub_time,title) VALUES"
                "('major_news','2018-06-01 09:00','d')")
    con.commit(); con.close()
    calls = []
    monkeypatch.setattr(FN.T, "has_token", lambda: True)
    monkeypatch.setattr("time.sleep", lambda s: None)

    def fake_fetch(d, path=None, with_content=False, lookback=None):
        calls.append(d)
        return 0
    monkeypatch.setattr(FN, "fetch_day", fake_fetch)
    monkeypatch.setattr(FN, "refresh_em", lambda path=None: 0)
    monkeypatch.setattr(FN, "_summary", lambda path=None: None)
    import datetime as dt
    today = dt.date.today().strftime("%Y%m%d")
    FN.run("20180601", today, env, tail=False)
    assert calls, "应有拉取"
    # 近端优先: 第一个拉的不是2018-06-02, 而是最近的未覆盖日
    first = dt.datetime.strptime(calls[0], "%Y%m%d").date()
    assert first.year >= 2026, f"近端优先失效: 首拉{first}"


def test_run_idempotent_capped(env, monkeypatch):
    """幂等+限频: capped返回真值; 全覆盖返回True"""
    import sqlite3
    FN.ensure_db(env)
    monkeypatch.setattr(FN.T, "has_token", lambda: True)
    monkeypatch.setattr("time.sleep", lambda s: None)
    # 全覆盖(单日区间已入库) → True零拉取
    con = sqlite3.connect(str(env))
    con.execute("INSERT INTO news_raw(src,pub_time,title) VALUES"
                "('major_news','2026-01-05 09:00','x')")
    con.commit(); con.close()
    calls = []
    monkeypatch.setattr(FN, "fetch_day",
                        lambda d, path=None, **kw: calls.append(d) or 0)
    assert FN.run("20260105", "20260105", env, tail=False) is True
    assert calls == []
    # 限频 → "capped"
    monkeypatch.setattr(FN, "fetch_day",
                        lambda d, path=None, **kw: calls.append(d) or -2)
    r = FN.run("20260106", "20260110", env, tail=False)
    assert r == "capped" and len(calls) == 1


def test_driver_cron_prompt_near_first():
    """run_rounds驱动的run继承近端优先(无需改 — run内部已重排)"""
    import inspect
    src = inspect.getsource(FN.run_rounds)
    assert "run(start, end, path, tail=False)" in src   # 调用面未变
    src_run = inspect.getsource(FN.run)
    assert "_missing_dates" in src_run


def test_refresh_backfill_false_skips_progressive(env, monkeypatch):
    """修复链backfill=False: 只拉近3日, 不做渐进回补(省配额给修复主链)"""
    calls = []
    monkeypatch.setattr(FN, "fetch_day",
                        lambda d, path=None, with_content=False, lookback=None:
                        calls.append(d) or 0)
    monkeypatch.setattr(FN, "refresh_em", lambda path=None: 0)
    monkeypatch.setattr(FN, "refresh_sina", lambda path=None: 0)
    monkeypatch.setattr(FN, "refresh_ths", lambda path=None: 0)
    monkeypatch.setattr(FN, "_summary", lambda path=None: None)
    import datetime as dt2
    monkeypatch.setattr("time.sleep", lambda s: None)
    FN.refresh(env, backfill=False)
    # 仅近3日(今天+昨天+前天), 无回补probe
    assert len(calls) == 3, f"backfill=False应只拉3日, 实际{len(calls)}: {calls}"
    # backfill=True走渐进回补(有额外probe日)
    calls.clear()
    FN.refresh(env, backfill=True)
    assert len(calls) > 3, "backfill=True应含渐进回补日"
