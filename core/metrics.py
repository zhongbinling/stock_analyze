# -*- coding: utf-8 -*-
"""指标层: 从 SQLite 组装宽表, 计算各分量的原始序列"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import pandas as pd  # noqa: E402

from db import connect  # noqa: E402


def fetch_frame(conn=None, primary="000001", secondary="399106") -> pd.DataFrame:
    """取数合并(指数日线×2 + mood_raw), 按日期升序 — D1-17 R2 拆出,
    供实时快照在拼行后再走 derive"""
    if conn is None:
        conn = connect()
    idx = pd.read_sql(
        f"SELECT date,open,high,low,close,volume,amount FROM index_daily WHERE code='{primary}'",
        conn, parse_dates=["date"])
    sec = pd.read_sql(
        f"SELECT date,volume AS volume2,amount AS amount2 FROM index_daily WHERE code='{secondary}'",
        conn, parse_dates=["date"])
    mood = pd.read_sql("SELECT * FROM mood_raw", conn, parse_dates=["date"])

    return (idx.merge(sec, on="date", how="left")
               .merge(mood, on="date", how="left")
               .sort_values("date").reset_index(drop=True))


def derive(df: pd.DataFrame) -> pd.DataFrame:
    """派生量价指标(与通达信V2.1口径一致) — 输入按日期升序宽表(D1-17 R2拆出)"""
    import numpy as np

    # ---- 派生量价指标(与通达信V2.1口径一致) ----
    prev_close = df["close"].shift(1)
    df["chg"] = (df["close"] / prev_close - 1) * 100
    df["amp"] = (df["high"] - df["low"]) / prev_close * 100
    df["gap"] = (df["open"] / prev_close - 1) * 100        # 跳空缺口(竞价情绪)
    rng = (df["high"] - df["low"]).abs()
    df["kr"] = ((df["close"] - df["open"]) / rng.replace(0, np.nan)).rolling(3).mean()
    ma20 = df["close"].rolling(20).mean()
    df["bias20"] = (df["close"] - ma20) / ma20 * 100
    ma60 = df["close"].rolling(60).mean()
    df["bias60"] = (df["close"] - ma60) / ma60 * 100

    # 量能统一用两市成交量(手): 腾讯回退源无成交额, 用量保证跨源口径一致
    df["vol_total"] = df["volume"].fillna(0) + df["volume2"].fillna(0)
    df["amount_total"] = df["amount"].fillna(0) + df["amount2"].fillna(0)
    ma5v = df["vol_total"].rolling(5).mean()
    ma20v = df["vol_total"].rolling(20).mean()
    df["vr"] = ma5v / ma20v
    # V1对照用的无方向量能(TDX V1 口径: VOL分位不带方向)
    df["vol_und"] = df["vol_total"]
    df["vr_und"] = df["vr"]

    diff = df["close"].diff()
    up = diff.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = diff.abs().ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = up / dn * 100
    df["rsi_lin"] = df["rsi"]
    return df


def build_frame(conn=None, primary="000001", secondary="399106") -> pd.DataFrame:
    """合并指数日线与情绪原始数据, 计算派生指标, 按日期升序"""
    return derive(fetch_frame(conn, primary, secondary))


# 分量元信息: (数据列, 中文名, 方向sign, 计算模式, 分组)
# 模式: pct=滚动分位; symdir=分位后乘当日方向(振幅);
#       asymdir=非对称方向化(量能, 通达信V2.1口径: 放量×方向, 缩量恒负/涨减半);
#       rsi=(x-50)*2线性(仅V1对照)
# 分组: pool=短线情绪(同花顺, 历史约一年, 用较短min_periods); px=量价(2015年起全历史)
COMPONENTS = [
    ("zt_count",      "涨停家数",          +1, "pct", "pool"),
    ("dt_count",      "跌停家数",          -1, "pct", "pool"),
    ("zb_rate",       "炸板率",            -1, "pct", "pool"),
    ("lb_max",        "连板高度",          +1, "pct", "pool"),
    ("jl_rate",       "接力率",            +1, "pct", "pool"),
    ("rsi",           "14日RSI",           +1, "pct", "px"),
    ("chg",           "当日涨跌幅",        +1, "pct", "px"),
    ("gap",           "跳空缺口",          +1, "pct", "px"),
    ("kr",            "K线实体强度",       +1, "pct", "px"),
    ("bias20",        "20日乖离",          +1, "pct", "px"),
    ("bias60",        "60日乖离",          +1, "pct", "px"),
    ("vol_total",     "两市成交量",        +1, "asymdir", "px"),
    ("vr",            "量比趋势5/20",      +1, "asymdir", "px"),
    ("close",         "N日价格位置",       +1, "pct", "px"),
    ("amp",           "振幅×方向",         +1, "symdir", "px"),
    ("rsi_lin",       "RSI(线性)",         +1, "rsi", "px"),  # 仅V1对照用
    ("vol_und",       "量能(无方向)",      +1, "pct", "px"),  # 仅V1对照用
    ("vr_und",        "量比(无方向)",      +1, "pct", "px"),  # 仅V1对照用
]

LABELS = {k: label for k, label, _, _, _ in COMPONENTS}
POOL_KEYS = {k for k, _, _, _, g in COMPONENTS if g == "pool"}
