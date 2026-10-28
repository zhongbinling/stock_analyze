# -*- coding: utf-8 -*-
"""D1-18 M1/M2: 宏观事件库 — 建库播种/检索过滤/增删/批量导入/API/页面/备份注册"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture()
def macro_mod(tmp_path, monkeypatch):
    """macro 模块指向临时库(每个测试独立重载, 播种干净)"""
    import server.macro as M
    monkeypatch.setattr(M, "MACRO_DB", tmp_path / "macro.db")
    return M


def test_ensure_db_seeds_each_level(macro_mod):
    macro_mod.ensure_db()
    evs = macro_mod.list_events()
    assert len(evs) == 5
    assert {e["level"] for e in evs} == {1, 2, 3, 4, 5}     # 每级≥1条
    assert all(e["source"] == "模拟样例" for e in evs)       # 可识别删除
    macro_mod.ensure_db()                                    # 幂等不重复播种
    assert len(macro_mod.list_events()) == 5


def test_list_filters(macro_mod):
    macro_mod.ensure_db()
    assert all(e["level"] == 5 for e in macro_mod.list_events(level=5))
    evs = macro_mod.list_events(d_from="2024-09-01", d_to="2024-09-30")
    assert {e["date"] for e in evs} == {"2024-09-24", "2024-09-19"}
    assert all(e["type"] == "货币" for e in macro_mod.list_events(type_="货币"))
    assert any("降息" in e["title"] for e in macro_mod.list_events(q="降息"))
    assert macro_mod.list_events(q="绝不存在的词XYZ") == []
    # 倒序
    dates = [e["date"] for e in macro_mod.list_events()]
    assert dates == sorted(dates, reverse=True)


def test_add_and_delete(macro_mod):
    macro_mod.ensure_db()
    eid = macro_mod.add_event({"date": "2026-10-29", "title": "测试事件",
                               "level": 2, "type": "政策", "source": "t"})
    assert macro_mod.delete_event(eid) is True
    assert macro_mod.delete_event(eid) is False              # 再删404语义
    with pytest.raises(ValueError):                          # 校验: level越界
        macro_mod.add_event({"date": "2026-10-29", "title": "x", "level": 9})
    with pytest.raises(ValueError):                          # 校验: 日期格式
        macro_mod.add_event({"date": "2026/10/29", "title": "x", "level": 3})


def test_import_batch(macro_mod):
    """批量导入: 合法入库/批内去重/库内去重/非法跳过计数"""
    macro_mod.ensure_db()
    r = macro_mod.import_events([
        {"date": "2026-10-29", "title": "事件A", "level": 3},
        {"date": "2026-10-29", "title": "事件A", "level": 3},    # 批内重复
        {"date": "2026-10-30", "title": "事件B", "level": 6},    # level非法
        {"date": "bad", "title": "事件C", "level": 3},           # 日期非法
        {"date": "2024-09-24",
         "title": "9·24政策组合拳: 降准降息+互换便利(模拟样例)",    # 库内重复(种子)
         "level": 4},
    ])
    assert r["inserted"] == 1 and r["skipped"] == 2
    assert len(r["errors"]) == 2
    assert macro_mod.import_events("notalist")["errors"] == ["events 应为数组"]


# ── API 与页面 ──

def test_macro_api_crud_and_search(server_app, monkeypatch):
    c = server_app["client"]
    # 真库闭环测试(首访自动建库播种=生产行为): 新增→检索→导入去重→删除, 不留残渣
    r = c.get("/admin/api/macro/events")
    assert r.status_code == 200 and "events" in r.get_json()
    r = c.post("/admin/api/macro/events", json={
        "date": "2026-10-29", "title": "API闭环测试事件", "level": 3, "type": "测试"})
    assert r.status_code == 200
    eid = r.get_json()["id"]
    r = c.get("/admin/api/macro/events?q=API闭环测试事件")
    assert any(e["id"] == eid for e in r.get_json()["events"])
    assert c.post("/admin/api/macro/events", json={"date": "x", "title": "", "level": 9}).status_code == 400
    assert c.delete(f"/admin/api/macro/events/{eid}").status_code == 200
    assert c.delete(f"/admin/api/macro/events/{eid}").status_code == 404
    # 批量导入API
    r = c.post("/admin/api/macro/import", json={"events": [
        {"date": "2026-10-29", "title": "导入闭环事件", "level": 2}]})
    assert r.status_code == 200 and r.get_json()["inserted"] == 1
    r2 = c.post("/admin/api/macro/import", json={"events": [
        {"date": "2026-10-29", "title": "导入闭环事件", "level": 2}]})
    assert r2.get_json()["skipped"] == 1                       # 去重
    # 清理闭环残渣
    for e in c.get("/admin/api/macro/events?q=闭环").get_json()["events"]:
        c.delete(f"/admin/api/macro/events/{e['id']}")
    assert c.post("/admin/api/macro/import", json={}).status_code == 400


def test_macro_page_renders(server_app):
    r = server_app["client"].get("/admin/macro")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    for marker in ("宏观舆情事件库", "L5·股灾级", "L4·重大级", "L3·重要级",
                   "L2·关注级", "L1·普通级", "批量导入", "btnImport",
                   "fLevel", "X-CSRFToken"):
        assert marker in body, f"页面缺标记: {marker}"


def test_macro_html_js_syntax():
    if not shutil.which("node"):
        pytest.skip("node 不可用")
    html = (REPO / "server" / "templates" / "admin" / "macro.html").read_text(encoding="utf-8")
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m, "缺 script 块"
    # Jinja表达式({{ lv_json }})替换为占位再解析 — 模板内嵌属预期
    js = re.sub(r"\{\{.*?\}\}", "null", m.group(1))
    r = subprocess.run([shutil.which("node"), "-e", "new Function(process.argv[1])", js],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"JS 语法错误: {(r.stderr or '')[:400]}"
    assert "{{ lv_json }}" in html        # M1r: 等级名单点注入


def test_macro_registered_in_backup():
    import backup as BK
    from server.routes_admin import BACKUP_BLOCKS, BLOCK_ASSETS
    assert "macro" in BK.ASSETS
    assert "macro" in BLOCK_ASSETS["base"]
    assert BACKUP_BLOCKS["macro"] == "宏观事件"
