# -*- coding: utf-8 -*-
"""build_payload / generate_html 测试 (core/sector_viewer.py, 夹具库驱动)"""
import json

import pytest

import sector_viewer as SV


@pytest.fixture()
def payload(monkeypatch, fixture_db):
    monkeypatch.setattr(SV, "DB", fixture_db)
    return SV.build_payload(years=3)


def test_payload_shapes(payload):
    assert len(payload["dates"]) == 30          # 夹具 30 日
    assert len(payload["codes"]) == 5           # 3 BK + SW1_ + SW2_
    assert len(payload["mkt"]) == 40            # sentiment 40 日
    assert payload["latest"] == payload["dates"][-1]


def test_payload_mat_null_defense(payload):
    """NULL score 行 → mat 单元为 None 而非崩溃"""
    ci = payload["codes"].index("BK0003")
    di = payload["dates"].index("2026-09-11")   # 夹具中 i=10 的 NULL 行
    assert payload["mat"][di][ci] is None
    # 其余日非 None
    assert payload["mat"][payload["dates"].index("2026-09-30")][ci] is not None


def test_payload_disp_name_strips_prefix(payload):
    names = dict(zip(payload["codes"], payload["names"]))
    assert names["SW1_信息技术"] == "信息技术"
    assert names["SW2_半导体"] == "半导体"
    assert names["BK0001"] == "半导体"          # BK 名不剥前缀(无前缀)


def test_payload_level_data_partition(payload):
    ld = payload["level_data"]
    all_cis = set(range(len(payload["codes"])))
    assert set(ld["L1"]) | set(ld["L2"]) | set(ld["L3"]) == all_cis
    assert not (set(ld["L1"]) & set(ld["L3"]))   # 互斥
    assert set(ld["L3"]) == {payload["codes"].index(c) for c in ("BK0001", "BK0002", "BK0003")}


def test_payload_sw_groups_and_parents(payload):
    g = payload["sw1_groups"]
    assert set(g["信息技术"]) == {payload["codes"].index("BK0001")}
    assert payload["sw2_to_sw1"]["半导体"] == "信息技术"
    assert payload["sw2_to_sw1"]["白酒"] == "消费"


def test_payload_top5_bot5(payload):
    assert len(payload["top5"]) == 3            # 仅 BK 板块入 sectors JOIN
    assert all("SW" not in c["c"] for c in payload["top5"])
    hot = payload["top5"][0]
    assert hot["c"] == "BK0001"                 # 升温板块最热
    assert payload["bot5"][0]["c"] == "BK0002"  # 修复中的白酒最冷


def test_payload_comp_recent5(payload):
    c1 = payload["comp"]["BK0001"]
    assert len(c1) == 5                          # 近5日
    last = max(c1)
    assert c1[last]["chg"] == 55 and c1[last]["vol_dir"] == -60


def test_payload_mkt_rounded(payload):
    assert all(isinstance(m[1], (int, float)) or m[1] is None for m in payload["mkt"])


def test_generate_html_fixture(fixture_html):
    html = fixture_html.read_text(encoding="utf-8")
    assert "板块温度计" in html
    assert "__PAYLOAD__" not in html and "__ECHARTS__" not in html  # 占位符已替换
    for marker in ["semanticOf", "divergenceOf", "curveZoomOpt", "tq250",
                   "function pal()", "flex:8"]:
        assert marker in html, f"missing marker: {marker}"
    payload = json.loads(html.split("const D = ")[1].split(";\n", 1)[0])
    assert payload["latest"]  # 内嵌数据可解析
