# -*- coding: utf-8 -*-
"""report.py 输出层: fmt数值格式/情绪日报文本/历史曲线图(matplotlib Agg冒烟)"""
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import report as RP  # noqa: E402


# ── fmt 数值格式 ──

def test_fmt():
    assert RP.fmt(None) == "--"
    assert RP.fmt(float("nan")) == "--"
    assert RP.fmt(72.0) == "72"                 # 整数float去小数
    assert RP.fmt(72.5) == "72.50"
    assert RP.fmt(2.35, plus=True) == "+2.35"   # plus带符号
    assert RP.fmt(3681.47) == "3681.47"
    assert RP.fmt(45) == "45"                   # int直str


# ── 情绪日报 ──

@pytest.fixture()
def frames():
    """3日小框架: df(原始指标)+comp(分量)+score_df(评分)"""
    dates = pd.to_datetime(["2026-11-12", "2026-11-13", "2026-11-16"])
    df = pd.DataFrame({
        "date": dates, "zt_count": [45, 60, 72], "dt_count": [3, 2, 1],
        "zb_rate": [30.0, 25.0, 20.0], "lb_max": [5, 7, 9], "jl_rate": [18.0, 22.0, 30.0],
        "close": [3600.0, 3650.5, 3681.47], "chg": [0.5, 1.4, 0.85],
        "vol_total": [7.2e8, 7.9e8, 8.1e8], "rsi": [55.0, 61.2, 64.8]})
    comp = pd.DataFrame({
        "zt_count": [40.0, 55.0, 70.0], "chg": [30.0, 60.0, 65.0],
        "rsi": [10.0, 20.0, 30.0], "bias20": [-10.0, 0.0, 15.0]},
        index=df.index)
    score_df = pd.DataFrame({
        "date": dates, "score": [10.0, 25.5, 40.2], "stage": ["中性", "中性偏强", "偏热/发酵"],
        "v1_score": [8.0, 20.0, 35.0], "close": [3600.0, 3650.5, 3681.47]})
    return df, comp, score_df


def test_text_report_latest(frames, capsys):
    """默认最新交易日: 分数/前日/日变化/主导分量/缺失分量提示"""
    df, comp, score_df = frames
    cfg = {"weights": {"zt_count": 0.3, "chg": 0.2, "rsi": 0.2, "bias20": 0.2,
                       "vr": 0.1}}
    RP.text_report(cfg, df, comp, score_df)
    out = capsys.readouterr().out
    assert "2026-11-16" in out and "+40.2" in out and "偏热/发酵" in out
    assert "+25.5" in out and "日变化: +14.7" in out     # 前日与变化
    assert "主导分量:" in out
    assert "缺失分量" in out and "量比" in out            # vr分量NaN → 归一化剔除提示


def test_text_report_by_date_and_missing(frames, capsys):
    """指定日期命中; 无评分日/空评分给提示不炸"""
    df, comp, score_df = frames
    cfg = {"weights": {"zt_count": 1.0}}
    RP.text_report(cfg, df, comp, score_df, "2026-11-13")
    assert "2026-11-13" in capsys.readouterr().out
    RP.text_report(cfg, df, comp, score_df, "2026-11-14")   # 非交易日
    assert "无评分" in capsys.readouterr().out
    empty = score_df.copy()
    empty["score"] = None
    RP.text_report(cfg, df, comp, empty)
    assert "无可用评分" in capsys.readouterr().out


def test_chart_smoke(frames, tmp_path):
    """历史曲线图: Agg渲染落盘, years窗口过滤生效"""
    df, _comp, score_df = frames
    png = tmp_path / "hist.png"
    RP.chart({}, df, score_df, years=None, out_png=png)
    assert png.exists() and png.stat().st_size > 5000
    RP.chart({}, df, score_df, years=1, out_png=png)       # years路径
    assert png.exists()
    RP.chart({}, df, score_df.dropna(), out_png=png)       # 幂等重画
    empty = score_df.copy()
    empty["score"] = None
    RP.chart({}, df, empty, out_png=png)                   # 空数据不炸
