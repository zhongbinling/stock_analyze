# -*- coding: utf-8 -*-
"""交互式情绪行情图查看器: 生成自包含 HTML(内嵌 ECharts + 数据)

用法:
  python viewer.py                # 全历史 → out/sentiment_viewer.html
  python viewer.py --years 2      # 近两年
  python viewer.py --open         # 生成后自动用浏览器打开

功能: K线+成交量 / 情绪分 / 涨跌停家数 / 炸板率·接力率 五联面板,
      十字光标联动, 悬停右侧面板实时显示当日全部构成指标(原始值/分位分/权重/贡献),
      滚轮缩放 + 底部区间选择。

情绪图多指数(MI-3): 页面上方有四个页签 — 上证指数(现役大盘情绪, 内容与原来相同)、
深证成指 / 创业板指 / 科创50(指数情绪分, 见 score_index)。上证的数据在载荷顶层、键名与含义不变;
三个指数的数据在载荷的 tabs 里, 每个页签一份与上证同构的载荷, 页面脚本按同一套代码出图。
MI-4: 指数页签另有两块副图 — 创业板 / 科创板的板块两融余额(core/board_margin.py, 有数据时代替交易所口径的
那条线)、三个指数的成分股广度(core/index_breadth.py: 成分股上涨占比 / 站上 20 日线占比)。只作附图, 不进评分。
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from score import compute, load_cfg, stage_label  # noqa: E402
from metrics import LABELS  # noqa: E402

ECHARTS_URLS = [
    "https://registry.npmmirror.com/echarts/5.5.1/files/dist/echarts.min.js",
    "https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js",
]
VENDOR = BASE / "vendor" / "echarts.min.js"

# dataviz 参考色板
C = {
    "surface": "#fcfcfb", "page": "#f9f9f7", "ink": "#0b0b0b", "ink2": "#52514e",
    "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7",
    "blue": "#2a78d6", "red": "#e34948", "aqua": "#1baf7a", "yellow": "#eda100",
}


def _j(v):
    """NaN/inf → null"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if (np.isnan(f) or np.isinf(f)) else round(f, 4)


DB_PATH = Path(__file__).resolve().parent / "data" / "sentiment.db"


def _read_aux(dates):
    """D1-14 H4/D1-15 I2: 副图与悬停数据(估值官方/两融/ETF/自算估值) — 对齐全量日期轴。

    四源各自独立容错(旧库可能只有部分表): 任一源缺失仅降级对应功能,
    不拖累其余源。
    """
    out = {"val": {}, "margin": [], "etf": {}, "mval": {}}
    import sqlite3 as _sq
    try:
        con = _sq.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    except Exception:  # noqa: BLE001
        return out
    try:
        try:
            from fetch_val import VAL_INDEXES
            for code, name in VAL_INDEXES.items():
                pe = dict(con.execute(
                    "SELECT date, pe FROM index_val_daily WHERE code=?", (code,)).fetchall())
                pb = dict(con.execute(
                    "SELECT date, pb FROM index_val_daily WHERE code=?", (code,)).fetchall())
                out["val"][code] = {"n": name,
                                    "pe": [_j(pe.get(d)) for d in dates],
                                    "pb": [_j(pb.get(d)) for d in dates]}
        except Exception:  # noqa: BLE001
            out["val"] = {}
        try:
            mg = dict(con.execute("SELECT date, rzrqye FROM margin_daily").fetchall())
            out["margin"] = [_j(v / 1e8) if v is not None else None
                             for v in (mg.get(d) for d in dates)]      # 元→亿
        except Exception:  # noqa: BLE001
            out["margin"] = []
        try:
            from fetch_etf import ETF_INDEXES
            for code, name in ETF_INDEXES.items():
                am = dict(con.execute(
                    "SELECT date, amount FROM etf_daily WHERE code=?", (code,)).fetchall())
                out["etf"][code] = {"n": name,
                                    "amt": [_j(v / 1e5) if v is not None else None
                                            for v in (am.get(d) for d in dates)]}  # 千元→亿
        except Exception:  # noqa: BLE001
            out["etf"] = {}
        # D1-15 I2: 自算全A/宽基估值(整体法)
        try:
            out["mval"] = _mval_series(dates, con)
        except Exception:  # noqa: BLE001
            out["mval"] = {}
    finally:
        con.close()
    return out


_MVAL_COLS = ("alla_pe", "alla_pb", "pe50", "pb50", "pe300", "pb300",
              "pe500", "pb500", "pe1000", "pb1000")


def _mval_series(dates, con) -> dict:
    """D1-15 I2: 自算全A/宽基估值(整体法), 对齐 dates。D1-15 I4: 水位分位(当前值在≤当日历史中的
    排名占比; 全史/近5年两口径)。bisect有序插入避免O(n²); 近5年为逐日滑动窗口(悬停历史日时
    看到的是'该日往前5年'的水位, 而非固定截止日); ≥60样本才输出"""
    out = {}
    rows = con.execute("SELECT date," + ",".join(_MVAL_COLS) + " FROM mkt_val_daily").fetchall()
    for i, c in enumerate(_MVAL_COLS):
        m = {r[0]: r[i + 1] for r in rows}
        out[c] = [_j(m.get(d)) for d in dates]
    import bisect as _bi
    import datetime as _dtx
    from collections import deque as _dq
    for key in ("alla_pe", "alla_pb", "pe300"):
        arr = out[key]
        s_all, s_5y = [], []              # 有序历史(全史/滑动5年)
        q_5y = _dq()                      # (日期, 值) 按插入序, 供滑出
        p_all, p_5y = [], []
        for d, v in zip(dates, arr):
            if v is None:
                p_all.append(None)
                p_5y.append(None)
                continue
            # 滑出5年窗外的旧值
            try:
                d_obj = _dtx.date.fromisoformat(d)
            except ValueError:
                d_obj = None
            if d_obj is not None:
                while q_5y:
                    fd, fv = q_5y[0]
                    try:
                        lim = _dtx.date.fromisoformat(fd)
                    except ValueError:
                        lim = None
                    if lim is not None and (d_obj - lim).days > 1826:
                        q_5y.popleft()
                        s_5y.remove(fv)      # 值唯一性由估值连续性保证近似
                    else:
                        break
            _bi.insort(s_all, v)
            _bi.insort(s_5y, v)
            q_5y.append((d, v))
            p_all.append(round(_bi.bisect_right(s_all, v) / len(s_all), 3)
                         if len(s_all) >= 60 else None)
            p_5y.append(round(_bi.bisect_right(s_5y, v) / len(s_5y), 3)
                        if len(s_5y) >= 60 else None)
        out[key + "_pct"] = p_all
        out[key + "_pct5"] = p_5y
    return out


# D1-18 M3: 宏观事件读取 — macro.db(独立库) → 情绪图标注序列
# 情绪图AI标注: L2+默认(L1日常新闻回补后海量, 入图纯噪音) + 载荷上限
EV_LEVEL_MIN = 2
EV_CAP = 3000


def _read_events(level_min=None) -> list:
    """情绪图标注事件(日期升序; 默认L2+, AI概要经标题匹配附加 — 复用
    server.macro._attach_ai_analysis与宏观页同口径)。macro.db 缺失/未建
    → 空列表(标注面板自动隐藏), 不拖累视图生成。"""
    import sqlite3
    lv = EV_LEVEL_MIN if level_min is None else int(level_min)
    p = BASE / "data" / "macro.db"
    if not p.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT date, level, title, type, source, content FROM macro_events "
            "WHERE level>=? ORDER BY date DESC, id DESC LIMIT ?",
            (lv, EV_CAP)).fetchall()
        rows = list(reversed(rows))            # 取最新N条后恢复升序
        events = [{"date": r[0], "level": r[1], "title": r[2], "type": r[3],
                   "source": r[4], "content": r[5]} for r in rows]
        try:                                   # AI概要附加(表缺/导入失败不拖累)
            from server.macro import _attach_ai_analysis
            _attach_ai_analysis(con, events)
        except Exception:                                  # noqa: BLE001
            pass
        con.close()
    except sqlite3.Error:
        return []
    return events


# ---- MI-3: 情绪图多指数 — 三个指数页签的载荷 ----
MI3_DETAIL_YEARS = 3            # 指数页签的悬停明细(原始值 / 分量分)只带最近这些年, 控制单文件体积
_BOARD_MARGIN = {"SH": "sse_rzrqye", "KCB": "sse_rzrqye", "SZ": "szse_rzrqye", "CYB": "szse_rzrqye"}
_MVAL_TAB_KEYS = ("alla_pe", "alla_pb", "pe300")     # 指数页签用得到的估值: 右侧"市场环境"表 + 全A分位副图
# 指数页签右侧明细表的分量名: 结构 / 量能两组的口径与上证不同, 换成指数口径的说法; 短一些, 表格才放得下
# (口径写在表头上方)。其余几个量价分量与上证同名, 沿用 metrics.LABELS
_TAB_LABELS = {"zt_count": "强势家数", "dt_count": "弱势家数", "zb_rate": "冲高回落率",
               "lb_max": "连续强势高度", "jl_rate": "接力率",
               "vol_total": "指数成交额", "vr": "量比趋势5/20"}


def _stage_table(cfg) -> list:
    """分档名单(下标 = 档位号), 与 score.stage_label 用同一套阈值"""
    th = sorted(cfg["thresholds"])
    pts = [th[0] - 1] + [(a + b) / 2 for a, b in zip(th, th[1:])] + [th[-1] + 1]
    return [stage_label(p, cfg["thresholds"]) for p in pts]


def _window_start(dates, years) -> int:
    """years 窗口内第一天的下标(与上证主图同一规则); 不限年返回 0"""
    if not years:
        return 0
    cutoff = max(dates) - pd_offset(years)
    hit = [i for i, d in enumerate(dates) if d >= cutoff]
    return hit[0] if hit else 0


