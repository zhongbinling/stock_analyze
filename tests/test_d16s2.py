# -*- coding: utf-8 -*-
"""D1-16 段2: 全A PE/PB 分位副图 — 模板接线/定域/参考线/开关记忆兼容"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(CORE))

import viewer as VW  # noqa: E402


def test_html_pct_panels_wired():
    """接线: 勾选框/PANELS键/序列消费/轴定域/参考线/轴覆盖"""
    h = VW.HTML
    assert 'id="ckPEPct"' in h and 'id="ckPBPct"' in h
    assert "key: 'pePct'" in h and "key: 'pbPct'" in h
    assert "pctSeries(gi, 'alla_pe', 'PE')" in h and "pctSeries(gi, 'alla_pb', 'PB')" in h
    assert "mv[key + '_pct']" in h and "mv[key + '_pct5']" in h   # 双口径数据源
    assert "min: 0, max: 100" in h and "formatter: '{value}%'" in h   # y轴定域
    assert "yAxis: 80" in h and "yAxis: 20" in h                  # 高估/低估参考线
    assert "p.yaxis ? p.yaxis(gi) : valAxis(gi)" in h             # 轴覆盖, 其余面板零影响
    assert "全A PE 分位%" in h and "全A PB 分位%" in h


def test_auxstate_defaults_merge_for_saved_state():
    """开关记忆兼容: 存量记忆(旧4键)缺新键 → 合并默认后新面板默认开;
    Object.assign 保证用户已存偏好(含显式false)不被覆盖"""
    h = VW.HTML
    assert ("Object.assign({ pe: true, pb: true, margin: true, etf: true,"
            in h) and "pePct: true, pbPct: true" in h


def test_pct_panels_hidden_when_mval_empty():
    """数据空自动隐藏: pePct/pbPct 各自判 mval 对应分位序列全空"""
    h = VW.HTML
    assert "p.key === 'pePct'" in h and "D.mval.alla_pe_pct" in h
    assert "p.key === 'pbPct'" in h and "D.mval.alla_pb_pct" in h


def test_pct_values_scaled_percent():
    """比值×100 取1位小数(0.687→68.7), null 透传"""
    h = VW.HTML
    assert "Math.round(v * 1000) / 10" in h
