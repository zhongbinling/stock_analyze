# -*- coding: utf-8 -*-
"""MI-4 情绪图多指数 · 情绪图页面(docs/开发计划-情绪图多指数.md 第 5 节 MI-4; 用户 11-27 确认做板块两融余额、成分股口径广度)

① 页签载荷里新增的两块副图数据(core/viewer.py _index_aux): 板块两融余额(创业板 / 科创板, aux.bmargin / aux.bn)、
   成分股广度(三个指数, aux.breadth = 上涨占比 / 站上 20 日线占比)。深证成指没有板块级两融(深市本来就是交易所口径);
   表还没建、没有这个指数的行、占比的分母为 0 时各项为空, 其余副图与上证的数据不受影响;
② 页面脚本: tests/viewer_behavior.js(手写 ECharts 桩)与 tests/viewer_echarts_real.js(仓库自带的 ECharts, 服务端渲染)
   末尾各有 MI-4 的一节。
夹具沿用 MI-3 的临时库(tests/test_mi3_viewer.py 的 env), 再加上 MI-4 的三张表; 不调真实接口。"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CORE = REPO / "core"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(CORE))

import board_margin as BM                # noqa: E402
import index_breadth as IB               # noqa: E402
import viewer as VW                      # noqa: E402
from test_mi3_viewer import CFG, CODES, N, _node, _tab, env  # noqa: F401  (env 是夹具)

BM_START, KCB_START, BR_START = 400, 200, 300        # 板块两融 / 成分股广度各有数的日子数(都到最新一天)
BM_MARK_AT = BM_START - 100                         # 创业板两融的标的在第 300 天前扩容(500 → 975 只)
NC = {"0.399001": 500, "0.399006": 100, "1.000688": 52}   # 各指数成分股快照里的只数


def _add_mi4_tables(env):
    """在 MI-3 的临时库上加上 MI-4 的三张表: 板块两融(创业板自 BM_START 天前、科创板自 KCB_START 天前;
    创业板的标的在第 BM_MARK_AT 天里扩容)、三个指数的成分股广度(自 BR_START 天前; 每条序列的头 19 天没有
    20 日线 — 两只新股)"""
    iso, n = env["iso"], N
    con = sqlite3.connect(str(env["path"]))
    con.executescript(BM.SCHEMA)
    con.executescript(IB.W_SCHEMA + ";" + IB.B_SCHEMA)
    for i in range(n - BM_START, n):                       # 两融余额 = 融资 + 融券; 每天 +1 亿
        con.execute("INSERT INTO board_margin VALUES(?,?,?,?,?,?,?)",
                    ("CYB", iso[i], 3.98e11 + i * 1e8, 2.0e9, 4.0e11 + i * 1e8, 2.2e10,
                     500 if i < n - BM_MARK_AT else 975))
    for i in range(n - KCB_START, n):
        con.execute("INSERT INTO board_margin VALUES(?,?,?,?,?,?,?)",
                    ("KCB", iso[i], 2.584e11 + i * 1e8, 1.6e9, 2.6e11 + i * 1e8, 1.5e10, 607))
    for code, nc in NC.items():
        for i in range(n - BR_START, n):
            young = i < n - BR_START + 19
            con.execute("INSERT INTO index_breadth VALUES(?,?,?,?,?,?,?)",
                        (code, iso[i], nc - 2, i % (nc - 2), 0 if young else nc - 4, 0 if young else 7, nc))
    con.commit()
    con.close()


@pytest.fixture()
def env4(env):
    _add_mi4_tables(env)
    return env


# ════════════ ① 页签载荷 ════════════

def test_tab_aux_board_margin_and_breadth(env4):
    """板块两融(创业板指 / 科创50): 元 → 亿取一位小数, 对齐本页签的日期轴, 标的只数与扩容日另存; 交易所口径的
    那条照带; 成分股广度(三个指数): 占比 = 只数 ÷ 分母, 一位小数"""
    P = VW.build_payload(CFG)
    a, b, c = (_tab(P, k) for k in CODES)
    n, iso = N, env4["iso"]
    assert len(b["aux"]["bmargin"]) == n and b["aux"]["bmargin"][n - BM_START - 1] is None
    assert b["aux"]["bmargin"][n - BM_START] == pytest.approx(4000 + n - BM_START, abs=0.06)   # 4.0e11 元 + 每天 1 亿
    assert b["aux"]["bmargin"][-1] == 4819.0 and b["aux"]["bn"][n - BM_START - 1] is None and b["aux"]["bn"][-1] == 975
    assert c["aux"]["bmargin"][n - KCB_START - 1] is None and c["aux"]["bmargin"][-1] == 3419.0 and c["aux"]["bn"][-1] == 607
    # 标的扩容的日子 [日期, 之前只数, 之后只数](图上标出来): 创业板有一次 500 → 975; 科创板的只数没变
    assert b["aux"]["bm_marks"] == [[iso[n - BM_MARK_AT], 500, 975]] and c["aux"]["bm_marks"] == []
    # 深证成指的板块是深市: 交易所口径本来就是它要的, 没有板块级的那条
    assert a["aux"]["bmargin"] == [] and a["aux"]["bn"] == [] and a["aux"]["bm_marks"] == []
    assert (a["aux"]["margin"][-1], b["aux"]["margin"][-1], c["aux"]["margin"][-1]) == (8500.0, 8500.0, 9500.0)
    # 成分股广度: 上涨占比 = 上涨只数 ÷ 有行情只数; 站上 20 日线占比 = 站上的只数 ÷ 有 20 日线的只数
    for t, up_last, ma_last, nc in ((a, 64.5, 1.4, 500), (b, 35.7, 7.3, 100), (c, 38.0, 14.6, 52)):
        br = t["aux"]["breadth"]
        assert set(br) == {"up", "ma20", "n"}
        assert len(br["up"]) == n and len(br["ma20"]) == n and len(br["n"]) == n
        assert br["up"][n - BR_START - 1] is None and br["ma20"][n - BR_START - 1] is None
        assert br["n"][n - BR_START - 1] is None and br["n"][-1] == nc - 2
        assert br["up"][n - BR_START] == round((n - BR_START) % (nc - 2) / (nc - 2) * 100, 1)
        assert br["up"][-1] == up_last                                    # 最新一天: (819 − 520) % (nc − 2) 只上涨
        assert br["ma20"][n - BR_START] is None and br["ma20"][n - BR_START + 18] is None   # 头 19 天没有 20 日线
        assert br["ma20"][n - BR_START + 19] == ma_last == br["ma20"][-1]  # 7 ÷ (nc − 4)


def test_mi4_tables_only_add_their_own_aux_keys(env):
    """加上这三张表前后: 上证的数据逐键相同, 页签里除新增的四个 aux 键外逐键相同"""
    before = VW.build_payload(CFG)
    _add_mi4_tables(env)
    after = VW.build_payload(CFG)
    assert {k: v for k, v in before.items() if k != "tabs"} == {k: v for k, v in after.items() if k != "tabs"}
    new = {"bmargin", "bn", "breadth", "bm_marks"}
    for t0, t1 in zip(before["tabs"], after["tabs"]):
        assert {k: v for k, v in t0.items() if k != "aux"} == {k: v for k, v in t1.items() if k != "aux"}
        assert {k: v for k, v in t0["aux"].items() if k not in new} == {k: v for k, v in t1["aux"].items() if k not in new}
        assert t0["aux"]["bmargin"] == [] and t0["aux"]["bn"] == [] and t0["aux"]["breadth"] is None
    assert _tab(after, "0.399006")["aux"]["bmargin"][-1] == 4819.0


def test_without_mi4_tables_the_aux_keys_are_empty_and_logged(env, capsys):
    """表还没建(回补之前 / 老库): 这两块副图的数据为空、日志里写明跳过, 其余副图与情绪分不受影响"""
    for t in VW.build_payload(CFG)["tabs"]:
        assert t["aux"]["bmargin"] == [] and t["aux"]["bn"] == [] and t["aux"]["bm_marks"] == []
        assert t["aux"]["breadth"] is None
        assert len(t["aux"]["margin"]) == N and len(t["aux"]["mf"]) == N and t["first"] == CFG["window"]
    out = capsys.readouterr().out
    assert out.count("板块两融副图跳过: OperationalError: no such table: board_margin") == 2      # 创业板指、科创50
    assert out.count("成分股广度副图跳过: OperationalError: no such table: index_breadth") == 3
    assert "深证成指 板块两融" not in out                                                   # 深市没有板块级两融这一说: 不去查


def test_board_margin_table_empty_or_all_null(env):
    """表建了还没有行 / 余额全是空: 副图数据为空(页面上回到交易所口径), 不报错"""
    con = sqlite3.connect(str(env["path"]))
    con.executescript(BM.SCHEMA)
    con.executemany("INSERT INTO board_margin VALUES(?,?,?,?,?,?,?)",
                    [("CYB", env["iso"][-1], None, None, None, None, None), ("KCB", env["iso"][-1], 1.0, 0.0, 1.0, 0.0, 1)])
    con.commit()
    con.close()
    P = VW.build_payload(CFG)
    b, c = _tab(P, "0.399006"), _tab(P, "1.000688")
    assert b["aux"]["bmargin"] == [None] * N and b["aux"]["bn"] == [None] * N      # 只有空值: 页面当作没有
    assert b["aux"]["bm_marks"] == [] and c["aux"]["bm_marks"] == []              # 只有一行 / 只数没变: 没有扩容日
    assert c["aux"]["bmargin"][-1] == 0.0 and c["aux"]["bn"][-1] == 1


def test_expansion_marks_rule(env):
    """标的扩容日的判定: 标的只数连续上升的一段里, 累计增加 ≥ 5 只且 ≥ 20%; 一段只标一次, 记这一段结束的那天与
    最终只数。余额只覆盖两融标的, 名单一扩余额就上一个台阶, 图上要标出来; 小幅增减(新股逐只成为标的、调出几只)
    不算; 某天没记只数不影响(连续两天 41 → 50 这种分批扩容也要能标出来 — 独立评审复核时指出)"""
    con = sqlite3.connect(str(env["path"]))
    con.executescript(BM.SCHEMA)
    iso = env["iso"]
    counts = [24, None, 26, 28, 30, 33, 60, 62, 58, 10, 40, 44, 48, 48, 52, 50, 60]
    con.executemany("INSERT INTO board_margin VALUES(?,?,?,?,?,?,?)",
                    [("KCB", iso[i], 1.0, 0.0, 1.0, 0.0, k) for i, k in enumerate(counts)]
                    # 创业板: 一次发生在本页签第一天之前的扩容(生产库里就是这样: 科创50 页签自 2019-12-31 起,
                    # 科创板 2019-11-06 那次扩容在它之前 — 画不到这个页签的日期轴上)
                    + [("CYB", "2023-09-15", 1.0, 0.0, 1.0, 0.0, 6), ("CYB", "2023-09-18", 1.0, 0.0, 1.0, 0.0, 32)])
    con.commit()
    con.close()
    P = VW.build_payload(CFG)
    marks = _tab(P, "1.000688")["aux"]["bm_marks"]
    assert marks == [[iso[7], 24, 62],          # 连续上升 24 → 62(中间有一天没记只数): 标在上升结束的那天
                     [iso[12], 10, 48],         # 10 → 48; 之后持平、再涨到 52(不足 20%)不标
                     [iso[16], 50, 60]]         # 50 → 60: 正好 +20%
    assert iso[0] > "2023-09-18" and _tab(P, "0.399006")["aux"]["bm_marks"] == []   # 页签第一天之前的扩容: 不带进载荷


def test_breadth_edge_rows(env):
    """分母为 0 的行占比为空(不除零); 不在日期轴上的行忽略"""
    con = sqlite3.connect(str(env["path"]))
    con.executescript(IB.B_SCHEMA)
    iso = env["iso"]
    con.executemany("INSERT INTO index_breadth VALUES(?,?,?,?,?,?,?)", [
        ("0.399006", iso[10], 0, 0, 0, 0, 100),          # 有行情只数为 0
        ("0.399006", iso[11], 100, 37, 0, 0, 100),       # 有行情、没有 20 日线
        ("0.399006", iso[12], 100, 37, 98, 50, 100),
        ("0.399006", "2099-01-01", 100, 1, 100, 1, 123),  # 不在这个页签的日期轴上
        ("0.399001", iso[5], 500, 250, 500, 250, 500)])
    con.commit()
    con.close()
    br = _tab(VW.build_payload(CFG), "0.399006")["aux"]["breadth"]
    assert br["up"][9] is None and br["n"][9] is None
    assert (br["n"][10], br["up"][10], br["ma20"][10]) == (0, None, None)
    assert (br["up"][11], br["ma20"][11]) == (37.0, None)
    assert (br["up"][12], br["ma20"][12]) == (37.0, pytest.approx(51.0))            # 50 ÷ 98
    assert br["up"][13] is None                                                    # 2099 年那行不进载荷
    other = _tab(VW.build_payload(CFG), "0.399001")["aux"]["breadth"]
    assert other["up"][5] == 50.0 and other["ma20"][5] == 50.0 and other["n"][4] is None


def test_template_and_generated_page_carry_mi4(env4, tmp_path, monkeypatch):
    monkeypatch.setattr(VW, "get_echarts_inline", lambda: "/*echarts*/")
    tpl = VW.HTML
    for frag in ('id="ckBreadth"', 'id="ckBreadthWrap" style="display:none"', '<span id="lbBreadth">成分股广度</span>',
                 "function breadthSeries(", "const BREADTH_PANEL", "PANELS.concat(MF_PANEL, BREADTH_PANEL)",
                 "breadth: true", "if (p.key === 'breadth') return !D.breadth;",
                 "show('ckBreadthWrap', D.isIdx && !!D.breadth)",
                 "bmMarks: hasBM ? (t.aux.bm_marks || null) : null", "if (D.bmMarks && D.bmMarks.length)",
                 "top: (top - (D.isIdx ? 20 : 14)) + 'px'",                         # 指数页签的面板标题抬 6px, 上证不动
                 "Object.assign({}, PANELS[5], { yaxis: pctAxisIdx }), Object.assign({}, PANELS[6], { yaxis: pctAxisIdx })",
                 "formatter: '{value}%', showMaxLabel: false }"):                   # 指数页签: 定域 0 ~ 100 的轴不画最高一档
        assert frag in tpl, frag
    assert tpl.count("showMaxLabel: false") == 1 and "const pctAxis = (gi) => ({ type: 'value', gridIndex: gi, min: 0, max: 100,\n  axisLabel: { color: C.muted, formatter: '{value}%' }," in tpl   # 上证页签的分位轴原样
    out = VW.generate_html(CFG, out=tmp_path / "v.html")
    text = out.read_text(encoding="utf-8")
    P = json.loads(re.search(r"const BASE = (\{.*?\});\n", text, re.S).group(1))
    assert _tab(P, "0.399006")["aux"]["bmargin"][-1] == 4819.0
    assert _tab(P, "0.399006")["aux"]["breadth"]["up"][-1] == 35.7
    assert _tab(P, "0.399001")["aux"]["bmargin"] == []
    assert 'id="ckBreadthWrap" style="display:none"' in text and "成分股广度</span>" in text


# ════════════ ② 页面脚本 ════════════

def test_viewer_page_behavior_including_mi4(tmp_path):
    """页面脚本放进 node 沙箱实跑(tests/viewer_behavior.js, 手写的 ECharts 桩): MI-4 的 6 条覆盖 —
    有板块级数据时两融线与名字换成板块口径、没有时回到交易所口径、悬停框与右侧面板的写法、
    成分股广度副图的两条线 / 轴 / 参照线 / 标题图例 / 开关记忆 / 上证没有"""
    tpl = tmp_path / "viewer.html"
    tpl.write_text(VW.HTML, encoding="utf-8")
    r, out = _node("viewer_behavior.js", tpl)
    assert r.returncode == 0, "页面行为测试失败:\n" + out[-2500:]
    js = (REPO / "tests" / "viewer_behavior.js").read_text(encoding="utf-8")
    n_cases = len(re.findall(r"^test\(", js, flags=re.M))
    n_mi4 = len(re.findall(r"(?m)^test\(.+(?:板块两融余额:|没有板块级数据时|板块两融与成分股广度的悬停框|板块数据只有余额|成分股广度副图:|成分股广度的开关:)", js))
    assert n_mi4 == 6 and n_cases == 38 and "ALL TESTS PASS passed 38" in out, out[-600:]


def test_viewer_page_on_real_echarts_with_mi4(env4, tmp_path, monkeypatch):
    """生成出来的页面 × 仓库自带的 ECharts(服务端渲染模式, tests/viewer_echarts_real.js 的第三个参数):
    板块两融线、成分股广度的两条线(同一根 0 ~ 100 的轴)与标题里的彩色图例都要被真实 ECharts 接受并画进 SVG"""
    if not (CORE / "vendor" / "echarts.min.js").exists():
        pytest.skip("ECharts 本地缓存不存在(core/vendor/echarts.min.js)")
    monkeypatch.setattr(VW, "get_echarts_inline", lambda: "")
    iso = env4["iso"]
    con = sqlite3.connect(str(env4["path"]))
    con.execute("DELETE FROM index_daily WHERE code='0.399006' AND date < ?", (iso[300],))   # 与 MI-3 的测试同一种夹具:
    con.commit()                                                                           # 创业板指少 300 天, 页签日期轴长短不同
    con.close()
    monkeypatch.setattr(VW, "_read_events", lambda level_min=None: [      # 与 MI-3 的真实 ECharts 测试相同的两条事件
        {"date": iso[N - 211], "level": 3, "title": "MI3-EVENT-OUT", "type": "", "source": "", "content": ""},
        {"date": iso[N - 200], "level": 3, "title": "MI3-EVENT-IN", "type": "", "source": "", "content": ""}])
    full = VW.generate_html(CFG, out=tmp_path / "mi4_full.html")
    two = VW.generate_html(CFG, years=2, out=tmp_path / "mi4_y2.html")
    r, out = _node("viewer_echarts_real.js", full, two, full)
    assert r.returncode == 0, "真实 ECharts 下的页面测试失败:\n" + out[-3000:]
    assert len(re.findall(r"(?m)^ok   MI-4 ", out)) == 2 and "ALL TESTS PASS passed 12" in out, out[-600:]