def _r(v, nd):
    """_j 的"按显示精度取小数位"版(指数页签用, 控制体积): nd=0 取整; NaN / inf / None → null"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if np.isnan(f) or np.isinf(f):
        return None
    r = round(f, nd)
    return int(r) if nd == 0 else r


def _aligned(m, dates, nd=2):
    """{日期: 值} → 对齐 dates 的列表(缺失为 None)"""
    return [_r(m.get(d), nd) for d in dates]


def _index_aux(cfg, sp: dict, con, dates) -> dict:
    """一个指数页签的副图, 各序列对齐本页签的日期轴 dates(ISO): 本指数 PE·PB、挂钩 ETF 的成交额与净流入、
    板块主力净流入、该交易所的两融余额(沪市 / 深市)、板块两融余额与成分股广度(MI-4)。
    每一项各自容错: 缺了只少对应面板。
    (全A 估值与水位不在这里算: 它是全市场一个数, 各页签要一致, 见 _fill_mval)"""
    from board_margin import BOARDS
    board = sp["board"]
    out = {"val": {}, "margin": [], "etf": {}, "etf_flow": [], "mf": [],
           "bmargin": [], "bn": [], "bm_marks": [], "breadth": None}
    try:
        ts = sp.get("ts_code")
        if ts:
            rows = con.execute("SELECT date, pe, pb FROM index_val_daily WHERE code=?", (ts,)).fetchall()
            out["val"][ts] = {"n": sp.get("name", ts),
                              "pe": _aligned({r[0]: r[1] for r in rows}, dates),
                              "pb": _aligned({r[0]: r[2] for r in rows}, dates)}
    except Exception as e:  # noqa: BLE001
        print(f"[viewer] 页签 {sp.get('name')} 估值副图跳过: {type(e).__name__}: {e}")
        out["val"] = {}
    try:
        col = _BOARD_MARGIN.get(board)
        if col:
            rows = con.execute(f"SELECT date, {col} FROM margin_daily").fetchall()      # 元
            out["margin"] = _aligned({r[0]: (None if r[1] is None else r[1] / 1e8) for r in rows}, dates, 1)
    except Exception as e:  # noqa: BLE001
        print(f"[viewer] 页签 {sp.get('name')} 两融副图跳过: {type(e).__name__}: {e}")
        out["margin"] = []
    try:
        if board in BOARDS:                 # MI-4: 创业板 / 科创板有板块级两融(沪市 / 深市本来就是交易所口径)
            rows = con.execute("SELECT date, rzrqye, n FROM board_margin WHERE board=?", (board,)).fetchall()   # 元
            out["bmargin"] = _aligned({r[0]: (None if r[1] is None else r[1] / 1e8) for r in rows}, dates, 1)
            n_of = {r[0]: r[2] for r in rows}
            out["bn"] = [n_of.get(d) for d in dates]            # 当日板块内两融标的只数(悬停用)
            # 标的扩容的日子 [日期, 扩容前只数, 扩容后只数]: 余额只覆盖两融标的, 名单一扩余额就上一个台阶(图上标出来)。
            # 判定: 标的只数连续上升的一段里, 累计增加 ≥ 5 只且 ≥ 20%; 一段只标一次, 记这一段的最后一天与最终只数
            # (实测创业板 4 次: 2013-09-16 / 2014-09-22 / 2019-08-19 / 2022-10-24; 科创板 2019-11-06 是连续两天 41 → 50)
            items = [(d, k) for d, k in sorted(n_of.items()) if k]     # 没记只数的日子不参与
            marks, run = [], None                  # run: 正在连续上升的那一段 [扩容前的只数]; None = 当前不在上升段里
            for i, (d, k) in enumerate(items):
                prev = items[i - 1][1] if i else None
                if prev is not None and k > prev:
                    if run is None:
                        run = [prev]
                else:
                    run = None                     # 不再上升: 这一段结束
                if run and k >= run[0] + 5 and k >= run[0] * 1.2:
                    if marks and marks[-1][3] is run:
                        marks[-1][0], marks[-1][2] = d, k      # 同一段还在涨: 并进上一条
                    else:
                        marks.append([d, run[0], k, run])
            # 早于本页签第一天的扩容画不到这个页签的日期轴上(科创50 页签自 2019-12-31 起, 科创板 2019-11-06
            # 那次扩容在它之前): 不带进载荷
            out["bm_marks"] = [m[:3] for m in marks if m[0] >= dates[0]]
    except Exception as e:  # noqa: BLE001
        print(f"[viewer] 页签 {sp.get('name')} 板块两融副图跳过: {type(e).__name__}: {e}")
        out["bmargin"], out["bn"], out["bm_marks"] = [], [], []
    try:
        rows = con.execute("SELECT date, n, up, n20, above20, nc FROM index_breadth WHERE code=? "
                           "ORDER BY date", (sp["code"],)).fetchall()
        got = {r[0]: r for r in rows}

        def pct(a, b):
            return None if not b else _r(a * 100.0 / b, 1)
        out["breadth"] = {                   # MI-4: 成分股广度(等权, %)
            "up": [pct(got[d][2], got[d][1]) if d in got else None for d in dates],     # 上涨占比 = 上涨只数 ÷ 有行情只数
            "ma20": [pct(got[d][4], got[d][3]) if d in got else None for d in dates],   # 站上20日线占比 ÷ 有20日线的只数
            "n": [got[d][1] if d in got else None for d in dates]}   # 当日有行情的只数(悬停 / 右侧面板显示分母)
    except Exception as e:  # noqa: BLE001
        print(f"[viewer] 页签 {sp.get('name')} 成分股广度副图跳过: {type(e).__name__}: {e}")
        out["breadth"] = None
    try:
        from fetch_etf import ETF_ALL
        etf = sp.get("etf")
        if etf and etf in ETF_ALL:
            rows = con.execute("SELECT date, amount FROM etf_daily WHERE code=?", (etf,)).fetchall()   # 千元
            out["etf"][etf] = {"n": ETF_ALL[etf],
                               "amt": _aligned({r[0]: (None if r[1] is None else r[1] / 1e5) for r in rows}, dates)}
            from fetch_etf_flow import net_flow_series
            flows = net_flow_series(n_days=6000, codes=(etf,))                           # 这一只ETF的净流入, 亿
            out["etf_flow"] = _aligned({x["date"]: x["net_yi"] for x in flows}, dates)
    except Exception as e:  # noqa: BLE001
        print(f"[viewer] 页签 {sp.get('name')} ETF 副图跳过: {type(e).__name__}: {e}")
        out["etf"], out["etf_flow"] = {}, []
    try:
        rows = con.execute("SELECT date, mf_net FROM board_daily WHERE board=?", (board,)).fetchall()   # 元
        out["mf"] = _aligned({r[0]: (None if r[1] is None else r[1] / 1e8) for r in rows}, dates)
    except Exception as e:  # noqa: BLE001
        print(f"[viewer] 页签 {sp.get('name')} 主力净流入副图跳过: {type(e).__name__}: {e}")
        out["mf"] = []
    return out


def _index_tab(cfg, sp: dict, con, years, SI) -> dict:
    """一个指数页签: 与上证载荷同构的一份(k / vol / pct / score / zt / dt / zb / jl / raw / cs /
    sub_starts + 副图), 日期轴是这个指数自己的。情绪分按生成规则的参数现算(与上证页签一样,
    管理台的自定义权重对它同样生效), 算法与库表 index_sentiment 是同一套(score_index)。
    口径映射: zt = 板块强势家数, dt = 弱势家数(取负, 画在下方), zb = 冲高回落率, jl = 接力率。
    raw / cs(悬停明细)只带最近 MI3_DETAIL_YEARS 年(给了 years 且窗口更窄时按窗口)。各序列按显示精度
    取小数位, 控制单文件体积。"""
    from boards import BOARDS
    code, board, name = sp["code"], sp["board"], sp.get("name", sp["code"])
    bars = pd.read_sql(
        "SELECT date, open, high, low, close, amount FROM index_daily WHERE code=? ORDER BY date",
        con, params=(code,), parse_dates=["date"])
    if bars.empty:
        raise RuntimeError("没有该指数的日线")
    bd = pd.read_sql(
        "SELECT date, strong, weak, fade, streak_max, streak2, prev_strong "
        "FROM board_daily WHERE board=? ORDER BY date", con, params=(board,), parse_dates=["date"])
    df = SI.build_frame(bars, bd)
    res = SI.score_frame(df, cfg)
    dates = [d.strftime("%Y-%m-%d") for d in df["date"]]
    n = len(dates)
    i0 = _window_start(list(df["date"]), years)
    d0 = max(i0, _window_start(list(df["date"]), MI3_DETAIL_YEARS))
    th = sorted(cfg["thresholds"])
    keys = [k for k in cfg["weights"] if k in res["comp"].columns]

    def win(series, start, nd):
        return [_r(v, nd) if i >= start else None for i, v in enumerate(series)]

    def detail(series, nd):                # 悬停明细: 只存 d0 起的那一段(页面脚本按 d0 放回原位)
        return [_r(v, nd) for v in series.iloc[d0:]]

    scored = res["score"].notna()
    first = next((i for i in range(n) if scored[i]), None)
    lim = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT date, zt, dt FROM board_daily WHERE board=?", (board,)).fetchall()}
    return {
        "key": code, "name": name, "board": BOARDS.get(board, (board,))[0],
        "dates": dates, "n": n, "d0": d0, "w0": i0,      # w0: years 窗口的起点下标(不限年为 0)
        # 窗口外的 K 线与上证一致, 写成四个空值组成的数组 — ECharts 的 K 线序列不接受裸 null(会抛错)
        "k": [[_r(x, 2) for x in row] if i >= i0 else [None, None, None, None]
              for i, row in enumerate(df[["open", "close", "low", "high"]].to_numpy())],
        "vol": win(df["vol_total"], i0, 2),              # 指数自身成交额, 亿
        "pct": win(df["chg"], i0, 2),
        "score": win(res["score"], i0, 1),
        "sc": [sum(1 for t in th if s > t) if i >= i0 and s == s else None
               for i, s in enumerate(res["score"])],     # 档位号(下标见载荷顶层 stages)
        "zt": win(df["zt_count"], 0, 0), "dt": win(-df["dt_count"], 0, 0),
        "zb": win(df["zb_rate"], 0, 2), "jl": win(df["jl_rate"], 0, 2),
        "lim": [list(lim[d]) if d in lim and lim[d][0] is not None and lim[d][1] is not None else None
                for d in dates],                          # 封板口径的涨停 / 跌停家数(悬停用)
        "raw": {k: detail(df[k], 2) for k in keys},
        "cs": {k: detail(res["comp"][k], 1) for k in keys},
        "labels": {k: _TAB_LABELS.get(k, LABELS.get(k, k)) for k in keys},
        "weights": {k: cfg["weights"][k] for k in keys},
        "first": first,                     # 首个出分日的下标; 之前是起步期
        # 出了分之后又没出分的日子: 原因(图上留空时标注)
        "notes": {i: SI._reason(res["missing"].iloc[i])
                  for i in range(first if first is not None else n, n) if not scored[i]},
        "aux": _index_aux(cfg, sp, con, dates),
    }


def _fill_mval(tabs: list, base_dates: list, mval: dict) -> None:
    """给各页签填全A估值与水位(aux.mval): 直接取上证页签算好的那一份(mval, 对齐 base_dates), 按日期对到
    各页签自己的日期轴上。全A 估值是全市场一个数, 四个页签必须一致; 而水位是"截至当日在历史里的排名",
    各页签日期轴长短不同, 各算各的会从各自的第一天起累计, 数字对不上。上证页签没有的日子, 该日为空。
    只保留页签用得到的几条(50 / 500 / 1000 等只有上证悬停框在用)。载荷里只带一份: 挂在日期最长的页签上,
    日期轴是它后缀的页签只记偏移 aux.mval_off(页面脚本按偏移截取), 不是后缀的自己带一份。"""
    keep = [k for k in (mval or {}) if k.split("_pct")[0] in _MVAL_TAB_KEYS]
    if not tabs or not keep:
        return
    pos = {d: i for i, d in enumerate(base_dates)}
    for t in tabs:
        idx = [pos.get(d) for d in t["dates"]]
        t["aux"]["mval"] = {k: [None if i is None else mval[k][i] for i in idx] for k in keep}
    host = max(tabs, key=lambda t: t["n"])
    for t in tabs:
        if t is not host and host["dates"][-t["n"]:] == t["dates"]:
            t["aux"]["mval_off"] = host["n"] - t["n"]
            t["aux"].pop("mval", None)


def _index_tabs(cfg, years=None) -> list:
    """config index.extra 里的每个指数一个页签(不含全A估值, 那部分由 _fill_mval 从上证页签的载荷里取);
    单个指数出错只少一个页签, 不影响其余部分"""
    import sqlite3 as _sq
    import score_index as SI
    tabs = []
    try:
        specs = SI.index_specs(cfg)
        if not specs:
            return tabs
        con = _sq.connect(f"file:{DB_PATH.as_posix()}?mode=ro", uri=True)
    except Exception as e:  # noqa: BLE001
        print(f"[viewer] 指数页签跳过: {type(e).__name__}: {e}")
        return tabs
    try:
        for sp in specs:
            try:
                tabs.append(_index_tab(cfg, sp, con, years, SI))
            except Exception as e:  # noqa: BLE001
                print(f"[viewer] 页签 {sp.get('name', sp.get('code'))} 跳过: {type(e).__name__}: {e}")
    finally:
        con.close()
    return tabs


def build_payload(cfg, years=None, tabs=True):
    """tabs=False 只生成上证页签的数据(不带 tabs / stages / window 三个键) — 只要 mval 的调用方(简报)用,
    省得连带把三个指数页签也算一遍"""
    df, comp, score_df = compute(cfg)
    n = len(df)
    if years:
        cutoff = df["date"].max() - pd_offset(years)
        idx0 = df.index[df["date"] >= cutoff]
        i0 = int(idx0[0]) if len(idx0) else 0
    else:
        i0 = 0

    # S2.1-B: 全量日期为统一轴; 主图数据(K线/情绪分)在 years 窗口外填 null
    # 附图数据(涨停/涨跌/炸板/接力)全量——拖动 dataZoom 可看全历史
    keys = [k for k in cfg["weights"] if k in comp.columns]
    payload = {
        "dates": [d.strftime("%Y-%m-%d") for d in df["date"]],  # 统一用全量日期
        "k": [[_j(r["open"]), _j(r["close"]), _j(r["low"]), _j(r["high"])]
              if i >= i0 else [None, None, None, None]
              for i, (_, r) in enumerate(df.iterrows())],
        "vol": [_j(v / 1e8) if i >= i0 else None
                for i, v in enumerate(df["vol_total"])],
        "pct": [_j(v) if i >= i0 else None for i, v in enumerate(df["chg"])],
        "score": [_j(v) if i >= i0 else None for i, v in enumerate(score_df["score"])],
        "v1": [_j(v) if i >= i0 else None for i, v in enumerate(score_df["v1_score"])],
        "stage": list(score_df["stage"]),
        "zt": [_j(v) for v in df["zt_count"]],
        "dt": [_j(-v) for v in df["dt_count"]],
        "zb": [_j(v) for v in df["zb_rate"]],
        "jl": [_j(v) for v in df["jl_rate"]],
        "raw": {k: [_j(v) if i >= i0 else None
                    for i, v in enumerate(df[k])] for k in keys},
        "cs": {k: [_j(v) if i >= i0 else None
                   for i, v in enumerate(comp[k])] for k in keys},
        "labels": {k: LABELS.get(k, k) for k in keys},
        "weights": {k: cfg["weights"][k] for k in keys},
        "n": n,
    }
    # S2.2-B: 附图各序列数据起始日
    def _first_date(series):
        for i, v in enumerate(series):
            if v is not None:
                return payload["dates"][i]
        return None
    payload["sub_starts"] = {
        "zt": _first_date(payload["zt"]),
        "dt": _first_date(payload["dt"]),
        "zb": _first_date(payload["zb"]),
        "jl": _first_date(payload["jl"]),
        "score": _first_date(payload["score"]),
    }
    # D1-14 H4: 估值/两融/ETF 副图(表缺失→空, 副图自动隐藏)
    aux = _read_aux(payload["dates"])
    payload.update(aux)
    # D1-18 M3: 事件标注(macro.db 独立库, 缺失→空)
    payload["events"] = _read_events()
    # C1: ETF净流入/流出序列(近120日, 副图叠加)
    try:
        import fetch_etf_flow as _EF
        # 全史窗口(共享轴联动缩放); flows明细JS不用, 剥离瘦身(~5000日payload)
        payload["etf_flow"] = [{"date": x["date"], "net_yi": x["net_yi"]}
                               for x in _EF.net_flow_series(n_days=6000)]
    except Exception:                                         # noqa: BLE001
        payload["etf_flow"] = []
    # MI-3: 情绪图多指数 — 上证的数据在上面原样(键名与含义不变), 另外三个指数各一个页签。
    # 页签里的情绪分现算、不读库表: 与上证一样跟着生成规则的参数走(管理台的自定义权重)。
    # 全A 估值与水位四个页签共用上证页签的这一份, 按日期对到各页签的日期轴上
    if not tabs:
        return payload
    payload["window"] = cfg["window"]                      # 指数页签"起步期"的提示用
    payload["stages"] = _stage_table(cfg)
    payload["tabs"] = _index_tabs(cfg, years)
    _fill_mval(payload["tabs"], payload["dates"], aux.get("mval") or {})
    return payload


def _payload_js(payload) -> str:
    """载荷 → 写进 <script> 的 JSON 文本。< 与行分隔符一律写成 \\uXXXX 转义: 事件正文是抓来的网页, 可能带
    </script>(浏览器会在那里提前关闭脚本块, 2026-11-10 实测整页空白)或 <!-- / <script>(进入 HTML 的
    脚本转义态)。这样 HTML 解析器在载荷里看不到任何标签起始, JS 解析出来的字符串与原值相同。
    (那天的事故当时修在 build_payload 里: 转义后立刻 json.loads 回原值, 写页面时又序列化成原样 — 没有生效)"""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (text.replace("<", "\\u003c")
                .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def pd_offset(years):
    from pandas import DateOffset
    return DateOffset(years=years)


def get_echarts_inline() -> str:
    """vendor 缓存 → 下载; 都失败则返回 CDN 引用标签"""
    if VENDOR.exists() and VENDOR.stat().st_size > 500_000:
        return VENDOR.read_text(encoding="utf-8")
    VENDOR.parent.mkdir(exist_ok=True)
    for url in ECHARTS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                js = r.read().decode("utf-8")
            if len(js) > 500_000:
                VENDOR.write_text(js, encoding="utf-8")
                print(f"[echarts] 已下载并缓存 ({len(js) // 1024}KB) <- {url}")
                return js
        except Exception as e:  # noqa: BLE001
            print(f"[echarts] {url} 失败: {e}")
    print("[echarts] 内联失败, 改用CDN引用(打开页面需联网)")
    return None


HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>A股市场情绪 · 交互行情图</title>
<style>
  :root {
    --surface: __C_SURFACE__; --page: __C_PAGE__; --ink: __C_INK__;
    --ink2: __C_INK2__; --muted: __C_MUTED__; --grid: __C_GRID__;
    --axis: __C_AXIS__; --blue: __C_BLUE__; --red: __C_RED__;
    --aqua: __C_AQUA__; --yellow: __C_YELLOW__;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--page); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
  }
  header {
    display: flex; flex-direction: column; gap: 6px;
    padding: 14px 20px 10px; border-bottom: 1px solid var(--grid);
  }
  .hrow { display: flex; align-items: baseline; gap: 18px; flex-wrap: wrap; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .hero { font-size: 30px; font-weight: 700; font-variant-numeric: tabular-nums; }
  header .sub { color: var(--ink2); }
  .chip {
    display: inline-block; padding: 2px 10px; border-radius: 12px;
    font-size: 12px; color: #fff; background: var(--muted); vertical-align: middle;
  }
  /* MI-3: 页签 / 对比条 / 情绪分叠加 */
  .tabs { display: inline-flex; gap: 4px; }
  .tab, .cmp-card {
    font-family: inherit; color: inherit; background: var(--surface); cursor: pointer;
  }
  .tab {
    border: 1px solid var(--axis); color: var(--ink2); border-radius: 14px;
    padding: 3px 12px; font-size: 12px;
  }
  .tab:hover, .cmp-card:hover { border-color: var(--blue); }
  .tab.on {
    border-color: var(--blue); color: var(--blue); font-weight: 600;
    background: rgba(42,120,214,0.07);
  }
  .tab:focus-visible, .cmp-card:focus-visible { outline: 2px solid var(--blue); outline-offset: 1px; }
  .cmp { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; padding: 10px 20px 0; }
  .cmp-card {
    display: flex; gap: 10px; align-items: center; text-align: left;
    border: 1px solid var(--grid); border-radius: 10px; padding: 7px 12px; min-width: 0;
  }
  .cmp-card.on { border-color: var(--blue); box-shadow: 0 0 0 2px rgba(42,120,214,0.14); }
  .cmp-main { flex: 1 1 auto; min-width: 0; }
  .cmp-name { font-size: 12px; color: var(--ink2); white-space: nowrap; }
  .cmp-name i {
    font-style: normal; color: var(--muted); font-size: 11px; margin-left: 6px;
    background: var(--grid); border-radius: 8px; padding: 0 6px;
  }
  .cmp-score { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1.3; }
  .cmp-score .chip { font-size: 11px; padding: 1px 7px; margin-left: 2px; }
  .cmp-meta { font-size: 11px; color: var(--muted); white-space: nowrap; }
  .cmp-spark { flex: 0 0 116px; height: 30px; display: block; }
  #overlayCtl {
    padding: 8px 20px 0; display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
    font-size: 12px; color: #898781;
  }
  #overlayBox { display: inline-flex; gap: 12px; flex-wrap: wrap; }
  #overlayCtl label, #subPanelCtl label, #auxPanelCtl label { cursor: pointer; user-select: none; }
  #overlayCtl input, #subPanelCtl input, #auxPanelCtl input { margin-right: 2px; vertical-align: -1px; }
  .wrap { display: flex; gap: 14px; padding: 8px 20px 14px; align-items: flex-start; }
  #chart { flex: 1 1 auto; min-width: 0; height: 470px; background: var(--surface);
           border: 1px solid var(--grid); border-radius: 10px; }
  aside {
    flex: 0 0 330px; max-height: 92vh; overflow-y: auto;
    background: var(--surface); border: 1px solid var(--grid);
    border-radius: 10px; padding: 14px 16px; position: sticky; top: 12px;
  }
  aside h2 { font-size: 13px; color: var(--ink2); margin: 0 0 4px; font-weight: 600; }
  .stage-line { display: flex; align-items: baseline; gap: 10px; margin-bottom: 6px; }
  .stage-line .big { font-size: 34px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .kv { color: var(--ink2); line-height: 1.7; }
  .kv b { color: var(--ink); font-variant-numeric: tabular-nums; }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; }
  th, td { padding: 3px 4px; text-align: right; font-variant-numeric: tabular-nums;
           border-bottom: 1px solid var(--grid); white-space: nowrap; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  td:first-child, th:first-child { text-align: left; }
  .bar-cell { width: 90px; }
  .track { position: relative; height: 10px; background: var(--grid); border-radius: 2px; }
  .track::after { content: ""; position: absolute; left: 50%; top: -2px; bottom: -2px;
                  width: 1px; background: var(--axis); }
  .fill { position: absolute; top: 1px; bottom: 1px; border-radius: 2px; }
  footer { color: var(--muted); padding: 8px 20px 18px; }
  .hint { color: var(--muted); font-size: 12px; }
  .btn-sm {
    border: 1px solid var(--axis); background: var(--surface); color: var(--ink2);
    border-radius: 6px; padding: 3px 10px; font-size: 11px; cursor: pointer;
  }
  .btn-sm:hover { border-color: var(--blue); color: var(--blue); }
  #subPanelCtl, #auxPanelCtl { padding: 0 20px 6px; display: flex; gap: 10px; align-items: center; font-size: 12px; color: #898781; }
  #auxPanelCtl { flex-wrap: wrap; }
</style>
</head>
<body>
<script>
window.onerror = function(msg, src, line, col) {
  var d = document.createElement("div");
  d.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:999;background:#d03b3b;color:#fff;padding:6px 12px;font:12px monospace";
  d.textContent = "页面脚本错误: " + msg + " @" + line + ":" + col + " (请截图反馈)";
  document.body.appendChild(d);
};
</script>
<header>
  <div class="hrow">
    <h1>A股市场情绪 · 交互行情图</h1>
    <nav class="tabs" id="tabs" aria-label="指数页签"></nav>
    <span style="flex:1"></span>
    <button class="btn-sm" onclick="setZoom(250)">最新1年</button>
    <button class="btn-sm" onclick="setZoom(500)">近2年</button>
    <button class="btn-sm" onclick="setZoom(0)">全部</button>
  </div>
  <div class="hrow">
    <span class="hero" id="heroScore">--</span>
    <span class="chip" id="heroStage">--</span>
    <span class="sub" id="heroDate">--</span>
    <span class="sub" id="heroV1Wrap">V1量价对照 <b id="heroV1">--</b></span>
  </div>
</header>
<div class="cmp" id="cmp"></div>
<div id="overlayCtl"><span>情绪分叠加(虚线):</span><span id="overlayBox"></span><span class="hint" id="overlayHint"></span></div>
<div class="wrap">
  <div id="chart"></div>
  <aside id="panel">加载中…</aside>
</div>
<div id="subPanelCtl">
  <span>底部面板:</span>
  <label><input type="checkbox" id="ckZb" checked> <span id="lbZb">炸板率</span></label>
  <label><input type="checkbox" id="ckJl" checked> <span id="lbJl">接力率</span></label>
  <label><input type="checkbox" id="ckUd"> 涨跌家数</label>
  <label><input type="checkbox" id="ckVol"> 成交额</label>
  <label><input type="checkbox" id="ckRsi"> RSI</label>
  <label><input type="checkbox" id="ckBias"> 乖离率</label>
</div>
<div id="auxPanelCtl">
  <span>分析副图(D1-14, 开关记忆):</span>
  <label><input type="checkbox" id="ckPE" checked> <span id="lbPE">指数PE</span></label>
  <label><input type="checkbox" id="ckPB" checked> <span id="lbPB">指数PB</span></label>
  <label><input type="checkbox" id="ckMargin" checked> <span id="lbMargin">两融余额</span></label>
  <label><input type="checkbox" id="ckEtf" checked> <span id="lbEtf">宽基ETF成交额</span></label>
  <label><input type="checkbox" id="ckEtfFlow" checked> <span id="lbEtfFlow">ETF净流入</span></label>
  <label id="ckMfWrap" style="display:none"><input type="checkbox" id="ckMf" checked> <span id="lbMf">板块主力净流入</span></label>
  <label id="ckBreadthWrap" style="display:none"><input type="checkbox" id="ckBreadth" checked> <span id="lbBreadth">成分股广度</span></label>
  <label><input type="checkbox" id="ckPEPct" checked> 全A PE分位</label>
  <label><input type="checkbox" id="ckPBPct" checked> 全A PB分位</label>
  <span style="margin-left:10px">事件标注:</span>
  <label><input type="checkbox" class="ckEvt" value="5" checked> L5</label>
  <label><input type="checkbox" class="ckEvt" value="4" checked> L4</label>
  <label><input type="checkbox" class="ckEvt" value="3" checked> L3</label>
  <label><input type="checkbox" class="ckEvt" value="2"> L2</label>
</div>
<footer>
  鼠标在图上移动查看当日构成 · 滚轮缩放 · 底部滑块选区间 · 页签与叠加选择记在本浏览器 ·
  情绪分是温度计不是买卖信号 · 生成于 __GENERATED__
</footer>
<script>__ECHARTS__</script>
<script>
// MI-3: 情绪图多指数。载荷顶层是上证(现役大盘情绪)的数据, 键名与含义不变; BASE.tabs 里是深证成指 /
// 创业板指 / 科创50, 每个一份与上证同构的数据。四个页签各对应一个"视图"(VIEWS[key]): 键名相同
// (dates / k / vol / score / zt / dt / zb / jl / raw / cs / val / margin / etf / mval …), 随页签变化的
// 文字放在 t 里。页面脚本一律按当前视图 D.* 取数 — 切换页签 = 换 D、重建依赖 D 的派生量、重新出图。
const BASE = __PAYLOAD__;
const nullish = v => (v === null || v === undefined);
const C = { blue: "__C_BLUE__", red: "__C_RED__", aqua: "__C_AQUA__",
            yellow: "__C_YELLOW__", ink: "__C_INK__", ink2: "__C_INK2__",
            muted: "__C_MUTED__", grid: "__C_GRID__", axis: "__C_AXIS__" };
const TAB_STORE = 'sentTabs_v1', OVL_STORE = 'sentOverlay_v1';
const TAB_ORDER = ['sh'].concat((BASE.tabs || []).map(t => t.key));   // 页签顺序: 上证在首
const TAB_COLOR = {sh: C.blue};                                        // 各指数固定一色
TAB_ORDER.slice(1).forEach((k, i) => { TAB_COLOR[k] = ['#e67e22', '#8e44ad', '#1baf7a', '#0f9bd7'][i % 4]; });
const STAGES = BASE.stages || [];
const SH = Object.assign({}, BASE, {                    // 上证视图: 载荷顶层原样 + 文案
  key: 'sh', isIdx: false, valShow: null,
  t: { idx: '上证指数', short: '上证', volSeries: '两市成交量(亿手)', volShort: '两市量', volUnit: '亿手', volLine: '成交额(亿)',
       ztSeries: '涨停家数', dtSeries: '跌停家数', zbSeries: '炸板率%', jlSeries: '接力率%',
       zt: '涨停', dt: '跌停', zb: '炸板率', jl: '接力率', margin: '两融余额', etf: '宽基ETF',
       etfFlow: 'ETF净流入(亿)', scope: '全市场口径', hint: '该日评分窗口数据不足(池数据2025-10起)。' },
});
const MVAL_HOST = (BASE.tabs || []).find(t => t.aux.mval) || null;   // 全A估值与水位各页签相同, 载荷里只带一份
function mvalOf(t) {
  if (t.aux.mval) return t.aux.mval;
  if (!MVAL_HOST || t.aux.mval_off == null) return {};
  const out = {};
  for (const k in MVAL_HOST.aux.mval) out[k] = MVAL_HOST.aux.mval[k].slice(t.aux.mval_off);
  return out;
}
function mkIdxView(t) {                                 // 指数视图: 情绪分 = 指数情绪分(score_index)
  const codes = Object.keys(t.aux.etf || {});
  const etfName = codes.length ? t.aux.etf[codes[0]].n : 'ETF';
  const raw = {}, cs = {};                  // 悬停明细在载荷里只存 d0 起的一段 → 放回全长数组(之前的日子为空)
  const full = (arr) => {
    const out = new Array(t.n).fill(null);
    if (arr) for (let j = 0; j < arr.length; j++) out[(t.d0 || 0) + j] = arr[j];
    return out;
  };
  for (const k in t.raw) { raw[k] = full(t.raw[k]); cs[k] = full(t.cs[k]); }
  // K 线窗口外的日子: 与上证一致, 写成四个空值组成的数组 — ECharts 的 K 线序列不接受裸 null(会抛错, 整张图停住)
  const k = t.k.map(x => nullish(x) ? [null, null, null, null] : x);
  // MI-4: 板块两融余额(创业板 / 科创板: 板块内两融标的的合计)。有就代替交易所口径的那条线; 没有(沪市 / 深市
  // 本来就是交易所口径, 或者还没回补)时照旧画交易所口径
  const hasBM = (t.aux.bmargin || []).some(x => x != null);
  const breadth = t.aux.breadth, brUp = (breadth && breadth.up) || [];
  const v = Object.assign({}, t, {
    isIdx: true, v1: null, raw, cs, k,
    stage: t.sc.map(c => nullish(c) ? "" : STAGES[c]),   // 档位号 → 档位名
    events: BASE.events,
    val: t.aux.val, margin: hasBM ? t.aux.bmargin : t.aux.margin, etf: t.aux.etf, mval: mvalOf(t),
    etf_flow: null, etf_flow_arr: t.aux.etf_flow,        // 挂钩ETF净流入: 对齐本页签日期轴的数组
    mf: t.aux.mf,
    valShow: Object.keys(t.aux.val || {}),
    bmargin: hasBM ? t.aux.bmargin : null,              // 板块两融余额(与 margin 相同; 页面上据此换面板名)
    exMargin: hasBM ? t.aux.margin : null,               // 该交易所的两融余额: 悬停时算板块在交易所里的占比
    bn: hasBM ? (t.aux.bn || null) : null,               // 当日板块内两融标的只数
    bmMarks: hasBM ? (t.aux.bm_marks || null) : null,    // 标的扩容的日子 [日期, 之前只数, 之后只数](图上标出来)
    breadth: brUp.some(x => x != null) ? breadth : null, // 成分股广度: 上涨占比 / 站上20日线占比(%)
  });
  const shExchange = t.board === '沪市' || t.board === '科创板';       // 科创板在上海
  v.t = { idx: t.name, short: t.name, volSeries: '指数成交额(亿)', volShort: '成交额', volUnit: '亿', volLine: '成交额(亿)',
          ztSeries: '强势家数', dtSeries: '弱势家数', zbSeries: '冲高回落率%', jlSeries: '接力率%',
          zt: '强势', dt: '弱势', zb: '冲高回落率', jl: '接力率',
          margin: hasBM ? t.board + '两融余额' : (shExchange ? '沪市两融余额' : '深市两融余额'),
          exch: shExchange ? '沪市' : '深市', etf: etfName,
          etfFlow: etfName + '净流入(亿)', scope: t.board + '板块', hint: '' };
  return v;
}
const VIEWS = {sh: SH};
(BASE.tabs || []).forEach(t => { VIEWS[t.key] = mkIdxView(t); });
function etfSumOf(v) {                     // ETF 成交额合计(tooltip / 右侧面板用)
  if (!v.etf) return null;
  const codes = Object.keys(v.etf), n = v.dates.length;
  const sum = new Array(n).fill(null);
  codes.forEach(c => {
    const a = v.etf[c].amt;
    for (let i = 0; i < n; i++)
      if (a[i] != null) sum[i] = (sum[i] || 0) + a[i];
  });
  return sum;
}
function flowMapOf(v) {                    // 日期 → 当日 ETF 净流入(亿)
  const m = {};
  for (const x of (v.etf_flow || [])) m[x.date] = x.net_yi;
  if (v.etf_flow_arr) v.etf_flow_arr.forEach((x, i) => { if (!nullish(x)) m[v.dates[i]] = x; });
  return m;
}
for (const key of TAB_ORDER) {             // 各视图自己的 日期→下标、ETF 合计、净流入查找表
  const v = VIEWS[key];
  v.idx = {};
  v.dates.forEach((d, i) => { v.idx[d] = i; });
  v.etfSum = etfSumOf(v);
  v.etfFlowMap = flowMapOf(v);
}

const chart = echarts.init(document.getElementById("chart"));
// S2.1-A: 两列布局(K线+量 左列, 情绪分+涨停 右列, 炸板率全宽)
const grids = [
  { top: 56,  height: 220, left: '3%',  width: '44%' },  // K线(左上)
  { top: 286, height: 60,  left: '3%',  width: '44%' },  // 成交量(左中)
  { top: 56,  height: 220, left: '52%', width: '46%' },  // 情绪分(右上)
  { top: 286, height: 60,  left: '52%', width: '46%' },  // 涨停/跌停(右中)
  { top: 360, height: 80,  left: '3%',  width: '95%' },  // 炸板率/接力率(全宽底)
];
const catAxis = (gi, dates) => ({
  type: "category", gridIndex: gi, data: dates || D.dates, show: gi === 4,
  axisLine: { lineStyle: { color: C.axis } },
  axisLabel: { color: C.muted }, axisTick: { show: false },
});
const valAxis = (gi) => ({
  type: "value", gridIndex: gi, scale: true,
  axisLabel: { color: C.muted }, splitLine: { lineStyle: { color: C.grid } },
});

// ---- D1-18 M3: 宏观事件标注(指数+情绪分双grid菱形, 默认L3-L5) ----
const EV_COLORS = {5: '#d03b3b', 4: '#e67e22', 3: '#eda100', 2: '#8e44ad', 1: '#898781'};
const escE = (x) => String(x ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const EV_ON = {5: true, 4: true, 3: true, 2: false, 1: false};   // 事件等级勾选(各页签共用)

// ---- 当前视图: D 以及由它派生的量(切换页签时在 mount 里整体重建) ----
let D = null, IDX = null, KEYS = [], baseOption = null, curIdx = 0;
function lastScored(v) { for (let i = v.n - 1; i >= 0; i--) if (!nullish(v.score[i])) return i; return v.n - 1; }
function firstDate(arr) { for (let i = 0; i < arr.length; i++) if (!nullish(arr[i])) return D.dates[i]; return null; }
function mount(key) {
  D = VIEWS[key] || SH;
  IDX = D.idx;
  KEYS = Object.keys(D.weights);
  if (D.isIdx) D.sub_starts = {zt: firstDate(D.zt), zb: firstDate(D.zb), jl: firstDate(D.jl), score: firstDate(D.score)};
  D.evOn = EV_ON;
  D.evIdx = (() => {            // 事件日期→交易日索引(吸附最近≥该日, 二分)
    const out = [];
    if (!D.events || !D.events.length) return out;
    for (const e of D.events) {
      let lo = 0, hi = D.dates.length;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (D.dates[mid] < e.date) lo = mid + 1; else hi = mid; }
      if (lo < D.dates.length) out.push({i: lo, e});
    }
    return out;
  })();
  baseOption = buildBase();
  syncChrome();
  curIdx = lastScored(D);
  render(curIdx);
}
function evSeriesData(kind) {   // kind: 'idx'(指数收盘)|'score'(情绪分)
  const out = [];
  for (const o of D.evIdx) {
    if (!D.evOn[o.e.level]) continue;
    const y = kind === 'idx' ? (D.k[o.i] && D.k[o.i][1]) : D.score[o.i];
    if (y == null) continue;
    out.push({value: [D.dates[o.i], y, o.e.level],
              itemStyle: {color: EV_COLORS[o.e.level]}, evt: o.e});
  }
  return out;
}
function setEvents() {
  if (!D.evIdx.length) return;
  chart.setOption({series: [
    {id: 'evIdx', data: evSeriesData('idx')},
    {id: 'evScore', data: evSeriesData('score')}]});
}
function buildBase() {
  const T = D.t, color = TAB_COLOR[D.key];
  const zoomStart = Math.max(0, 100 - (250 / D.n) * 100);   // 默认窗口: 最近 250 个交易日
  const baseOption = {
    animation: false,
    backgroundColor: "transparent",
    // S2.2-A: 面板标题
    title: [
      { text: T.idx, left: "4%", top: "38px", textStyle: { fontSize: 11, color: C.ink2 } },
      { text: "情绪分", left: "53%", top: "38px", textStyle: { fontSize: 11, color: C.ink2 } },
      { text: "底部面板(可勾选)", left: "4%", top: "352px", textStyle: { fontSize: 10, color: C.muted } },
    ],
    axisPointer: { link: [{ xAxisIndex: "all" }],
                   lineStyle: { color: C.axis } },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", label: { backgroundColor: C.ink2 } },
      backgroundColor: "rgba(252,252,251,0.96)", borderColor: C.grid,
      textStyle: { color: C.ink, fontSize: 12 },
      formatter: (ps) => {
        const i = ps[0].dataIndex;
        const k = D.k[i], pct = D.pct[i];
        let h = `<b>${D.dates[i]}</b>`;
        if (k && !nullish(k[1])) h += `<br/>${T.short} 收 <b>${k[1]}</b> (${nullish(pct) ? "--" : (pct >= 0 ? "+" : "") + pct}%) 开 ${k[0]} 高 ${k[3]} 低 ${k[2]}`;
        h += `<br/>${T.volShort} ${nullish(D.vol[i]) ? "--" : D.vol[i]} ${T.volUnit}`;
        const s = D.score[i];
        h += `<br/>情绪分 <b style="color:${nullish(s) ? C.muted : (s >= 0 ? C.red : C.blue)}">${nullish(s) ? "--" : (s >= 0 ? "+" : "") + s}</b> · ${D.stage[i] || "--"}`;
        if (!nullish(D.zt[i])) {
          h += `<br/>${T.zt} <b>${D.zt[i]}</b> / ${T.dt} <b>${-D.dt[i]}</b> · ${T.zb} ${D.zb[i]}% · ${T.jl} ${nullish(D.jl[i]) ? "--" : D.jl[i]}%`;
          // 指数页签: 封板口径的涨停 / 跌停家数(只作信息)
          if (D.lim && D.lim[i]) h += ` <span style="color:#898781;font-size:11px">(封板涨停 ${D.lim[i][0]} / 跌停 ${D.lim[i][1]})</span>`;
        }
        // S2.2-C: 追加详细分量信息
        const v1 = D.v1 ? D.v1[i] : null;
        if (!nullish(v1)) h += `<br/><span style="color:#898781">V1对照 ${v1 >= 0 ? "+" : ""}${v1}</span>`;
        if (i > 0 && !nullish(D.score[i-1]) && !nullish(s)) {
          const d = s - D.score[i-1];
          h += ` <span style="color:${d >= 0 ? C.red : C.blue}">环比${d >= 0 ? "+" : ""}${d.toFixed(1)}</span>`;
        }
        // MI-3: 叠加的其他指数情绪分
        for (const key of OVERLAY) {
          const v = VIEWS[key];
          if (!v || key === D.key) continue;
          const j = v.idx[D.dates[i]];
          if (j === undefined || nullish(v.score[j])) continue;
          h += `<br/><span style="color:${TAB_COLOR[key]}">${escE(v.t.idx)}情绪分 ${v.score[j] >= 0 ? "+" : ""}${v.score[j]}</span>`;
        }
        // 分量明细(来自 cs, key与config weights一致)
        const compBits = [];
        const cKeys = ['rsi', 'vol_total', 'bias20', 'zt_count', 'amp'];
        const cNms = {rsi: 'RSI', vol_total: '量能', bias20: '乖离', zt_count: T.zt, amp: '振幅'};
        for (const ck of cKeys) {
          if (D.cs && D.cs[ck] && !nullish(D.cs[ck][i]))
            compBits.push(`${cNms[ck]} ${D.cs[ck][i] > 0 ? '+' : ''}${Math.round(D.cs[ck][i])}`);
        }
        if (compBits.length) h += `<br/><span style="color:#898781;font-size:11px">${compBits.join(' · ')}</span>`;
        const mv = D.mval || {};
        if (D.isIdx) {
          // 指数页签: 本指数估值 / 该交易所两融 / 挂钩ETF成交额 / 板块主力净流入
          const o = D.val && D.val[D.valShow[0]];
          const pe = o && o.pe[i], pb = o && o.pb[i];
          const bits = [];
          if (!nullish(pe)) bits.push(`${T.idx} PE ${pe.toFixed(1)}`);
          if (!nullish(pb)) bits.push(`PB ${pb.toFixed(2)}`);
          if (bits.length)
            h += `<br/><span style="color:#898781;font-size:11px">${bits.join('/')}</span>`;
          if (D.margin && !nullish(D.margin[i])) {
            h += ` <span style="color:#898781;font-size:11px">· ${T.margin} ${Math.round(D.margin[i])}亿`;
            if (D.bn && !nullish(D.bn[i])) h += ` (标的 ${D.bn[i]} 只)`;       // 板块级两融: 只覆盖两融标的
            h += `</span>`;
          }
          if (D.etfSum && !nullish(D.etfSum[i]))
            h += ` <span style="color:#898781;font-size:11px">· ${escE(T.etf)} ${D.etfSum[i].toFixed(1)}亿</span>`;
          if (D.mf && !nullish(D.mf[i]))
            h += ` <span style="color:${D.mf[i] >= 0 ? C.red : C.blue};font-size:11px">· ${T.scope}主力净流入 ${D.mf[i] >= 0 ? "+" : ""}${D.mf[i].toFixed(1)}亿</span>`;
          if (D.breadth) {                       // MI-4: 成分股广度(占比固定写一位小数, 不随数值忽长忽短; 带上有行情的只数 —
            const bb = [];                       // 停牌多的日子分母会小很多, 只看占比会误读)
            if (!nullish(D.breadth.up[i]) && !nullish(D.breadth.n[i]))
              bb.push(`上涨占比 ${D.breadth.up[i].toFixed(1)}%(${D.breadth.n[i]} 只)`);
            if (!nullish(D.breadth.ma20[i])) bb.push(`站上20日线 ${D.breadth.ma20[i].toFixed(1)}%`);
            if (bb.length) h += `<br/><span style="color:#898781;font-size:11px">成分股(等权): ${bb.join(' · ')}</span>`;
          }
        } else {
          // D1-15 I2: 估值全套(自算整体法) + 两融/ETF
          const fmtPE = (k) => nullish(mv[k] && mv[k][i]) ? null : mv[k][i].toFixed(1);
          const fmtPB = (k) => nullish(mv[k] && mv[k][i]) ? null : mv[k][i].toFixed(2);
          const peBits = [];
          if (!nullish(mv.alla_pe && mv.alla_pe[i]))
            peBits.push(`全A ${fmtPE('alla_pe')}${fmtPB('alla_pb') ? '/' + fmtPB('alla_pb') : ''}`);
          if (mv.pe50 && !nullish(mv.pe50[i])) peBits.push(`50 ${fmtPE('pe50')}`);
          if (mv.pe300 && !nullish(mv.pe300[i]))
            peBits.push(`300 ${fmtPE('pe300')}${fmtPB('pb300') ? '/' + fmtPB('pb300') : ''}`);
          if (mv.pe500 && !nullish(mv.pe500[i])) peBits.push(`500 ${fmtPE('pe500')}`);
          if (mv.pe1000 && !nullish(mv.pe1000[i])) peBits.push(`1000 ${fmtPE('pe1000')}`);
          if (peBits.length)
            h += `<br/><span style="color:#898781;font-size:11px">PE ${peBits.join(' · ')}</span>`;
          if (D.margin && !nullish(D.margin[i]))
            h += ` <span style="color:#898781;font-size:11px">· 两融 ${D.margin[i]}亿</span>`;
          if (D.etfSum && !nullish(D.etfSum[i]))
            h += ` <span style="color:#898781;font-size:11px">· 宽基ETF ${Math.round(D.etfSum[i])}亿</span>`;
        }
        // D1-16②r: 全A水位入悬停框 — 分位副图联动读数(悬停任意grid即出)
        const fp = (a) => !a || nullish(a[i]) ? null : (a[i] * 100).toFixed(1) + '%';
        const peA = fp(mv.alla_pe_pct), pe5 = fp(mv.alla_pe_pct5);
        const pbA = fp(mv.alla_pb_pct), pb5 = fp(mv.alla_pb_pct5);
        const lvBits = [];
        if (peA) lvBits.push(`PE ${peA}` + (pe5 ? `/${pe5}` : ''));
        if (pbA) lvBits.push(`PB ${pbA}` + (pb5 ? `/${pb5}` : ''));
        if (lvBits.length)
          h += `<br/><span style="color:#898781;font-size:11px">全A水位·全史/近5年 ${lvBits.join(' · ')}</span>`;
        // D1-18 M3: 事件标注详情(轴悬停覆盖标记日时); 情绪图AI标注: 附AI概要
        for (const p of ps.filter(x => x.seriesId === 'evIdx' || x.seriesId === 'evScore')) {
          const e = p.data && p.data.evt;
          if (!e) continue;
          h += `<br/><span style="color:${EV_COLORS[e.level] || '#555'}">◆ L${e.level}${e.type ? '·' + escE(e.type) : ''} ${escE(e.title)}</span>`;
          if (e.ai_impact)
            h += `<br/><span style="color:#6250d6;font-size:11px">AI ${e.ai_level ? 'L' + e.ai_level + ' ' : ''}${escE(String(e.ai_impact).slice(0, 60))}</span>`;
          else if (e.content) h += `<br/><span style="color:#898781;font-size:11px">${escE(e.content).slice(0, 90)}</span>`;
        }
        return h;
      },
    },
    grid: grids.map(g => ({ ...g, containLabel: true, right: 20 })),
    xAxis: grids.map((_, gi) => catAxis(gi)),
    yAxis: [
      valAxis(0),                                   // 0 K线
      valAxis(1),                                   // 1 量
      { type: "value", gridIndex: 2, min: -105, max: 105,   // 2 情绪分(定域)
        axisLabel: { color: C.muted },
        splitLine: { lineStyle: { color: C.grid } } },
      valAxis(3),                                   // 3 家数
      valAxis(4),                                   // 4 %
    ],
    dataZoom: [
      // S2.2-A: 默认近~1年窗口。滚轮缩放与滑块是两个组件、共用一组横轴, 两个都要写上起点 —
      // 只给滑块写的话, 两者初始化时以滚轮那个的 0 ~ 100% 为准, 打开就是全史(2026-11-27 修)
      { type: "inside", xAxisIndex: [0, 1, 2, 3, 4], start: zoomStart },
      { type: "slider", xAxisIndex: [0, 1, 2, 3, 4], bottom: 4, height: 20,
        borderColor: C.grid, fillerColor: "rgba(42,120,214,0.10)",
        handleStyle: { color: C.blue }, textStyle: { color: C.muted },
        start: zoomStart },
    ],
    series: [
      { name: T.idx, type: "candlestick", xAxisIndex: 0, yAxisIndex: 0,
        data: D.k, barMaxWidth: 12,
        itemStyle: { color: C.red, color0: C.aqua,
                     borderColor: C.red, borderColor0: C.aqua } },
      { name: T.volSeries, type: "bar", xAxisIndex: 1, yAxisIndex: 1,
        data: D.vol, barMaxWidth: 12, itemStyle: { color: "rgba(137,135,129,0.55)" } },
      { name: "情绪分", type: "line", xAxisIndex: 2, yAxisIndex: 2,
        data: D.score, showSymbol: false, lineStyle: { width: 2, color },
        itemStyle: { color },
        markArea: {
          silent: true, itemStyle: { color: "rgba(227,73,72,0.06)" },
          data: [[{ yAxis: 60 }, { yAxis: 100 }], [{ yAxis: -100 }, { yAxis: -60 }]],
        },
        markLine: {
          silent: true, symbol: "none", label: { show: false },
          lineStyle: { color: C.axis, type: "dashed", width: 1 },
          data: [{ yAxis: 0 }, { yAxis: 60 }, { yAxis: -60 },
                 { yAxis: 80, lineStyle: { color: C.axis, opacity: 0.6 } },
                 { yAxis: -80, lineStyle: { color: C.axis, opacity: 0.6 } }],
        } },
      { name: T.ztSeries, type: "bar", xAxisIndex: 3, yAxisIndex: 3,
        data: D.zt, barMaxWidth: 8, itemStyle: { color: C.red } },
      { name: T.dtSeries, type: "bar", xAxisIndex: 3, yAxisIndex: 3,
        data: D.dt, barMaxWidth: 8, itemStyle: { color: C.aqua } },
      // D1-14r: 炸板率/接力率不进base series — 由底部面板勾选(SUB_OPTS默认勾选)
      // 统一提供, 避免render()再拼时重复添加(旧rebuildSubPanel的top5语义)
    ],
    // S2.2-B: 附图数据起始日标注
    graphic: (() => {
      const els = [];
      const ss = D.sub_starts || {};
      const mk = (text, x, y) => ({
        type: 'text', left: x, top: y, silent: true, z: 100,
        style: { text, fontSize: 9, fill: '#898781', fontFamily: 'sans-serif' } });
      if (ss.zt) els.push(mk(`${T.zt}起 ${ss.zt}`, '52%', '348px'));
      if (ss.zb) els.push(mk(`${T.zb}起 ${ss.zb}`, '3%', '438px'));
      if (ss.jl && ss.jl !== ss.zb) els.push(mk(`${T.jl}起 ${ss.jl}`, '20%', '438px'));
      // 指数页签: 行情比情绪分早一个分位窗口 — 标出情绪分从哪天起有
      if (D.isIdx && ss.score) els.push(mk(`情绪分起 ${ss.score}`, '62%', '38px'));
      return els;
    })(),
  };

  // D1-18 M3: 事件标记双grid(指数收盘/情绪分), 无事件零series
  if (D.evIdx.length) {
    baseOption.series.push(
      {id: 'evIdx', name: '事件', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0,
       data: evSeriesData('idx'), symbol: 'diamond', z: 5,
       symbolSize: (v) => 6 + (v[2] || 1) * 1.6, label: {show: false}},
      {id: 'evScore', name: '事件', type: 'scatter', xAxisIndex: 2, yAxisIndex: 2,
       data: evSeriesData('score'), symbol: 'diamond', z: 5,
       symbolSize: (v) => 6 + (v[2] || 1) * 1.6, label: {show: false}});
  }
  return baseOption;
}

// ---- MI-3: 页签 / 对比条 / 情绪分叠加 ----
function storeGet(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function storeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
let OVERLAY = (() => {                      // 勾选了要叠加情绪分的指数(key); 与当前页签相同的不画
  let ks = [];
  try { ks = JSON.parse(storeGet(OVL_STORE) || '[]') || []; } catch (e) {}
  return ks.filter(k => VIEWS[k]);
})();
const stageColor = (s) => nullish(s) ? C.muted : (s >= 60 ? C.red : s <= -60 ? C.blue : C.muted);
function mkEl(tag, cls, txt) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (txt != null) el.textContent = txt;
  return el;
}
function show(id, on) {
  const el = document.getElementById(id);
  if (el) el.style.display = on ? '' : 'none';
}
function setText(id, txt) {
  const el = document.getElementById(id);
  if (el) el.textContent = txt;
}
function renderTabs() {
  const nav = document.getElementById('tabs');
  nav.innerHTML = '';
  for (const key of TAB_ORDER) {
    const b = mkEl('button', 'tab' + (key === D.key ? ' on' : ''), VIEWS[key].t.idx);
    b.type = 'button';
    b.dataset.tab = key;
    b.onclick = () => activate(key);
    nav.appendChild(b);
  }
}
function cmpVals(v) {                       // 最新有分的日子往前数, 至多 60 个有分的日子(升序)
  const vals = [];
  for (let j = lastScored(v); j >= 0 && vals.length < 60; j--) if (!nullish(v.score[j])) vals.push(v.score[j]);
  return vals.reverse();
}
function cmpScale() {                       // 对比条迷你走势的纵轴: 四个指数共用各自窗口里的最小 / 最大值 —
  let lo = Infinity, hi = -Infinity;        // 固定 ±100 会把日常波动压成平线, 各自定标又没法横向比
  for (const key of TAB_ORDER)
    for (const x of cmpVals(VIEWS[key])) { lo = Math.min(lo, x); hi = Math.max(hi, x); }
  if (!isFinite(lo)) return [-100, 100];
  const pad = Math.max((hi - lo) * 0.12, 2);
  return [Math.max(-100, lo - pad), Math.min(100, hi + pad)];
}
function spark(vals, [ylo, yhi], color) {
  if (vals.length < 2) return '';
  const y = x => (2 + (yhi - x) / (yhi - ylo) * 26).toFixed(1);
  const pts = vals.map((x, k) => `${(k / (vals.length - 1) * 116).toFixed(1)},${y(x)}`);
  const zero = (0 >= ylo && 0 <= yhi) ? `<line x1="0" x2="116" y1="${y(0)}" y2="${y(0)}" stroke="${C.axis}" stroke-width="0.6"/>` : '';
  return `<svg class="cmp-spark" viewBox="0 0 116 30" preserveAspectRatio="none" aria-hidden="true">` + zero +
         `<polyline points="${pts.join(' ')}" fill="none" stroke="${color}" stroke-width="1.4"/></svg>`;
}
function renderCmp() {                      // 对比条: 最新分 / 分档 / 较前一日 / 近 60 日迷你走势; 点击切换页签
  const box = document.getElementById('cmp');
  box.innerHTML = '';
  const scale = cmpScale();
  for (const key of TAB_ORDER) {
    const v = VIEWS[key];
    const i = lastScored(v), s = v.score[i];
    let p = i;
    while (p > 0 && nullish(v.score[p - 1])) p--;
    const delta = (!nullish(s) && p > 0 && !nullish(v.score[p - 1])) ? s - v.score[p - 1] : null;
    const card = mkEl('button', 'cmp-card' + (key === D.key ? ' on' : ''));
    card.type = 'button';
    card.dataset.tab = key;
    card.title = key === 'sh' ? '上证 = 现役大盘情绪（全市场口径）' : `${v.t.idx} = 指数情绪分（该指数日线 + ${v.t.scope}）`;
    card.innerHTML = `<div class="cmp-main">` +
      `<div class="cmp-name">${escE(v.t.idx)}<i>${key === 'sh' ? '全市场' : escE(v.board)}</i></div>` +
      `<div class="cmp-score" style="color:${nullish(s) ? C.muted : (s >= 0 ? C.red : C.blue)}">${nullish(s) ? '--' : (s >= 0 ? '+' : '') + s.toFixed(1)}` +
      `<span class="chip" style="background:${stageColor(s)}">${v.stage[i] || '--'}</span></div>` +
      `<div class="cmp-meta">${v.dates[i].slice(5)} · ${nullish(delta) ? '--' : '较前日 ' + (delta >= 0 ? '+' : '') + delta.toFixed(1)}</div></div>` +
      spark(cmpVals(v), scale, TAB_COLOR[key]);
    card.onclick = () => activate(key);
    box.appendChild(card);
  }
}
function overlayHint() {                   // 叠加框后面的一句提示
  const on = OVERLAY.filter(k => k !== D.key).length;
  setText('overlayHint', '· ' + (on ? `虚线为叠加的 ${on} 个指数的情绪分` : '勾选后在该指数的 K 线上对照其它指数的情绪分'));
}
function renderOverlayCtl() {               // 叠加勾选: 列出除当前页签外的其它指数
  const box = document.getElementById('overlayBox');
  box.innerHTML = '';
  for (const key of TAB_ORDER) {
    if (key === D.key) continue;
    const lb = mkEl('label');
    const ck = mkEl('input');
    ck.type = 'checkbox';
    ck.checked = OVERLAY.includes(key);
    ck.dataset.overlay = key;
    ck.onchange = () => {
      OVERLAY = ck.checked ? OVERLAY.concat(key) : OVERLAY.filter(k => k !== key);
      storeSet(OVL_STORE, JSON.stringify(OVERLAY));
      overlayHint();
      renderAux();
    };
    lb.appendChild(ck);
    lb.appendChild(document.createTextNode(' ' + VIEWS[key].t.idx));
    box.appendChild(lb);
  }
  overlayHint();
}
function syncChrome() {                     // 随页签变化的部分: 页签、对比条、叠加框、控件文字、只对某类页签有意义的控件
  renderTabs();
  renderCmp();
  renderOverlayCtl();
  const T = D.t;
  setText('lbZb', T.zb);
  setText('lbJl', T.jl);
  setText('lbPE', D.isIdx ? '本指数PE' : '指数PE');
  setText('lbPB', D.isIdx ? '本指数PB' : '指数PB');
  setText('lbMargin', D.isIdx ? T.margin : '两融余额');
  setText('lbEtf', D.isIdx ? T.etf + '成交额' : '宽基ETF成交额');
  setText('lbEtfFlow', D.isIdx ? '挂钩ETF净流入' : 'ETF净流入');
  show('ckMfWrap', D.isIdx);                // 板块主力净流入只有指数页签有
  show('ckBreadthWrap', D.isIdx && !!D.breadth);   // 成分股广度: 指数页签有、且这个指数算出来了才有
  show('heroV1Wrap', !D.isIdx);             // V1 量价对照只有上证有
  document.title = D.isIdx ? `A股市场情绪 · ${T.idx} · 交互行情图` : 'A股市场情绪 · 交互行情图';
}
function overlaySeries() {                  // 叠加线: 按当前页签的日期轴对齐, 没有分的日子留空
  const out = [];
  for (const key of OVERLAY) {
    const v = VIEWS[key];
    if (!v || key === D.key) continue;
    const data = new Array(D.n).fill(null);
    for (let i = 0; i < D.n; i++) {
      const j = v.idx[D.dates[i]];
      if (j !== undefined) data[i] = v.score[j];
    }
    out.push({ name: v.t.idx + '情绪分', type: 'line', xAxisIndex: 2, yAxisIndex: 2,
      data, showSymbol: false, z: 3,
      lineStyle: { width: 1.3, type: 'dashed', color: TAB_COLOR[key] },
      itemStyle: { color: TAB_COLOR[key] } });
  }
  return out;
}
function zoomDates() {                      // 当前缩放窗口覆盖的日期区间 [起, 止]; 没有窗口信息返回 null
  let z = null;
  try { const cur = chart.getOption();
    if (cur && cur.dataZoom && cur.dataZoom[0] && cur.dataZoom[0].start != null) z = cur.dataZoom[0];
  } catch (e) {}
  if (!z) return null;
  // 百分比 → 下标带一点容差: applyZoom 写进去的正是"下标 ÷ n × 100", 直接取整会因浮点误差差一天(只会往早漂)
  const lo = Math.max(0, Math.floor(z.start / 100 * D.n + 1e-6));
  const hi = Math.min(D.n - 1, Math.ceil(z.end / 100 * D.n - 1e-6) - 1);
  return [D.dates[lo], D.dates[Math.max(lo, hi)]];
}
function applyZoom(z) {                     // 把日期区间换算到当前页签的日期轴上
  let s = baseOption.dataZoom[1].start, e = 100;      // 默认: 本页签自己的默认窗口(近一年)
  if (z && z[1] >= D.dates[0] && z[0] <= D.dates[D.n - 1]) {   // 原窗口与本页签的日期范围有重叠: 早于本页签起点的部分从头算
    let i0 = 0;
    while (i0 < D.n - 1 && D.dates[i0] < z[0]) i0++;
    let i1 = D.n - 1;
    while (i1 > 0 && D.dates[i1] > z[1]) i1--;
    if (i1 >= i0) { s = i0 / D.n * 100; e = Math.min(100, (i1 + 1) / D.n * 100); }
  }                                         // 原窗口整个落在本页签的日期范围之外(更早或更晚): 用本页签的默认窗口
  chart.dispatchAction({ type: 'dataZoom', start: s, end: e });
}
function activate(key) {                    // 切换页签: 缩放窗口按日期保持
  if (!VIEWS[key] || key === D.key) return;
  const z = zoomDates();
  mount(key);
  renderAux();                              // 先沿用旧的百分比窗口(与其它重绘一致)
  applyZoom(z);                             // 再换算成同一批日期在新日期轴上的位置
  render(curIdx);                           // 右侧面板里的"时间区事件"按新窗口重算(mount 时用的还是旧窗口)
  storeSet(TAB_STORE, key);
}

// ---- D1-14 H4: 分析副图(估值PE/PB·两融余额·宽基ETF) — 可折叠+开关记忆 ----
const P_COLORS = ['#2f5cd6', '#e34448', '#1baf7a', '#eda100', '#8e44ad', '#0f9bd7', '#7f8c8d'];
const VAL_SHOW = ['000016.SH', '000300.SH', '000905.SH', '000852.SH'];   // 50/300/500/1000
const AUX_STORE = 'auxPanels_v1';
function auxState() {
  let st = null;
  try { st = JSON.parse(localStorage.getItem(AUX_STORE) || 'null'); } catch (e) {}
  // D1-16②: 新面板默认开 — 以默认键补齐存量记忆缺的key(用户已存偏好,
  // 含显式关掉新面板的 false, 不被覆盖)
  return Object.assign({ pe: true, pb: true, margin: true, etf: true,
                         etfFlow: true, pePct: true, pbPct: true, mf: true, breadth: true }, st || {});
}
function saveAuxState(st) {
  try { localStorage.setItem(AUX_STORE, JSON.stringify(st)); } catch (e) {}
}
function valSeries(gi, k) {          // k='pe'|'pb' — 上证页签: 4宽基多线; 指数页签: 本指数一条
  const out = [];
  (D.valShow || VAL_SHOW).forEach((code, i) => {
    const o = D.val && D.val[code];
    if (!o) return;
    out.push({ name: `${o.n}${k === 'pe' ? 'PE' : 'PB'}`, type: 'line',
      xAxisIndex: gi, yAxisIndex: gi, data: o[k], showSymbol: false,
      lineStyle: { width: 1.3, color: P_COLORS[i] }, itemStyle: { color: P_COLORS[i] } });
  });
  return out;
}
function marginSeries(gi) {
  const s = { name: D.t.margin + '(亿)', type: 'line', xAxisIndex: gi, yAxisIndex: gi,
    data: D.margin, showSymbol: false, lineStyle: { width: 1.4, color: '#6250d6' },
    itemStyle: { color: '#6250d6' }, areaStyle: { opacity: 0.08 } };
  // 板块两融: 标的扩容的日子画一条竖虚线、标出"标的 a→b 只" — 余额只覆盖两融标的, 名单一扩余额就上一个台阶,
  // 不标的话台阶会被读成情绪变化。文字比线的顶端再低 12px: 放在顶端那一行会紧挨着面板标题的末尾, 读起来像
  // 标题的一部分(全史缩放下第一处标注离左边很近)
  if (D.bmMarks && D.bmMarks.length)
    s.markLine = { silent: true, symbol: 'none', animation: false,
      lineStyle: { color: '#b9aef0', type: 'dashed', width: 1 },
      label: { show: true, position: 'insideEndTop', offset: [0, 12], fontSize: 9, color: '#6250d6', rotate: 0 },
      data: D.bmMarks.map(m => ({ xAxis: m[0], value: m[2],
        label: { formatter: '标的 ' + m[1] + '→' + m[2] + ' 只' } })) };
  return [s];
}
// C1: ETF净流入/流出柱(红=流入/绿=流出, 8只ETF日净申购×收盘价)
function etfFlowSeries(gi) {
  // 共享D.dates全史轴(与其他子图dataZoom联动) — 份额已回补2006起全史;
  // 净流入自各ETF上市逐步丰富(早期仅510050), 无数据日null(轴连续)
  const data = [];
  for (let i = 0; i < D.dates.length; i++) {
    const v = D.etfFlowMap[D.dates[i]];
    data.push(v != null ? { value: [D.dates[i], v],
      itemStyle: { color: v >= 0 ? '#e34948' : '#1baf7a' } } : null);
  }
  return [{ name: D.t.etfFlow, type: 'bar', xAxisIndex: gi, yAxisIndex: gi,
    data, barWidth: 2, itemStyle: { color: '#8ba0b6' } }];
}
function etfSeries(gi) {             // 8只堆叠=总额观感, 图例式查看不便→色阶
  const out = [];
  Object.keys(D.etf || {}).forEach((c, i) => {
    const o = D.etf[c];
    out.push({ name: o.n, type: 'bar', stack: 'etf', xAxisIndex: gi, yAxisIndex: gi,
      data: o.amt, barMaxWidth: 10,
      itemStyle: { color: P_COLORS[i % P_COLORS.length], opacity: 0.85 } });
  });
  return out;
}
// MI-3: 板块主力净流入(亿, 板块内个股合计·红进绿出) — 只有指数页签有
function mfSeries(gi) {
  const data = [];
  for (let i = 0; i < D.dates.length; i++) {
    const v = D.mf[i];
    data.push(v == null ? null : { value: [D.dates[i], v],
      itemStyle: { color: v >= 0 ? '#e34948' : '#1baf7a' } });
  }
  return [{ name: D.t.scope + '主力净流入(亿)', type: 'bar', xAxisIndex: gi, yAxisIndex: gi,
    data, barMaxWidth: 6, itemStyle: { color: '#8ba0b6' } }];
}
// D1-16②: 全A估值分位双线(全史蓝/近5年红) — 比值×100, 80/20参考线
const PCT_ALL = '#2f5cd6', PCT_5Y = '#e34448';
const pctAxis = (gi) => ({ type: 'value', gridIndex: gi, min: 0, max: 100,
  axisLabel: { color: C.muted, formatter: '{value}%' },
  splitLine: { lineStyle: { color: C.grid } } });
// 指数页签上定域 0 ~ 100 的轴: 最高一档的刻度文字("100%")不画 — 它正好落在网格顶边那一线, 会压在面板标题开头
// (上证页签沿用 pctAxis, 不动)
const pctAxisIdx = (gi) => ({ type: 'value', gridIndex: gi, min: 0, max: 100,
  axisLabel: { color: C.muted, formatter: '{value}%', showMaxLabel: false },
  splitLine: { lineStyle: { color: C.grid } } });
function pctSeries(gi, key, label) {
  const mv = D.mval || {};
  const pct = (arr) => (arr || []).map(v => v == null ? null : Math.round(v * 1000) / 10);
  return [
    { name: label + '·全史%', type: 'line', xAxisIndex: gi, yAxisIndex: gi,
      data: pct(mv[key + '_pct']), showSymbol: false,
      lineStyle: { width: 1.3, color: PCT_ALL }, itemStyle: { color: PCT_ALL },
      markLine: { silent: true, symbol: 'none',
        label: { show: true, position: 'insideEndTop', fontSize: 9, color: PCT_5Y },
        lineStyle: { type: 'dashed', width: 1, color: PCT_5Y },
        data: [{ yAxis: 80 }] } },                    // 高估警戒线
    { name: label + '·近5年%', type: 'line', xAxisIndex: gi, yAxisIndex: gi,
      data: pct(mv[key + '_pct5']), showSymbol: false,
      lineStyle: { width: 1.3, color: PCT_5Y }, itemStyle: { color: PCT_5Y },
      markLine: { silent: true, symbol: 'none',
        label: { show: true, position: 'insideEndBottom', fontSize: 9, color: PCT_ALL },
        lineStyle: { type: 'dashed', width: 1, color: PCT_ALL },
        data: [{ yAxis: 20 }] } },                    // 低估机会线
  ];
}
const PANELS = [
  { key: 'pe',     id: 'ckPE',     name: '宽基指数PE(50/300/500/1000)', h: 84, build: gi => valSeries(gi, 'pe') },
  { key: 'pb',     id: 'ckPB',     name: '宽基指数PB',                  h: 84, build: gi => valSeries(gi, 'pb') },
  { key: 'margin', id: 'ckMargin', name: '两融余额(亿, 沪深北合计)',    h: 84, build: gi => marginSeries(gi) },
  { key: 'etf',    id: 'ckEtf',    name: '宽基ETF成交额(亿·8只堆叠)',   h: 84, build: gi => etfSeries(gi) },
  { key: 'etfFlow', id: 'ckEtfFlow', name: 'ETF净流入(亿·红进绿出·与其他图联动缩放)', h: 84,
    build: gi => etfFlowSeries(gi) },
  { key: 'pePct',  id: 'ckPEPct',  name: '全A PE 分位%(全史·近5年)',    h: 84,
    build: gi => pctSeries(gi, 'alla_pe', 'PE'), yaxis: pctAxis },
  { key: 'pbPct',  id: 'ckPBPct',  name: '全A PB 分位%(全史·近5年)',    h: 84,
    build: gi => pctSeries(gi, 'alla_pb', 'PB'), yaxis: pctAxis },
];
// MI-3: 指数页签的副图 — 本指数 PE·PB / 该交易所两融 / 挂钩 ETF 成交额与净流入 / 板块主力净流入;
// 全A 估值分位与上证页签共用同两个面板对象
const MF_PANEL = { key: 'mf', id: 'ckMf', h: 84, build: gi => mfSeries(gi) };
// MI-4: 成分股广度 — 成分股上涨占比 / 站上 20 日线占比(等权, %), 只有指数页签有。两条线共用一根 0 ~ 100 的轴
// (不做双轴); 上涨占比用页面里"上涨 / 强势"的那个红, 另一条用估值副图的蓝 — 红蓝这一对对红绿色弱也分得开
// (11-27 用配色校验脚本验过: 色弱可分辨度 ΔE 23.9); 页面没有图例框, 面板标题里用彩色名字当图例; 50% 虚线 = 涨跌各半
const BR_COLORS = [C.red, P_COLORS[0]];
function breadthSeries(gi) {
  const b = D.breadth;
  const line = (name, arr, color) => ({ name, type: 'line', xAxisIndex: gi, yAxisIndex: gi,
    data: arr, showSymbol: false, lineStyle: { width: 1.4, color }, itemStyle: { color } });
  const up = line('成分股上涨占比%', b.up, BR_COLORS[0]);
  up.markLine = { silent: true, symbol: 'none', label: { show: false },
    lineStyle: { color: C.axis, type: 'dashed', width: 1 }, data: [{ yAxis: 50 }] };
  return [up, line('站上20日线占比%', b.ma20, BR_COLORS[1])];
}
const BREADTH_PANEL = { key: 'breadth', id: 'ckBreadth', h: 84, build: gi => breadthSeries(gi), yaxis: pctAxisIdx };
function idxPanels() {
  const T = D.t;
  return [
    { key: 'pe',     id: 'ckPE',     name: `${T.idx} PE`,  h: 84, build: gi => valSeries(gi, 'pe') },
    { key: 'pb',     id: 'ckPB',     name: `${T.idx} PB`,  h: 84, build: gi => valSeries(gi, 'pb') },
    // 两融: 这个板块有自己的两融余额(创业板 / 科创板)时画板块口径, 否则(沪市 / 深市)画交易所口径
    { key: 'margin', id: 'ckMargin', name: `${T.margin}(亿${D.bmargin ? '·板块内两融标的合计' : ''})`, h: 84,
      build: gi => marginSeries(gi) },
    { key: 'etf',    id: 'ckEtf',    name: `${T.etf}成交额(亿)`, h: 84, build: gi => etfSeries(gi) },
    { key: 'etfFlow', id: 'ckEtfFlow', name: `${T.etfFlow}·红进绿出`, h: 84, build: gi => etfFlowSeries(gi) },
    Object.assign({ name: `${T.scope}主力净流入(亿·板块内个股合计·红进绿出)` }, MF_PANEL),
    Object.assign({ name: `${T.idx} 成分股广度(%·等权)  {u|上涨占比}  {m|站上20日线占比}`,
                    rich: { u: { color: BR_COLORS[0] }, m: { color: BR_COLORS[1] } } }, BREADTH_PANEL),
    Object.assign({}, PANELS[5], { yaxis: pctAxisIdx }), Object.assign({}, PANELS[6], { yaxis: pctAxisIdx }),
  ];
}
function auxDataEmpty(p) {
  if (p.key === 'margin') return !D.margin || !D.margin.some(v => v != null);
  if (p.key === 'etf') return !D.etf || !Object.keys(D.etf).length;
  if (p.key === 'etfFlow') return !D.etfFlowMap || !Object.keys(D.etfFlowMap).length;
  if (p.key === 'mf') return !D.mf || !D.mf.some(v => v != null);
  if (p.key === 'breadth') return !D.breadth;
  if (p.key === 'pePct') return !D.mval || !(D.mval.alla_pe_pct || []).some(v => v != null);
  if (p.key === 'pbPct') return !D.mval || !(D.mval.alla_pb_pct || []).some(v => v != null);
  return !D.val || !(D.valShow || VAL_SHOW).some(c => D.val[c]);
}
function renderAux() {
  // 悬停框还显示着的时候整页重排, ECharts 会拿旧页签的序列下标去刷新它 — 触摸 / 键盘操作下出脚本报错、
  // 旧悬停框留在新图上。先收掉悬停框
  chart.dispatchAction({ type: 'hideTip' });
  const st = auxState();
  const panels = D.isIdx ? idxPanels() : PANELS;
  const vis = panels.filter(p => st[p.key] && !auxDataEmpty(p));
  // 动态画布高度(基础470 + 每副图112)
  const el = document.getElementById('chart');
  el.style.height = (470 + vis.length * 112) + 'px';
  chart.resize();
  const opt = Object.assign({}, baseOption, {   // 浅克隆可变数组, tooltip.formatter按引用保留
    grid: [...baseOption.grid], xAxis: [...baseOption.xAxis],
    yAxis: [...baseOption.yAxis], title: [...baseOption.title],
    series: [...baseOption.series],
    dataZoom: baseOption.dataZoom.map(z => Object.assign({}, z)),
  });
  let top = 466;
  vis.forEach((p, pi) => {
    const gi = 5 + pi;
    opt.grid.push({ top, height: p.h, left: '3%', width: '95%', containLabel: true, right: 20 });
    opt.xAxis.push(p.xData ? catAxis(gi, p.xData) : catAxis(gi));
    opt.yAxis.push(p.yaxis ? p.yaxis(gi) : valAxis(gi));   // D1-16②: 分位轴定域覆盖
    const ts = { fontSize: 10, color: C.muted };
    if (p.rich) ts.rich = p.rich;     // p.rich: 标题里带彩色名字(图例), 要挂在 textStyle.rich 下
    // 指数页签的面板标题比上证页签再抬 6px: 纵轴最高一档的刻度文字落在网格顶边那一线, 离得近会压在标题开头
    // (定域 0 ~ 100 的轴上"100%"正好压住标题的前几个字)。上证页签保持原样
    opt.title.push({ text: p.name, left: '3%', top: (top - (D.isIdx ? 20 : 14)) + 'px', textStyle: ts });
    opt.series.push(...p.build(gi));
    top += p.h + 28;
  });
  // 副图x轴标签显示(最末可见grid的axisLabel展示)
  if (vis.length) {
    const lastGi = 5 + vis.length - 1;
    opt.xAxis = opt.xAxis.map((a, i) =>
      i === lastGi ? { ...a, show: true } : { ...a, show: i === lastGi });
  }
  const allIdx = opt.grid.map((_, i) => i);
  opt.dataZoom[0].xAxisIndex = allIdx;
  opt.dataZoom[1].xAxisIndex = allIdx;
  // 底部面板勾选线(原rebuildSubPanel逻辑并入)
  for (const o of SUB_OPTS) {
    const ck = document.getElementById(o.id);
    if (ck && ck.checked) { const s = subSeriesFor(o.id); if (s) opt.series.push(s); }
  }
  // MI-3: 叠加的其他指数情绪分
  opt.series.push(...overlaySeries());
  // 保留当前缩放窗口
  let zs = null;
  try { const cur = chart.getOption();
    if (cur && cur.dataZoom && cur.dataZoom[0] && cur.dataZoom[0].start != null)
      zs = { s: cur.dataZoom[0].start, e: cur.dataZoom[0].end };
  } catch (e) {}
  // graphic 也在替换列表里: 各页签的起始日标注不同, 合并的话旧页签的文字会留下
  chart.setOption(opt, { replaceMerge: ['grid', 'xAxis', 'yAxis', 'series', 'title', 'dataZoom', 'graphic'] });
  if (zs) chart.dispatchAction({ type: 'dataZoom', start: zs.s, end: zs.e });
}
function initAuxCtl() {
  const st = auxState();
  PANELS.concat(MF_PANEL, BREADTH_PANEL).forEach(p => {
    const el = document.getElementById(p.id);
    if (el) { el.checked = !!st[p.key];
      el.addEventListener('change', () => {
        st[p.key] = el.checked; saveAuxState(st); renderAux();
      }); }
  });
}

// ---- 右侧详情面板: 悬停联动显示当日全部构成 ----
const fmtv = (v, plus) => {
  if (nullish(v)) return "--";
  const s = typeof v === "number" && Number.isInteger(v) ? v.toString()
          : v.toFixed(2);
  return plus && v >= 0 ? "+" + s : s;
};
function barHTML(v) {
  if (nullish(v)) return '<div class="track"></div>';
  const w = Math.min(Math.abs(v), 100) / 2;
  const pos = v >= 0
    ? `left:50%;width:${w}%;background:${C.red}`
    : `left:${50 - w}%;width:${w}%;background:${C.blue}`;
  return `<div class="track"><div class="fill" style="${pos}"></div></div>`;
}
function blankHint(i) {                     // 该日没有情绪分的原因
  if (!D.isIdx) return D.t.hint;            // 上证: 原文案
  if (D.first == null) return "该指数还没有出过情绪分: 量价分量要满一个分位窗口, 且五个组都得有数据。";
  if (i < D.first) return `起步期: 量价分量要满一个分位窗口(${BASE.window || 250} 个交易日)才有值, 本指数自 ${D.dates[D.first]} 起有情绪分。`;
  if (D.w0 && i < D.w0) return "在生成规则的 years 窗口之外: 该日有情绪分, 只是没有带进这张图。";
  return `该日没出分: ${(D.notes && D.notes[i]) || "有分量组缺数据"}。`;
}
function render(i) {
  const s = D.score[i];
  const d = D.dates[i];
  const k = D.k[i];
  const T = D.t;
  const v1 = D.v1 ? D.v1[i] : null;
  const prev = i > 0 ? D.score[i - 1] : null;
  const delta = (!nullish(s) && !nullish(prev)) ? s - prev : null;
  document.getElementById("heroDate").textContent = d;
  const hs = document.getElementById("heroScore");
  hs.textContent = nullish(s) ? "--" : (s >= 0 ? "+" : "") + s.toFixed(1);
  hs.style.color = nullish(s) ? C.muted : (s >= 0 ? C.red : C.blue);
  const chip = document.getElementById("heroStage");
  chip.textContent = D.stage[i] || "--";
  chip.style.background = stageColor(s);
  document.getElementById("heroV1").textContent = nullish(v1) ? "--" : (v1 >= 0 ? "+" : "") + v1.toFixed(1);

  let h = `<h2>${d} 当日构成</h2>`;
  if (D.isIdx) h += `<div class="hint" style="margin:0 0 6px">强势 / 弱势：${T.scope}里当日涨跌幅 ≥ +9.8% / ≤ −9.8% 的家数；冲高回落率 = 冲高回落 ÷（强势 + 冲高回落）；接力率 = 连续强势家数 ÷ 前一日强势家数。</div>`;
  h += `<div class="stage-line"><span class="big" style="color:${nullish(s) ? C.muted : (s >= 0 ? C.red : C.blue)}">${nullish(s) ? "--" : (s >= 0 ? "+" : "") + (Math.round(s * 10) / 10)}</span>
        <span class="chip" style="background:${stageColor(s)}">${D.stage[i] || "--"}</span></div>`;
  h += `<div class="kv">前日差 <b>${nullish(delta) ? "--" : (delta >= 0 ? "+" : "") + delta.toFixed(1)}</b>${D.v1 ? ` · V1量价对照 <b>${nullish(v1) ? "--" : (v1 >= 0 ? "+" : "") + v1.toFixed(1)}</b>` : ""}<br>`;
  if (k && !nullish(k[1])) h += `${T.short} 收<b>${k[1]}</b> (${fmtv(D.pct[i], true)}%) · ${T.volShort} <b>${fmtv(D.vol[i])}</b> ${T.volUnit}<br>`;
  if (!nullish(D.zt[i])) h += `${T.zt} <b>${D.zt[i]}</b> ${T.dt} <b>${-D.dt[i]}</b> · ${T.zb} <b>${fmtv(D.zb[i])}</b>% · ${T.jl} <b>${fmtv(D.jl[i])}</b>%`;
  h += `</div>`;
  h += `<table><tr><th>分量</th><th>原始值</th><th class="bar-cell">分位分</th><th>权重</th><th>贡献</th></tr>`;
  let rows = KEYS.map(kk => {
    const w = D.weights[kk];
    const sv = D.cs[kk][i];
    return { kk, raw: D.raw[kk][i], s: sv, w, c: nullish(sv) ? null : sv * w };
  });
  const wsum = rows.filter(r => !nullish(r.s)).reduce((a, r) => a + r.w, 0) || 1;
  rows.forEach(r => { r.w = r.w / wsum; r.c = nullish(r.s) ? null : r.s * r.w; });
  rows.forEach(r => {
    h += `<tr><td>${D.labels[r.kk]}</td><td>${fmtv(r.raw)}</td>
          <td class="bar-cell">${barHTML(r.s)}</td>
          <td>${(r.w * 100).toFixed(0)}%</td>
          <td style="color:${nullish(r.c) ? C.muted : (r.c >= 0 ? C.red : C.blue)}">${fmtv(r.c, true)}</td></tr>`;
  });
  h += `</table>`;
  // MI-3: 指数页签 — 本指数与板块(估值 / 该交易所两融 / 挂钩ETF成交额 / 板块主力净流入)
  if (D.isIdx) {
    const o = D.val && D.val[D.valShow[0]];
    const pe = o && o.pe[i], pb = o && o.pb[i];
    const kv = (nm, val) => nullish(val) ? "" : `<tr><td>${nm}</td><td><b>${val}</b></td></tr>`;
    let own = "";
    own += kv(`${T.idx} PE`, nullish(pe) ? null : pe.toFixed(1));
    own += kv(`${T.idx} PB`, nullish(pb) ? null : pb.toFixed(2));
    if (!nullish(D.margin[i])) {
      let mv = D.margin[i].toFixed(0);
      if (D.bn && !nullish(D.bn[i])) {       // MI-4: 板块级两融只覆盖两融标的 — 带上标的只数, 以及在该交易所里的占比
        const ex = D.exMargin ? D.exMargin[i] : null;
        mv += ` <span class="hint">(标的 ${D.bn[i]} 只${(!nullish(ex) && ex > 0) ? ` · 占${T.exch} ${(D.margin[i] / ex * 100).toFixed(0)}%` : ''})</span>`;
      }
      own += kv(`${T.margin}(亿)`, mv);
    }
    if (D.breadth) {                          // MI-4: 成分股广度(等权)
      const b = D.breadth;
      if (!nullish(b.up[i]))
        own += kv('成分股上涨占比', `${b.up[i].toFixed(1)}% <span class="hint">(${b.n[i]} 只有行情)</span>`);
      if (!nullish(b.ma20[i])) own += kv('站上20日线占比', `${b.ma20[i].toFixed(1)}%`);
    }
    own += kv(`${T.etf}成交额(亿)`, !D.etfSum || nullish(D.etfSum[i]) ? null : D.etfSum[i].toFixed(1));
    if (!nullish(D.mf[i]))
      own += kv(`${T.scope}主力净流入(亿)`, `<span style="color:${D.mf[i] >= 0 ? C.red : C.blue}">${D.mf[i] >= 0 ? "+" : ""}${D.mf[i].toFixed(1)}</span>`);
    if (own) h += `<table style="margin-top:8px"><tr><th>本指数与板块</th><th></th></tr>${own}</table>`;
  }
  // D1-15 I3: 市场环境区块 — 镜像悬停框数据(估值/两融/ETF), 缺失段自动隐藏
  const mv = D.mval || {};
  const envBits = [mv.alla_pe, mv.pe50, mv.pe300, mv.pe500, mv.pe1000]
    .filter(a => !nullish(a && a[i]));   // D1-16: 表格化后仅存在性判定
  const hasMargin = D.margin && !nullish(D.margin[i]);
  const hasEtf = D.etfSum && !nullish(D.etfSum[i]);
  if (envBits.length || hasMargin || hasEtf) {   // D1-15r: 开合对称(估值缺失仅两融时div曾未开先闭)
    // D1-16: 市场环境结构化表格 — 每行一个指标, 分位带进度条(时间占比口径)
    const pctColor = (p) => nullish(p) ? C.muted : (p >= 0.8 ? C.red : p <= 0.2 ? C.blue : C.muted);
    const pctBar = (p) => nullish(p) ? '<div class="track"></div>' :
      `<div class="track"><div class="fill" style="left:0;width:${Math.max(p * 100, 2)}%;background:${pctColor(p)}"></div></div>`;
    const pctTxt = (p) => nullish(p) ? '--' :
      `<b style="color:${pctColor(p)}">${Math.round(p * 100)}%</b>`;
    const envRow = (nm, val, pa, p5) => {
      if (nullish(val) && nullish(pa) && nullish(p5)) return '';
      return `<tr><td>${nm}</td><td><b>${val ?? '--'}</b></td>
        <td class="bar-cell">${pctBar(pa)}</td><td>${pctTxt(pa)}</td>
        <td class="bar-cell">${pctBar(p5)}</td><td>${pctTxt(p5)}</td></tr>`;
    };
    let envRows = '';
    const v1 = (a) => nullish(a && a[i]) ? null : a[i].toFixed(1);
    const v2 = (a) => nullish(a && a[i]) ? null : a[i].toFixed(2);
    envRows += envRow('全A PE', v1(mv.alla_pe), mv.alla_pe_pct && mv.alla_pe_pct[i], mv.alla_pe_pct5 && mv.alla_pe_pct5[i]);
    envRows += envRow('全A PB', v2(mv.alla_pb), mv.alla_pb_pct && mv.alla_pb_pct[i], mv.alla_pb_pct5 && mv.alla_pb_pct5[i]);
    envRows += envRow('300 PE', v1(mv.pe300), mv.pe300_pct && mv.pe300_pct[i], mv.pe300_pct5 && mv.pe300_pct5[i]);
    if (envRows)
      h += `<table style="margin-top:8px"><tr><th>指标</th><th>值</th>
        <th class="bar-cell" colspan="2">水位·全史</th>
        <th class="bar-cell" colspan="2">水位·近5年</th></tr>${envRows}</table>`;
    const footBits = [];
    // 指数页签的两融 / ETF 已经列在"本指数与板块"里(口径不同: 单个交易所 / 单只挂钩ETF)
    if (hasMargin && !D.isIdx) footBits.push(`两融余额 <b>${D.margin[i].toFixed(0)}</b> 亿`);
    if (hasEtf && !D.isIdx) footBits.push(`宽基ETF <b>${D.etfSum[i].toFixed(0)}</b> 亿`);
    if (footBits.length)
      h += `<div class="kv" style="margin-top:4px">${footBits.join(' · ')}</div>`;
  }
  if (nullish(s)) h += `<p class="hint">${blankHint(i)}</p>`;
  // D1-18 M3: 当前时间区事件(可见缩放窗内·勾选等级, 日期倒序, 至多12条)
  if (D.evIdx && D.evIdx.length) {
    let z = null;
    try { const cur = chart.getOption();
      if (cur && cur.dataZoom && cur.dataZoom[0] && cur.dataZoom[0].start != null) z = cur.dataZoom[0];
    } catch (e) {}
    const lo = z ? Math.max(0, Math.floor(z.start / 100 * D.n)) : 0;
    const hi = z ? Math.min(D.n - 1, Math.ceil(z.end / 100 * D.n)) : D.n - 1;
    const vis = D.evIdx.filter(o => o.i >= lo && o.i <= hi && D.evOn[o.e.level])
      .slice(-12).reverse();
    if (vis.length)
      h += `<table style="margin-top:8px"><tr><th>时间区事件</th><th>等级</th><th style="text-align:left">标题</th></tr>`
        + vis.map(o => `<tr><td class="hint">${o.e.date.slice(5)}</td>
            <td><b style="color:${EV_COLORS[o.e.level] || '#555'}">L${o.e.level}</b></td>
            <td style="text-align:left">${(o.e.source || '').startsWith('ai_') ? '<b style="color:#6250d6">AI</b> ' : ''}${escE(o.e.title)}</td></tr>`).join('') + `</table>`;
  }
  document.getElementById("panel").innerHTML = h;
}
// 初始页签: 上次记住的那个(没有或已不存在 → 上证)。到这里 D、右侧面板、页签 / 对比条 / 叠加框都已就位
mount((() => { const k = storeGet(TAB_STORE); return VIEWS[k] ? k : 'sh'; })());
chart.on("updateAxisPointer", (e) => {
  const ax = (e.axesInfo || []).find(a => a.axisDim === "x");
  if (!ax) return;
  const v = ax.value;
  let i = typeof v === "number" ? v : IDX[v];
  if (i === undefined || i === curIdx) return;
  curIdx = i; render(i);
});
window.addEventListener("resize", () => chart.resize());

// S2.2-A: 快捷缩放
function setZoom(days) {
  if (!days) chart.dispatchAction({ type: 'dataZoom', start: 0, end: 100 });
  else {
    const pct = Math.max(0, 100 - (days / D.n) * 100);
    chart.dispatchAction({ type: 'dataZoom', start: pct, end: 100 });
  }
}

// S2.2-A: 底部面板可选(checkbox) — D1-14 H4: 显隐并入render统一重排
// name 是文案键(D.t 里的键)或固定文字: 炸板率 / 接力率在指数页签上是冲高回落率 / 接力率(板块)
const SUB_OPTS = [
  { id: 'ckZb',  name: 'zbSeries',   key: 'zb',  color: '#eda100' },
  { id: 'ckJl',  name: 'jlSeries',   key: 'jl',  color: '#1baf7a' },
  { id: 'ckRsi', name: 'RSI',      key: 'rsi', color: '#eb6834' },
  { id: 'ckBias', name: '20日乖离',  key: 'bias20', color: '#6250d6' },
  { id: 'ckVol', name: 'volLine', key: 'vol', color: '#898781' },
];
function subSeriesFor(id) {
  const opt = SUB_OPTS.find(o => o.id === id);
  if (!opt) return null;
  let data = null;
  if (opt.key === 'zb') data = D.zb;
  else if (opt.key === 'jl') data = D.jl;
  else if (opt.key === 'vol') data = D.vol;
  else if (D.cs && D.cs[opt.key]) data = D.cs[opt.key];
  if (!data) return null;
  const name = D.t[opt.name] || opt.name;
  return { name, type: 'line', xAxisIndex: 4, yAxisIndex: 4,
    data: data, showSymbol: false,
    lineStyle: { width: 1.4, color: opt.color }, itemStyle: { color: opt.color } };
}
for (const opt of SUB_OPTS) {
  const el = document.getElementById(opt.id);
  if (el) el.addEventListener('change', renderAux);
}
// D1-14 H4: 初始渲染(副图开关记忆 + 底部面板勾选)
// D1-18 M3: 事件等级勾选 → 双grid数据过滤 + 面板时间区列表刷新
document.querySelectorAll('.ckEvt').forEach(el =>
  el.addEventListener('change', () => {
    D.evOn[el.value] = el.checked;
    setEvents();
    render(curIdx);
  }));
initAuxCtl();
renderAux();
</script>
</body>
</html>
"""


