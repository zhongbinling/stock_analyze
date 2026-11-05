# -*- coding: utf-8 -*-
"""D1-28新闻回补: 限频语义(40次/天实测) + run_rounds自适应退避驱动"""
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
    return {"db": db, "mp": monkeypatch}


# ── run() 返回语义: "capped"(真值) / True ──

def test_run_returns_capped(env, monkeypatch):
    """限频中断 → "capped"(真值: fetch_daily backfill协议把False当自报失败)"""
    monkeypatch.setattr(FN.T, "has_token", lambda: True)
    monkeypatch.setattr(FN, "fetch_day", lambda d, path=None: -2)
    monkeypatch.setattr(FN, "refresh_em", lambda path=None: 0)
    monkeypatch.setattr(FN, "_summary", lambda path=None: None)
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = FN.run("20260101", "20260103", env["db"])
    assert r == "capped"


def test_run_returns_true_on_complete(env, monkeypatch):
    """区间走完无限频 → True"""
    monkeypatch.setattr(FN.T, "has_token", lambda: True)
    monkeypatch.setattr(FN, "fetch_day", lambda d, path=None: 0)
    monkeypatch.setattr(FN, "refresh_em", lambda path=None: 0)
    monkeypatch.setattr(FN, "_summary", lambda path=None: None)
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = FN.run("20260101", "20260103", env["db"])
    assert r is True


# ── run_rounds() 自适应驱动 ──

def test_rounds_progress_then_complete(monkeypatch):
    """有进展轮重置退避立即续传; 完成轮返回True(不sleep)"""
    seq = [{"a"}, {"a", "b", "c"}, {"a", "b", "c"}]
    monkeypatch.setattr(FN, "days_done",
                        lambda src, path=None: seq.pop(0) if seq else seq)
    runs = iter(["capped", True])
    monkeypatch.setattr(FN, "run", lambda *a, **kw: next(runs))
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    assert FN.run_rounds(60, "20260101", "20260103") is True
    assert sleeps == []                        # 进展轮不退避


def test_rounds_bails_after_empty_rounds(monkeypatch):
    """连续零进展轮 → 退避后仍无恢复则退出(防无限空转)"""
    monkeypatch.setattr(FN, "days_done", lambda src, path=None: {"a"})
    monkeypatch.setattr(FN, "run", lambda *a, **kw: "capped")
    monkeypatch.setattr(FN, "MAX_EMPTY_ROUNDS", 2)
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    assert FN.run_rounds(60, "20260101", "20260103") is False
    assert sleeps == [5 * 60]                  # 第1空轮退避5min, 第2空轮即退出


def test_rounds_backoff_doubles(monkeypatch):
    """退避指数增长: 5→10→20min封顶前逐档翻倍"""
    monkeypatch.setattr(FN, "days_done", lambda src, path=None: {"a"})
    monkeypatch.setattr(FN, "run", lambda *a, **kw: "capped")
    monkeypatch.setattr(FN, "MAX_EMPTY_ROUNDS", 3)
    sleeps = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))
    FN.run_rounds(60, "20260101", "20260103")
    assert sleeps == [300, 600]                # 空1→5min, 空2→10min, 空3→退出


def test_rounds_deadline_exits(monkeypatch):
    """截止时间到 → 退出返回False(断点已保存语义)"""
    monkeypatch.setattr(FN, "days_done", lambda src, path=None: {"a"})
    monkeypatch.setattr(FN, "run", lambda *a, **kw: "capped")
    monkeypatch.setattr(FN, "MAX_EMPTY_ROUNDS", 99)   # 永不空轮退出 → 靠截止
    # deadline=0 → 第一轮前即到期
    assert FN.run_rounds(0, "20260101", "20260103") is False


# ── fetch_day 限频原文透传 ──

def test_fetch_day_prints_api_limit_text(env, monkeypatch, capsys):
    """限频打印API原始限额文本(40次/天), 不再硬编码30次/小时"""
    def boom(api, fields="", **kw):
        raise RuntimeError("tushare major_news: [40203] 抱歉，您访问接口"
                           "(major_news)频率超限(40次/天)，具体频次详情: x")
    monkeypatch.setattr(FN.T, "call", boom)
    r = FN.fetch_day("20260101", env["db"])
    out = capsys.readouterr().out
    assert r == -2
    assert "40次/天" in out and "30次/小时" not in out


# ── CLI/注册 wiring ──

def test_cli_wiring():
    src = (CORE / "fetch_news.py").read_text(encoding="utf-8")
    assert "--deadline-min" in src and "run_rounds" in src
    # fetch_daily backfill协议: news_fetch执行lambda调run() — 返回值真值语义
    # (限频"capped"非False)已由test_run_returns_capped锁定
    import fetch_daily as FD
    assert "news_fetch" in FD.BACKFILLS