def generate_html(cfg=None, years=None, out=None):
    """生成交互式查看器 HTML, 返回文件路径(供 admin.py 调用)"""
    import time as _time

    cfg = cfg or load_cfg()
    payload = build_payload(cfg, years)
    js = get_echarts_inline()
    echarts_tag = f'<script src="{ECHARTS_URLS[0]}"></script>' if js is None else None

    html = (HTML
            .replace("__C_SURFACE__", C["surface"]).replace("__C_PAGE__", C["page"])
            .replace("__C_INK2__", C["ink2"]).replace("__C_INK__", C["ink"])
            .replace("__C_MUTED__", C["muted"]).replace("__C_GRID__", C["grid"])
            .replace("__C_AXIS__", C["axis"]).replace("__C_BLUE__", C["blue"])
            .replace("__C_RED__", C["red"]).replace("__C_AQUA__", C["aqua"])
            .replace("__C_YELLOW__", C["yellow"])
            .replace("__GENERATED__", _time.strftime("%Y-%m-%d %H:%M"))
            .replace("__ECHARTS__", js if js is not None else "/*cdn*/")
            .replace("__PAYLOAD__", _payload_js(payload)))
    if echarts_tag:
        html = html.replace("/*cdn*/", "").replace("<script>/*cdn*/", echarts_tag + "<script>")

    out = Path(out) if out else (BASE / "out" / "sentiment_viewer.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"[viewer] {out}  ({out.stat().st_size / 1e6:.2f} MB, "
          f"{payload['n']} 交易日, {payload['dates'][0]} ~ {payload['dates'][-1]})")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    out = generate_html(years=args.years, out=args.out)
    if args.open:
        import webbrowser
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
